#pragma once
#include <Arduino.h>

// Glance-first UI. Design rules live in firmware UI/UX notes; the load-bearing
// ones encoded here: tone is an EDGE BAR (never text colour, never a word),
// neutral shows no bar, mismatch is amber + dashed (hue is never the only cue),
// low confidence dims the read, and PAUSED is an unmistakable privacy screen.
namespace display {
  enum class State { Dark, Glance, History, Status };

  void  begin();
  void  loop();                       // handles the ~8s glance timeout
  void  setState(State s);
  State state();
  void  setRotation(int rot);

  // Glance data. lowConfidence dims the read (uncertainty must be visible).
  void  setGlance(const String& read, const String& tone,
                  const String& transcript, bool lowConfidence = false);
  void  setConnection(const String& label);   // "ready" -> green dot, else amber
  void  setProcessing(bool on);                // heard, still thinking
  void  setPaused(bool on);                    // privacy switch: mic not listening
}
