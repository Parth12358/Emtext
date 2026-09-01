# CLAUDE.md

Guidance for Claude when working in this repo. Keep it current when conventions change.

## What this is

**emtext** — a live conversation interpreter for a neurodivergent listener. A client
streams mic audio over a websocket; the server segments it into utterances, transcribes
each (Whisper, CPU), and reads its emotional subtext (local LLM via Ollama, GPU). A
browser page is the test client today; an **ESP32 will replace it later using the exact
same wire protocol**.

## Running things

- Run all commands from the **project root** (the directory containing `server/`), not a
  subfolder. There is no nested project dir — the repo root *is* the project.
- Local venv is `.venv/`. Segmenter self-test (synthetic audio, no deps beyond numpy):
  `.venv/bin/python -m server.segmenter`
- Start the server: `python -m server.main` (needs full `requirements.txt` + Ollama up).
- The only automated test is that segmenter self-test. Run it after touching
  `segmenter.py`.

## Architecture & module boundaries (respect these)

- `server/config.py` — every tunable, each overridable by an env var of the same name. New
  knobs go here, never hardcoded elsewhere.
- `server/segmenter.py` — **pure logic, zero I/O.** Energy-based VAD state machine: PCM
  bytes in → float32 utterances out. Must stay importable and testable without network,
  files, or async. Keep the `__main__` self-test passing.
- `server/transcriber.py` — thin faster-whisper wrapper. Model loaded **once** at import.
  Blocking CPU call — callers must run it off the event loop.
- `server/interpreter.py` — thin Ollama wrapper + rolling context window. Must **degrade
  gracefully**: if Ollama is unreachable or returns bad JSON, still emit the transcript
  with read `(interpreter offline)`, never raise.
- `server/main.py` — FastAPI app + `/stream` websocket. **Wiring only, no signal logic.**
- `server/static/index.html` — single-file browser client. **No build step, no external
  CDNs/fonts.** Uses AudioWorklet (not MediaRecorder) and downsamples to 16 kHz in JS.

## Invariants — do not break

- **Wire protocol is a contract** (an ESP32 will implement it): connect to `/stream`; first
  frame is a TEXT auth token (any value accepted when `AUTH_TOKEN` env is unset); then
  BINARY frames of raw PCM 16 kHz mono int16 LE. Server replies with the JSON frame types
  `ready` / `status` / `utterance` / `read`. Changing this means changing future firmware.
- **Never block the websocket read loop.** Transcription runs in a thread executor;
  each utterance is a fire-and-forget `asyncio` task so audio keeps flowing while thinking.
- **Hardware split:** Whisper stays on **CPU**; the Intel Arc B580 GPU is reserved for the
  **LLM**. Don't move Whisper to GPU.
- **Interpreter prompt** has an intentional, currently-unused `voice sounded like: X` slot
  for a future acoustic-emotion model. Keep it wired but empty.

## Scope limits (from the brief — don't add unprompted)

No auth beyond the token, no database, no Docker, no `tests/` folder.

## Style

Python 3.11+, type hints, docstrings that explain the **why** of non-obvious choices. The
segmenter state machine, the executor pattern, and the worklet/downsampling are commented
generously on purpose (the author is learning from them) — preserve that when editing.
