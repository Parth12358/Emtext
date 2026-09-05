#pragma once
#include <Arduino.h>

// Wire frames, as value types. Fixed char buffers (no String/heap) so a Frame
// can be copied across cores through a FreeRTOS queue by value.
namespace proto {
  enum class Type { Unknown, Ready, Status, Utterance, Read, Ping, Pong };
  enum class Tone { Neutral, Positive, Negative, Sarcastic, Mixed };

  struct Frame {
    Type  type = Type::Unknown;
    int   id   = 0;
    char  status[16]     = {0};   // status: listening/heard/thinking
    Tone  tone = Tone::Neutral;   // read
    char  transcript[160]= {0};   // utterance
    char  read[128]      = {0};   // read
    char  emotion[24]    = {0};   // read.voice (optional)
    float valence = -1.0f;        // -1 = absent
    float arousal = -1.0f;
    double t = 0;                 // ping/pong timestamp
  };

  const char* toneName(Tone t);            // "neutral"/"positive"/...
  Type parse(const char* json, Frame& out); // fills out, returns out.type
}
