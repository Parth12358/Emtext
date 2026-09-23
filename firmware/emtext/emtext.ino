#include <Arduino.h>
#include <M5Unified.h>

//importing from src
#include "src/logx.h"
#include "src/config/config.h"
#include "src/display/display.h"
#include "src/controls/controls.h"
#include "src/audio/audio.h"
#include "src/net/net.h"
#include "src/power/power.h"
#include "src/portal/portal.h"

// ---- semantic button callbacks (wiring only) ----
// Setup AP toggle (pressed from the Status screen).
static bool g_ap = false;
static void onToggleAp() {
  g_ap = !g_ap;
  net::setPortal(g_ap);
  portal::setActive(g_ap);
  display::setPortal(g_ap, config::get().apSsid, config::get().apPass, "192.168.4.1");
  LOG_INFO("setup AP %s", g_ap ? "enabled" : "disabled");
}

static void onWake() {
  bool wasAsleep = power::standby() || power::grace();
  power::noteActivity();                    // resets idle; wakes from standby if in it
  if (wasAsleep) return;                   // the waking press just wakes -- don't also advance
  using S = display::State;
  switch (display::state()) {
    case S::Dark: display::setState(S::Glance); break;
    case S::Glance: display::setState(S::History); break;
    case S::Status: display::settingsSelect(); break;  // BtnA selects the highlighted setting
    default: display::setState(S::Glance); break;
  }
}
// Mic runs whenever we're not paused (privacy).
static bool g_paused = false;
static void applyMic() {
  audio::setPaused(g_paused);
}

// BtnA hold: back out of Settings; otherwise save a clip (server-stored; needs server support).
static uint32_t g_clipAt = 0;            // millis() of the last clip action (dev clip glyph)
static int      g_lastUttId = 0;         // highest utterance id seen (utterance OR read frame)
static uint32_t g_lastUttAt = 0;         // millis() the last utterance/read arrived (tail detection)
static bool     g_clipHolding = false;   // BtnA held
static bool     g_clipFinalizing = false;// released, waiting for the tail utterance to land
static uint32_t g_clipReleaseAt = 0;     // millis() of release (grace cap)
static int      g_clipStartAfter = 0;    // last utt id at press -> clip captures ids AFTER this
// BtnA hold BEGINS here (fires once at the hold threshold). Hold-to-record: the clip is the
// utterances heard until release -- the "held window" (see README.md, wire protocol). Release is handled in loop().
static void onHoldA() {
  bool wasAsleep = power::standby() || power::grace();
  power::noteActivity();
  if (wasAsleep) return;                   // the waking hold just wakes -- don't also start a clip
  if (display::state() == display::State::Status) {
    if (!display::settingsLocked()) display::setState(display::State::Glance);
    return;
  }
  g_clipHolding = true;
  g_clipStartAfter = g_lastUttId;
  display::setState(display::State::Glance);      // wake so the recording overlay is visible
  display::setClipRec(true);
  LOG_INFO("clip: REC START -- capturing utterances after #%d", g_clipStartAfter);
}
static void onPause() {
  bool wasAsleep = power::standby() || power::grace();
  power::noteActivity();
  if (wasAsleep) return;                   // the waking press just wakes -- don't also pause
  // In Settings, BtnB scrolls the rows instead of toggling privacy pause.
  if (display::state() == display::State::Status) {
    display::settingsScroll();
    return;
  }
  g_paused = !g_paused;
  display::setPaused(g_paused);
  applyMic();
  LOG_INFO("streaming %s", g_paused ? "paused" : "resumed");
}
static void onStatus() {
  bool wasAsleep = power::standby() || power::grace();
  power::noteActivity();
  if (wasAsleep) return;                   // the waking hold just wakes -- don't also open Settings
  using S = display::State;
  if (display::state() == S::Status) {  // BtnB hold in Settings = back to Glance
    if (!display::settingsLocked()) display::setState(S::Glance);
    return;
  }
  display::setState(S::Status);  // open Settings
}
// PWR on the M5StickS3 is the M5PM1 PMIC's power key -- a tap does a HARDWARE power-off that
// firmware can't intercept or retime, so there is no firmware power-off gesture. Deliberate
// shutdown is the automatic idle path (power::loop -> grace -> low-power standby -> full power
// off), which closes the socket cleanly first. See src/power/power.h.

// Settings rows that reach other modules (Brightness is handled inside display itself).
static void onSetting(display::Setting s) {
  switch (s) {
    case display::Setting::Wifi: onToggleAp(); break;  // launch / stop the setup portal
    case display::Setting::Power:
      // Informational only -- the row shows battery %. There is no firmware power-off
      // (PWR is a hardware off; deliberate shutdown is the idle auto-sleep path).
      break;
    default: break;
  }
}
static void onOrient(int rot) {
  display::setRotation(rot);
}
static void onLift() {
  if (power::standby()) return;   // motion does NOT wake low-power standby (button only)
  if (display::state() == display::State::Dark) display::setState(display::State::Glance);
}

