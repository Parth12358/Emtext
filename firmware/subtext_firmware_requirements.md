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
  ready, status, utterance, read, and ping.
- The device shall send a closing frame before any intentional disconnect.
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
  connection, ready. Failure at each stage shall be distinguishable on screen
  and by tone.
- Power off shall require a deliberate two-step action and shall close the
  connection cleanly before sleeping.
- The device shall power off automatically after a configurable idle period
  with no speech-energy audio, following an audible warning and an on-screen
  grace period that any button cancels.
- Idle detection shall incorporate motion: stationary and silent shall permit
  power off; in motion and silent shall extend the timeout.
- The device shall wake from deep sleep on button press.
- Processor clock, radio sleep behaviour, backlight state and silence gating
  shall be configured to minimise average current draw.
- The device shall report battery level and shall signal low battery once.
- Speaker output shall be limited to 75% volume while on battery.
- Target runtime: at least 2.5 hours of continuous operation with the display
  dark by default.

## 5. Display

- The display shall default to off.
- The display shall support exactly four states: dark, glance, history, status.
- The glance state shall be triggered by lift gesture or button press and shall
  return to dark after approximately 8 seconds.
- The glance state shall show the interpretation as the primary element, at 3–5
  words per line, with a smaller transcript excerpt and a connection indicator.
- Screen orientation shall follow device orientation.
- A processing indicator shall be shown when an interpretation is pending.
- The history state shall present the five most recent interpretations with
  their relative position.
- The status state shall present network name, signal strength, server state,
  battery level and uptime.
- Tone shall be conveyed by colour using no more than four distinct values, and
  shall never be conveyed by colour alone.
- Low-confidence interpretations shall be rendered with visibly reduced
  emphasis relative to high-confidence interpretations.
- The interface shall contain no scrolling text and no continuous animation.
- Interpretation text shall not exceed eight words.

## 6. Audio output

- Tone cues shall not exceed 150 ms in duration.
- Distinct cues shall be defined for negative tone and for words–voice
  mismatch. No cue shall sound for neutral or positive.
- Tone output shall be mutable by a single action.

## 7. Controls

- Button A: short press wakes the display or advances history; long press
  toggles mute.
- Button B: short press pauses and resumes streaming, with a visible change of
  display state; long press opens the status screen.
- A dedicated long press shall initiate power off.

## 8. Configuration

- All tunable values shall reside in persistent configuration: network list,
  server address, token, chunk size, energy floor, idle timeout, tone mapping
  and timeouts.
- No tunable value shall be embedded in program logic.
- Serial logging shall support severity levels.
- Credentials and tokens shall be excluded from version control.

## 9. Out of scope for version 1

- Utterance segmentation on the device.
- Over-the-air updates.
- Certificate pinning.
- Network roaming beyond the fallback list.
- On-device machine learning.

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
