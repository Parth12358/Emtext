#pragma once
#include <Arduino.h>

// Stage 8 power management. Owns M5.Power (battery, power off) + Wi-Fi power settings, and the
// idle -> sleep escalation. Pure policy + M5.Power calls: it never touches the display
// or the network directly -- those happen through the callbacks below, wired in
// emtext.ino, so the module boundaries stay clean (display owns the screen, net owns
// the socket). Reads its knobs from config at call time; reads motion from controls.
//
// Staged power cycle (NO ESP deep sleep -- see the note below):
//   Awake  ->  Idle (display auto-dim, still listening)  ->  grace countdown
//          ->  Low-power STANDBY (mic off, ping off, Wi-Fi kept connected)  ->  full power off
// Standby keeps the socket alive on the server's own keepalives (the device stops pinging),
// so it recovers INSTANTLY to Awake on a BUTTON press -- no reconnect. Motion is IGNORED in
// standby (it only keeps the device awake / delays entering standby). After standbyMaxS in
// standby the link is closed cleanly and the device fully powers off.
//
// Why no deep sleep: no BLE, no timer/RTC wake, no RTC-memory retention, and wake would be
// the PMIC button anyway -- ESP deep sleep still restarts the sketch (no fast resume) and
// draws more than a full power-off, so it buys nothing. The recoverable low-power tier is the
// connected standby (which deep sleep can't do -- it drops Wi-Fi); the terminal tier is a full
// power off.
//
// NOTE: there is NO firmware power-off gesture. On the M5StickS3 the PWR button is the M5PM1
// PMIC's power key and does a HARDWARE power-off on a tap -- firmware can neither intercept it
// (only a "click" is ever reported, never a hold) nor retime it. So PWR = the physical off
// switch; deliberate shutdown otherwise is the automatic idle path above (clean close first).
namespace power {
  void begin();              // low-power config (CPU clock, Wi-Fi modem sleep) + start the idle timer
  void loop();               // idle escalation + standby + battery poll -- call every loop

  void noteActivity();       // a button / conversation event: reset idle, cancel grace, WAKE from standby
  void noteVoiced(bool on);  // speech-energy from the mic gate: reset the idle timer while true
  bool standby();            // currently in low-power standby? (so a waking press can be consumed)
  bool grace();              // currently in the pre-standby grace countdown? (a press cancels+consumes)

  // Callbacks (wired in emtext.ino). onStandby(true/false) toggles the low-power state's
  // effects (mic off, ping off, screen off); onSleep fires immediately BEFORE the full power
  // off, so the wiring can close the socket cleanly (net::shutdown()).
  void onGrace(void (*cb)(bool on));      // show/hide the pre-standby grace overlay
  void onGraceTick(void (*cb)(int secs)); // update the grace countdown number
  void onStandby(void (*cb)(bool on));    // enter/leave low-power standby (mic/ping/screen)
  void onSleep(void (*cb)());             // about to power off -> close the socket cleanly
  void onLowBatt(void (*cb)());           // one-time low-battery warning
}