// Frames from the network (dispatched on core 1 by net::loop()).
static String g_lastTranscript;  // from `utterance`; shown under the read on the glance

static void onNetFrame(const proto::Frame& f) {
  switch (f.type) {
    case proto::Type::Ready:
      LOG_INFO("net: [frame] ready");
      break;
    case proto::Type::Status:
      LOG_INFO("net: status=%s", f.status);
      if (strcmp(f.status, "thinking") == 0) display::setProcessing(true);
      break;
    case proto::Type::Utterance:
      LOG_INFO("net: utterance #%d '%s'", f.id, f.transcript);
      power::noteActivity();                          // conversation in progress -> not idle
      g_lastTranscript = f.transcript;
      if (f.id > g_lastUttId) g_lastUttId = f.id;   // includes `user` lines (they get no `read`)
      g_lastUttAt = millis();
      if (g_clipHolding || g_clipFinalizing) LOG_INFO("clip: captured utterance #%d", f.id);
      break;
    case proto::Type::Read:
      LOG_INFO("net: read #%d [%s/%s%s] '%s' conf=%s", f.id,
               f.shown[0] ? f.shown : "-", proto::toneName(f.tone), f.adjusted ? "*" : "",
               f.read, f.confLevel[0] ? f.confLevel : "-");
      if (f.id > g_lastUttId) g_lastUttId = f.id;   // clip-save target (same id space as utterance)
      g_lastUttAt = millis();
      display::setProcessing(false);
      // A "low" confidence read is drawn faint (the requirement: visibly
      // reduced emphasis). Medium and high, or no field at all, draw as before.
      // The heart's colour key is the headline emotion when the server sends
      // one (seven voice-model labels), else the tone (older servers).
      display::setGlance(f.read, f.shown[0] ? String(f.shown) : String(proto::toneName(f.tone)),
                         g_lastTranscript, strcmp(f.confLevel, "low") == 0);
      break;
    case proto::Type::Saved:                      // clip-save reply (see README.md, wire protocol)
      LOG_INFO("net: clip %s (#%d)", f.ok ? "saved" : "FAILED", f.id);
      display::setClip(f.ok ? "saved" : "save failed");
      break;
    default: break;
  }
}

void setup() {
  // put your setup code here, to run once:
  // setting up M5
  auto cfg = M5.config();
  cfg.serial_baudrate = 115200;
  cfg.clear_display = true;
  M5.begin(cfg);

  config::load();
  auto board = M5.getBoard();
  bool isStickS3 = (board == m5::board_t::board_M5StickS3);

  logx::setLevel(logx::INFO);
  LOG_INFO("boot: board id=%d isStickS3=%d", (int)board, isStickS3);

  M5.Display.setRotation(1);  // landscape; orientation comes from IMU later

  // Stage 2: bring up the UX modules and wire their events.
  display::begin();
  display::onSetting(onSetting);  // Settings-page rows -> cross-module actions
  controls::begin();
  controls::onWake(onWake);
  controls::onHoldA(onHoldA);
  controls::onPause(onPause);
  controls::onStatus(onStatus);
  controls::onOrient(onOrient);
  controls::onLift(onLift);
  // No controls::onPowerOff: PWR is a hardware off on the StickS3 (see onPowerOff comment above).

  // Stage 8: power callbacks -- the module owns M5.Power; screen/mic/socket effects go here.
  power::onGrace([](bool on)  { display::setGrace(on); });
  power::onGraceTick([](int s){ display::setGraceSecs(s); });
  power::onStandby([](bool on){                          // low-power standby enter/leave
    audio::setPaused(on ? true : g_paused);             // mic off in standby; restore privacy state on wake
    net::pausePing(on);                                 // stop/resume the client keepalive ping
    display::setState(on ? display::State::Dark : display::State::Glance);
  });
  power::onSleep([]()         { net::shutdown(); });   // clean WS close before full power off
  power::onLowBatt([]()       { display::setLowBatt(); });

  // Stage 3: bring up the mic + energy gate.
  audio::begin();

  // Stage 4: bring up the network owner (runs on its own task on core 0), showing
  // visible boot stages (network -> server -> ready) while it connects.
  display::setBoot(0);                 // network (in progress)
  net::begin();
  net::onFrame(onNetFrame);
  uint32_t bootStart = millis();
  int lastStage = -1; bool lastFail = false;
  while (millis() - bootStart < 20000) {
    net::loop();                       // drain frames so `ready` is seen here
    net::State s = net::state();
    int stage; bool fail = false;
    switch (s) {
      case net::State::Ready:
      case net::State::Degraded:         stage = 3; break;               // all done
      case net::State::Connected:
      case net::State::SocketConnecting: stage = 1; break;               // wifi up, bringing up server
      case net::State::Halted:           stage = 1; fail = true; break;  // auth rejected
      default:                           stage = 0; break;               // searching for wifi
    }
    if (stage != lastStage || fail != lastFail) {
      const char* names[] = { "network", "server", "server", "ready" };
      LOG_INFO("power[boot]: stage %d/%s%s", stage, names[stage], fail ? " FAILED" : "");
      display::setBoot(stage, fail); lastStage = stage; lastFail = fail;
    }
    if (s == net::State::Ready) break;
    if (s == net::State::Halted) { M5.delay(1500); break; }  // show the failure, then carry on
    M5.delay(50);
  }

  // Stage 5.2: gated mic chunks -> network (cross-core handoff). Wired after boot so no
  // audio streams while connecting.
  audio::onChunk([](const int16_t* p, size_t n) {
    net::sendAudio(p, n);
  });

  // Stage 8: low-power config (CPU clock + Wi-Fi modem sleep) + start the idle timer.
  power::begin();

  // Stage 4P: setup AP portal (toggled from the Status screen; serves the config page).
  portal::begin();

  // Leave the boot screen: dev keeps the screen on (never auto-dims); otherwise go Dark
  // and let real reads / a lift wake the glance.
  display::setConnection(net::state() == net::State::Ready ? "ready" : "searching");
  display::setState(config::get().devMode ? display::State::Glance : display::State::Dark);
}

