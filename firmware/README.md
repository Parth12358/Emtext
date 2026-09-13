# emtext firmware — architecture

ESP32-S3 pendant (**M5StickS3 / K150**). The device is a mic + a display: it streams
16 kHz PCM to the emtext server over a secure WebSocket and renders the server's `read`
frames. All transcription and interpretation stay on the server. The wire protocol is a
fixed contract — see [`subtext_firmware_requirements.md`](subtext_firmware_requirements.md)
and the reference client `server/static/index.html`.

Toolchain: **Arduino IDE** (best-supported by M5Stack). The sketch is
`firmware/emtext/emtext.ino`; each module lives in its own subfolder under
`firmware/emtext/src/` (the IDE compiles `src/` recursively).

## Status

| Stage | Module(s) | State |
|---|---|---|
| 0 board bring-up | `emtext.ino` | ✅ done |
| 1 config + logging | `config`, `logx` | ✅ done |
| 2 controls + display skeleton | `controls`, `display` | ✅ done |
| 3 mic + energy gate | `audio` | ✅ done |
| 4 connectivity | `net`, `transport`, `proto` | ✅ 4a–4d (validated wss) |
| 4P Wi-Fi provisioning portal | `portal` (SoftAP + captive page) | ✅ toggle + auto-pop |
| 5 wire protocol end-to-end | `net` + `proto` + `audio` | ✅ stream + outage + degraded |
| 6 glance rendering (real reads) | `display` | ✅ reads + history on glance |
| 7 audio cues | `cues` | ⬜ |
| 8 power management | `power` | ⬜ |
| 9 acceptance + compliance | — | ⬜ |

## Two rules that make it modular

1. **Dependencies flow one way (layering).** Foundation modules know nothing about the
   modules above them: `net` never includes `display.h`; `audio` never includes `net.h`.
2. **Upward communication is via callbacks wired only in `emtext.ino`.** When a module has
   something to report it fires an event; the `.ino` is the only place that routes it. This is
   why `.ino` is "wiring only," and why any module is testable without the ones above it.

Also: each `.cpp` hides its state in an anonymous `namespace {}` — only the header's
functions are public. Every tunable is read from `config::get()`, never hardcoded. One owner
per resource: only `net` touches the radio, only `audio` the mic, only `display` the screen.

## Dependency layering

```
                        emtext.ino  (orchestrator: constructs + wires callbacks)
        ┌───────────────┬───────────────┼───────────────┬──────────────┐
     audio             net            display          cues          power
        │           ┌───┴───┐            │               │              │
        │        transport  │            │               │              │
        └───────────┴───────┴──── proto ─┴───────────────┴──────────────┘
                     config  •  logx           (foundation — no deps upward)
```

Arrows point down only. No cycles. Every box is a header/`.cpp` pair (except header-only `logx`).

## Sections

**Foundation** (no hardware, usable everywhere):

- **`config`** ✅ — owns the `Config` struct (**every tunable**: `nets[3]`, `serverHost`,
  `serverPath`, `token`, `chunkMs`, `energyFloor`, `idleTimeoutS`, `logLevel`) + NVS
  persistence + a serial console (`get` / `set <key> <val>` / `save` / `clear`). Load order:
  `secrets.h` defaults → NVS overrides. Exposes `load()`, `save()`, `clear()`,
  `get() -> const Config&`, `handleSerial()`. NVS keys are ≤15 chars. Deps: `Preferences`, `secrets.h`.
- **`logx`** ✅ — header-only leveled serial logging. `LOG_ERR/WARN/INFO/DEBUG(fmt, …)`, runtime
  `logx::setLevel()` (driven by `config.logLevel`), timestamped. Lives at `src/logx.h`. Deps: `Serial`.
- **`proto`** ✅ — wire types + JSON decode, **pure logic**. `Type`/`Tone` enums and a
  value-type `Frame` (fixed char buffers so it queues across cores by value), `parse(json,
  Frame&)`, `toneName()`. Deps: `ArduinoJson` v7.

