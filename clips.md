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
  {"type": "save", "from": <first id>, "to": <last id>}
  ```
  An **inclusive id range** = the utterances captured while Button A was **held** (press→release);
  see "Clip span" below. `from`/`to` are per-connection wire ids from the `read` frames. A single-
  utterance clip is just `from == to`. *(Superseded: the old single form was `{"type":"save","id":n}`.)*

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

### Clip span (hold-to-record)
Button A is **hold-to-record**: the clip is **only the utterances that occur while the button is
held** (the "held window") — it does **not** include the utterance the user had already heard at
press. On press the device notes the current last-read id `P`; on release it sends
`from = P + 1, to = <last read id at release>`. If no new utterance completed during the hold
(`to < from`), the device sends nothing and shows "empty" — so the server always gets `to >= from`.

## What the server needs to build

1. **Short rolling retention, keyed by `id`.** A `read` (and thus the id the user reacts to) arrives
   *after* the utterance's audio was processed, and the user reacts a moment later still. So keep a
   small, **bounded, in-memory** ring of recent utterances keyed by id:
   `{audio (PCM/wav), transcript, tone, read, voice, speaker, timestamp}` — long enough that a save arriving
   a few seconds after the read still finds it. **Not indefinite** — bound it like `metrics.py`
   (memory-only, rolling), so it can never grow without limit or take the server down.
2. **Bundle the range into ONE clip, persist to FILES (not a database).** On `save`, gather every
   retained utterance whose id is in `[from, to]`, **concatenate their audio in id order** and join
   their transcripts/reads, and write it as a **single** clip — e.g. a `clips/` dir with a `wav` +
   a JSON sidecar. Ids not in the retention ring are skipped; if the range yields no audio, reply
   `ok: false`. This is explicit, user-initiated retention.
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

## Firmware side (device) — hold-to-record, built

- **BtnA is hold-to-record.** On the hold threshold the device shows a persistent **"rec"** badge and
  notes the last-read id `P`; on **release** it sends `{"type":"save","from":P+1,"to":<last read id>}`
  and shows an optimistic **"saved"** badge (upgraded to the server's `ok`/`error` when the `saved`
  reply arrives). An empty hold (no new utterance) sends nothing and shows **"empty"**.
- The device **ignores** the feature gracefully if the server never replies (pre-implementation).
- No audio is re-sent; no local storage.

## Server implementation (done)

> ⚠️ **Built for the superseded single-`id` form.** The contract is now an **id range**
> `{"type":"save","from":F,"to":T}` (hold-to-record). The server must be updated to accept it and
> **bundle** the retained utterances in `[F,T]` into one clip (concatenate audio in id order, join
> metadata). The retention ring, file storage, review page, and reply shape all carry over — only
> the `save` handler and `clips.save()` need to take a range instead of a single id.

- `server/clips.py` -- per-connection retention ring (`Recent`), `save()`, listing/lookup/delete. Only
  this module knows the file layout.
- `server/main.py` -- handles `{"type":"save"}` between PCM frames; the disk write runs in the executor
  as its own task, so the read loop never waits. Replies `saved` either way. Counts `clip_saved` /
  `clip_failed` for the dashboard.
- `server/dashboard.py` -- `GET /api/clips` (list), `GET /api/clips/{id}/audio` (wav),
  `DELETE /api/clips/{id}`. Token-gated on the same rule as `/stream`.
- `server/static/dashboard.html` -- "Saved clips" card: list, play, delete.
- `server/static/index.html` -- a "save" link on every read, so the path is testable without the device.
- Knobs in `server/config.py`: `CLIPS_ENABLED`, `CLIPS_DIR`, `CLIP_RETENTION_N`, `CLIP_RETENTION_S`,
  `CLIPS_MAX_FILES`.

### `saved` reply details

On success the reply also carries `"clip": "<clip id>"` (the file stem). Clients may ignore it.
`error` strings on failure: `unknown id` (not in the retention ring, or not an integer -- covers the
stale-after-reconnect case too), `no audio`, `clip store full` (over `CLIPS_MAX_FILES`; delete some from
the dashboard), `clips disabled`, `write failed`.

## Open questions -- answered

- **Q1.** Audio + transcript + tone + read + voice (+ the speaker-id label, once enrolled: a `user`
  clip is the listener's own voice, which is worth knowing before keeping it). "Review later" means listen back, and the audio is
  already in memory, so metadata-only would save nothing.
- **Q2.** Last **8 utterances** and **45 s**, per connection, whichever bound is tighter
  (`CLIP_RETENTION_N` / `CLIP_RETENTION_S`). Audio is kept as int16, so the worst case is ~3.8 MB per
  connection, and the ring dies with the socket.
- **Q3.** `clips/<YYYYMMDDTHHMMSS>_<uid>_<rand4>.wav` + same-stem `.json` sidecar. The directory is the
  index: nothing to corrupt, and a half-written save leaves an orphan wav (ignored) rather than a
  listed clip with no audio.
- **Q4.** Confirmed exactly as written above. Only addition: the optional `clip` field on success.
