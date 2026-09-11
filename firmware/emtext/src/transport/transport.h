#pragma once
#include <Arduino.h>

// WebSocket-over-TLS transport, abstracted so the backend is swappable. Used only
// from net's core-0 task, so it needs no thread-safety. connect() performs the TLS
// handshake AND the HTTP upgrade in one call. Backend selected at build time
// (transport_ahc.cpp = ArduinoHttpClient; transport_l2004.cpp = Links2004, later).
namespace transport {
  bool   connect(const char* host, uint16_t port, const char* path);  // true on HTTP 101
  bool   sendText(const char* s);                                     // one TEXT frame
  bool   sendBin(const uint8_t* data, size_t n);                      // one BINARY frame (Stage 5)
  size_t poll(char* buf, size_t cap);   // one inbound TEXT frame -> buf (0-terminated); 0 if none
  bool   connected();
  void   close();
}
