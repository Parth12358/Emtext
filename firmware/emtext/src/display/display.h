#pragma once
#include <Arduino.h>

// Glance-first UI. Design rules live in firmware UI/UX notes; the load-bearing
// ones encoded here: tone is an EDGE BAR (never text colour, never a word),
// neutral shows no bar, mismatch is amber + dashed (hue is never the only cue),
// low confidence dims the read, and PAUSED is an unmistakable privacy screen.
namespace display {
  enum class State { Dark, Glance, History, Status };

  // Settings rows, in scroll order. Status doubles as the Settings page; these are the
  // selectable rows. Brightness is handled locally (a display property); the rest fire
  // onSetting() so emtext.ino performs the cross-module effect (portal, power).
  enum class Setting { Wifi, Brightness, Power, COUNT };

  void  begin();
  void  loop();                       // handles the ~8s glance timeout
  void  setState(State s);
  State state();
  void  setRotation(int rot);

  // Settings navigation (called from emtext.ino only while state()==Status).
  void  settingsScroll();             // BtnB click: cursor -> next row (no-op while locked)
  void  settingsSelect();             // BtnA click: activate the highlighted row
  bool  settingsLocked();             // true while the setup AP is up: nav is frozen
  void  onSetting(void (*cb)(Setting s));  // cross-module action requested from a row

  // Glance data. lowConfidence dims the read (uncertainty must be visible).
  void  setGlance(const String& read, const String& tone,
                  const String& transcript, bool lowConfidence = false);
  void  setConnection(const String& label);   // "ready" -> green dot, else amber
  void  setProcessing(bool on);                // heard, still thinking
  void  setButtons(bool a, bool b, bool pwr);  // button-held -> light the press indicators
  void  setPing(int ms);                       // median RTT (ms) for the status bar; -1 = unknown
  void  setActivity(bool listening, bool sending, bool receiving, bool clip);  // dev status glyphs
  void  setClip(const String& msg);            // transient clip-save confirmation ("saved" / …)
  void  setClipRec(bool on);                   // full-screen recording overlay while holding
  void  setSaving(bool on);                    // "saving..." overlay while finalizing the clip
  void  setPaused(bool on);                    // privacy switch: mic not listening
  void  setPortal(bool on, const String& ssid, const String& pass, const String& ip);  // setup-AP status
}
