#include "display.h"
#include "../logx.h"
#include "../config/config.h"      // settings page shows the wifi ssid + AP credentials
#include <M5Unified.h>
#include <math.h>

namespace {
  display::State st = display::State::Dark;
  uint32_t       glanceSince = 0;
  const uint32_t GLANCE_MS = 8000;

  const int      BAR_W = 7;      // tone edge-bar width, px
  const uint8_t  BRIGHT_PAUSED = 60;
  const int      STRIP = 12;     // thickness of the top status bar AND the button-indicator bars

  // User-adjustable screen brightness (the Settings "bright" row cycles these). Replaces
  // the old fixed BRIGHT_GLANCE constant so all lit screens follow the chosen preset.
  const uint8_t  BRIGHT_PRESETS[4] = { 40, 90, 150, 220 };
  int            brightIdx = 2;                       // -> 150
  uint8_t        uiBright  = BRIGHT_PRESETS[2];

  int            selCursor = 0;                       // highlighted Settings row
  void (*cbSetting)(display::Setting) = nullptr;      // cross-module action from a row

  String gRead = "";            // no text until a real read arrives
  String gTone = "positive";    // default heart is RED (positive) on start
  String gTranscript = "";
  bool   gLowConf = false;
  bool   connReady = false;
  int    connState = 0;                           // 0 = searching/off, 1 = ready, 2 = degraded
  bool   processing = false;
  bool   paused = false;
  bool   portalOn = false;
  bool   bA = false, bB = false, bPwr = false;   // button-held -> press indicators
  int    pingMs = -1;                            // median RTT (ms) from net; -1 = unknown
  bool   actListen = false, actSend = false, actRecv = false, actClip = false;   // dev activity
  String portalSsid, portalPass, portalIp;

  // history: ring of the last 5 reads, rendered most-recent-first
  struct Hist { String read; String tone; };
  Hist hist[5];
  int  histCount = 0, histNext = 0;

  // UI-chrome accent colors (color565). Tone/emotion colors are NOT here -- those are
  // the Undertale soul colors in toneBar(); these are just for dots, cursor and text.
  uint16_t cPos()  { return M5.Display.color565( 90, 170,  90); }  // green (UI accent: ready dot, cursor)
  uint16_t cMis()  { return M5.Display.color565(220, 160,  40); }  // amber (UI accent: processing, portal)
  uint16_t cDim()  { return M5.Display.color565(150, 150, 150); }
  uint16_t cFaint(){ return M5.Display.color565( 90,  90,  90); }

  struct Bar { uint16_t color; bool dashed; bool show; };
  // Undertale soul colors carry the tone on the heart mark (pure color-only, per design).
  // `show` also gates the History tick: neutral shows no tick (absence is the signal).
  Bar toneBar(const String& t) {
    auto& d = M5.Display;
    if (t == "positive")  return { d.color565(255,   0,  40), false, true };  // red    -- Determination
    if (t == "negative")  return { d.color565(  0, 120, 255), false, true };  // blue   -- Integrity
    if (t == "sarcastic") return { d.color565(190,  30, 255), false, true };  // purple -- Perseverance
    if (t == "mixed")     return { d.color565(255, 216,   0), false, true };  // yellow -- Justice
    return { d.color565(120, 120, 120), false, false };  // neutral: dim gray (quiet), no tick
  }

  // Keep only the first `maxWords` words; append "..." if there were more.
  String capWords(const String& s, int maxWords) {
    int words = 0, i = 0;
    while (i < (int)s.length()) {
      int sp = s.indexOf(' ', i);
      if (++words >= maxWords) {
        int end = (sp < 0) ? s.length() : sp;
        return (end < (int)s.length()) ? s.substring(0, end) + "..." : s;
      }
      if (sp < 0) return s;
      i = sp + 1;
    }
    return s;
  }

