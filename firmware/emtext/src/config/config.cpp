#include "config.h"
#include "../logx.h"
#include <Preferences.h>

#if __has_include("../../secrets.h")
  #include "../../secrets.h"
#endif

namespace {
  config::Config cfg;
  Preferences    prefs;
  const char*    NS = "emtext";
  String         line;

  void applySecrets() {
  #ifdef CFG_WIFI0_SSID
    cfg.nets[0] = { CFG_WIFI0_SSID, CFG_WIFI0_PASS }; cfg.netCount = 1;
  #endif
  #ifdef CFG_SERVER_HOST
    cfg.serverHost = CFG_SERVER_HOST;
  #endif
  #ifdef CFG_TOKEN
    cfg.token = CFG_TOKEN;
  #endif
  }
}


const config::Config& config::get() { return cfg; }

void config::load() {
  applySecrets();
  prefs.begin(NS, true);
  if (prefs.isKey("host"))  cfg.serverHost = prefs.getString("host");
  if (prefs.isKey("path"))  cfg.serverPath = prefs.getString("path");
  if (prefs.isKey("token")) cfg.token      = prefs.getString("token");
  if (prefs.isKey("ssid0")) { cfg.nets[0].ssid = prefs.getString("ssid0"); cfg.nets[0].pass = prefs.getString("pass0"); cfg.netCount = 1; }
  cfg.chunkMs      = prefs.getUInt("chunkMs", cfg.chunkMs);
  cfg.energyFloor  = prefs.getUInt("efloor",  cfg.energyFloor);
  cfg.idleTimeoutS = prefs.getUInt("idleS",   cfg.idleTimeoutS);
  cfg.logLevel     = prefs.getUInt("logLvl",  cfg.logLevel);
  prefs.end();
  logx::setLevel((logx::Level)cfg.logLevel);
  LOG_INFO("config loaded: host=%s nets=%d chunk=%d floor=%d", cfg.serverHost.c_str(), cfg.netCount, cfg.chunkMs, cfg.energyFloor);
}


void config::save() {
  prefs.begin(NS, false);
  prefs.putString("host", cfg.serverHost);
  prefs.putString("path", cfg.serverPath);
  prefs.putString("token", cfg.token);
  prefs.putString("ssid0", cfg.nets[0].ssid);
  prefs.putString("pass0", cfg.nets[0].pass);
  prefs.putUInt("chunkMs", cfg.chunkMs);
  prefs.putUInt("efloor",  cfg.energyFloor);
  prefs.putUInt("idleS",   cfg.idleTimeoutS);
  prefs.putUInt("logLvl",  cfg.logLevel);
  prefs.end();
  LOG_INFO("config saved to NVS");
}



void config::clear() {
  prefs.begin(NS, false); prefs.clear(); prefs.end();
  LOG_WARN("config NVS cleared (reboot to reload defaults)");
}



static void dispatch(const String& s) {
  if (s == "get") {
    LOG_INFO("host=%s path=%s tokenLen=%d", cfg.serverHost.c_str(), cfg.serverPath.c_str(), cfg.token.length());
    LOG_INFO("wifi0=%s chunk=%d floor=%d idleS=%lu logLvl=%d", cfg.nets[0].ssid.c_str(), cfg.chunkMs, cfg.energyFloor, (unsigned long)cfg.idleTimeoutS, cfg.logLevel);
    return;
  }
  if (s == "save")  { config::save();  return; }
  if (s == "clear") { config::clear(); return; }

  if (!s.startsWith("set ")) { LOG_WARN("commands: get | set <key> <val> | save | clear"); return; }
  int sp2 = s.indexOf(' ', 4);
  if (sp2 < 0) { LOG_WARN("usage: set <key> <value>"); return; }
  String key = s.substring(4, sp2);
  String val = s.substring(sp2 + 1);
  if      (key == "host")  cfg.serverHost   = val;
  else if (key == "path")  cfg.serverPath   = val;
  else if (key == "token") cfg.token        = val;
  else if (key == "ssid")  { cfg.nets[0].ssid = val; cfg.netCount = 1; }
  else if (key == "pass")  cfg.nets[0].pass = val;
  else if (key == "chunk") cfg.chunkMs      = val.toInt();
  else if (key == "floor") cfg.energyFloor  = val.toInt();
  else if (key == "idle")  cfg.idleTimeoutS = val.toInt();
  else if (key == "log")   { cfg.logLevel = val.toInt(); logx::setLevel((logx::Level)cfg.logLevel); }
  else { LOG_WARN("unknown key: %s", key.c_str()); return; }
  LOG_INFO("set %s (unsaved -- type `save`)", key.c_str());
}




void config::handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') { if (line.length()) { dispatch(line); line = ""; } }
    else if (line.length() < 200) line += c;
  }
}
