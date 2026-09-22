# emtext firmware — architecture

ESP32-S3 pendant (**M5StickS3 / K150**). The device is a mic + a display: it streams
16 kHz PCM to the emtext server over a secure WebSocket and renders the server's `read`
frames. All transcription and interpretation stay on the server. The wire protocol is a
fixed contract — see [`docs/design/firmware-requirements.md`](../design/firmware-requirements.md)
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
| ~~7 audio cues~~ | ~~`cues`~~ | ❌ removed — no on-device audio output (visual-only) |
| 8 power management | `power` | ✅ built — validate on hardware |
| 9 acceptance + compliance | — | ⬜ |

Beyond the stages, also built since: the **status-bar overlays** (connectivity glyph — shape, not
colour — + live ping + battery %/charging bolt), the **Undertale-heart glance** in both orientations
with vibrant soul colours, orientation-debounce, History → "More Info" (read + transcript) with
idle auto-dim, **hold-to-record clips** (recording/​saving/​status overlays, §9.7 in `docs/design/firmware-ui-ux.md`), and
**Stage 8 power management** (`power`) — see below.

**What's remaining:**
- **Stage 8 — hardware validation**: the `power` module is code-complete — staged cycle
  **Awake → Idle → grace → low-power standby (Wi-Fi kept, mic/ping off, button-only wake) → full power
  off** after `standbyMaxS`, plus visible boot stages, one-time low-battery warning, and CPU/Wi-Fi
  current-draw config. **No ESP deep sleep** (pointless for a button-woken, no-BLE device). Still needs
  on-hardware tuning of `MOTION_G`/`LIFT_G`, confirming the socket survives standby on server
  keepalives, and the **2.5 h runtime** measurement. **No firmware power-off gesture** — PWR is the
  M5PM1 hardware off (uninterceptable).
- **Stage 9 — acceptance + compliance**: outage recovery visible on screen, "mic live" affordance
  while Dark, runtime verification (or press-to-listen fallback).
- **TLS hardening** (deployment blocker): the active Links2004 backend runs unvalidated `beginSSL`
  (`L2004_INSECURE`) — move to validated `wss` (see Deviations).
- **Speaker on the glance** (deferred, next candidate): the server now sends an optional `speaker`
  label (`user`/`other`); the firmware ignores it — surfacing it on the glance is not yet built.
- Smaller UI gaps + nice-to-haves (landscape History/Settings relayout, clock,
  button-press shadows, Quick Setup at boot): tracked in `docs/design/firmware-ui-ux.md` §8/§9.

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
        ┌───────────────┬───────────────┼──────────────┐
     audio             net            display          power
        │           ┌───┴───┐            │               │
        │        transport  │            │               │
        └───────────┴───────┴──── proto ─┴───────────────┘
                     config  •  logx      (foundation — no deps upward)