  // Greedy word-wrap into up to `maxLines` lines at the current text size. No ellipsis:
  // callers cap the word count first and size the area so the words fit.
  int wrapN(const String& s, int areaW, int maxLines, String out[]) {
    auto& d = M5.Display;
    for (int k = 0; k < maxLines; k++) out[k] = "";
    int line = 0, i = 0;
    while (i < (int)s.length() && line < maxLines) {
      int sp = s.indexOf(' ', i);
      String word = (sp < 0) ? s.substring(i) : s.substring(i, sp);
      String trial = out[line].length() ? out[line] + " " + word : word;
      if (out[line].length() == 0 || d.textWidth(trial.c_str()) <= areaW) {
        out[line] = trial;
        i = (sp < 0) ? s.length() : sp + 1;
      } else {
        line++;
      }
    }
    int used = 0;
    for (int k = 0; k < maxLines; k++) if (out[k].length()) used = k + 1;
    return used;
  }

  // Emotion mark: the hero of the glance. The Undertale "DETERMINATION" soul -- a 16x16
  // pixel heart (extracted from the reference sprite; every edge is a multiple of the
  // native cell, so this is the true grid). ONE shape for every tone: the emotion is
  // carried by COLOR (Undertale soul colors), per the UX design. Each row is a 16-bit
  // mask, MSB = column 0.
  const uint16_t HEART16[16] = {
    0x300C, 0x7C3E, 0xFE7F, 0xFE7F, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF,
    0xFFFF, 0xFFFF, 0x3FFC, 0x3FFC, 0x0FF0, 0x0FF0, 0x03C0, 0x03C0,
  };
  void drawHeart(int cx, int cy, int cell, uint16_t col) {
    auto& d = M5.Display;
    int ox = cx - 8 * cell, oy = cy - 8 * cell;            // 16 cells wide/tall, centered
    for (int r = 0; r < 16; r++) {
      uint16_t bits = HEART16[r];
      for (int c = 0; c < 16; c++)
        if (bits & (0x8000 >> c))                          // MSB = column 0
          d.fillRect(ox + c * cell, oy + r * cell, cell, cell, col);
    }
  }

  void drawGlance() {
    auto& d = M5.Display;
    int W = d.width(), H = d.height();

    // emotion mark = the hero: the Undertale heart, colored by tone (neutral = dim gray).
    // Fixed ~96px sprite (16 x 6px cells) to match the Figma, positioned per orientation.
    Bar b = toneBar(gTone);
    uint16_t col = b.show ? b.color : cDim();
    const int cell = 6;

    String text = capWords(gRead, 5);               // at most 5 words -- and they must fit
    d.setFont(&fonts::FreeSansBold9pt7b);           // bold + bigger: the words are the message
    d.setTextSize(1);
    d.setTextColor(gLowConf ? cFaint() : TFT_WHITE, TFT_BLACK);   // white = bold/high-contrast
    String lines[5];
    int lh = d.fontHeight() + 2;

    if (H >= W) {
      // PORTRAIT: heart high under the top bar, read lower-centre (matches the Figma)
      drawHeart(W / 2, 69, cell, col);
      int n = wrapN(text, W - 6, 5, lines);          // near-full width, up to 5 lines
      d.setTextDatum(top_center);
      int y0 = 172 - n * lh / 2;                     // block centred in the lower area
      for (int k = 0; k < n; k++) d.drawString(lines[k].c_str(), W / 2, y0 + k * lh);
    } else {
      // LANDSCAPE: heart on the left, read on the right (matches the Figma)
      drawHeart(75, H / 2, cell, col);
      int n = wrapN(text, 100, 5, lines);            // right column, up to 5 lines
      d.setTextDatum(top_center);
      int y0 = H / 2 - n * lh / 2;
      for (int k = 0; k < n; k++) d.drawString(lines[k].c_str(), 180, y0 + k * lh);
    }

    d.setFont(&fonts::Font0);                        // restore the default font for chrome/other screens
    d.setTextSize(1);

    if (processing) {                               // heard, still thinking (static)
      d.setTextColor(cMis(), TFT_BLACK);
      d.setTextDatum(top_left);
      d.drawString("...", 14, 13);                  // clear of the top bar / left strip
    }
  }

