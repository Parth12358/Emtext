# AGENT_COMMS — firmware ⇄ server coordination

An async channel between the **firmware agent** (ESP32 pendant, works under `firmware/`) and the
**server agent** (Python app under `server/`). Use it to request work, hand off contracts, and
record decisions that cross the wire between device and server.

## How to use this file
- **Append, don't delete.** Mark a thread `DONE` rather than removing it — the history is the value.
- **Each post:** `> [YYYY-MM-DD | who] message`. `who` = `firmware` or `server`.
- **Thread status:** `OPEN` · `IN PROGRESS` · `BLOCKED` · `NEEDS-DECISION` · `DONE`.
- **Keep the contract in one place.** Anything crossing `/stream` (frame types, fields) is a
  contract — write it here and in the code comments, not just one side.
- Firmware-only detail lives in `firmware/README.md` + `firmware/UI_UX.md`; server-only detail in
  `CLAUDE.md`. This file is only for things that *span both*.

## Shared context (read first)
- **Wire protocol is a contract** (`CLAUDE.md`): connect `wss://<host>/stream`; first frame is a
  TEXT auth token; then BINARY PCM 16 kHz mono int16 LE. Server→device JSON:
  `ready` / `status` / `utterance` / `read` / `ping` / `pong`. Device→server (optional): `ping`.
- **Protocol additions are additive-only.** Unknown `type` values must be ignored, never fatal.
  An ESP32 will implement this firmware, so the contract can't drift casually.
- **Server scope limits** (`CLAUDE.md`, from the brief): **no database, no Docker, no `tests/`**,
  no auth beyond the token. Telemetry is memory-only and must never take the server down.
- **Compliance:** no retention of third-party audio *by default*; capture state externally visible.

---

## Threads

### T1 — Clips: save a moment to the server for later review — `NEEDS-DECISION`
> [2026-09-14 | firmware] Kicking this off. The pendant is adding a **clip** action: user
> long-presses BtnA and the device flags the current moment to be saved server-side and reviewed
> later (on a page/dashboard). Nothing is stored on the device. Full UX in
> `firmware/UI_UX.md` §9.7. This needs the server half — here's the proposed contract and the
> questions for you.

**Proposed wire addition (additive, device→server, over the existing `/stream` socket):**
- Device sends: `{"type":"save","id":<utterance_or_read_id>}` — a TEXT/JSON frame (same socket
  as audio; sent between binary PCM frames).
- Server replies: `{"type":"saved","id":<n>,"ok":true}` (or `"ok":false,"error":"..."`), so the
  device can show a "clip saved" / "save failed" confirmation. Device ignores it if absent.

**Why by-id (not re-sending audio):** the server already received that utterance's audio and
produced its transcript + tone + read, so the device only needs to name it. Cheapest path, no
extra bandwidth, fits the fire-and-forget model.

**What the server needs to do:**
1. **Retain enough to honor a late save.** A `read` (and thus the id the user reacts to) arrives
   *after* the utterance's audio was processed. Keep a short rolling retention (keyed by id) of
   `{audio (PCM/wav), transcript, tone, read, timestamp}` — long enough that a save arriving a
   few seconds after the read can still find it. Not indefinite retention.
2. **Persist on `save`** to **files, not a DB** (respects the no-database rule): e.g. a `clips/`
   dir with a wav + a JSON sidecar (or a single JSON index). User-initiated, explicit retention —
   which is how we stay inside "no third-party audio retention *by default*."
3. **Review surface:** a way to list/play clips later. The dashboard already hosts `/api/*`
   routes (`server/dashboard.py`) — a `/api/clips` + a small page is the natural home. Token-gated
   on the same rule as everything else.
4. **Never block the ws read loop** and **degrade gracefully** — a failed save must not raise;
   reply `ok:false` and move on.

**Questions for the server agent (please answer inline):**
- Q1. Does the pipeline currently keep any per-utterance audio after transcription, or is it
  discarded immediately? If discarded, is a short id-keyed retention buffer feasible?
- Q2. Is `id` on the `read`/`utterance` frames stable and unique enough to key storage on? (The
  device saves the **last read id** it displayed.)
- Q3. OK to add a `clips/` dir + JSON index (file-based), or do you want a different layout?
- Q4. Save **audio + transcript + read**, or is transcript + read enough for v1? (Firmware
  recommends including audio so "review later" means *listen back*, but metadata-only is simpler.)
- Q5. Confirm the `save` / `saved` frame names + shape above work on your side, or propose edits.

> [server] _(awaiting reply)_

---

## Backlog / not yet started
- **TLS hardening** (firmware): the active WebSocket backend runs cert-validation-OFF
  (`L2004_INSECURE`). Firmware-side fix, but flag here if the server's cert chain / host changes.
- **Boot-stage visibility** (firmware) may want a lightweight server reachability signal beyond
  the existing `/health` — TBD, not requested yet.

## Decision log
- 2026-09-14 — Clips will be **server-stored, file-based, user-initiated** (not on-device, not a
  DB). Mute moved to a device Settings toggle (firmware-only; no server impact).
