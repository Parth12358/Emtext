# Clips — save a moment for later review

A spec shared by the two halves. The **firmware** (M5StickS3 pendant) sends the save request and
shows the confirmation; the **server** retains, bundles, stores, and serves clips for review.

## Status

- **Firmware (device):** ✅ **built** — Button A is hold-to-record; on release it sends the
  `{from,to}` id range, shows a rec/saved/empty confirmation, and handles the `saved` reply.
- **Server:** ✅ **built for the range contract** — accepts `{from,to}`, bundles every retained
  utterance in the span into one clip, and still accepts the old single-`id` form as `from == to`.
  See "Server implementation status".
- **Testable today:** both paths. The **browser** "save" link sends a one-utterance range; the
  **device** hold-to-record sends the real span. Verified over the wire with a two-utterance range.

## What it is

The pendant user **holds Button A** to flag the current moment so it can be **reviewed later** on a
page/dashboard. Nothing is stored on the device (it has no storage beyond NVS config) — the device
only *names* the moment by id range; the server already has the audio + metadata for it.

## Wire protocol (additive, over the existing `/stream` socket) — current contract

The clip request rides the same WebSocket as audio, as a **TEXT/JSON frame sent between the binary
PCM frames**. **Additive** — an unknown `type` must stay ignored (never fatal).

- **Device → server:**
  ```json
  {"type": "save", "from": <first id>, "to": <last id>}
  ```
  An **inclusive id range** — the utterances captured while Button A was held (press→release), see
  "Clip span". `from`/`to` are per-connection wire ids from the `read` frames; a single-utterance
  clip is `from == to`.
  *(The old form was `{"type":"save","id":n}` — the server still speaks that today; the range is the
  new contract it needs to move to.)*

- **Server → device:**
  ```json
  {"type": "saved", "id": <n>, "ok": true, "clip": "<clip id>"}
  ```
  or on failure `{"type": "saved", "ok": false, "error": "<why>"}`. The device shows a brief
  "saved" / "save failed" confirmation, and **tolerates the reply being absent** (it confirms
  optimistically on send). `clip` (the file stem) is optional; clients may ignore it.

### `id` semantics
`id` is the **per-connection** utterance counter (starts at 1, increments per utterance; see
`server/main.py`) and **resets on every new connection**. A range is only meaningful within the
*current* connection; if the socket dropped and reconnected since those reads, the ids are stale —
reply `ok: false` rather than save the wrong moment.

### Clip span (hold-to-record)
The clip is **only the utterances that occur while the button is held** (the "held window") — it does
**not** include the utterance the user had already heard at press. On press the device notes the
current last-read id `P`; on release it sends `from = P + 1, to = <last read id at release>`. If no
new utterance completed during the hold, the device sends nothing and shows "empty" — so the server
always receives `to >= from`.

## What the server needs (the range update)

1. **Short rolling retention, keyed by `id`** — a `read` arrives *after* the audio is processed, and
   the user reacts a moment later, so keep a small **bounded, in-memory** ring of recent utterances
   keyed by id: `{audio (PCM/wav), transcript, tone, read, voice, speaker, timestamp}`. Bound it like
   `metrics.py` (memory-only, rolling); it must never grow without limit.
2. **Bundle the range into ONE clip, persist to FILES (not a DB).** On `save`, gather every retained
   utterance whose id is in `[from, to]`, **concatenate their audio in id order**, join their
   transcripts/reads, and write a **single** clip (a `wav` + JSON sidecar under `clips/`). Ids not in
   the ring are skipped; if the range yields no audio, reply `ok: false`.
3. **Review surface** — `GET /api/clips` (list) + audio playback + a small page on the dashboard,
   token-gated on the same rule as everything else.
4. **Never block the ws read loop**; **degrade gracefully** — a failed save replies `ok: false`, never raises.
5. **Reply with `saved`** so the device can confirm.

## Compliance

The brief allows **no retention of third-party audio *by default***. A clip is an **explicit,
user-initiated** retention with a visible on-screen confirmation — the sanctioned exception. The
"saved" confirmation doubles as the visible-retention signal. Retain nothing on `save` beyond the
flagged clip.

## Constraints (from `CLAUDE.md`)

- Additive-only protocol; no database, no Docker, no `tests/`; buffers memory-only and bounded;
  never block the `/stream` read loop.

## Firmware side (device) — built

- **BtnA hold-to-record.** On the hold threshold the device shows a persistent **"rec"** badge and
  notes the last-read id `P`; on **release** it sends `{"type":"save","from":P+1,"to":<last read id>}`,
  shows an optimistic **"saved"** badge (upgraded to the server's `ok`/`error` on the `saved` reply),
  and shows **"empty"** for an empty hold. Full serial trace of the lifecycle.
- Ignores the feature gracefully if the server never replies. No audio re-sent, no local storage.
- Code: `net::saveClip(from,to)` (queues the frame), `proto::Type::Saved` (reply), `emtext.ino`
  (hold-start/release), `display::setClip`/`setClipRec` (badges).

## Server implementation status

Built and working for the **range** contract:

- `server/clips.py` — `Recent.get_range(from, to)` walks the ring (never the range, so a huge `to`
  costs nothing) and returns the retained entries in id order; `save(entries)` concatenates their
  audio into one wav and writes a sidecar with the joined `transcript` / `read`, the last read's
  `tone`, a bundle `speaker` (unanimous label or `mixed`), `from` / `to` / `count`, and a
  per-utterance `utterances` list holding the individual detail.
- `server/main.py` — parses `{from,to}`; a frame carrying `id` instead is treated as `from == to`.
  Non-integer, inverted (`to < from`) → `unknown id`; a span with nothing retained → `no audio`.
  The reply's `id` is the range's `to`.
- `server/dashboard.py` — `GET /api/clips`, `GET /api/clips/{id}/audio`, `DELETE /api/clips/{id}`,
  token-gated.
- `server/static/dashboard.html` — "Saved clips" card (list/play/delete); multi-utterance clips
  show their span and count, and a `user` clip is flagged as the listener's own voice.
- `server/static/index.html` — "save" link on each read sends `{from: id, to: id}`.
- `server/config.py` — `CLIPS_ENABLED`, `CLIPS_DIR`, `CLIP_RETENTION_N`, `CLIP_RETENTION_S`,
  `CLIPS_MAX_FILES`.

### `saved` reply — error strings
`unknown id` (not in the ring / not an integer — also covers stale-after-reconnect), `no audio`,
`clip store full` (over `CLIPS_MAX_FILES`; delete some from the dashboard), `clips disabled`,
`write failed`. For a range, an out-of-ring `from`/`to` where nothing in `[from,to]` is retained →
`ok: false` (`no audio`).

## Decisions (answered)

- **Q1 — what to store:** audio + transcript + tone + read + voice (+ the `speaker` label once
  enrolled — a `user` clip is the listener's own voice, worth knowing before keeping it). Audio is
  already in memory, so metadata-only saves nothing.
- **Q2 — retention window:** last **8 utterances** and **45 s** per connection, whichever is tighter
  (`CLIP_RETENTION_N` / `CLIP_RETENTION_S`). int16 audio → ~3.8 MB worst case; the ring dies with the socket.
- **Q3 — layout:** `clips/<YYYYMMDDTHHMMSS>_<uid>_<rand4>.wav` + same-stem `.json` sidecar. The
  directory is the index; a half-written save leaves an ignored orphan wav, not a listed clip with no audio.
- **Q4 — frame shapes:** the **range** `{save,from,to}` + `saved` reply above (supersedes the earlier
  single-`id` confirmation). The optional `clip` field on success stays.
