#include "display.h"
#include "../logx.h"
#include <M5Unified.h>
#include <math.h>

namespace {
  display::State st = display::State::Dark;
  uint32_t       glanceSince = 0;
  const uint32_t GLANCE_MS = 8000;

  const int      BAR_W = 7;      // tone edge-bar width, px
  const uint8_t  BRIGHT_GLANCE = 130;
  const uint8_t  BRIGHT_PAUSED = 60;

  String gRead = "(read)";
  String gTone = "neutral";
  String gTranscript = "(transcript)";
  bool   gLowConf = false;
  bool   connReady = false;
  bool   processing = false;
  bool   paused = false;
  bool   muted = false;
  bool   portalOn = false;
  String portalSsid, portalIp;

  // history: ring of the last 5 reads, rendered most-recent-first
  struct Hist { String read; String tone; };
  Hist hist[5];
  int  histCount = 0, histNext = 0;

  // Desaturated palette (color565). Saturated TFT_* reads as an alarm; muted
  // tones read as observation, per the UX notes.
  uint16_t cPos()  { return M5.Display.color565( 90, 170,  90); }  // green
  uint16_t cNeg()  { return M5.Display.color565(200,  80,  70); }  // red, muted
  uint16_t cMis()  { return M5.Display.color565(220, 160,  40); }  // amber
  uint16_t cDim()  { return M5.Display.color565(150, 150, 150); }
  uint16_t cFaint(){ return M5.Display.color565( 90,  90,  90); }