  void drawPaused() {                               // privacy screen: unmistakable
    auto& d = M5.Display;
    d.setBrightness(BRIGHT_PAUSED);
    d.fillScreen(TFT_BLACK);
    int cx = d.width() / 2, cy = d.height() / 2;
    int bw = 8, bh = 34, gap = 10;
    uint16_t c = cDim();
    d.fillRect(cx - gap / 2 - bw, cy - bh / 2, bw, bh, c);   // pause glyph ||
    d.fillRect(cx + gap / 2,      cy - bh / 2, bw, bh, c);
    d.setTextColor(c, TFT_BLACK);
    d.setTextDatum(bottom_center);
    d.setTextSize(1);
    d.drawString("paused", cx, d.height() - 6);
  }

  // ---- persistent chrome overlays (on every lit screen; not Dark / Paused) ----
  // Top bar: connectivity + ping (dark strip) and battery (magenta). Horizontal along
  // the top in portrait, vertical down the left in landscape -- it rides the same edge.
  // Connectivity, encoded by SHAPE (not colour -- the user is colour-blind): filled disc =
  // ready, hollow ring = searching/off, ring-with-a-dot = degraded. Colour is a second cue.
  void drawConnGlyph(int cx, int cy) {
    auto& d = M5.Display;
    uint16_t c = connReady ? cPos() : cMis();
    if (connState == 1) {                       // ready: solid disc
      d.fillCircle(cx, cy, 3, c);
    } else {                                    // searching / degraded: hollow ring
      d.drawCircle(cx, cy, 3, c);
      if (connState == 2) d.fillCircle(cx, cy, 1, c);   // degraded: + a centre dot
    }
  }

  // Small lightning bolt = charging. A drawn glyph (shape, not colour -- the user is
  // colour-blind); ~5x9 px in a box at (x, y).
  void drawBolt(int x, int y) {
    auto& d = M5.Display;
    uint16_t c = d.color565(255, 216, 0);                          // yellow; the bolt shape is the cue
    d.fillTriangle(x + 4, y,     x,     y + 5, x + 3, y + 5, c);   // upper wedge
    d.fillTriangle(x + 1, y + 3, x + 4, y + 3, x,     y + 8, c);   // lower wedge
  }

  // Small horizontal battery icon with a proportional fill -- legible in the narrow
  // landscape strip where the stacked "NN%" digits were not. ~8x6 px at (x, y).
  void drawBattIcon(int x, int y, int lvl) {
    auto& d = M5.Display;
    uint16_t c = cDim();
    d.drawRect(x, y, 7, 6, c);                       // body outline
    d.fillRect(x + 7, y + 2, 1, 2, c);              // + terminal nub
    int fw = ((7 - 2) * lvl) / 100;                  // inner fill width (0..5)
    if (fw > 0) d.fillRect(x + 1, y + 1, fw, 4, c);
  }

  // Draw a short string as a vertical stack of characters -- for the narrow landscape
  // strip, where horizontal text won't fit. ~10px per character.
  void drawVText(int cx, int y0, const String& s, uint16_t fg, uint16_t bg) {
    auto& d = M5.Display;
    d.setTextDatum(top_center); d.setTextSize(1); d.setTextColor(fg, bg);
    for (int i = 0; i < (int)s.length(); i++) { char c[2] = { s[i], 0 }; d.drawString(c, cx, y0 + i * 10); }
  }

  // Tiny dev activity glyphs -- distinct SHAPES (colour-blind safe): dot = listening,
  // up-triangle = sending, down-triangle = receiving, square = clip.
  void gListen(int cx, int cy, uint16_t c) { M5.Display.fillCircle(cx, cy, 2, c); }
  void gUp(int cx, int cy, uint16_t c)     { M5.Display.fillTriangle(cx, cy - 2, cx - 2, cy + 2, cx + 2, cy + 2, c); }
  void gDown(int cx, int cy, uint16_t c)   { M5.Display.fillTriangle(cx, cy + 2, cx - 2, cy - 2, cx + 2, cy - 2, c); }
  void gClip(int cx, int cy, uint16_t c)   { M5.Display.fillRect(cx - 2, cy - 2, 4, 4, c); }