**Network** (✅ 4a–4d done — authenticated + cert-validated `wss`; degraded folds into Stage 5):

- **`net`** ✅ — **the single network owner**, on a **FreeRTOS task pinned to core 0** so the
  blocking WiFi/TLS work never stalls the core-1 loop; frames cross to core 1 through a queue
  drained in `loop()`. *Done (4a–4d):* WiFi over the fallback list (+ scan diagnostic),
  `setAutoReconnect(false)` + disconnect-before-retry, NTP (`configTzTime`), the logged state
  machine (searching→connected→socket-connecting→ready), backoff 1→5 s, WebSocket connect to
  `/stream` via `transport`, token auth handshake, auth-reject halt (close-before-`ready` →
  `Halted`, auto-resumes on token change), TLS cert validation against the GTS bundle (AHC
  backend), and **live audio streaming** — `sendAudio()` (core 1) → a **~3 s drop-oldest PSRAM
  ring** → task drains in ~64 ms frames → `sendBIN` (5.2/5.3, verified: real `utterance`/`read`
  come back, and audio spoken during a ~3 s WiFi drop survives), plus **degraded** state (5.4) —
  a 15 s client keepalive ping + a 45 s no-frame watchdog that keeps the socket and keeps
  buffering instead of tearing down.
  Exposes `begin()`, `loop()`,
  `state()`, `onFrame(cb)`, `sendAudio(pcm,n)`.
- **`transport`** ✅ — WebSocket-over-TLS, abstracted for swappability. `connect()` does the TLS
  handshake **and** the HTTP upgrade in one call; `sendText/sendBin/poll/connected/close`. Two
  backends, selected by `TRANSPORT_BACKEND_L2004` in `transport.h`; used only from the core-0
  task, so no thread-safety needed.
  - **`transport_l2004`** (Links2004 arduinoWebSockets) — **active.** `sendBIN` sends a full
    2048 B chunk as **one** frame → real-time capable. Measured: the 5.1 synthetic burst
    (~1.8 s of audio) sends in **~1.96 s**. Needs the net task at **32 KB** stack (its SSL path
    reaches mbedtls deeper than AHC; 16 KB overflowed → `LoadProhibited`). ⚠️ **On insecure TLS
    for now** (`L2004_INSECURE 1`, `beginSSL`): `beginSslWithCA` against the GTS bundle fails
    (connection/upgrade are fine — cert path only; likely heap/handshake-timeout). Hardening
    TODO: try pinning **GTS Root R4 alone** (1 cert vs 4) + a handshake timeout.
  - **`transport_ahc`** (ArduinoHttpClient) — works but **too slow for sustained streaming.** Its
    128 B TX buffer can't be raised from the sketch (its impl `.cpp` compiles with the default),
    so a chunk is split into ~17 sub-frames = ~270 tiny TLS records/s; the same 5.1 burst took
    **~6-7 s**. Kept as reference/fallback, and it **does** validate the GTS bundle (`setCACert`).
- **`portal`** ✅ — phone-based Wi-Fi provisioning (verified on hardware). **Toggled from the
  Status screen** (BtnA press): `net` brings up an **AP+STA** setup hotspot (`emtext-setup`, WPA2)
  on core 0; `portal` serves a self-contained config page (`WebServer` + `DNSServer`). **Captive
  auto-pop:** `onNotFound` returns the form (200) for *any* URL, so the OS reachability probes
  trigger the "sign in to network" sheet automatically. The form (ssid/pass/host/token) writes
  `config` + `save()` and calls `net::reconnect()`. Owns only the HTTP/HTML/form — the radio
  stays `net`'s (`net::setPortal`/`portalOn`, `config::setWifi/setHost/setToken`). To avoid a
  boot race, the WebServer/DNS start **only when the AP is up**, never at boot. AP off by default.
  Deps: `WiFi` (AP), `WebServer`, `DNSServer`, config, net.

**I/O** (one hardware resource each):