  struct Bar { uint16_t color; bool dashed; bool show; };
  Bar toneBar(const String& t) {
    if (t == "positive") return { cPos(), false, true };
    if (t == "negative") return { cNeg(), false, true };
    if (t == "sarcastic" || t == "mixed") return { cMis(), true, true };  // mismatch
    return { 0, false, false };  // neutral: absence is the signal
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

  // Wrap into <=2 lines at the current text size; if text is left over, the last line
  // is truncated with an ASCII "..." (default font has no real ellipsis glyph).
  int wrap2(const String& s, int areaW, String out[2], bool* clipped) {
    auto& d = M5.Display;
    out[0] = ""; out[1] = ""; *clipped = false;
    int line = 0, i = 0;
    while (i < (int)s.length() && line < 2) {
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
    int used = out[1].length() ? 2 : (out[0].length() ? 1 : 0);
    if (i < (int)s.length()) {                      // overflow -> clip last line with "..."
      *clipped = true;
      int last = used ? used - 1 : 0;
      while (out[last].length() && d.textWidth((out[last] + "...").c_str()) > areaW)
        out[last] = out[last].substring(0, out[last].length() - 1);
      out[last] += "...";
      if (!used) used = 1;
    }
    return used;
  }

  // Emotion mark: the hero of the glance. A tone-colored curve drawn as overlapping
  // dots -- upward arc = positive, downward = negative, flat = neutral, wave = mismatch.
  void drawMark(const String& tone, int cx, int cy, int w, int amp, int rad, uint16_t col) {
    auto& d = M5.Display;
    const int N = 22;
    for (int i = 0; i <= N; i++) {
      float t = -1.0f + 2.0f * i / N;                       // -1 .. 1
      float x = cx + t * (w * 0.5f);
      float y;
      if      (tone == "positive")                y = cy + amp * (1 - t * t);   // smile
      else if (tone == "negative")                y = cy - amp * (1 - t * t);   // frown
      else if (tone == "sarcastic" || tone == "mixed") y = cy - amp * sinf(t * PI); // wave
      else                                        y = cy;                       // neutral: flat
      d.fillCircle((int)x, (int)y, rad, col);
    }
  }

  void drawGlance() {
    auto& d = M5.Display;
    int W = d.width(), H = d.height();

    // emotion mark = the hero (tone color; neutral is a calm dim flat line)
    Bar b = toneBar(gTone);
    uint16_t col = b.show ? b.color : cDim();
    drawMark(gTone, W / 2, (int)(H * 0.40f), (int)(W * 0.55f), (int)(H * 0.13f), 4, col);

    // the read is a small supporting caption -- not the headline
    String text = capWords(gRead, 6);
    d.setTextColor(gLowConf ? cFaint() : cDim(), TFT_BLACK);
    d.setTextDatum(top_center);
    d.setTextSize(1);
    String lines[2]; bool clip; int n = wrap2(text, W - 12, lines, &clip);
    int lh = d.fontHeight();
    int y = H - 4 - n * lh;
    for (int k = 0; k < n; k++) { d.drawString(lines[k].c_str(), W / 2, y); y += lh; }

    if (processing) {                               // heard, still thinking (static)
      d.setTextColor(cMis(), TFT_BLACK);
      d.setTextDatum(top_left);
      d.drawString("...", 4, 2);
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

  void drawConnDot() {
    auto& d = M5.Display;
    d.fillCircle(d.width() - 7, 7, 3, connReady ? cPos() : cMis());
  }

  void draw() {
    auto& d = M5.Display;
    if (paused) { drawPaused(); return; }           // privacy takes precedence
    d.fillScreen(TFT_BLACK);
    if (st == display::State::Dark) { d.setBrightness(0); return; }
    d.setBrightness(BRIGHT_GLANCE);
    drawConnDot();
    if (muted) {                      // muted: mute glyph, bottom-left
      int cx = 12, cy = d.height() - 10, r = 5;
      d.drawCircle(cx, cy, r, cMis());
      d.drawLine(cx - 4, cy + 4, cx + 4, cy - 4, cMis());
    }

    switch (st) {
      case display::State::Glance:
        drawGlance();
        break;

      case display::State::History:
        d.setTextDatum(top_left);
        d.setTextSize(1);
        d.setTextColor(cDim(), TFT_BLACK);
        d.drawString("history", 4, 4);
        if (histCount == 0) { d.drawString("(nothing yet)", 10, 22); break; }
        for (int i = 0; i < histCount; i++) {       // most-recent first
          int idx = (histNext - 1 - i + 10) % 5;
          int y = 22 + i * 16;
          Bar tb = toneBar(hist[idx].tone);
          if (tb.show) d.fillRect(2, y + 2, 4, 8, tb.color);
          d.setTextColor(cDim(), TFT_BLACK);
          String label = (i == 0) ? "now  " : (String(i) + " ago ");
          d.drawString((label + hist[idx].read).c_str(), 10, y);
        }
        break;

      case display::State::Status:
        d.setTextDatum(top_left);
        d.setTextSize(1);
        d.setTextColor(TFT_WHITE, TFT_BLACK);
        d.drawString("status", 4, 4);
        d.drawString(("wifi: " + String(connReady ? "ready" : "searching")).c_str(), 4, 24);
        d.drawString(("batt: " + String(M5.Power.getBatteryLevel()) + "%").c_str(), 4, 40);
        d.drawString(("up:   " + String(millis() / 1000) + "s").c_str(), 4, 56);
        if (portalOn) {
          d.setTextColor(cMis(), TFT_BLACK);
          d.drawString(("setup AP: ON " + portalSsid).c_str(), 4, 76);
          d.drawString(("join " + portalIp).c_str(), 4, 90);
        } else {
          d.setTextColor(cDim(), TFT_BLACK);
          d.drawString("setup AP: off", 4, 76);
        }
        d.setTextColor(cFaint(), TFT_BLACK);
        d.drawString("[A] toggle AP", 4, 108);
        break;

      default: break;
    }
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
  if (!paused && st != State::Dark) draw();
}

void display::setProcessing(bool on) {
  processing = on;
  if (!paused && st == State::Glance) draw();
}

void display::setMuted(bool on) {
  muted = on;
  if (!paused && st != State::Dark) draw();
}

void display::setPortal(bool on, const String& ssid, const String& ip) {
  portalOn = on; portalSsid = ssid; portalIp = ip;
  if (!paused && st == State::Status) draw();
}

void display::setPaused(bool on) {
  paused = on;
  LOG_INFO("paused -> %d", (int)on);
  if (on) draw();
  else    setState(State::Glance);    // leaving pause returns to the read (auto-dims after 8s)
}