  void drawTopBar(int W, int H) {
    auto& d = M5.Display;
    uint16_t bg = d.color565(12, 22, 28);       // #0C161C  strip (connectivity + ping + battery)
    int lvl = M5.Power.getBatteryLevel();
    if (lvl < 0) lvl = 0; else if (lvl > 100) lvl = 100;
    bool charging = (M5.Power.isCharging() == m5::Power_Class::is_charging);
    int cy = STRIP / 2;
    if (H >= W) {                               // portrait: strip along the top
      d.fillRect(0, 0, W, STRIP, bg);
      drawConnGlyph(6, cy);                                   // connectivity glyph, left
      d.setTextSize(1);
      d.setTextColor(cDim(), bg);
      if (pingMs >= 0) {                                      // ping, left of centre
        d.setTextDatum(middle_left);
        d.drawString((String(pingMs) + "ms").c_str(), 14, cy);
      }
      int ax = W / 2 - 14;                                    // dev activity cluster, centre
      if (actListen) gListen(ax,      cy, TFT_WHITE);
      if (actSend)   gUp    (ax + 10, cy, TFT_WHITE);
      if (actRecv)   gDown  (ax + 20, cy, TFT_WHITE);
      if (actClip)   gClip  (ax + 30, cy, TFT_WHITE);
      String bstr = String(lvl) + "%";
      d.setTextDatum(middle_right);                           // battery %, right
      d.drawString(bstr.c_str(), W - 2, cy);
      if (charging) drawBolt(W - 2 - d.textWidth(bstr.c_str()) - 7, cy - 4);   // bolt left of it
    } else {                                    // landscape: strip down the left (text stacked)
      d.fillRect(0, 0, STRIP, H, bg);
      int gx = STRIP / 2;
      if (actListen) gListen(gx - 2, 4,  TFT_WHITE);         // dev activity, 2x2 grid at top
      if (actSend)   gUp    (gx + 2, 4,  TFT_WHITE);
      if (actRecv)   gDown  (gx - 2, 12, TFT_WHITE);
      if (actClip)   gClip  (gx + 2, 12, TFT_WHITE);
      int y = 20;
      drawBattIcon(gx - 4, y, lvl); y += 9;                  // battery as an icon (legible)
      if (charging) { drawBolt(gx - 2, y); y += 11; }        // charging bolt below it
      if (pingMs >= 0) drawVText(gx, y, String(pingMs), cDim(), bg);   // ping (number), stacked
      drawConnGlyph(gx, H - 6);                              // connectivity glyph at the bottom
    }
  }

  // Button-press indicators: rest blue, light in the button's colour while held.
  // Each sits on its button's physical edge and rotates with the device.
  void drawIndicators(int W, int H) {
    auto& d = M5.Display;
    uint16_t rest = d.color565(2, 8, 46);       // resting: darker, bluer navy
    uint16_t la = bA   ? d.color565(156, 0, 3)  : rest;   // A   #9C0003 red
    uint16_t lb = bB   ? TFT_WHITE              : rest;   // B   white
    uint16_t lp = bPwr ? d.color565(43, 165, 3) : rest;   // PWR #2BA503 green
    if (H >= W) {                               // portrait: "U" along the bottom
      d.fillRect(0, H - 35, STRIP, 35, lp);                    // PWR  left bar
      d.fillRect(W - STRIP, H - 35, STRIP, 35, lb);            // B    right bar
      d.fillRect(STRIP, H - STRIP, W - 2 * STRIP, STRIP, la);  // A    bottom bar
    } else {                                    // landscape: "]" on the right
      d.fillRect(W - STRIP, STRIP, STRIP, H - 2 * STRIP, la);  // A    right edge
      d.fillRect(W - 35, 0, 35, STRIP, lb);                    // B    top-right
      d.fillRect(W - 35, H - STRIP, 35, STRIP, lp);            // PWR  bottom-right
    }
  }

