# Clips — save a moment for later review

A spec for the **server** to build against. The **firmware** side (the M5StickS3 pendant) is
described here too so the contract is in one place. Owner split: firmware implements the
device→server request and the on-screen confirmation; the server implements retention, storage,
and the review surface.

## What it is

The pendant user **long-presses Button A** to flag the current moment so it can be **reviewed
later** on a page/dashboard. Nothing is stored on the device (it has no storage beyond NVS config).
The device only *names* the moment; the server already has everything for it.

## Wire protocol (additive, over the existing `/stream` socket)

The clip request rides the same WebSocket as audio, as a **TEXT/JSON frame sent between the binary
PCM frames**. It is **additive** — an unknown `type` must stay ignored (never fatal), per the
protocol contract.

- **Device → server:**
  ```json
  {"type": "save", "id": <utterance/read id>}
  ```
  `id` is the integer id the device last displayed (from the `read` / `utterance` frames).

- **Server → device (reply):**
  ```json
  {"type": "saved", "id": <n>, "ok": true}
  ```
  or on failure `{"type": "saved", "id": <n>, "ok": false, "error": "<why>"}`. The device shows a
  brief "clip saved" / "save failed" confirmation. **The device tolerates the reply being absent**
  (e.g. before the server implements this) — it shows an optimistic confirm on send.

### Why by `id` (not re-sending audio)
The server already received that utterance's audio and produced its transcript, tone, and read, so
the device only needs to name it. Cheapest path, no extra bandwidth, fits the fire-and-forget model.

### `id` semantics (important)
`id` is the **per-connection** utterance counter (starts at 1, increments per utterance; see
`server/main.py`). It **resets on every new connection**. A `save` is only meaningful for an id from
the *current* connection. If the socket dropped and reconnected since the read, the id is stale —
the server should reply `ok: false` rather than save the wrong moment.

## What the server needs to build

1. **Short rolling retention, keyed by `id`.** A `read` (and thus the id the user reacts to) arrives
   *after* the utterance's audio was processed, and the user reacts a moment later still. So keep a
   small, **bounded, in-memory** ring of recent utterances keyed by id:
   `{audio (PCM/wav), transcript, tone, read, voice, timestamp}` — long enough that a save arriving
   a few seconds after the read still finds it. **Not indefinite** — bound it like `metrics.py`
   (memory-only, rolling), so it can never grow without limit or take the server down.
2. **Persist on `save` to FILES, not a database** (respects the no-database rule): e.g. a `clips/`
   dir with a `wav` + a JSON sidecar per clip, or a single JSON index. This is explicit,
   user-initiated retention.
3. **Review surface.** A way to list/play clips later. The dashboard already hosts `/api/*`
   (`server/dashboard.py`) — a `GET /api/clips` (list) + audio playback + a small page is the
   natural home. **Token-gated on the same rule as everything else** (only enforced when
   `AUTH_TOKEN` is set), so there is one auth rule in the project.
4. **Never block the ws read loop** and **degrade gracefully.** A failed save must not raise — reply
   `ok: false` and move on. If the id isn't in the retention buffer, `ok: false`.
5. **Reply with the `saved` frame** so the device can confirm.

## Compliance (why this is allowed)

The brief says **no retention of third-party audio *by default***. A clip is an **explicit user
action** that retains one moment, with a visible on-screen confirmation — the sanctioned way to stay
inside that rule. The "clip saved" confirmation doubles as the visible-retention signal. Do not
retain anything on `save` beyond the flagged clip.

## Constraints (from `CLAUDE.md`, don't break)

- **Additive-only protocol** — unknown `type`s are ignored, never fatal.
- **No database, no Docker, no `tests/` folder.** Clips are file-based and user-initiated.
- **Telemetry/buffers are memory-only and bounded** and must never take the server down.
- **Never block the `/stream` read loop**; do heavy work off the event loop, like the existing
  transcription/SER/LLM path.

## Firmware side (device)

- **BtnA-hold** → device sends `{"type":"save","id":<last read id>}` and shows a brief on-screen
  **"saved"** confirmation (optimistic; upgraded to the server's `ok` result when the `saved` reply
  arrives). The device already tracks the last displayed read id.
- The device **ignores** the feature gracefully if the server never replies (pre-implementation).
- No audio is re-sent; no local storage.

## Open questions for the server (please answer inline or in a reply)

- **Q1.** Save **audio + transcript + read**, or is transcript + read enough for v1? (Firmware
  recommends including audio so "review later" means *listen back*, but metadata-only is simpler.)
- **Q2.** Retention window: how many seconds / how many recent utterances to keep keyed by id?
  (Must cover read-latency + user reaction time; a handful of utterances is plenty.)
- **Q3.** Storage layout: `clips/<timestamp>.wav` + `.json` sidecar, or a single `clips/index.json`?
- **Q4.** Confirm the `save` / `saved` frame names + shapes above, or propose edits.