- **`audio`** ✅ — owns the mic. Captures 16 kHz mono int16 in `chunkMs`-sized chunks via a
  4-buffer ring (lag-2, because `M5.Mic.record()` is a non-blocking enqueue). Computes
  **DC-corrected AC RMS** + peak, gates at `config.energyFloor` with a **500 ms hangover**, and
  emits gated chunks via `onChunk`. `AUDIO_ENERGY_SERIAL` build flag streams a live RMS/peak
  meter. Mic `magnification` lowered to 4. Exposes `begin()`, `loop()`,
  `onChunk(cb)`, `energy()`, `voiced()`, `setPaused(bool)`. Deps: `M5.Mic`, config, logx.
- **`controls`** ✅ — buttons + IMU → **semantic events**. Exposes `begin()`, `loop()`, and
  callbacks: `onWake` (BtnA short), `onMute` (BtnA long), `onPause` (BtnB short), `onStatus`
  (BtnB long), `onPowerOff` (BtnPWR long), `onOrient(rot)` (IMU auto-rotate), `onLift`
  (lift-to-wake — currently a coarse jerk detector, tuned in Stage 8). Deps: `M5.BtnA/BtnB/BtnPWR`, `M5.Imu`.
- **`display`** ✅ — owns the screen; four states **Dark / Glance / History / Status** plus a
  **PAUSED** overlay. Exposes `begin()`, `loop()`, `setState()`, `state()`, `setRotation()`,
  `setGlance(read, tone, transcript, lowConfidence=false)`, `setConnection()`,
  `setProcessing()`, `setPaused()`, `setMuted()`. Now fed **real `read` frames** (Stage 6) —
  `setGlance` renders them and records a 5-deep history ring. See **Glance UX** below.
  Deps: `M5.Display`, config.
- **`cues`** ⬜ — speaker cues + mute. `begin()`, `onRead(Read)`, `setMuted(bool)`, `loop()`.
  Deps: `M5.Speaker`, `M5.Power`, proto, config.
- **`power`** ⬜ — PMIC: boot stages, idle/motion auto-off, two-step off, sleep/wake, battery.
  `begin()`, `loop()`, `noteActivity()`, `batteryLevel()`, callbacks `onWarnIdle/onGrace/onOff`.
  Deps: `M5.Power`, config.

**Orchestrator:**

- **`emtext.ino`** ✅ — holds the module instances, wires callbacks between them, pumps each
  `loop()`. Also owns the mic-gating logic (below).

## Glance UX (implemented per the UI/UX notes)

The governing rule: **return the user's attention to the person, don't capture it.** So:

- **Tone is a left edge bar, never a word and never the text colour** — read text stays white
  (high contrast). Word-wrapped to ≤2 lines, shrinking size 2→1 to fit.
- **Neutral shows no bar** — absence is the calm default.
- **Mismatch (`sarcastic`/`mixed`) is amber *and* dashed** — hue is never the only cue (red
  vs amber are confusable at low backlight, so the bar style differs too).
- **Desaturated `color565` palette** — muted red/green/amber read as observation, not alarm.
- **Low confidence dims the read** (`setGlance(..., true)`), ready for a confidence source.
- **PAUSED** is a dim, unmistakable privacy screen (`||` glyph); **muted** shows a corner glyph.
- Instant redraws only — no scrolling, no animation. Connection dot cornered; processing = `...`.

## Controls & mic gating (in emtext.ino)

- BtnA short → wake / cycle glance↔history. BtnA long → **mute**. BtnB short → **pause**
  (privacy screen). BtnB long → status. BtnPWR long → power-off (stub until Stage 8). Lift → wake.
- **The mic runs only when neither paused nor muted.** `emtext.ino` holds `g_paused`/`g_muted`
  and calls `audio::setPaused(g_paused || g_muted)` — so pause and mute both cut the mic, and
  un-muting never silently reopens the mic while paused (a privacy fail-safe).

## Wiring (target — lives only in emtext.ino)

