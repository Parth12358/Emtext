#include "power.h"
#include "../logx.h"
#include "../config/config.h"
#include "../controls/controls.h"   // motion signal for the idle gate (controls owns the IMU)
#include <M5Unified.h>
#include <WiFi.h>

// See power.h for the staged model. This file is all policy + M5.Power/WiFi power settings;
// the display / mic / socket effects go out through the callbacks so nothing here reaches
// into another module's hardware.

namespace {
  void (*cbGrace)(bool)    = nullptr;
  void (*cbGraceTick)(int) = nullptr;
  void (*cbStandby)(bool)  = nullptr;
  void (*cbSleep)()        = nullptr;
  void (*cbLowBatt)()      = nullptr;

  uint32_t idleSince = 0;      // millis() of the last activity (button / speech / motion)
  bool     inGrace   = false;  // pre-standby countdown running
  uint32_t graceEnteredAt = 0; // millis() the grace began (activity after this cancels it)
  uint32_t lastGraceTick  = 0; // millis() of the last one-second decrement
  int      graceLeft = 0;      // seconds remaining on the countdown

  bool     inStandby   = false; // low-power connected standby (mic off, ping off, Wi-Fi kept)
  uint32_t standbySince = 0;    // millis() standby began (times out -> full power off)

  bool     paused      = false; // privacy pause active -> motion must NOT keep the device awake

  uint32_t lastBattPoll = 0;   // millis() of the last battery read
  bool     warnedLow    = false;  // low-battery warning already shown this discharge

  const uint32_t MOTION_FRESH_MS = 1000;  // motion within this long = "not stationary"
  const uint32_t BATT_POLL_MS    = 30000; // battery check cadence

  // ---- terminal action (owns M5.Power) -- does not return ----
  void enterOff() {
    LOG_WARN("power[off]: FULL POWER OFF now (wake = PWR button, cold boot)");
    M5.Display.setBrightness(0);
    delay(30);                                          // let the last log flush
    M5.Power.powerOff();                                // M5PM1 cuts the rails
  }

  void cancelGrace() {
    if (!inGrace) return;
    inGrace = false;
    LOG_INFO("power[grace]: CANCELLED (activity) -> awake");
    if (cbGrace) cbGrace(false);
  }

  // Enter the low-power connected standby: mic off, device ping off, screen off, Wi-Fi held
  // (deeper modem sleep). The socket stays alive on the server's own keepalives, so a button
  // press recovers instantly with no reconnect. Effects go out via cbStandby(true).
  void enterStandby() {
    inGrace = false;
    inStandby = true;
    standbySince = millis();
    WiFi.setSleep(WIFI_PS_MAX_MODEM);                   // sleep the radio harder while idle
    LOG_WARN("power[standby]: ENTERING (mic off, ping off, link kept warm; button to wake)");
    if (cbStandby) cbStandby(true);
  }

  void exitStandby() {
    if (!inStandby) return;
    inStandby = false;
    idleSince = millis();
    WiFi.setSleep(WIFI_PS_MIN_MODEM);                   // back to responsive
    LOG_INFO("power[standby]: RECOVERED (button) -> awake");
    if (cbStandby) cbStandby(false);
  }
}

void power::begin() {
  // Minimise average current: cap the CPU and let the Wi-Fi modem sleep between beacons
  // (during silence no audio streams, so the radio idles). 160 MHz keeps headroom for the
  // mbedtls/TLS + streaming path; 80 MHz (the Wi-Fi floor) saves a little more if the
  // runtime target demands it -- try that only after confirming streaming stays clean.
  setCpuFrequencyMhz(160);
  WiFi.setSleep(WIFI_PS_MIN_MODEM);
  idleSince = millis();
  lastBattPoll = 0;
  warnedLow = false;
  inGrace = inStandby = false;
  const config::Config& c = config::get();
  LOG_INFO("power[begin]: awake (cpu=160MHz, wifi modem-sleep; idle=%us grace=%us standbyMax=%us)",
           (unsigned)c.idleTimeoutS, (unsigned)c.graceS, (unsigned)c.standbyMaxS);
}

