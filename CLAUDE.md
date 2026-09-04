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
- Local venv is `.venv/` (`.venv/Scripts/` on Windows, `.venv/bin/` elsewhere). No
  Python version is pinned -- use whatever `python` resolves to. Segmenter self-test
  (synthetic audio, no deps beyond numpy): `python -m server.segmenter`
- `transformers` is pinned `<5`: MERaLiON's remote code calls `.item()` on a tensor
  built during `__init__`, which fails under transformers 5's meta-device init.
- On Windows set `HF_HUB_DISABLE_SYMLINKS=1` before any first-time model download;
  without it the HF cache needs symlink privileges and fails with WinError 1314.
- `server/ser.py` has two backends behind one `analyze()`: `meralion` (default,
  has valence/arousal/dominance) and `emotion2vec` (via funasr, categorical only,
  9 classes normalised to our 7). `SER_BACKEND=auto` infers from `SER_MODEL`.
  Compare them with `python -m eval.bench --ser-models`; compare whisper models
  with `python -m eval.asr_eval`.
- Test without a mic: `experiments/make_speech.ps1` (SAPI -> 16 kHz WAV) then
  `experiments/wav_client.py` (streams it over the real wire protocol). Score one
  WAV directly with `python -m server.ser <wav>`.
- Start the server: `python -m server.main` (needs full `requirements.txt` + Ollama up).
- The only automated test is that segmenter self-test. Run it after touching
  `segmenter.py`.
- The eval suite is `python -m eval.run_all` (`--quick` for a minutes-long pass,
  `--list` to see stages and their prerequisites). Each stage writes a per-item
  CSV under `eval/results/`. Stages stream progress on purpose -- a full run is
  ~1-2h and silence looks like a hang.
- `python -m eval.pipeline_eval` is the only eval that runs the WHOLE workflow
  (Segmenter -> whisper + SER -> Interpreter) over RAVDESS, across a 2 SER x 3 LLM
  matrix. ASR+SER are cached per SER backend and replayed across LLMs -- do not
  "fix" that into per-cell recomputation; it is 3x cheaper AND makes the LLM
  comparison exact. Ctrl+C = clean stop, and stop IS pause (resume is keyed on
  `(ser_backend, llm_model, file)`); `eval/results/PAUSE` pauses without exiting.
  **It always evicts the LLM from VRAM on exit** -- `interpreter.py` sends
  `keep_alive: -1`, so without that a run leaves 8-9GB pinned forever and
  model-swapping can silently offload layers to CPU, which reads as "slow model".
- **`TODO.md` holds open issues with the measurements behind them.** Read it before
  touching the segmenter or the VAD knobs, and add to it rather than re-deriving.
- RAVDESS is much quieter than a live mic: at the default `SPEECH_RMS=500` the VAD
  drops ~50% of clips, and they are the QUIET emotions (sad/fearful/neutral) --
  exactly what this app exists to interpret. Run `--vad-check` before any long run,
  and pass `--speech-rms 150` (5/10 drops -> 1/10). The eval falls back to the raw
  clip and flags `vad_dropped=1`, so SER and WER stay usable, but those rows carry
  garbled transcripts -- filter them out of TONE accuracy. Do NOT loudness-normalise
  to work around it: loudness is an emotional cue and flattening it corrupts SER.
  Full write-up, including what was ruled out, is in `TODO.md`.
- `python -m eval.ser_eval` scores SER against RAVDESS in `data/ravdess/`
  (gitignored, 1440 clips). It is the ONLY test using real emotional audio --
  everything else uses flat SAPI speech, which cannot measure SER accuracy.
  Report macro-average recall, not raw accuracy: `calm` folds into `neutral`,
  making that class double-sized. Watch the valence-separation line; if valence
  stops separating pleasant from unpleasant, the mismatch rule is dead.
- Benchmark the three models with `python -m eval.bench` (`--skip-llm` to avoid
  needing Ollama). Re-run it after changing a model, compute type, or device.
- Measure user-facing latency with `python -m eval.latency` (warms every model
  before timing; `--skip-wire` needs no server). It trims trailing silence from
  its clips on purpose -- without that the VAD closes the utterance before the
  clock starts and the wire number comes out impossibly low.
