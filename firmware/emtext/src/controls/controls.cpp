#include "controls.h"
#include "../logx.h"
#include <M5Unified.h>
#include <math.h>

namespace {
  void (*cbWake)()      = nullptr;
  void (*cbMute)()      = nullptr;
  void (*cbPause)()     = nullptr;
  void (*cbStatus)()    = nullptr;
  void (*cbPower)()     = nullptr;
  void (*cbOrient)(int) = nullptr;
  uint32_t lastImu = 0;
  int      lastRot = -1;
}

void controls::begin() { lastRot = M5.Display.getRotation(); }

void controls::onWake(void (*cb)())      { cbWake   = cb; }
void controls::onMute(void (*cb)())      { cbMute   = cb; }
void controls::onPause(void (*cb)())     { cbPause  = cb; }
void controls::onStatus(void (*cb)())    { cbStatus = cb; }
void controls::onPowerOff(void (*cb)())  { cbPower  = cb; }
void controls::onOrient(void (*cb)(int)) { cbOrient = cb; }

void controls::loop() {
  // Buttons (M5.update() is called in emtext.ino before this).
  if (M5.BtnA.wasClicked() && cbWake)   cbWake();
  if (M5.BtnA.wasHold()    && cbMute)   cbMute();
  if (M5.BtnB.wasClicked() && cbPause)  cbPause();
  if (M5.BtnB.wasHold()    && cbStatus) cbStatus();
  if (M5.BtnPWR.wasHold()  && cbPower)  cbPower();

  // Orientation from gravity, throttled to ~5 Hz.
  uint32_t now = millis();
  if (now - lastImu >= 200) {
    lastImu = now;
    M5.Imu.update();
    float ax, ay, az;
    if (M5.Imu.getAccel(&ax, &ay, &az)) {
      int rot;
      if (fabsf(ax) > fabsf(ay)) rot = (ax > 0) ? 2 : 0;   // portrait
      else                       rot = (ay > 0) ? 1 : 3;   // landscape
      if (rot != lastRot) { lastRot = rot; if (cbOrient) cbOrient(rot); }
    }
  }
}
