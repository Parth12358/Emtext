#include <Arduino.h>
#include <M5Unified.h>

//importing from src
#include "src/logx.h"
#include "src/config/config.h"
#include "src/display/display.h"
#include "src/controls/controls.h"
#include "src/audio/audio.h"

// ---- semantic button callbacks (wiring only) ----
static void onWake() {
  using S = display::State;
  switch (display::state()) {
    case S::Dark:   display::setState(S::Glance);  break;
    case S::Glance: display::setState(S::History); break;
    default:        display::setState(S::Glance);  break;
  }
}
// Mic runs only when neither paused (privacy) nor muted -- so un-muting never
// silently re-opens the mic while paused.
static bool g_paused = false;
static bool g_muted  = false;
static void applyMic() { audio::setPaused(g_paused || g_muted); }

static void onMute() {
  g_muted = !g_muted;
  display::setMuted(g_muted);
  applyMic();
  LOG_INFO("muted %s (mic %s)", g_muted ? "on" : "off", (g_paused || g_muted) ? "off" : "on");
}
static void onPause() {
  g_paused = !g_paused;
  display::setPaused(g_paused);
  applyMic();
  LOG_INFO("streaming %s", g_paused ? "paused" : "resumed");
}
static void onStatus()   { display::setState(display::State::Status); }
static void onPowerOff() { LOG_WARN("power off (stub)"); }
static void onOrient(int rot) { display::setRotation(rot); }
static void onLift() {
  if (display::state() == display::State::Dark) display::setState(display::State::Glance);
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


  M5.Display.setRotation(1);                 // landscape; orientation comes from IMU later
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextColor(isStickS3 ? TFT_GREEN : TFT_RED, TFT_BLACK);
  M5.Display.setTextDatum(middle_center);    // datum for drawString centering
  M5.Display.setTextSize(2);
  M5.Display.drawString("emtext",
                        M5.Display.width()  / 2,
                        M5.Display.height() / 2);
  M5.delay(600);   // brief boot splash before the display module takes over

  // Stage 2: bring up the UX modules and wire their events.
  display::begin();
  controls::begin();
  controls::onWake(onWake);
  controls::onMute(onMute);
  controls::onPause(onPause);
  controls::onStatus(onStatus);
  controls::onPowerOff(onPowerOff);
  controls::onOrient(onOrient);
  controls::onLift(onLift);

  // Stage 3: bring up the mic + energy gate.
  audio::begin();

  // seed fake data so glance/history/status show something (real reads land in Stage 6)
  display::setGlance("hey, nice work", "positive", "hey nice work");
  display::setConnection("searching");
}

void loop() {
  // put your main code here, to run repeatedly:
  M5.update();
  config::handleSerial();
  controls::loop();
  display::loop();
  audio::loop();

  static uint32_t last = 0;
  uint32_t now = millis();
  if (now - last >= 1000) {
    last = now;
    LOG_DEBUG("hb up=%lus batt=%d%%", now/1000, M5.Power.getBatteryLevel());
  }

  M5.delay(1);
}