```

Arrows point down only. No cycles. Every box is a header/`.cpp` pair (except header-only `logx`).

## Sections

**Foundation** (no hardware, usable everywhere):

- **`config`** ✅ — owns the `Config` struct (**every tunable**: `nets[3]` + `netCount`, `serverHost`,
  `serverPath`, `token`, `chunkMs`, `energyFloor`, `idleTimeoutS` (silence+stationary before the sleep
  grace), `graceS` (grace countdown), `standbyMaxS` (max time in low-power standby → full off; keep
  < server 900 s), `lowBattPct`,
  `logLevel`, `devMode` (`set dev 1` keeps the screen awake **and disables auto-sleep** — lit from
  boot), and the setup-AP credentials `apSsid` / `apPass`) + NVS
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
  `state()`, `stateName()`, `onFrame(cb)`, `sendAudio(pcm,n)`, `saveClip(from,to)` (the clip
  `save` frame), `pingMs()` (median keepalive RTT, -1 until measured), `lastTxMs()` / `lastRxMs()`
  (dev activity glyphs), `reconnect()` (after a config change), `setPortal(on)` / `portalOn()`
  (the setup AP radio).
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
- **`controls`** ✅ — buttons + IMU → **semantic events**. Exposes `begin()`, `loop()`, the
  held-state queries `heldA()` / `heldB()` / `heldPwr()` (drive the press indicators and the
  clip release), the motion signal `lastMotionMs()` (accel deviation from 1 g rest — feeds the power
  idle gate), and callbacks: `onWake` (BtnA short), `onHoldA` (BtnA long — hold-to-record /
  Settings back), `onPause` (BtnB short), `onStatus` (BtnB long), `onPowerOff` (BtnPWR long — declared
  but unused on the StickS3: PWR is a hardware off), `onOrient(rot)` (IMU auto-rotate), `onLift`
  (lift-to-wake — a coarse jerk detector, `LIFT_G`/`MOTION_G` tuned on hardware). Deps:
  `M5.BtnA/BtnB/BtnPWR`, `M5.Imu`.
- **`display`** ✅ — owns the screen; four states **Dark / Glance / History / Status** plus the
  **PAUSED** / recording / saving overlays and the **Stage 8 power screens**. Exposes `begin()`,
  `loop()`, `setState()`, `state()`,
  `setRotation()`, `setGlance(read, tone, transcript, lowConfidence=false)`, `setConnection()`,
  `setProcessing()`, `setPaused()`; the Settings page — `settingsScroll()`, `settingsSelect()`,
  `settingsLocked()`, `onSetting(cb)`; the chrome — `setButtons(a,b,pwr)`, `setPing(ms)`,
  `setActivity(...)`; clips — `setClipRec(on)`, `setSaving(on)`, `setClip(msg)`; power screens —
  `setBoot(stage, failed)` (network→server→ready progress; shapes not colour), `setGrace(on)` +
  `setGraceSecs(s)` (pre-sleep countdown), `setLowBatt()`
  (transient low-battery warning); and
  `setPortal(on, ssid, pass, ip)` for the setup-AP credentials panel. Now fed **real `read`
  frames** (Stage 6) — `setGlance` renders them and records a 5-deep history ring (History shows
  the 3 most recent). See **Glance UX** below. Deps: `M5.Display`, config.
- **`cues`** ❌ **removed** — the device has no speaker output, so there is no audio-cue module.
  The emotional signal is conveyed **visually only** (the colour of the glance heart). No tone cues,
  no alert/boot/low-battery tones, no audible idle warning, no mute.
- **`power`** ✅ — **the single `M5.Power` owner.** Pure policy + PMIC/Wi-Fi-power calls; it never
  touches the screen, mic or socket — those go through callbacks wired in `.ino`. **Staged cycle**
  (silence + stationary; **no ESP deep sleep** — see Deviations): **Awake → Idle** (display auto-dim,
  still listening) **→ grace countdown** (`config.graceS`, any button/motion cancels) **→ low-power
  standby** **→ full power off** after `config.standbyMaxS`. **Low-power standby** = mic off + device
  ping off + screen off + Wi-Fi kept connected (deeper modem sleep); the socket stays alive on the
  server's own keepalives, so it recovers **instantly on a BUTTON press** (`onStandby(false)` — no
  reconnect). **Motion is ignored in standby** (button-only wake); it only keeps the device awake /
  delays entering standby. **Idle inputs:** `noteVoiced(audio::voiced())` + `noteActivity()`
  (buttons) + motion from `controls::lastMotionMs()`. **No firmware power-off gesture:** PWR is the
  M5PM1 hardware off (uninterceptable); the only clean shutdown is this idle path (`net::shutdown()`
  before `M5.Power.powerOff()`). **Low battery:** one-time warning below `config.lowBattPct`
  (hysteresis; cleared on charge). **Current draw:** `begin()` sets `setCpuFrequencyMhz(160)` +
  Wi-Fi modem sleep. **Disabled in dev mode.** Exposes `begin()`, `loop()`, `noteActivity()`,
  `noteVoiced(on)`, `standby()`, and callbacks
  `onGrace(on)`/`onGraceTick(secs)`/`onStandby(on)`/`onSleep()`/`onLowBatt()`.
  Deps: `M5.Power`, `WiFi` (modem sleep), `controls` (motion), config.

**Orchestrator:**

- **`emtext.ino`** ✅ — holds the module instances, wires callbacks between them, pumps each
  `loop()`. Also owns the mic-gating logic (below).

## Glance UX (implemented per the UI/UX notes)

The governing rule: **return the user's attention to the person, don't capture it.** So:

- **Tone is the Undertale "DETERMINATION" heart** (`drawHeart`, a 16×16 pixel sprite) rendered as
  the hero at screen centre, filled in the tone's **Undertale soul colour**: positive = red,
  negative = blue, sarcastic = purple, mixed = yellow, neutral = dim gray. The read sits beside/below
  it in a **bold white font** (`FreeSansBold9pt7b`), **capped at 5 words** and word-wrapped so all 5
  always fit (`wrapN`, up to 5 lines).
- **One shape for every tone — the emotion is the colour** (pure colour-only, like Undertale's
  souls, by design). Tradeoff, accepted intentionally: colour is the *sole* cue, so it is not
  colour-blind / low-backlight redundant the way the old curve mark was.
- **Neutral is a dim gray heart** and shows **no History tick** — quiet is the calm default.
- **Low confidence dims the read** (`setGlance(..., true)`), ready for a confidence source.
- **PAUSED** is a dim, unmistakable privacy screen (`||` glyph). (There is no mute glyph —
  audio output is removed, and the old `display::setMuted` hook has been deleted.)
- Instant redraws only — no scrolling, no animation. Connection dot cornered; processing = `...`.

## Controls & mic gating (in emtext.ino)

- BtnA short → wake / cycle glance↔history. BtnA long → **hold-to-record a clip**: the hold marks
  the last utterance id seen, release waits a short grace for the tail utterance, then sends
  `{"type":"save","from":P+1,"to":<last id>}` (`net::saveClip`); the server stores it
  (`server/clips.py`, `/api/clips*`) and answers `saved`. BtnB short → **pause** (privacy screen).
  BtnB long → status. **BtnPWR = hardware off** (the M5PM1 PMIC power key; a tap powers the device off
  and firmware can't intercept it — there is no firmware power-off gesture; deliberate shutdown is the
  idle auto-sleep path, which closes the socket cleanly). Lift → wake. (No mute — audio output is removed.)
- **In Settings the same buttons re-bind** (`emtext.ino` checks `display::state()`): BtnA click =
  select the highlighted row, BtnB click = scroll to the next row, BtnA hold or BtnB hold = back
  to Glance. All three are dead while the setup portal is up (`settingsLocked()`).
- **The mic runs only when not paused.** `emtext.ino` holds `g_paused` and calls
  `audio::setPaused(g_paused)` — the privacy pause is the only thing that cuts the mic.

## Wiring (target — lives only in emtext.ino)

```cpp
// current (Stages 2–3)
controls::onOrient(display::setRotation);      // IMU auto-rotate
controls::onLift([]{ /* wake to glance */ });  // lift-to-wake
controls::onPause -> display::setPaused + audio::setPaused(paused)   // privacy pause cuts the mic