```cpp
// current (Stages 2–3)
controls::onOrient(display::setRotation);      // IMU auto-rotate
controls::onLift([]{ /* wake to glance */ });  // lift-to-wake
controls::onPause / onMute -> display::setPaused/setMuted + audio::setPaused(paused||muted)

// Stage 5 repoints audio at the network, audio itself unchanged:
audio::onChunk([](const int16_t* p, size_t n){ net::sendAudio(p, n); });   // audio → net
net::onFrame ([](const proto::Frame& f){ display::setGlance(...); cues::onRead(f); });
```

`net` and `display` never reference each other. `audio` emits chunks rather than calling
`net::sendAudio` directly, so Stage 3 points `onChunk` at the serial meter and Stage 5 repoints
it at the network with `audio` unchanged.

## File layout (subfolder per module)

```
firmware/emtext/
├─ emtext.ino            // wiring only
├─ secrets.h             // gitignored — creds for early bring-up
├─ secrets.example.h
└─ src/
   ├─ logx.h                          // header-only, at src root
   ├─ config/    config.h    config.cpp
   ├─ controls/  controls.h  controls.cpp
   ├─ display/   display.h   display.cpp
   ├─ audio/     audio.h     audio.cpp
   ├─ proto/  transport/  net/  cues/  power/     (planned)
```

Conventions:

- `.ino` includes `"src/config/config.h"`, `"src/logx.h"`, etc. (sketch-relative).
- Within a module's `.cpp`, includes are relative to the file: sibling `"config.h"`; the
  shared logger `"../logx.h"`; another module `"../config/config.h"`; secrets `"../../secrets.h"`.
- Header = **declarations only**; definitions live in the `.cpp`.
- Library includes (`<WiFiClientSecure.h>`, etc.) go in the `.cpp` that uses them, **not** in
  shared headers — so a dependency doesn't leak to every file that includes the header.
- `#include <M5Unified.h>` is the only M5 include needed (it pulls in M5GFX/M5Utility/M5HAL and
  exposes everything via the `M5.` object); include it first.
- Paste large files via a `cat > file <<'EOF'` heredoc in a terminal, not the IDE editor — the
  editor has dropped characters mid-line on long pastes.

## The wire contract (fixed — mirrors `server/static/index.html`)

- Connect `wss://<host>/stream`. **First frame = TEXT auth token**, within 5 s of connect.
  Then **BINARY** frames of raw PCM **16 kHz, mono, int16 little-endian**, each **< 64 KiB**
  (~20–100 ms of audio per frame). Do not negotiate per-message-deflate.
- Server → device JSON frames: `ready`; `status` (`state` ∈ `listening`/`heard`/`thinking`);
  `utterance` (`id`, `transcript`); `read` (`id`, `tone`, `read`, optional
  `voice{emotion,valence,arousal}`); `ping` (`t`); `pong` (`t`).
  **`tone` ∈ `positive | negative | neutral | sarcastic | mixed`.** Ignore unknown `type`s.
- Device → server (optional): `{"type":"ping","t":...}` → server echoes `pong`.
- Timing: server pings after 30 s idle and **force-closes after 900 s with no audio** (only
  inbound audio resets that timer). Cloudflare culls proxied sockets after ~100 s of silence.
  Close codes: `1008` auth, `1000` idle.
- Server VAD the device must satisfy (not implement): speech RMS ≥ 500, 650 ms trailing silence
  ends an utterance, 350 ms minimum. **The device energy floor sits below 500** so real speech
  clears the server threshold. (Device RMS is DC-corrected AC RMS, on the same scale.)

## Deviations from the original plan

- **Subfolder per module** (`src/audio/audio.cpp`), not a flat `src/`.
- **The 2–3 s outage buffer moved from `audio` to `net`.** "Drop oldest when the network is
  out" is a network-state decision; `audio` doesn't know about the network. `audio` just
  captures, gates, and emits; `net` owns the send queue that absorbs an outage (Stage 5).
- **Mute cuts the mic**, not just the cues — so mute and pause both gate the mic.
- **⚠️ TLS is insecure on the active (Links2004) backend** — a known regression from 4d. Cert
  validation works under AHC but `beginSslWithCA` fails under Links2004, so it runs `beginSSL`
  (encrypted, unvalidated) for now. Fine for dev on a trusted network; **must be fixed before
  deployment** (try single-cert GTS Root R4 + handshake timeout). The `L2004_INSECURE` flag
  gates it.
