# emtext firmware — architecture

ESP32-S3 pendant (**M5StickS3 / K150**). The device is a mic + a display: it streams
16 kHz PCM to the emtext server over a secure WebSocket and renders the server's `read`
frames. All transcription and interpretation stay on the server. The wire protocol is a
fixed contract — see [`subtext_firmware_requirements.md`](subtext_firmware_requirements.md)
and the reference client `server/static/index.html`.

Toolchain: **Arduino IDE** (best-supported by M5Stack). The sketch is
`firmware/emtext/emtext.ino` with modules under `firmware/emtext/src/` — the one subfolder
the IDE compiles recursively.

## Two rules that make it modular

1. **Dependencies flow one way (layering).** Foundation modules know nothing about the
   modules above them: `net` never includes `display.h`; `audio` never includes `net.h`.
2. **Upward communication is via callbacks wired only in `emtext.ino`.** When `net`
   receives a `read` frame it fires an event; the `.ino` is the only place that routes it to
   `display` and `cues`. This is why `.ino` is "wiring only," and why any module is testable
   without the ones above it.

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

Arrows point down only. No cycles. Every box is a header/`.cpp` pair.

## Sections

**Foundation** (no hardware, usable everywhere):

- **`config`** — owns the `Config` struct (**every tunable**) + NVS persistence + serial
  console. Exposes `load()`, `save()`, `get() -> const Config&`, `handleSerial()`.
  Deps: `Preferences`, `secrets.h`.
- **`logx`** — leveled serial logging. Exposes `LOG_ERR/WARN/INFO/DEBUG`. Deps: `Serial`.
- **`proto`** — wire types + JSON encode/decode, **pure logic**. Exposes the `Tone` enum,
  `Read`/`Status`/`Frame` structs, `parse(text)`, `encodePing()`. Deps: `ArduinoJson`.

**Network:**

- **`transport`** — raw WSS framing, abstracted for swappability. Interface
  `connect/sendText/sendBin/poll/close/onText`; backends `transport_ahc.cpp`
  (ArduinoHttpClient) and `transport_l2004.cpp` (Links2004), one selected by a build flag.
  Deps: `WiFiClientSecure` + one backend lib.
- **`net`** — **the single network owner**: WiFi + fallback list, NTP, TLS CA bundle,
  connection state machine, backoff 1→5 s, keepalive, auth handshake. Exposes `begin()`,
  `loop()`, `sendAudio(pcm,n)`, `state()`, `onFrame(cb)`. Deps: transport, proto, config, logx.

**I/O** (one hardware resource each):

- **`audio`** — mic + ring buffer (2–3 s, drop-oldest) + energy gate + ~500 ms hangover.
  Exposes `begin()`, `loop()`, `onChunk(cb)`, `energy()`, `paused(bool)`. Deps: `M5.Mic`,
  config, logx.
- **`controls`** — buttons + IMU turned into **semantic events**. Exposes `begin()`,
  `loop()`, callbacks `onWake/onMute/onPause/onStatus/onPowerOff/onLift`, `motion()`,
  `orientation()`. Deps: `M5.BtnA/BtnB`, `M5.Imu`.
- **`display`** — screen + 4-state UI (dark/glance/history/status). Exposes `begin()`,
  `loop()`, `showRead(Read)`, `setState(State)`, `setConnection(net::State)`,
  `setProcessing(bool)`. Deps: `M5.Display`, proto, config.
- **`cues`** — speaker cues + mute. Exposes `begin()`, `onRead(Read)`, `setMuted(bool)`,
  `loop()`. Deps: `M5.Speaker`, `M5.Power`, proto, config.
- **`power`** — PMIC: boot stages, idle/motion auto-off, two-step off, sleep/wake, battery.
  Exposes `begin()`, `loop()`, `noteActivity()`, `batteryLevel()`, callbacks
  `onWarnIdle/onGrace/onOff`. Deps: `M5.Power`, config.

**Orchestrator:**

- **`emtext.ino`** — holds the instances, wires the callbacks between modules, pumps each
  `loop()`.

## Wiring (lives only in emtext.ino)

```cpp
audio.onChunk([](const int16_t* p, size_t n){ net::sendAudio(p, n); });            // audio → net
net.onFrame ([](const proto::Frame& f){ display::showRead(f); cues::onRead(f); }); // net → display+cues
controls.onPause([]{ audio::paused(true); display::setState(PAUSED); });           // controls → audio+display
controls.onMute ([]{ cues::setMuted(true); });                                     // controls → cues
// power reads controls.motion() each loop for idle extension;
// net status frames → display::setConnection / display::setProcessing
```

`net` and `display` never reference each other — swap or test either alone by rewiring one
callback. `audio` emits chunks (rather than calling `net::sendAudio` directly) so Stage 3 can
point `onChunk` at a serial dump and Stage 5 repoints it at the network, with `audio`
unchanged.

## File layout (flat `src/`, recommended for this size)

```
firmware/emtext/
├─ emtext.ino          // wiring only
├─ secrets.h           // gitignored — creds for early bring-up
└─ src/
   ├─ config.h/.cpp   logx.h   proto.h/.cpp
   ├─ transport.h   transport_ahc.cpp   transport_l2004.cpp
   ├─ net.h/.cpp   audio.h/.cpp   controls.h/.cpp
   ├─ display.h/.cpp   cues.h/.cpp   power.h/.cpp
```

Conventions:

- `.ino` includes `"src/net.h"`; within `src/`, siblings include `"net.h"`.
- Header = **declarations only**; definitions live in the `.cpp`.
- Library includes (`<WiFiClientSecure.h>`, etc.) go in the `.cpp` that uses them, **not** in
  shared headers — so a dependency doesn't leak to every file that includes the header.
- `#include <M5Unified.h>` is the only M5 include needed (it pulls in M5GFX/M5Utility/M5HAL
  and exposes everything via the `M5.` object); include it first.
- Subdirectories under `src/` also compile (recursively), but need relative includes
  (`"../core/proto.h"`) — not worth it at this size.

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
- Server VAD the device must satisfy (not implement): speech RMS ≥ 500, 650 ms trailing
  silence ends an utterance, 350 ms minimum. **The device energy floor must sit below 500** so
  real speech clears the server threshold.

## Build stages

The firmware is built in small, independently verifiable stages (see the build plan). In
order: **0** board bring-up → **1** config + serial console → **2** controls + display
skeleton (fake data) → **3** mic capture + energy gate (serial sink) → **4** connectivity
state machine → **5** wire protocol end-to-end → **6** glance rendering → **7** audio cues →
**8** power management → **9** acceptance + compliance. Each stage lights up one or two boxes
bottom-up; because the boundaries are callbacks, an unfinished upper module is just an unwired
callback, never a compile break below it.

## Out of scope for v1

On-device segmentation, OTA, certificate pinning (a CA **bundle** is used instead), network
roaming beyond the fallback list, on-device ML. (§9 of the requirements.)
