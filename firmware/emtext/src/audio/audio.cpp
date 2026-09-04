#include "audio.h"
#include "../logx.h"
#include "../config/config.h"
#include <M5Unified.h>
#include <math.h>
#include <esp_heap_caps.h>

// Build option: stream per-chunk energy over serial (requirement Section 2).
// Set to 0 to silence the meter once the gate is tuned.
#define AUDIO_ENERGY_SERIAL 1

namespace {
  const int      NBUF = 4;             // ring depth; >=3 so the read chunk (lag 2)
  int16_t*       ring[NBUF] = { nullptr, nullptr, nullptr, nullptr };  // is never mid-fill
  size_t         CHUNK = 1024;         // samples per chunk (set from config in begin)
  uint8_t        wIdx = 0;             // write head
  uint8_t        primed = 0;           // records issued before the lag is satisfied

  int            lastRms = 0;
  int32_t        lastPeak = 0;
  uint32_t       lastVoiced = 0;
  bool           gateOpen = false;
  bool           paused = false;
  const uint32_t HANGOVER_MS = 500;    // keep transmitting after the last voiced chunk

  void (*cbChunk)(const int16_t*, size_t) = nullptr;

  void processChunk(const int16_t* s, size_t n) {
    // Remove the DC offset first: PDM mics carry a large constant bias, and the
    // RMS of a pure offset equals the offset -- that is why silence read ~1300.
    // Measure the AC (sound) energy about the mean instead.
    int64_t sum = 0;
    for (size_t i = 0; i < n; i++) sum += s[i];
    int32_t mean = (int32_t)(sum / (int64_t)n);

    // RMS about the mean. Accumulate in 64-bit: v^2 over a full chunk overflows 32-bit.
    uint64_t sum2 = 0; int32_t peak = 0;
    for (size_t i = 0; i < n; i++) {
      int32_t v = (int32_t)s[i] - mean;
      sum2 += (uint32_t)(v * v);
      int32_t a = (v < 0) ? -v : v;
      if (a > peak) peak = a;
    }
    lastRms  = (int)sqrtf((float)sum2 / (float)n);
    lastPeak = peak;

    // Gate + hangover. Device floor sits below the server's SPEECH_RMS (500).
    uint32_t now = millis();
    int floor = config::get().energyFloor;
    if (lastRms >= floor) lastVoiced = now;
    gateOpen = (lastVoiced != 0) && (now - lastVoiced <= HANGOVER_MS);

#if AUDIO_ENERGY_SERIAL
    int bars = lastRms / 50; if (bars > 24) bars = 24;
    char bar[25]; for (int i = 0; i < 24; i++) bar[i] = (i < bars) ? '#' : ' '; bar[24] = 0;
    bool clip = (lastPeak >= 32000);   // full-scale int16 is 32767
    Serial.printf("[mic] rms=%4d peak=%5d |%s| %s%s\n",
                  lastRms, lastPeak, bar, gateOpen ? "VOICED" : "", clip ? " CLIP!" : "");
#endif

    // Only emit while the gate is open (voiced or within the hangover window).
    if (gateOpen && cbChunk) cbChunk(s, n);
  }
}

void audio::onChunk(void (*cb)(const int16_t*, size_t)) { cbChunk = cb; }
int  audio::energy() { return lastRms; }
bool audio::voiced() { return gateOpen; }

void audio::begin() {
  CHUNK = (size_t)config::get().chunkMs * 16;   // 16 samples per ms at 16 kHz
  if (CHUNK < 256) CHUNK = 256;
  for (int i = 0; i < NBUF; i++) {
    ring[i] = (int16_t*)heap_caps_malloc(CHUNK * sizeof(int16_t), MALLOC_CAP_8BIT);
    if (!ring[i]) LOG_ERR("audio: chunk buffer %d alloc failed", i);
  }
  M5.Speaker.end();               // mic and speaker share the I2S pins
  auto mcfg = M5.Mic.config();
  mcfg.sample_rate   = 16000;
  mcfg.magnification = 4;         // default 16 is far too hot; lower gain (try 1-4)
  M5.Mic.config(mcfg);
  M5.Mic.begin();
  LOG_INFO("audio: mic %s, chunk=%u samples (%ums), floor=%d",
           M5.Mic.isEnabled() ? "on" : "OFF", (unsigned)CHUNK,
           config::get().chunkMs, config::get().energyFloor);
}

void audio::loop() {
  if (paused || !M5.Mic.isEnabled()) return;
  if (M5.Mic.record(ring[wIdx], CHUNK)) {
    if (primed >= 2) {
      size_t rIdx = (wIdx + NBUF - 2) % NBUF;   // chunk 2 records back is fully filled
      processChunk(ring[rIdx], CHUNK);
    } else {
      primed++;
    }
    wIdx = (wIdx + 1) % NBUF;
  }
}

void audio::setPaused(bool on) {
  if (on == paused) return;
  paused = on;
  if (on) {
    M5.Mic.end();                 // truly stop listening -- privacy, not just a flag
    gateOpen = false; lastRms = 0;
    LOG_INFO("audio: mic stopped (paused)");
  } else {
    M5.Speaker.end();
    M5.Mic.begin();
    primed = 0; wIdx = 0;
    LOG_INFO("audio: mic resumed");
  }
}