- **Open decision — the low-confidence *source*.** `display` can dim an uncertain read, but the
  `read` frame carries no confidence yet. Options (server-side, the user's call): hedged
  language in the `read` text ("sounds like she's joking"), and/or an additive optional
  `confidence` field on the `read` frame. Language-hedging is favored; the display hook is ready.

## Build stages (plan of record)

Each stage is independently verifiable; modules light up bottom-up, and because the boundaries
are callbacks, an unfinished upper module is just an unwired callback, never a compile break below.

- **0 — board bring-up** ✅ `M5.begin`, detect board=StickS3, LCD text, serial heartbeat.
- **1 — config + logging** ✅ `Config` struct + NVS + serial console; leveled `logx`.
  Verify: `set`/`save`/reboot persists; `set log 3` toggles debug.
- **2 — controls + display skeleton** ✅ four states + PAUSED, buttons→events, IMU auto-rotate +
  lift-to-wake, the glance UX. Driven by fake data. Verify: buttons switch states, glance times
  out, rotate flips, pause shows the privacy screen.
- **3 — mic + energy gate** ✅ `M5.Mic` 16 kHz, DC-corrected AC RMS + peak/clip, gate at
  `energyFloor` + 500 ms hangover, `AUDIO_ENERGY_SERIAL` meter. Verify: RMS rises on speech,
  gate holds ~500 ms, mute/pause stop the mic.
- **4 — connectivity** ✅ `net` + `transport`. **4a ✅** WiFi (fallback list + scan diagnostic) +
  NTP + logged core-0 state machine; **4b ✅** TLS + `GET /health` (`200`, `body-ok=1`); **4c ✅**
  `transport` WebSocket to `/stream` + token auth → `ready` (dot green), backoff/reconnect +
  auth-reject halt; **4d ✅** `setCACert` GTS root bundle (`certs.h`) — validated `wss` handshake
  (verified on hardware, ~2 s). Degraded state folds into Stage 5.
- **4P — Wi-Fi provisioning portal** ✅ `portal`: SoftAP + captive page + form → writes `config`
  → `net` reconnects. Toggled from the Status screen (BtnA press). WPA2 AP, off by default.
  **Captive auto-pop verified** (form returned for any URL → OS "sign in" sheet). WebServer/DNS
  start only when the AP is up (avoids a boot race).
- **5 — wire protocol end-to-end** ✅ **5.1** TX path proven (synthetic burst → `status:heard`);
  **5.2** real mic streaming — `audio.onChunk → net::sendAudio` → cross-core handoff → `sendBIN`
  (speak → `utterance` + `read` come back); **5.3** ~3 s drop-oldest PSRAM outage ring (audio
  during a ~3 s WiFi drop survives); **5.4** degraded state (client ping + 45 s no-frame watchdog,
  keeps buffering). (Requires the Links2004 backend — AHC's sub-framing is too slow.)
- **6 — glance rendering (real reads)** ✅ `read` → `display::setGlance` on the glance (white text,
  tone edge bar, transcript, `...` processing indicator), a real 5-deep history ring, timeout
  refresh while conversing. Content updates without auto-waking (lift/press to view). **Verified
  on hardware.**
- **7 — audio cues** ⬜ `cues`: ≤150 ms tones, negative + mismatch only, silent for
  neutral/positive; mute already gates the mic + shows the glyph. 75% volume cap on battery.
- **8 — power management** ⬜ `power`: visible boot stages, idle+motion auto-off, two-step off,
  deep sleep + button wake, battery; tune lift-to-wake to orientation-gated.
- **9 — acceptance + compliance** ⬜ outage recovery visible on screen, capture externally
  visible, no third-party retention, 2.5 h runtime (else press-to-listen default).

## Out of scope for v1

On-device segmentation, OTA, certificate pinning (a CA **bundle** is used instead), network
roaming beyond the fallback list, on-device ML. (§9 of the requirements.)