  // The Status screen doubles as a scrollable Settings page. BtnB scrolls, BtnA selects
  // (routing is in emtext.ino). While the setup AP is up the whole page is LOCKED to a
  // credentials panel -- so the device can't wander mid-configure -- and the only way out
  // is turning the portal off (BtnA) or PWR.
  void drawSettings() {
    auto& d = M5.Display;
    d.setTextDatum(top_left);
    d.setTextSize(1);

    if (portalOn) {                                   // locked credentials view
      d.setTextColor(cMis(), TFT_BLACK);
      d.drawString("SETUP PORTAL ON", 14, 14);
      d.setTextColor(TFT_WHITE, TFT_BLACK);
      d.drawString(("join: " + portalSsid).c_str(), 14, 30);
      d.drawString(("pass: " + portalPass).c_str(), 14, 46);
      d.drawString(("at:   " + portalIp).c_str(),   14, 62);
      d.setTextColor(cFaint(), TFT_BLACK);
      d.drawString("[A] turn off", 14, 88);
      d.drawString("nav locked in setup", 14, 104);
      return;
    }

    d.setTextColor(TFT_WHITE, TFT_BLACK);
    d.drawString("settings", 14, 14);
    d.setTextColor(cFaint(), TFT_BLACK);
    d.setTextDatum(top_right);
    d.drawString("[B]next [A]sel", d.width() - 4, 14);
    d.setTextDatum(top_left);

    const char* labels[(int)display::Setting::COUNT] = { "wifi", "bright", "power" };
    for (int i = 0; i < (int)display::Setting::COUNT; i++) {
      int  y   = 30 + i * 16;
      bool sel = (i == selCursor);
      d.setTextColor(sel ? cPos() : cFaint(), TFT_BLACK);
      d.drawString(sel ? ">" : " ", 14, y);           // cursor
      String val;
      switch ((display::Setting)i) {
        case display::Setting::Wifi: {                // show the joined ssid, truncated
          String s = config::get().nets[0].ssid;
          val = s.length() ? (s.length() > 8 ? s.substring(0, 8) : s) : "(unset)";
          break;
        }
        case display::Setting::Brightness: val = String(brightIdx + 1) + "/4"; break;
        case display::Setting::Power:      val = String(M5.Power.getBatteryLevel()) + "%"; break;
        default: break;
      }
      d.setTextColor(sel ? TFT_WHITE : cDim(), TFT_BLACK);
      d.drawString((String(labels[i]) + "  " + val).c_str(), 24, y);
    }

    // aside: live status, deliberately faint and out of the way (never the hero)
    d.setTextColor(cFaint(), TFT_BLACK);
    d.setTextDatum(bottom_left);
    String foot = String(connReady ? "ready" : "search") + " | up " +
                  String(millis() / 1000) + "s | " +
                  String(M5.Power.getBatteryLevel()) + "%";
    d.drawString(foot.c_str(), 14, d.height() - 16);    // clear of the bottom indicator bar
  }

  void draw() {
    auto& d = M5.Display;
    if (paused) { drawPaused(); return; }           // privacy takes precedence
    d.fillScreen(TFT_BLACK);
    if (st == display::State::Dark) { d.setBrightness(0); return; }
    d.setBrightness(uiBright);

    switch (st) {
      case display::State::Glance:
        drawGlance();
        break;

      case display::State::History:
        d.setTextDatum(top_left);
        d.setTextSize(1);
        d.setTextColor(cDim(), TFT_BLACK);
        d.drawString("history", 14, 14);            // below the top bar
        if (histCount == 0) { d.drawString("(nothing yet)", 14, 32); break; }
        for (int i = 0; i < histCount; i++) {       // most-recent first
          int idx = (histNext - 1 - i + 10) % 5;
          int y = 32 + i * 16;
          Bar tb = toneBar(hist[idx].tone);
          if (tb.show) d.fillRect(14, y + 2, 4, 8, tb.color);
          d.setTextColor(cDim(), TFT_BLACK);
          String label = (i == 0) ? "now  " : (String(i) + " ago ");
          d.drawString((label + hist[idx].read).c_str(), 22, y);
        }
        break;

      case display::State::Status:      // Status doubles as the Settings page
        drawSettings();
        break;

      default: break;
    }

    // persistent chrome -- drawn last so it overlays the screen content
    drawTopBar(d.width(), d.height());
    drawIndicators(d.width(), d.height());
  }
}

void display::begin() { st = State::Dark; M5.Display.setBrightness(0); draw(); }
display::State display::state() { return st; }

void display::setState(State s) {
  st = s;
  if (s == State::Glance) glanceSince = millis();
  LOG_INFO("display -> %d", (int)s);
  draw();
}

