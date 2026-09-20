#include "controls.h"
#include "../logx.h"
#include <M5Unified.h>
#include <math.h>

namespace {
  void (*cbWake)()       = nullptr;
  void (*cbHoldA)()      = nullptr;
  void (*cbPause)()      = nullptr;
  void (*cbStatus)()     = nullptr;
  void (*cbPower)()      = nullptr;
  void (*cbOrient)(int)  = nullptr;
  void (*cbLift)()       = nullptr;
  uint32_t lastImu    = 0;
  uint32_t lastLift   = 0;
  uint32_t lastMotion = 0;   // millis() of the last accel deviation from rest (idle-sleep gating)
  float    motionEma  = 0;   // low-passed |accel|-1g, so brief jolts don't count as motion
  int      lastRot    = -1;
  int      pendingRot   = -1;   // orientation debounce: a candidate rotation not yet committed
  uint32_t pendingSince = 0;    // when that candidate was first seen
}

void controls::begin() { lastRot = M5.Display.getRotation(); }

void controls::onWake(void (*cb)())      { cbWake   = cb; }
void controls::onHoldA(void (*cb)())     { cbHoldA  = cb; }
void controls::onPause(void (*cb)())     { cbPause  = cb; }
void controls::onStatus(void (*cb)())    { cbStatus = cb; }
void controls::onPowerOff(void (*cb)())   { cbPower      = cb; }
void controls::onOrient(void (*cb)(int))  { cbOrient = cb; }
void controls::onLift(void (*cb)())       { cbLift   = cb; }

bool controls::heldA()   { return M5.BtnA.isPressed(); }
bool controls::heldB()   { return M5.BtnB.isPressed(); }
bool controls::heldPwr() { return M5.BtnPWR.isPressed(); }
uint32_t controls::lastMotionMs() { return lastMotion; }

void controls::loop() {
  // Buttons (M5.update() is called in emtext.ino before this).
  if (M5.BtnA.wasClicked() && cbWake)   cbWake();
  if (M5.BtnA.wasHold()    && cbHoldA)  cbHoldA();
  if (M5.BtnB.wasClicked() && cbPause)  cbPause();
  if (M5.BtnB.wasHold()    && cbStatus) cbStatus();
  if (M5.BtnPWR.wasHold() && cbPower) cbPower();   // PWR: hardware off on the StickS3 (rarely reaches here)

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
      // Hardware rotation offset: the raw quadrant->rot table above landed 90 deg off on
      // this unit, so nudge every committed rotation by a fixed number of 90 deg steps.
      // 3 == one step counter-clockwise. If it comes out the WRONG way (still 90 deg off),
      // set this to 1; if it's 180 deg off, the axis mapping (not this) is the problem.
      const int ROT_OFFSET = 3;                            // 90 deg CCW correction
      rot = (rot + ROT_OFFSET) & 3;
      if (rot == lastRot) {
        pendingRot = rot;                                  // back to committed: cancel any pending flip
      } else if (rot != pendingRot) {
        pendingRot = rot; pendingSince = now;              // new candidate: (re)start the timer
      } else if (now - pendingSince >= ORIENT_STABLE_MS) {
        lastRot = rot;                                     // held steady long enough: commit
        // DIAGNOSTIC (orientation tuning): log the accel signature that produced this rot,
        // so the quadrant->rot mapping above can be corrected against the real hardware.
        LOG_INFO("orient: commit rot=%d  (ax=%.2f ay=%.2f az=%.2f)", rot, ax, ay, az);
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

      // Motion vs rest, for the idle-sleep gate: at rest the accel magnitude is ~1g
      // (gravity only); handling/wearing pushes it off 1g. To avoid a single jolt or
      // sensor noise flagging "moving", the deviation is DAMPENED by a low-pass (EMA)
      // and compared after smoothing -- only sustained motion clears the threshold.
      // MOTION_LP is the smoothing (lower = more damping); both are loose, tune on hw.
      const float MOTION_G  = 0.08f;
      const float MOTION_LP = 0.20f;                       // ~200 ms time constant at 20 Hz
      motionEma += (fabsf(mag - 1.0f) - motionEma) * MOTION_LP;
      if (motionEma > MOTION_G) lastMotion = now;
    }
  }
}
