#pragma once
#include <Arduino.h>


namespace config {
  struct Net { String ssid;  String pass; };

  struct Config {
    Net nets[3]; //fallback list
    uint8_t netCount = 3;

    String serverHost;  //server website
    String serverPath = "/stream";
    String token;

    uint16_t chunkMs = 64; //audio frame size
    uint16_t energyFloor = 256; //device gate; < serer SPEECH_RMS = 500
    uint32_t idleTimeoutS = 300; //auto off timer
    uint8_t logLevel = 2;   //  0 ERR; 1 WARN; 2 INFO; 2 DEBUG;

    // permanent setup access point (always on; join to reconfigure)
    String apSsid = "emtext-setup";
    String apPass = "emtextsetup";   // >= 8 chars (WPA2); override in secrets.h
  };

  void load();  //secrets.h defaults, then NVS overrides
  void save(); //persist current config to NVS
  void clear(); //wipe NVS namespace
  const Config& get();
  void handleSerial();

  // programmatic setters (used by the Wi-Fi provisioning portal)
  void setWifi(const String& ssid, const String& pass);
  void setHost(const String& host);
  void setToken(const String& token);
}