// Stage 5 repoints audio at the network, audio itself unchanged:
audio::onChunk([](const int16_t* p, size_t n){ net::sendAudio(p, n); });   // audio → net
net::onFrame ([](const proto::Frame& f){ display::setGlance(...); });       // read → glance (visual only)
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
   ├─ proto/  transport/  net/  portal/  power/     (cues removed — no audio output)
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
  `utterance` (`id`, `transcript`, optional `speaker`); `read` (`id`, `tone`, `read`, optional
  `voice{emotion,valence,arousal}`, optional `speaker{label,score}`); `saved` (`id`, `ok`, optional
  `error`/`clip`); `ping` (`t`); `pong` (`t`).
  **`tone` ∈ `positive | negative | neutral | sarcastic | mixed`.** A `user` (own-voice) utterance
  gets an `utterance` but **no `read`**. Ignore unknown `type`s.
- Device → server: `{"type":"ping","t":...}` → `pong`; `{"type":"save","from":F,"to":T}` — clip an
  inclusive utterance-id range (hold-to-record; `from==to` for one utterance) → `saved`.
- Timing: server pings after 30 s idle and **force-closes after 900 s with no audio** (only
  inbound audio resets that timer). Cloudflare culls proxied sockets after ~100 s of silence.
  Close codes: `1008` auth, `1000` idle.
- Opt-in only: the server can also stream `vad` / `vad_close` telemetry frames when a client
  connects with `?vad=1` on the `/stream` URL. The device never requests them, so they never
  arrive; if they ever did, they fall under "ignore unknown `type`s".
- Server VAD the device must satisfy (not implement): speech RMS ≥ 500, 650 ms trailing silence
  ends an utterance, 350 ms minimum. **The device energy floor sits below 500** so real speech
  clears the server threshold. (Device RMS is DC-corrected AC RMS, on the same scale.)

## Deviations from the original plan

- **Subfolder per module** (`src/audio/audio.cpp`), not a flat `src/`.
- **The 2–3 s outage buffer moved from `audio` to `net`.** "Drop oldest when the network is
  out" is a network-state decision; `audio` doesn't know about the network. `audio` just
  captures, gates, and emits; `net` owns the send queue that absorbs an outage (Stage 5).
- **Audio output removed** — the pendant has no speaker, so there are no tone cues and no mute;
  the emotional signal is visual only. Privacy **pause** is the only thing that gates the mic.
- **No firmware power-off / PWR is a hardware off.** On the StickS3, PWR is the M5PM1 PMIC's power
  key: a tap does a hardware power-off that firmware cannot intercept (M5Unified only ever reports a
  "click", never a hold) or retime (the M5PM1 driver exposes no key-behaviour register). So the
  requirement's *deliberate two-step power-off* is not implemented as a button gesture; the deliberate
  clean-shutdown path is the **idle auto-sleep** (grace → standby → full power off, `net::shutdown()`
  first). PWR remains the physical off switch (uninterceptable, so not a clean close).
