# Subtext Pendant — Firmware Technical Requirements

**Target hardware:** M5StickS3 (SKU K150). Worn as a pendant, screen facing
outward. The server performs all transcription and interpretation; the device
is a microphone and a display.

---

## 1. Wire protocol

- The device shall connect to the server over a secure WebSocket.
- The device shall send the shared authentication token as the first text
  frame after connecting.
- The device shall stream audio as binary frames: raw PCM, 16 kHz, mono,
  16-bit signed little-endian.
- Chunk size shall be configurable, defaulting to 64 ms.
- The device shall accept inbound JSON text frames of the following types:
  ready, status, utterance, read, ping, pong (reply to the device's keep-alive)
  and saved (reply to a clip save). Unknown types shall be ignored.
- The device shall send a closing frame before any intentional disconnect.
  **Implemented** (Stage 8): `net::shutdown()` sends a WebSocket CLOSE frame and
  parks the network task; it runs before the auto-sleep full power-off. (Low-power
  standby does NOT close the socket — it keeps it warm; a raw PWR press is a hardware
  off that firmware can't intercept, so it has no clean close — see §4.)
- The server address shall be configurable at runtime and persist across
  reboots. It shall not be fixed at build time.

## 2. Audio capture

- Audio capture shall run continuously and shall never be blocked by network
  or display activity.
- Captured audio shall be buffered such that at least 2–3 seconds of network
  outage causes no loss of audio; the oldest audio shall be discarded when the
  buffer is full.
- The device shall gate transmission on a configurable energy floor, and shall
  continue transmitting for approximately 500 ms after the last frame above
  that floor.
- The device energy floor shall be lower than the server's speech threshold.
- A build option shall report per-chunk audio energy over the serial interface.

## 3. Connectivity

- A single component shall own all network state; no other component shall
  interact with the radio or socket directly.
- The connection shall progress through defined states: searching for network,
  network connected, socket connecting, ready, degraded.
- Reconnection shall use backoff, from 1 second to a 5-second maximum.
- Authentication rejection shall halt retries and surface an error.
- The device shall send a keep-alive at least every 20 seconds.
- A degraded state, in which keep-alives are unanswered but the socket is
  live, shall continue buffering audio rather than tearing down the connection.
- The device shall support an ordered list of fallback networks.
- Connection health shall be verified on a timer and reconnection initiated
  explicitly.
- Transport security shall use a bundle of root certificates rather than a
  single pinned certificate.
- Network time shall be synchronised before the first secure handshake.
- Every network state transition shall be logged with a timestamp.

## 4. Power

- Boot shall proceed through visible stages: network connection, server
  connection, ready. Failure at each stage shall be distinguishable on screen.
  (The device has no speaker; failures are shown visually only — see §6.)
- Power off shall require a deliberate two-step action and shall close the
  connection cleanly before sleeping.
- The device shall power off automatically after a configurable idle period
  with no speech-energy audio, following an on-screen grace period that any
  button cancels. (No audible warning — the device has no speaker; see §6.)
- Idle detection shall incorporate motion: stationary and silent shall permit
  power off; in motion and silent shall extend the timeout.
- The device shall wake from its low-power state on button press. (Implemented as a
  connected standby that wakes on a button — see the note below; no ESP deep sleep.)
- Processor clock, radio sleep behaviour, backlight state and silence gating
  shall be configured to minimise average current draw.
- The device shall report battery level and shall signal low battery once.
- Target runtime: at least 2.5 hours of continuous operation with the display
  dark by default.

  **Implemented** (Stage 8, `power` module; validate on hardware): boot stages
  (network→server→ready, distinguishable on screen by shape — filled disc / hollow
  ring / X / faint ring — never colour alone); a **staged auto-sleep** —
  **Awake → Idle → grace → low-power standby → full power off** — after
  `config.idleTimeoutS` of silence + stationary, with a `config.graceS` on-screen
  grace countdown that any button or motion cancels (motion, IMU via
  `controls::lastMotionMs()`, resets/extends the timer). On grace expiry the device
  enters **low-power standby**: mic off, device keepalive ping off (`net::pausePing`),
  screen off, **Wi-Fi kept connected** on deeper modem sleep — the socket stays alive
  on the server's own keepalives, so it recovers **instantly on a button press** with
  no reconnect. After `config.standbyMaxS` in standby (kept under the server's ~15-min
  no-audio close) or on low battery, `net::shutdown()` (clean WebSocket close) then
  `M5.Power.powerOff()`. Current-draw config (`setCpuFrequencyMhz(160)` + Wi-Fi modem
  sleep, backlight dark by default, energy-gated radio); battery level on the status
  bar with a one-time low-battery warning below `config.lowBattPct`.
  **No ESP deep sleep** — it buys nothing for a button-woken, no-BLE device (it
  restarts the sketch and draws more than a full off); the recoverable low-power tier
  is the connected standby (which deep sleep can't do — it drops Wi-Fi).
  **Deviation — power-off:** the *deliberate two-step power-off* is not a firmware
  button gesture. On the M5StickS3, PWR is the M5PM1 PMIC's power key: a tap does a
  **hardware** power-off that firmware cannot intercept (only a "click" is reported,
  never a hold) or retime (no M5PM1 key-behaviour register). PWR is therefore the
  physical off switch, and the deliberate clean-shutdown path is the **staged
  auto-sleep** above. **Deviation — wake:** low-power standby wakes on a **button
  press only** (motion is ignored in standby); a full power-off wakes on **PWR** (cold
  boot). **Open:** the 2.5 h runtime is not yet measured — fallback is press-to-listen.

## 5. Display

- The display shall default to off.
- The display shall support exactly four states: dark, glance, history, status.
- The glance state shall be triggered by lift gesture or button press and shall
  return to dark after approximately 8 seconds.
- The glance state shall show the interpretation as the primary element, at 3–5
  words per line, with a connection indicator. The transcript is not shown on
  the glance; it is rendered beneath each interpretation in the history state
  ("More Info"). *(Amended: the original requirement placed a smaller
  transcript excerpt on the glance; the shipped design keeps the glance to the
  tone mark and the read.)*
- Screen orientation shall follow device orientation.
- A processing indicator shall be shown when an interpretation is pending.
- The history state shall retain the five most recent interpretations and
  present the three most recent, newest first, each with its transcript.
  *(Amended: the original requirement rendered all five with an ordinal
  position label; the shipped screen shows three, two lines each, no ordinal.)*
- The status state shall present network name, server state, battery level
  and uptime. *(Amended: signal strength (RSSI) is not shown; the top bar's
  live ping stands in as the link-quality indicator.)*
- Tone shall be conveyed by colour. **Deviation, deliberate:** the shipped design
  uses five tone colours (positive, negative, sarcastic, mixed, neutral) on a
  single heart shape, so colour *is* the sole tone cue -- see `docs/design/firmware-ui-ux.md` §6 for
  the rationale. The original requirement (no more than four values, never
  colour alone) is recorded here as superseded, not met.
- Low-confidence interpretations shall be rendered with visibly reduced
  emphasis relative to high-confidence interpretations.
- The interface shall contain no scrolling text and no continuous animation.
- Interpretation text shall not exceed eight words.

## 6. Audio output — REMOVED (out of scope)

The device has **no speaker output**. All emotional signalling is **visual** (the
colour of the glance heart, §5): there are no tone cues, no alert/boot/low-battery
tones, and no audible idle warning. Muting is therefore not applicable. See §9.

*(Superseded: this section previously required ≤150 ms cues for negative and
words–voice mismatch, mutable by a single action.)*

## 7. Controls

- Button A: short press wakes the display or advances to history; on the
  status/settings screen it selects the highlighted row. Long press is
  hold-to-record: the utterances heard while held are saved on the server as
  one clip on release (from the status screen, long press instead returns to
  the glance). (The former long-press mute action is removed with audio
  output, §6.)
- Button B: short press pauses and resumes streaming, with a visible change of
  display state; long press opens the status screen.
- A dedicated long press shall initiate power off. **On the M5StickS3 this is the
  PWR button — a hardware power-off via the M5PM1 PMIC that firmware cannot intercept
  or make two-step; the deliberate clean-shutdown path is the idle auto-sleep (§4).**

## 8. Configuration

- All tunable values shall reside in persistent configuration: network list,
  server address, token, chunk size, energy floor, idle timeout, and timeouts.
  (Tone-cue mapping is gone with audio output, §6.)
- No tunable value shall be embedded in program logic.
- Serial logging shall support severity levels.
- Credentials and tokens shall be excluded from version control.

## 9. Out of scope for version 1

- Utterance segmentation on the device.
- Over-the-air updates.
- Certificate pinning.
- Network roaming beyond the fallback list.
- On-device machine learning.
- On-device audio output (tone cues, alert/boot/low-battery tones, audible
  warnings) — the pendant has no speaker; all signalling is visual.

## 10. Acceptance criteria

- Network interruptions of up to 3 seconds shall produce no loss of audio.
- A 10-second network loss shall result in unattended recovery and resumed
  streaming, with the interruption visible on screen throughout.
- An interpretation shall be displayed within 3 seconds of the end of an
  utterance.
- The glance state shall be legible at arm's length.
- The device shall power off when stationary and silent, and shall remain
  powered when in motion and silent.
- The device shall meet the 2.5-hour runtime target, or shall operate in a
  press-to-listen mode instead of continuous capture.

## 11. Compliance

- The device shall not retain recordings of third parties by default.
- The device's operating state shall be externally visible while capturing
  audio.