void loop() {
  // put your main code here, to run repeatedly:
  M5.update();
  config::handleSerial();
  controls::loop();
  power::noteVoiced(audio::voiced());   // speech-energy keeps the device awake (idle gate)
  power::loop();                        // idle escalation (grace -> standby -> off) + battery poll
  display::setButtons(controls::heldA(), controls::heldB(), controls::heldPwr());  // press indicators
  display::setPing(net::pingMs());                                                 // status-bar ping
  uint32_t nowMs = millis();                                                       // dev activity glyphs
  display::setActivity(audio::voiced(),                                            // listening
                       nowMs - net::lastTxMs() < 250,                              // sending
                       nowMs - net::lastRxMs() < 250,                              // receiving
                       nowMs - g_clipAt < 500);                                    // clip
  // hold-to-record, release: don't finalize yet -- the last utterance spoken is still being
  // transcribed (its utterance/read lands 1-3s later), so wait for the tail before sending.
  if (g_clipHolding && !controls::heldA()) {
    g_clipHolding = false;
    g_clipFinalizing = true;
    g_clipReleaseAt = nowMs;
    display::setClipRec(false);
    display::setSaving(true);                             // "saving..." while the tail finishes
    LOG_INFO("clip: RELEASE -- finalizing (waiting for tail); startAfter=#%d", g_clipStartAfter);
  }
  if (g_clipFinalizing) {
    const uint32_t QUIET_MS = 1200, GRACE_MAX = 4000;
    bool settled = (nowMs - g_lastUttAt) > QUIET_MS;     // utterances stopped arriving
    bool capped  = (nowMs - g_clipReleaseAt) > GRACE_MAX; // don't wait forever
    if (settled || capped) {
      g_clipFinalizing = false;
      display::setSaving(false);
      int from = g_clipStartAfter + 1, to = g_lastUttId; // every utterance heard during the hold
      LOG_INFO("clip: FINALIZE (%s) startAfter=#%d lastUtt=#%d -> range %d..%d",
               capped ? "cap" : "settled", g_clipStartAfter, g_lastUttId, from, to);
      if (to >= from) {
        net::saveClip(from, to);                          // -> {"type":"save","from":..,"to":..}
        display::setClip("saved");                        // optimistic; server's `saved` confirms
        g_clipAt = nowMs;
        LOG_INFO("clip: SAVE REQUESTED #%d..#%d (%d utterance(s))", from, to, to - from + 1);
      } else {
        display::setClip("empty");                        // nothing was spoken during the hold
        LOG_INFO("clip: EMPTY -- no utterance during the hold");
      }
    }
  }
  display::loop();
  audio::loop();
  net::loop();
  portal::loop();

  // reflect the network state on the display's connectivity glyph (shape, not colour)
  static net::State lastNet = net::State::Off;
  if (net::state() != lastNet) {
    lastNet = net::state();
    const char* lbl = (lastNet == net::State::Ready)    ? "ready"
                    : (lastNet == net::State::Degraded) ? "degraded" : "searching";
    display::setConnection(lbl);
  }

  static uint32_t last = 0;
  uint32_t now = millis();
  if (now - last >= 1000) {
    last = now;
    LOG_DEBUG("hb up=%lus batt=%d%%", now / 1000, M5.Power.getBatteryLevel());
  }

  M5.delay(1);
}
