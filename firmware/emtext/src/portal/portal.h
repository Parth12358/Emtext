#pragma once
// Always-on Wi-Fi provisioning portal. `net` owns and starts the setup AP; this serves
// the config page (WebServer + DNS captive redirect) to anyone who joins it. Pumped from
// the main loop (core 1) so it stays responsive even while `net` (core 0) is (re)connecting.
namespace portal {
  void begin();             // register the WebServer once (call after net::begin())
  void setActive(bool on);  // enable/disable serving (follows the setup-AP toggle)
  void loop();              // call every main-loop pass
}