void display::loop() {
  if (!paused && st == State::Glance && millis() - glanceSince >= GLANCE_MS)
    setState(State::Dark);

  // Keep the top bar live (battery %, charging bolt, ping) without a full redraw/flicker.
  static uint32_t lastBar = 0;
  if (!paused && st != State::Dark && millis() - lastBar >= 5000) {
    lastBar = millis();
    drawTopBar(M5.Display.width(), M5.Display.height());
  }
}

void display::setRotation(int rot) {
  M5.Display.setRotation(rot);
  LOG_DEBUG("rotation -> %d", rot);
  draw();
}

void display::setGlance(const String& read, const String& tone,
                        const String& transcript, bool lowConfidence) {
  gRead = read; gTone = tone; gTranscript = transcript; gLowConf = lowConfidence;
  hist[histNext].read = read;                    // record into history
  hist[histNext].tone = tone;
  histNext = (histNext + 1) % 5;
  if (histCount < 5) histCount++;
  if (st == State::Glance) { glanceSince = millis(); draw(); }   // refresh timeout + redraw
}

void display::setConnection(const String& label) {
  connReady = (label == "ready");
  connState = (label == "ready") ? 1 : (label == "degraded") ? 2 : 0;
  if (!paused && st != State::Dark)
    drawTopBar(M5.Display.width(), M5.Display.height());   // repaint the bar only (no flicker)
}

void display::setProcessing(bool on) {
  processing = on;
  if (!paused && st == State::Glance) draw();
}

void display::setButtons(bool a, bool b, bool pwr) {
  if (a == bA && b == bB && pwr == bPwr) return;   // only repaint when it changes
  bA = a; bB = b; bPwr = pwr;
  // Repaint ONLY the indicator segments -- they are opaque rects on the screen edges, so
  // there is no need to fillScreen + redraw everything. A full draw() here flickers the
  // whole screen on every press/release; this does not.
  if (!paused && st != State::Dark)
    drawIndicators(M5.Display.width(), M5.Display.height());
}

void display::setPing(int ms) {
  if (ms == pingMs) return;                        // updates ~every 15s; only repaint on change
  pingMs = ms;
  if (!paused && st != State::Dark)
    drawTopBar(M5.Display.width(), M5.Display.height());   // repaint the bar only (no flicker)
}

void display::setActivity(bool listening, bool sending, bool receiving, bool clip) {
  if (listening == actListen && sending == actSend && receiving == actRecv && clip == actClip) return;
  actListen = listening; actSend = sending; actRecv = receiving; actClip = clip;
  if (!paused && st != State::Dark)
    drawTopBar(M5.Display.width(), M5.Display.height());   // repaint the bar only (no flicker)
}

void display::setPortal(bool on, const String& ssid, const String& pass, const String& ip) {
  portalOn = on; portalSsid = ssid; portalPass = pass; portalIp = ip;
  if (!paused && st == State::Status) draw();
}

// ---- Settings navigation ------------------------------------------------------------
// Nav is frozen while the setup AP is up: the page can't scroll or leave, so the device
// state can't drift while someone is configuring it over the portal.
bool display::settingsLocked() { return portalOn; }

void display::settingsScroll() {
  if (portalOn) return;                               // locked: cursor pinned
  selCursor = (selCursor + 1) % (int)Setting::COUNT;
  if (!paused && st == State::Status) draw();
}

void display::settingsSelect() {
  // While locked, the only selectable action is turning the portal back off.
  if (portalOn) { if (cbSetting) cbSetting(Setting::Wifi); return; }

  Setting s = (Setting)selCursor;
  if (s == Setting::Brightness) {                     // display-local: cycle presets live
    brightIdx = (brightIdx + 1) % 4;
    uiBright  = BRIGHT_PRESETS[brightIdx];
    if (!paused && st != State::Dark) M5.Display.setBrightness(uiBright);
    draw();
  } else if (cbSetting) {
    cbSetting(s);                                     // Wifi/Power handled in emtext.ino
  }
}

void display::onSetting(void (*cb)(Setting)) { cbSetting = cb; }

void display::setPaused(bool on) {
  paused = on;
  LOG_INFO("paused -> %d", (int)on);
  if (on) draw();
  else    setState(State::Glance);    // leaving pause returns to the read (auto-dims after 8s)
}
