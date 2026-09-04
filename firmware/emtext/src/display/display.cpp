#include "display.h"
#include "../logx.h"
#include <M5Unified.h>

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

  void drawBar(const Bar& b, int h) {
    if (!b.show) return;
    auto& d = M5.Display;
    if (!b.dashed) { d.fillRect(0, 0, BAR_W, h, b.color); return; }
    for (int y = 0; y < h; y += 12) d.fillRect(0, y, BAR_W, 7, b.color);  // dashed
  }

  // Greedy word-wrap at the current text size; fills out[] up to maxLines.
  int wrapText(const String& s, int areaW, String out[], int maxLines) {
    auto& d = M5.Display;
    int n = 0; String cur = ""; int i = 0;
    while (i < (int)s.length() && n < maxLines) {
      int sp = s.indexOf(' ', i);
      String word = (sp < 0) ? s.substring(i) : s.substring(i, sp);
      String trial = cur.length() ? cur + " " + word : word;
      if (cur.length() == 0 || d.textWidth(trial.c_str()) <= areaW) {
        cur = trial;
      } else {
        out[n++] = cur; cur = word;
      }
      i = (sp < 0) ? s.length() : sp + 1;
    }
    if (n < maxLines && cur.length()) out[n++] = cur;
    return n;
  }

  void drawRead() {
    auto& d = M5.Display;
    int x0 = BAR_W + 6;
    int areaW = d.width() - x0 - 10;               // right margin leaves room for dot
    d.setTextColor(gLowConf ? cDim() : TFT_WHITE, TFT_BLACK);
    d.setTextDatum(middle_left);

    String lines[3]; int n = 0, chosen = 1;
    for (int size = 2; size >= 1; size--) {         // shrink to fit 2 lines
      d.setTextSize(size);
      n = wrapText(gRead, areaW, lines, 3);
      chosen = size;
      if (n <= 2) break;
    }
    d.setTextSize(chosen);
    int lh = d.fontHeight();
    int shown = (n < 2) ? n : 2;
    int y = d.height() / 2 - (shown - 1) * lh / 2;
    for (int k = 0; k < shown; k++) { d.drawString(lines[k].c_str(), x0, y); y += lh; }
  }

  void drawGlance() {
    auto& d = M5.Display;
    Bar b = toneBar(gTone);
    drawBar(b, d.height());
    drawRead();

    // transcript: dim, small, bottom — a check that it heard the right sentence
    d.setTextColor(cFaint(), TFT_BLACK);
    d.setTextDatum(bottom_center);
    d.setTextSize(1);
    d.drawString(gTranscript.c_str(), d.width() / 2, d.height() - 2);

    if (processing) {                               // heard, still thinking (static)
      d.setTextColor(cMis(), TFT_BLACK);
      d.setTextDatum(top_left);
      d.drawString("...", BAR_W + 6, 2);
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

    switch (st) {
      case display::State::Glance:
        drawGlance();
        break;

      case display::State::History:
        d.setTextDatum(top_left);
        d.setTextSize(1);
        d.setTextColor(cDim(), TFT_BLACK);
        d.drawString("history", 4, 4);
        for (int i = 0; i < 5; i++) {               // "N ago", tone tick, dim text
          int y = 22 + i * 16;
          static const char* fakeTone[5] = { "neutral", "sarcastic", "negative", "positive", "neutral" };
          Bar tb = toneBar(fakeTone[i]);
          if (tb.show) d.fillRect(2, y + 2, 4, 8, tb.color);
          d.setTextColor(cDim(), TFT_BLACK);
          d.drawString((String(i + 1) + " ago  read " + String(i + 1)).c_str(), 10, y);
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
  if (st == State::Glance) draw();
}

void display::setConnection(const String& label) {
  connReady = (label == "ready");
  if (!paused && st != State::Dark) draw();
}

void display::setProcessing(bool on) {
  processing = on;
  if (!paused && st == State::Glance) draw();
}

void display::setPaused(bool on) {
  paused = on;
  LOG_INFO("paused -> %d", (int)on);
  if (on) draw();
  else    setState(State::Dark);      // leaving pause returns to the calm default
}