void power::noteActivity() {
  idleSince = millis();
  if (inGrace)   cancelGrace();
  if (inStandby) exitStandby();      // a BUTTON (or conversation event) wakes standby
}

void power::noteVoiced(bool on) {
  if (!on) return;
  idleSince = millis();
  if (inGrace) cancelGrace();
  // (no standby exit: the mic is off in standby, so this never fires while asleep)
}

void power::setPaused(bool on) { paused = on; }

bool power::standby() { return inStandby; }
bool power::grace()   { return inGrace; }

void power::loop() {
  uint32_t now = millis();
  const config::Config& c = config::get();

  // Motion (controls owns the IMU) counts as activity ONLY while awake -- it keeps the device
  // out of standby, but is IGNORED once in standby (only a button wakes it back). While
  // privacy-paused the mic is off and there is nothing to wait for, so motion is ignored too:
  // a worn, paused pendant idles -> standby -> off on the normal timeout instead of staying lit.
  if (!inStandby && !paused && now - controls::lastMotionMs() < MOTION_FRESH_MS) idleSince = now;

  // Battery: poll on a slow cadence; warn once when low and not charging, with hysteresis
  // so a sagging reading near the threshold can't re-trigger the warning repeatedly.
  if (now - lastBattPoll >= BATT_POLL_MS) {
    lastBattPoll = now;
    int lvl = M5.Power.getBatteryLevel();
    bool charging = (M5.Power.isCharging() == m5::Power_Class::is_charging);
    if (charging || lvl > c.lowBattPct + 5) {
      warnedLow = false;
    } else if (lvl >= 0 && lvl <= c.lowBattPct && !warnedLow) {
      warnedLow = true;
      LOG_WARN("power[batt]: LOW %d%% (<=%u%%) -> warning", lvl, c.lowBattPct);
      if (cbLowBatt) cbLowBatt();
    }
  }

  // Auto-sleep is off in dev mode (no grace, no standby).
  if (c.devMode) return;

  // ---- Standby: hold the warm connection until the timeout, then full power off ----
  if (inStandby) {
    if (now - standbySince >= (uint32_t)c.standbyMaxS * 1000) {
      LOG_WARN("power[standby]: %us held -> shutting down link then POWER OFF", (unsigned)c.standbyMaxS);
      if (cbSleep) cbSleep();     // wiring: net::shutdown() (clean WS close) -- runs first
      enterOff();                 // owns M5.Power; does not return
    }
    return;                       // wake is a button (noteActivity -> exitStandby), not here
  }

  // ---- Awake -> grace ----
  uint32_t idleMs = (uint32_t)c.idleTimeoutS * 1000;
  if (!inGrace) {
    if (now - idleSince >= idleMs) {                    // idle (silent + stationary) long enough
      inGrace = true;
      graceEnteredAt = now;
      lastGraceTick = now;
      graceLeft = c.graceS ? c.graceS : 1;
      LOG_INFO("power[idle]: %us silent+still -> GRACE started (%ds countdown)",
               (unsigned)c.idleTimeoutS, graceLeft);
      if (cbGrace)     cbGrace(true);
      if (cbGraceTick) cbGraceTick(graceLeft);
    }
    return;
  }

  // ---- Grace: any activity since it began cancels it; expiry -> standby ----
  if (idleSince > graceEnteredAt) { cancelGrace(); return; }
  if (now - lastGraceTick >= 1000) {
    lastGraceTick += 1000;
    if (--graceLeft > 0) {
      LOG_INFO("power[grace]: standby in %ds", graceLeft);
      if (cbGraceTick) cbGraceTick(graceLeft);
    } else {
      LOG_WARN("power[grace]: expired -> low-power standby");
      enterStandby();             // mic/ping/screen off via cbStandby; socket kept warm
    }
  }
}

// ---- callback registration ----
void power::onGrace(void (*cb)(bool))    { cbGrace     = cb; }
void power::onGraceTick(void (*cb)(int)) { cbGraceTick = cb; }
void power::onStandby(void (*cb)(bool))  { cbStandby   = cb; }
void power::onSleep(void (*cb)())        { cbSleep     = cb; }
void power::onLowBatt(void (*cb)())      { cbLowBatt   = cb; }
