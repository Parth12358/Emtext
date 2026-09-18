#include "controls.h"
#include "../logx.h"
#include <M5Unified.h>
#include <math.h>

namespace {
  void (*cbWake)()      = nullptr;
  void (*cbHoldA)()     = nullptr;
  void (*cbPause)()     = nullptr;
  void (*cbStatus)()    = nullptr;
  void (*cbPower)()     = nullptr;
  void (*cbOrient)(int) = nullptr;
  void (*cbLift)()      = nullptr;
  uint32_t lastImu  = 0;
  uint32_t lastLift = 0;
  int      lastRot  = -1;
  int      pendingRot   = -1;   // orientation debounce: a candidate rotation not yet committed
  uint32_t pendingSince = 0;    // when that candidate was first seen
}

void controls::begin() { lastRot = M5.Display.getRotation(); }

void controls::onWake(void (*cb)())      { cbWake   = cb; }
void controls::onHoldA(void (*cb)())     { cbHoldA  = cb; }
void controls::onPause(void (*cb)())     { cbPause  = cb; }
void controls::onStatus(void (*cb)())    { cbStatus = cb; }
void controls::onPowerOff(void (*cb)())  { cbPower  = cb; }
void controls::onOrient(void (*cb)(int)) { cbOrient = cb; }
void controls::onLift(void (*cb)())      { cbLift   = cb; }

bool controls::heldA()   { return M5.BtnA.isPressed(); }
bool controls::heldB()   { return M5.BtnB.isPressed(); }
bool controls::heldPwr() { return M5.BtnPWR.isPressed(); }

void controls::loop() {
  // Buttons (M5.update() is called in emtext.ino before this).
  if (M5.BtnA.wasClicked() && cbWake)   cbWake();
  if (M5.BtnA.wasHold()    && cbHoldA)  cbHoldA();
  if (M5.BtnB.wasClicked() && cbPause)  cbPause();
  if (M5.BtnB.wasHold()    && cbStatus) cbStatus();
  if (M5.BtnPWR.wasHold()  && cbPower)  cbPower();

  // IMU: orientation + lift-to-wake, sampled ~20 Hz.
  uint32_t now = millis();
  if (now - lastImu >= 50) {
    lastImu = now;
    M5.Imu.update();
    float ax, ay, az;
    if (M5.Imu.getAccel(&ax, &ay, &az)) {
      // orientation -- debounced so it can't flip-flop near the 45 deg boundary (where
      // ax ~= ay and the raw quadrant jitters). A new reading must hold steady for
      // ORIENT_STABLE_MS before we commit it and fire. Signs tuned on the hardware.
      const uint32_t ORIENT_STABLE_MS = 500;
      int rot;
      if (fabsf(ax) > fabsf(ay)) rot = (ax > 0) ? 2 : 0;   // portrait
      else                       rot = (ay > 0) ? 1 : 3;   // landscape
      if (rot == lastRot) {
        pendingRot = rot;                                  // back to committed: cancel any pending flip
      } else if (rot != pendingRot) {
        pendingRot = rot; pendingSince = now;              // new candidate: (re)start the timer
      } else if (now - pendingSince >= ORIENT_STABLE_MS) {
        lastRot = rot;                                     // held steady long enough: commit
        if (cbOrient) cbOrient(rot);
      }

      // lift-to-wake: a brief motion spike, then a cooldown so it fires once.
      // Coarse on purpose -- tune LIFT_G / LIFT_COOLDOWN_MS on the hardware.
      float mag = sqrtf(ax * ax + ay * ay + az * az);
      const float    LIFT_G = 1.35f;
      const uint32_t LIFT_COOLDOWN_MS = 1500;
      if (mag > LIFT_G && (now - lastLift) > LIFT_COOLDOWN_MS) {
        lastLift = now;
        if (cbLift) cbLift();
      }
    }
  }
}
