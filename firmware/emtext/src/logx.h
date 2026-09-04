#pragma once
#include <Arduino.h>

namespace logx {
  enum Level { ERR = 0, WARN = 1, INFO = 2, DEBUG = 3 };

  inline Level& level() { static Level l = INFO; return l;}
  inline void setLevel(Level l) { level() = l;}

  inline const char* tag(Level l) {
    switch(l) {
      case ERR: return "E"; case WARN: return "W"; case INFO: return "I"; default: return "D";
    }
     
  }
}

#define LOG_AT(lvl, fmt, ...) do { if ((lvl) <= logx::level()) Serial.printf("[%8lu][%s] " fmt "\n", millis(), logx::tag(lvl), ##__VA_ARGS__); } while (0)

#define LOG_ERR(...)   LOG_AT(logx::ERR,   __VA_ARGS__)
#define LOG_WARN(...)  LOG_AT(logx::WARN,  __VA_ARGS__)
#define LOG_INFO(...)  LOG_AT(logx::INFO,  __VA_ARGS__)
#define LOG_DEBUG(...) LOG_AT(logx::DEBUG, __VA_ARGS__)
