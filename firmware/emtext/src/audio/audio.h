#pragma once
#include <Arduino.h>

// Mic capture + energy gate. Owns the microphone; knows nothing about the
// network. Emits gated PCM chunks via onChunk() -- in Stage 3 that feeds the
// serial energy meter; in Stage 5 it is repointed at net::sendAudio().
namespace audio {
  void begin();
  void loop();                                          // pump: record -> RMS -> gate -> emit
  void onChunk(void (*cb)(const int16_t* pcm, size_t n));
  int  energy();                                        // last chunk RMS
  bool voiced();                                        // gate open (speech or within hangover)?
  void setPaused(bool on);                              // mic off while paused (privacy)
}
