#pragma once
// Buttons + IMU -> semantic events. Owns no screen/network state.
namespace controls {
  void begin();
  void loop();                              // call after M5.update() each loop
  void onWake(void (*cb)());                // BtnA short  (wake / advance)
  void onHoldA(void (*cb)());               // BtnA long   (save clip / settings-back)
  void onPause(void (*cb)());               // BtnB short  (pause/resume)
  void onStatus(void (*cb)());              // BtnB long   (status screen)
  void onPowerOff(void (*cb)());            // BtnPWR long  (power off)
  void onOrient(void (*cb)(int rotation));  // orientation changed (0..3)
  void onLift(void (*cb)());                // lift-to-wake gesture
  bool heldA();                             // BtnA currently held (drives a press indicator)
  bool heldB();                             // BtnB currently held
  bool heldPwr();                           // BtnPWR currently held
}