- A/B interpreter LLMs with `python -m eval.model_eval` against the labeled cases
  in `eval/tone_cases.jsonl`. **Re-run it after any change to `SYSTEM_PROMPT`** --
  the prompt is the product, and the `literal` / `low-confidence` categories are
  the guard rails against over-reading tone into plain speech. The `mismatch`
  pairs (identical text, opposite voice) are the only check that the SER signal
  is actually being used; a model ignoring `voice` scores 0 there.

## Architecture & module boundaries (respect these)

- `server/config.py` — every tunable, each overridable by an env var of the same name. New
  knobs go here, never hardcoded elsewhere.
- `server/segmenter.py` — **pure logic, zero I/O.** Energy-based VAD state machine: PCM
  bytes in → float32 utterances out. Must stay importable and testable without network,
  files, or async. Keep the `__main__` self-test passing.
- `server/transcriber.py` — thin faster-whisper wrapper. Model loaded **once** at import.
  Blocking CPU call — callers must run it off the event loop.
- `server/ser.py` — speech emotion recognition (MERaLiON). Model loaded **once** at
  import, on CPU. The rest of the codebase knows only `ser.analyze()` and
  `ser.available()` — swapping the model must not touch any other file. Must
  **never** raise: a load or inference failure returns None and the pipeline runs
  as if SER didn't exist. Blocking CPU call, ~3.3s flat per utterance.
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
- **Never block the websocket read loop.** Transcription and SER run in a thread
  executor, `asyncio.gather`ed so they overlap rather than queue; each utterance is a
  fire-and-forget `asyncio` task so audio keeps flowing while thinking.
- **Hardware split:** Whisper and SER stay on **CPU**; the Intel Arc B580 GPU is
  reserved for the **LLM**. Don't move either to GPU.
- **Protocol additions are additive only.** The `voice` object on the `read` frame is
  optional and omitted when SER has nothing; clients must be able to ignore it.
- **Interpreter prompt**'s `voice sounded like: X` slot is now filled by `ser.py`.
  The prompt must keep explaining the words-vs-voice **mismatch** rule (positive
  words + low valence = sarcasm/masking; negative words + high valence = teasing;
  agreement = literal), keep downweighting readings below `SER_MIN_CONFIDENCE`,
  and keep working with the slot absent — SER is optional and often off.

## Exposure / deployment

- The client derives the websocket scheme from `location.protocol`
  (`server/static/index.html`). **Never hardcode `ws://`** -- browsers block it as
  mixed content from an HTTPS page, so it works on localhost and then fails
  silently and totally behind any TLS terminator. The ESP32 needs `wss://` too.
- `AUTH_TOKEN` unset means **any first frame is accepted**. That is fine for
  localhost and unacceptable the moment the server is reachable from outside;
  there is no rate limiting, origin check or connection cap behind it.
- Cloudflare **quick tunnel** is the documented path out (`tunnel/README.md`):
  `cloudflared tunnel --url http://localhost:8000`, random hostname, changes every
  restart. The server stays unaware of it -- no tunnel code, no config.
- Keepalives: uvicorn pings every 20s, AND `stream()` sends `{"type":"ping"}` after
  `WS_IDLE_PING_S` (30s) with no audio, AND clients may send `{"type":"ping"}` to
  get a `{"type":"pong"}` with their timestamp echoed. Cloudflare culls proxied
  websockets after ~100s idle, so this is belt-and-braces on purpose.
- `/health` returns `{"status":"ok"}`, unauthenticated on purpose -- it is the
  first thing to check through a tunnel (502 = tunnel up, server down). It is a
  same-origin fetch, so `remote.html` cannot check it cross-origin; that is
  expected, and websockets are unaffected since they bypass CORS.
- `server/static/remote.html` is the phone-side diagnostic page. Keep it
  standalone: it must point at an arbitrary host, because the quick-tunnel
  hostname changes constantly.

## Scope limits (from the brief — don't add unprompted)

No auth beyond the token, no database, no Docker, no `tests/` folder.

## Style

Type hints, docstrings that explain the **why** of non-obvious choices. The
segmenter state machine, the executor pattern, and the worklet/downsampling are commented
generously on purpose (the author is learning from them) — preserve that when editing.
