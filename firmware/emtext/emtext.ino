#include <Arduino.h>
#include <M5Unified.h>

//importing from src
#include "src/logx.h"
#include "src/config/config.h"
#include "src/display/display.h"
#include "src/controls/controls.h"
#include "src/audio/audio.h"
#include "src/net/net.h"
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
static uint32_t g_clipAt = 0;         // millis() of the last clip action (drives the dev clip glyph)
static int      g_lastReadId = 0;     // id of the last displayed read
static bool     g_clipHolding = false;// BtnA hold-to-record in progress
static int      g_clipStartAfter = 0; // last read id at press -> the clip captures ids AFTER this
// BtnA hold BEGINS here (fires once at the hold threshold). Hold-to-record: the clip is the
// utterances heard until release -- the "held window" (see clips.md). Release is handled in loop().
static void onHoldA() {
  if (display::state() == display::State::Status) {
    if (!display::settingsLocked()) display::setState(display::State::Glance);
    return;
  }
  g_clipHolding = true;
  g_clipStartAfter = g_lastReadId;
  display::setState(display::State::Glance);      // wake so the "rec" badge is visible
  display::setClipRec(true);
  LOG_INFO("clip: REC START -- holding; will capture reads AFTER #%d", g_clipStartAfter);
}
static void onPause() {
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
  using S = display::State;
  if (display::state() == S::Status) {  // BtnB hold in Settings = back to Glance
    if (!display::settingsLocked()) display::setState(S::Glance);
    return;
  }
  display::setState(S::Status);  // open Settings
}
static void onPowerOff() {  // interim: PWR -> Dark (two-step off is Stage 8)
  display::setState(display::State::Dark);
  LOG_WARN("power: -> dark (two-step off TODO)");
}

// Settings rows that reach other modules (Brightness is handled inside display itself).
static void onSetting(display::Setting s) {
  switch (s) {
    case display::Setting::Wifi: onToggleAp(); break;  // launch / stop the setup portal
    case display::Setting::Power:
      LOG_WARN("power profile toggle (stub -- Stage 8)");
      break;
    default: break;
  }
}
static void onOrient(int rot) {
  display::setRotation(rot);
}
static void onLift() {
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
      g_lastTranscript = f.transcript;
      break;
    case proto::Type::Read:
      LOG_INFO("net: read #%d [%s] '%s'", f.id, proto::toneName(f.tone), f.read);
      g_lastReadId = f.id;                       // the clip-save target
      if (g_clipHolding) LOG_INFO("clip: captured read #%d during hold", f.id);
      display::setProcessing(false);
      display::setGlance(f.read, proto::toneName(f.tone), g_lastTranscript);
      break;
    case proto::Type::Saved:                      // clip-save reply (see clips.md)
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
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextColor(isStickS3 ? TFT_GREEN : TFT_RED, TFT_BLACK);
  M5.Display.setTextDatum(middle_center);  // datum for drawString centering
  M5.Display.setTextSize(2);
  M5.Display.drawString("emtext",
                        M5.Display.width() / 2,
                        M5.Display.height() / 2);
  M5.delay(600);  // brief boot splash before the display module takes over

  // Stage 2: bring up the UX modules and wire their events.
  display::begin();
  display::onSetting(onSetting);  // Settings-page rows -> cross-module actions
  controls::begin();
  controls::onWake(onWake);
  controls::onHoldA(onHoldA);
  controls::onPause(onPause);
  controls::onStatus(onStatus);
  controls::onPowerOff(onPowerOff);
  controls::onOrient(onOrient);
  controls::onLift(onLift);

  // Stage 3: bring up the mic + energy gate.
  audio::begin();

  // Stage 4: bring up the network owner (runs on its own task on core 0).
  net::begin();
  net::onFrame(onNetFrame);

  // Stage 5.2: gated mic chunks -> network (cross-core handoff).
  audio::onChunk([](const int16_t* p, size_t n) {
    net::sendAudio(p, n);
  });

  // Stage 4P: setup AP portal (toggled from the Status screen; serves the config page).
  portal::begin();

  // start blank -- real reads populate the glance once the conversation begins
  display::setConnection("searching");
}

void loop() {
  // put your main code here, to run repeatedly:
  M5.update();
  config::handleSerial();
  controls::loop();
  display::setButtons(controls::heldA(), controls::heldB(), controls::heldPwr());  // press indicators
  display::setPing(net::pingMs());                                                 // status-bar ping
  uint32_t nowMs = millis();                                                       // dev activity glyphs
  display::setActivity(audio::voiced(),                                            // listening
                       nowMs - net::lastTxMs() < 250,                              // sending
                       nowMs - net::lastRxMs() < 250,                              // receiving
                       nowMs - g_clipAt < 500);                                    // clip
  // hold-to-record: finalize the clip when BtnA is released
  if (g_clipHolding && !controls::heldA()) {
    g_clipHolding = false;
    display::setClipRec(false);
    int from = g_clipStartAfter + 1, to = g_lastReadId;   // the held-window utterances
    LOG_INFO("clip: REC RELEASE -- startAfter=#%d lastRead=#%d -> range %d..%d",
             g_clipStartAfter, g_lastReadId, from, to);
    if (to >= from) {
      net::saveClip(from, to);                            // -> {"type":"save","from":..,"to":..}
      display::setClip("saved");                          // optimistic; server's `saved` confirms
      g_clipAt = nowMs;
      LOG_INFO("clip: SAVE REQUESTED #%d..#%d (%d utterance(s))", from, to, to - from + 1);
    } else {
      display::setClip("empty");                          // nothing new completed during the hold
      LOG_INFO("clip: EMPTY -- no NEW read completed during the hold (reads lag speech by ~1-3s)");
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
