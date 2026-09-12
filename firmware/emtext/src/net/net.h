#pragma once
#include <Arduino.h>
#include "../proto/proto.h"

// The single network owner. Runs a FreeRTOS task pinned to core 0 so the blocking
// WiFi/TLS/WebSocket work never stalls the core-1 loop (audio/display/controls).
// Received frames cross to core 1 through a queue drained in loop(); the task
// never touches the display.
namespace net {
  enum class State { Off, Searching, Connected, SocketConnecting, Ready, Degraded, Halted };

  void        begin();     // spawn the core-0 task
  void        loop();      // core-1: drain the RX queue, dispatch onFrame
  State       state();     // atomic-ish read, safe from core 1
  const char* stateName();
  void        onFrame(void (*cb)(const proto::Frame&));   // dispatched on core 1
  void        sendAudio(const int16_t* pcm, size_t n);    // core 1 -> TX queue -> core 0
  void        reconnect();                                // drop STA + reconnect (after a config change)
  void        setPortal(bool on);                         // enable/disable the setup AP (from core 1)
  bool        portalOn();                                 // is the setup AP currently up?
}