- **No ESP deep sleep.** Deep sleep buys nothing here (no BLE, no timer/RTC wake, no RTC retention;
  wake would be the PMIC button anyway) — it still restarts the sketch (no fast resume) and draws
  more than a full power-off. The recoverable low-power tier is a **connected standby** (Wi-Fi kept
  associated → instant resume, which deep sleep can't do); the terminal tier is a full power off.
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
  gate holds ~500 ms, pause stops the mic.
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
- **6 — glance rendering (real reads)** ✅ `read` → `display::setGlance` on the glance (the
  tone-coloured heart via `drawHeart`, the read in bold white text, `...` processing indicator —
  the transcript is **not** on the glance, it is the dim second line of each More Info/History
  entry), a real 5-deep history ring of which 3 entries are rendered, timeout refresh while
  conversing. Content updates without auto-waking (lift/press to view). **Verified on hardware.**
- **7 — audio cues** ❌ **removed** — the device has no speaker output; the emotional signal is
  conveyed visually only (the colour of the glance heart). No `cues` module, no tones, no mute.
- **8 — power management** ✅ built (validate on hardware) `power`: visible boot stages
  (network→server→ready, **on-screen only — no failure tones**), staged cycle
  **Awake → Idle → grace** (**on-screen countdown, any button/motion cancels, no audible warning**)
  **→ low-power standby** (mic off, ping off, Wi-Fi kept connected, **button-only wake**, instant, no
  reconnect) **→ full power off** after `standbyMaxS` (clean WS close first). One-time low-battery
  warning, CPU-clock + Wi-Fi-modem-sleep current-draw config. **No ESP deep sleep; no firmware
  power-off gesture** (PWR = M5PM1 hardware off). Verify on hardware: idle grace + cancel; standby
  holds the socket on server keepalives; button wakes standby (no reconnect); long-standby → full off;
  low-battery; **≥ 2.5 h runtime**; tune `MOTION_G`/`LIFT_G`.
- **9 — acceptance + compliance** ⬜ outage recovery visible on screen, capture externally
  visible, no third-party retention, 2.5 h runtime (else press-to-listen default).

## Releases and the public mirror

The server repository is private; the firmware is public. GitHub visibility is per repo,
so the firmware lives in a **read-only mirror**, `Parth12358/emtext-firmware`, that two
workflows in this repo feed. Nobody edits the mirror; everything lands here and flows out.

- **`firmware ci`** (`.github/workflows/firmware-ci.yml`) compiles the sketch with
  `arduino-cli` on every PR or push touching `firmware/`, using the shared toolchain action
  (`.github/actions/arduino-toolchain`: M5Stack core + `M5Unified`, `ArduinoJson`,
  `WebSockets`, `ArduinoHttpClient`, cached). `secrets.h` is absent in CI on purpose; the
  sketch compiles with its defaults.
- **`firmware mirror`** (`firmware-mirror.yml`) runs on pushes to `main` that touch the
  firmware or its three docs. `.github/scripts/stage_firmware.sh` builds the public tree
  (`emtext/` sketch, `docs/` flattened with links rewritten, `README.md` from
  `firmware/mirror/README.md`, `LICENSE` if present, a `ci.yml`, and `SOURCE.txt` naming the
  upstream commit) and force-pushes it as ONE orphan commit. The mirror therefore has no
  history and never contains anything from `server/`.
- **`firmware release`** (`firmware-release.yml`) runs on a tag `fw-vMAJOR.MINOR.PATCH`
  (or by hand with a version input). It compiles with `--export-binaries`, packages
  `emtext-firmware-<tag>.merged.bin` (flash at `0x0`), `.app.bin`, bootloader and partition
  images, `emtext-firmware-<tag>-source.zip` (the same staged tree), `FLASHING.md` and
  `SHA256SUMS.txt`, refreshes the mirror so its `main` is exactly this source, and creates
  the GitHub Release **on the public repo** with release notes from the firmware commits
  since the previous `fw-v*` tag.

Both publishing workflows need the `FIRMWARE_MIRROR_TOKEN` secret: a fine-grained PAT
scoped to the public repo with **Contents: read/write** and **Workflows: read/write** (the
mirror pushes a `.github/workflows/ci.yml`, and GitHub rejects that from a token without the
Workflows permission). The board is
`m5stack:esp32:m5stack_sticks3`; if your IDE build relies on a menu option (partition
scheme, PSRAM mode, USB CDC), pass the full fqbn to the workflow's `fqbn` input and, once it
is known to matter, make it the default in the toolchain action.

To cut a release:

```
git tag fw-v0.1.0 && git push origin fw-v0.1.0
```

## Out of scope for v1

On-device segmentation, OTA, certificate pinning (a CA **bundle** is used instead), network
roaming beyond the fallback list, on-device ML. (§9 of the requirements.)
