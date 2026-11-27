# CLAUDE.md

Guidance for Claude when working in this repo. Keep it current when conventions change.

## What this is

**emtext** — a live conversation interpreter for a neurodivergent listener. A client
streams mic audio over a websocket; the server segments it into utterances, transcribes
each (Parakeet by default, Whisper selectable, CPU), scores how the voice sounded (SER, CPU), decides whose voice it was
(speaker id, CPU), and reads the emotional subtext (local LLM via Ollama, GPU). The
listener's own lines get no read. A hold on the pendant's button saves the moment as a
clip for later review. A browser page is the test client; an **M5StickS3 pendant
(ESP32) speaks the exact same wire protocol** and is the real client.

Documentation lives in `docs/` (index: `docs/README.md`), organised by kind: `status/`
(TODO with measurements), `design/` (research, firmware UI/UX and requirements, website
brief), `guides/` (firmware, tunnel, website how-tos), `reports/` (one-off measurements).
Only `README.md` and this file stay at the root; `firmware/`, `tunnel/` and `website/` keep a
three-line README stub pointing into `docs/`. New docs go under `docs/`, not next to code.

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
- Windows 11 ships `System32\onnxruntime.dll` (1.17.1), and it wins over the one
  bundled with sherpa-onnx, killing every sherpa ASR key with "requested API version
  [28] is not available". `transcriber._pin_sherpa_onnxruntime()` loads the bundled
  DLL by path first; keep it. `pipeline_eval`'s CPU worker writes its stderr to
  `eval/results/_worker_<slug>.log` -- look there when it "failed to start".
- Close GPU-heavy desktop apps before timing the 12B/14B: with ~2 GB of VRAM taken,
  qwen3:14b spills into shared memory and reads in 4-6 s while `ollama ps` still
  says "100% GPU" (`docs/reports/small-interpreter-stage0.md`).
- With the monitor on the motherboard's AMD iGPU, Ollama's model runners can open the iGPU
  instead of the B580: Vulkan's device order is not stable from one runner to the next, so
  `ollama ps` still says "100% GPU" while a 0.6B read takes 2.2 s (0.2 s on the B580) and the
  14B 22 s. A restart fixed one load, not the next. The fix is to hide the AMD Vulkan driver
  from Ollama's process only: start it with `VK_LOADER_DRIVERS_DISABLE=*amd-vulkan*`
  (`ollama serve`). The server log's `using device Vulkan0 (...)` line names the card actually
  used (`docs/reports/small-interpreter-stage1.md`).
- `server/ser.py` has two backends behind one `analyze()`: `emotion2vec` (default,
  via funasr, categorical only, 9 classes normalised to our 7, ~0.17s) and `meralion`
  (opt-in via `SER_MODEL`, has valence/arousal/dominance, ~2.7s flat).
  `SER_BACKEND=auto` infers from `SER_MODEL`.
  Compare them with `python -m eval.bench --ser-models`; compare whisper models
  with `python -m eval.asr_eval`.
- Test without a mic: `experiments/make_speech.ps1` (SAPI -> 16 kHz WAV) then
  `experiments/wav_client.py` (streams it over the real wire protocol). Score one
  WAV directly with `python -m server.ser <wav>`.
- Start the server: `python -m server.main` (needs full `requirements.txt` + Ollama up).
- The only self-contained assert test is that segmenter self-test. Run it after
  touching `segmenter.py`. `python -m server.ser <wav>` and
  `python -m server.speaker a.wav b.wav` are smoke tests, not assertions.
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
- **`docs/status/TODO.md` holds open issues with the measurements behind them.** Read it before
  touching the segmenter or the VAD knobs, and add to it rather than re-deriving.
- RAVDESS is much quieter than a live mic: at a FIXED `SPEECH_RMS=500` the VAD
  dropped ~50% of clips, and they were the QUIET emotions (sad/fearful/neutral) --
  exactly what this app exists to interpret. The adaptive threshold now segments
  10/10 with no override; still run `--vad-check` before any long run. Passing
  `--speech-rms N` switches adaptation OFF (an explicit number means a constant),
  which is how to reproduce the old fixed-threshold results. The eval falls back to the raw
  clip and flags `vad_dropped=1`, so SER and WER stay usable, but those rows carry
  garbled transcripts -- filter them out of TONE accuracy. Do NOT loudness-normalise
  to work around it: loudness is an emotional cue and flattening it corrupts SER.
  Full write-up, including what was ruled out, is in `docs/status/TODO.md`.
- `python -m eval.spk_eval` scores speaker id against RAVDESS (same clips, silence-
  trimmed at RMS 150): `--eer`, `--pairs`, `--label`, and `--real me.wav conv.wav` for
  the day a real two-person recording exists -- every RAVDESS number is studio audio
  and therefore a ceiling. Embeddings are cached in `eval/results/_cache_spk.jsonl`.
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
- An audio-input LLM (Gemma 4 E4B, Voxtral Mini, Qwen2.5-Omni behind
  `llama-server`) as a one-shot replacement for ASR+SER+LLM was measured on
  2026-09-22 and **not adopted**: the models answer `neutral` on ~all RAVDESS
  audio and follow a wrong transcript over the delivery. The harness was removed
  with the decision; the verdict, method and tables are in
  `docs/reports/audio-llm.md`. Do not re-open this without new evidence.

## Branches, CI and releases (how work ships)

- **`main` is the dev branch; `prod` is the release branch.** Commit and push day-to-day work
  to `main`. Nothing runs on a `main` push (no server CI exists; the only assert test is
  `python -m server.segmenter`, run it by hand). Never push straight to `prod`: ship by
  opening a PR from `main` into `prod` and merging it (merge or fast-forward, not squash, so
  `prod` stays identical to `main`). Open PRs via the compare link
  `https://github.com/Parth12358/Emtext/compare/prod...main?quick_pull=1` when `gh` is absent.
- **The one CI check** is `.github/workflows/firmware.yml`. On a PR into `prod` it compiles
  `firmware/emtext` with `arduino-cli` (`m5stack:esp32:m5stack_sticks3`, no `secrets.h`) and
  stops. It only triggers when the PR touches `firmware/**`, the three firmware docs
  (`docs/guides/firmware.md`, `docs/design/firmware-*.md`), the workflow, the toolchain
  action or `.github/scripts/stage_firmware.sh`; a server-only PR shows no check at all.
  The check is not yet required by branch protection, so red does not block the merge.
- **Merging into `prod` releases.** The same workflow, now with `PUBLISH` true, compiles with
  exported binaries, runs `stage_firmware.sh`, force-pushes the public tree as one orphan
  commit to `Parth12358/emtext-firmware` (authored as the shipping commit's author), and
  creates the GitHub Release there with the merged/app/bootloader/partition images, a
  source zip, `FLASHING.md`, `SHA256SUMS.txt` and notes listing the firmware commits since
  the previous `prod` head.
- **Release versioning is `firmware/VERSION`** (`X.Y.Z`). The release tag is
  `fw-v<VERSION>`. Bump it on `main`, in the same PR as the change it describes, BEFORE
  merging into `prod`: patch for fixes, minor for features, major for wire-protocol or
  flashing changes. If a `prod` merge lands without a bump and `fw-v<VERSION>` already
  exists on the public repo, the run still publishes, tagged `fw-v<VERSION>-<yyyymmdd>.<sha>`,
  which is noise rather than an error; the next bump makes a clean tag again.
- **Secrets and scope.** Publishing needs `FIRMWARE_MIRROR_TOKEN` (fine-grained PAT on the
  public repo: Contents + Workflows read/write). Only firmware, its three docs,
  `firmware/mirror/README.md`, `firmware/VERSION` and `firmware/LICENSE` (PolyForm
  Noncommercial 1.0.0) ever reach the public repo; nothing under `server/` may be added to
  the staging script. Never edit the mirror repo directly.

## Architecture & module boundaries (respect these)

- `server/config.py` — every tunable, each overridable by an env var of the same name. New
  knobs go here, never hardcoded elsewhere.
- `server/confidence.py` — **pure logic, zero I/O**, self-test in `__main__`. Turns the
  signals the pipeline already has (the interpreter's first-token probability for its
  tone from Ollama `logprobs`, ASR confidence from `transcriber.transcribe_scored`, SER
  confidence, voiced seconds, speaker label, `mixed` tone, offline) into
  `{"score","level","reasons"}`. Base = Platt-scaled tone probability; each weak input
  multiplies by `CONFIDENCE_PENALTY` and adds a listener-facing reason. It never changes
  what the LLM sees, so `SYSTEM_PROMPT` and the eval baselines are untouched. The
  thresholds and Platt parameters come from `python -m eval.confidence_eval`, never
  from hand-tuning; verbalised "I am 80% sure" was rejected on the literature (small
  models are badly overconfident).
- `server/segmenter.py` — **pure logic, zero I/O.** Energy-based VAD state machine: PCM
  bytes in → float32 utterances out. The speech threshold is **noise-floor-relative**:
  `clamp(noise_floor * NOISE_MARGIN, SPEECH_RMS_MIN, SPEECH_RMS)`, with the floor learned
  only from non-speech frames (falls fast, rises slowly). `SPEECH_RMS` is the ceiling, not
  the threshold; `VAD_ADAPTIVE=0` restores the constant. Must stay importable and
  testable without network, files, or async. Keep the `__main__` self-test (five cases)
  passing.
- `server/sentences.py` — **pure logic, zero I/O**, self-test in `__main__`. After ASR, cuts
  a multi-sentence utterance at sentence ends using the transducer's per-token timestamps
  (`transcriber.transcribe_timed`); `main.py` then gives each piece its own SER, read and
  wire id (`new_id()`; no protocol change). Pieces under `SPLIT_MIN_S` / `SPLIT_MIN_WORDS`
  merge into a neighbour. It never cuts inside a sentence (half a sentence is a worse input
  than a long one; docs/status/TODO.md). Speaker id is re-run per piece only when the whole
  utterance was `mixed`. Whisper has no token timings, so it never splits.
  `SENTENCE_SPLIT=0` restores one read per utterance.
- `server/transcriber.py` — speech-to-text behind one `transcribe(audio) -> str`. A
  **catalogue** of backends (`whisper:<name>` via faster-whisper; `parakeet:v2/v3`,
  `moonshine:tiny/base`, `sensevoice:small` via sherpa-onnx, int8 files under
  `models/asr/`), exactly **one resident** at a time, swapped behind the function by
  `select(key)` (blocking, executor) so `main.py`'s bound `transcribe` keeps working.
  Default `parakeet:v3` (multilingual; an explicit `WHISPER_MODEL` still wins).
  Import loads `config.ASR_MODEL` synchronously and raises on failure, as before; a
  later switch that fails keeps the old model and reports in `status()`. Only catalogue
  keys are ever accepted from the wire. `build(key)` makes a backend without installing
  it (what `eval/asr_eval.py` uses). Blocking CPU call — callers must run it off the
  event loop. Re-run `python -m eval.asr_eval` after adding a backend; with
  `--dir <folder>` it scores a `server.translog --export` set of real speech instead
  of the synthetic SAPI lines.
- `server/ser.py` — speech emotion recognition (emotion2vec by default, MERaLiON
  opt-in). Model loaded **once** at import, on CPU. The rest of the codebase knows
  only `ser.analyze()` and `ser.available()` — swapping the model must not touch any
  other file. Must **never** raise: a load or inference failure returns None and the
  pipeline runs as if SER didn't exist. Blocking CPU call, ~0.17s per utterance on
  the default backend. There `valence` is None **by design** (the LLM reads it from the
  words) and `arousal`/`dominance` are **derived** as the anchor-weighted mean over the
  7-class distribution (`dims_source: "derived"`); MERaLiON measures all three.
- `server/speaker.py` — speaker identification: is this utterance the **listener's own
  voice**? CAM++ embeddings via funasr (needs `kaldi-native-fbank`), CPU, loaded once,
  **never raises**; the rest of the codebase knows only `available()`, `identify()`,
  `profile` (the user's enrolled voiceprint, embeddings-only JSON under `models/`) and
  `Session` (the per-connection online partner centroid). It **labels, never cuts**:
  the label comes from the fraction of 1 s windows matching the user (`user` / `other` /
  `mixed` / `unknown`); splitting a glued utterance was measured and rejected (docs/status/TODO.md).
  A `user` line is transcribed into the interpreter's context but **gets no read** --
  explaining the listener's own feelings back to them is the one output this product
  must not produce. Enrolment is `/enroll.html` -> `POST /api/enroll`; forget with
  `DELETE /api/speaker`. Score it with `python -m eval.spk_eval` (RAVDESS; ~94% on
  actor pairs, 1.4% partner-as-user). Enrol across moods: calm-only enrolment loses the
  user precisely when they are upset.
- `server/interpreter.py` — thin Ollama wrapper + rolling context window. Must **degrade
  gracefully**: if Ollama is unreachable or returns bad JSON, still emit the transcript
  with read `(interpreter offline)`, never raise. Context entries are `(who, line)`;
  speaker labels are rendered in `_build_prompt` **only when a label exists**, so with
  no profile enrolled the prompt is byte-identical to the measured one and
  `SYSTEM_PROMPT` is untouched. `remember()` adds a line without interpreting it.
  **Load-time Ollama options (`use_mmap`, `num_ctx`) come only from
  `interpreter.load_options()`**, and every request that can load or unload a model
  (interpreter, dashboard warm/evict, eval warm-ups) merges it in: Ollama reloads the
  runner whenever two callers disagree, which reads as "slow model". `use_mmap` is off
  because on Windows the mmap loader keeps the whole GGUF resident in host RAM (8.6 GB
  for qwen3:14b; `docs/reports/server-resources.md`).
- `server/main.py` — FastAPI app + `/stream` websocket. **Wiring only, no signal logic.**
- `server/translog.py` — the transcript log: one JSON line per utterance
  (`logs/transcripts/<day>.jsonl`: transcript, `asr_model`, tone, read, speaker,
  voice, timings) plus the utterance WAV under `audio/` with a rolling cap. Same
  contract as `clips.py`: blocking file I/O, run in the executor, **never raises**,
  only this module knows the layout. `--stats` and `--export DIR` in its `__main__`.
- `server/clips.py` — "save this moment" (hold-to-record on the pendant). `Recent`
  is a per-connection ring of the last few utterances keyed by the wire `id`
  (bounded by count AND age, dropped with the connection); `save()` takes the
  entries in a `[from,to]` range and writes them as ONE `wav` + `json` sidecar
  under `CLIPS_DIR`. Only this module knows the file layout -- `main.py` calls
  `Recent`/`save`, `dashboard.py` calls the list/lookup/delete helpers. Never
  raises: a failed save is `{"type":"saved","ok":false,"error":...}` on the wire.
- `distill/` — the teacher-student dataset for a small interpreter adapter
  (`docs/guides/distill.md`). Not imported by the server. `spec.py` is the versioned rule
  table (categories, voice variants, and the outcome SYSTEM_PROMPT's own rules predict;
  self-test `python -m distill.spec`). Bump `SPEC_VERSION` when it changes. `voice.py`
  samples hints through `ser._expected()` and the real anchors, never hand-typed numbers.
  `pipeline.py` renders inputs with `Interpreter._build_prompt` and labels through
  `Interpreter.interpret`, so the data is production's prompt byte for byte. Keep it that
  way rather than re-implementing the prompt. Output goes to `data/distill/` (gitignored).
  Rows near an eval transcript are rejected at build, so the eval suites stay held out.
  `label --api deepseek` sends the weak cells to a hosted teacher; its key comes from the
  environment only and must never be written to a file, a log or a commit.
  `distill/train.py` fine-tunes the student and runs ONLY in `.venv-train` (torch +xpu),
  never the server's `.venv`. Stop the server and evict Ollama before training: the B580
  is shared.
- `server/static/index.html` — single-file browser client. **No build step, no external
  CDNs/fonts.** Uses AudioWorklet (not MediaRecorder) and downsamples to 16 kHz in JS.
  The same single-file, no-CDN rule covers `dashboard.html`, `remote.html` and
  `enroll.html`.

## Invariants — do not break

- **Wire protocol is a contract** (an ESP32 will implement it): connect to `/stream`; first
  frame is a TEXT auth token (any value accepted when `AUTH_TOKEN` env is unset); then
  BINARY frames of raw PCM 16 kHz mono int16 LE. Server replies with the JSON frame types
  `ready` / `status` / `utterance` / `read` / `ping` / `pong` / `saved`, plus `vad` and
  `vad_close` only on a `?vad=1` connection. Changing this means changing the firmware.
- **Never block the websocket read loop.** Transcription, SER and speaker id run in a
  thread executor, `asyncio.gather`ed so they overlap rather than queue; each utterance
  is a fire-and-forget `asyncio` task so audio keeps flowing while thinking.
- **Hardware split:** Whisper, SER and speaker id stay on **CPU**; the Intel Arc B580
  GPU is reserved for the **LLM**. Don't move any of the three to GPU.
- **Protocol additions are additive only.** The `voice` object on the `read` frame is
  optional and omitted when SER has nothing; clients must be able to ignore it. Likewise
  the `speaker` object (`{"label","score"}`) on `utterance` and `read`, present only once
  a voice is enrolled -- and a `user` utterance gets **no `read` frame at all**, so a
  client must never block waiting for one. Likewise `confidence`
  (`{"score","level","reasons"}`) on `read`, always sent by this server: `high` renders
  as normal, `medium` de-emphasised, `low` suppressed behind a "not sure" marker
  (browser) or drawn faint (pendant, fw 0.1.2+). The server never withholds a read on
  confidence; suppression is the client's decision so logs and evals keep the record.
  The only client->server TEXT frames after auth are `{"type":"ping"}` and
  `{"type":"save","from":a,"to":b}` (an inclusive id range bundled into one clip,
  answered by `saved`; the older `{"id":n}` form still works as `from == to`);
  anything else is ignored, never fatal.
- **The headline emotion is the SER's, adjusted by the interpreter** (since
  2026-09-22). The `read` frame's `emotion` object (`{"label","voice","adjusted"}`)
  uses `ser.EMOTIONS` (neutral/happy/sad/angry/fearful/disgusted/surprised); the
  LLM's schema requires an `emotion` in that enum and the prompt tells it to START
  from the voice label and move only when the words make it untenable. Interpreter
  offline = the voice label untouched; SER off = words alone. `tone` stays on the
  wire for `sarcastic`/`mixed` and for older clients, and the firmware (0.2.0+)
  colours the heart by `emotion.label`, falling back to `tone`. Measured on 280
  RAVDESS clips: SER alone 89%, shown emotion 88% -- the adjustment is roughly
  neutral there because the words are neutral by construction; the case for it is
  real speech, where the words carry information. Do not make the LLM the primary
  source again without re-measuring.
- **Interpreter prompt**'s `voice sounded like: X` slot is now filled by `ser.py`.
  The prompt must keep explaining the words-vs-voice **mismatch** rule in both forms:
  on valence (positive words + low valence = sarcasm/masking; negative words + high
  valence = teasing; agreement = literal) AND on arousal+dominance (positive words +
  low/low = masking; negative + high/high = real anger; negative + low/low =
  resignation), because the default backend supplies only the latter. It must keep
  downweighting readings below `SER_MIN_CONFIDENCE`, keep the caution paragraph firing
  for BOTH "no voice field" and "label only, no scales", and keep working with the
  slot absent — SER is optional and often off. Off-enum tones go through
  `_TONE_SYNONYMS` (ironic → sarcastic) before falling to neutral.

## Exposure / deployment

- The client derives the websocket scheme from `location.protocol`
  (`server/static/index.html`). **Never hardcode `ws://`** -- browsers block it as
  mixed content from an HTTPS page, so it works on localhost and then fails
  silently and totally behind any TLS terminator. The ESP32 needs `wss://` too.
- `AUTH_TOKEN` unset means **any first frame is accepted**, and empty/whitespace
  now normalises to unset (it used to report "auth enabled" while the empty
  string was the valid credential). Fine on loopback; on any other bind the
  server **refuses to start** unless `ALLOW_NO_AUTH=1`. `HOST` defaults to
  `127.0.0.1` — cloudflared connects over loopback, so binding `0.0.0.0` only
  adds a LAN entrance that bypasses the tunnel.
- **The exposure limits in `config.py` are load-bearing, not decoration.**
  `WS_MAX_MESSAGE_BYTES`, `MAX_INFLIGHT_UTTERANCES`, `MAX_CONNECTIONS`,
  `WS_AUTH_TIMEOUT_S`, `WS_IDLE_CLOSE_S` and `ws_per_message_deflate=False`
  each close a measured amplification: one 16 MiB frame segmented into ~500
  utterances, and PCM deflates ~21:1, so ~1.6 KB on the wire bought a full
  Whisper+SER+LLM job. A conforming client never approaches any of them, so the
  ESP32 contract is unaffected — but don't "simplify" them away.
- The **transcript log** (`server/translog.py`) is the second retention path and
  the one the brief did NOT ask for, so it is **off by default, field by field**:
  the dashboard's "Transcript log" card has a master switch plus `audio`,
  `transcript`, `result`, `voice`, `speaker`, `timing` checkboxes
  (`GET/POST /api/translog`), runtime only, not persisted -- a restart returns to
  `TRANSCRIPT_LOG_ENABLED` / `TRANSCRIPT_LOG_FIELDS`. Rows go to
  `logs/transcripts/<day>.jsonl`; with `audio` on, the WAV goes beside it under a
  rolling cap (`TRANSCRIPT_LOG_MAX_AUDIO`, oldest evicted). Its purpose is scoring ASR
  on this mic and room: `python -m server.translog --export DIR` writes an
  `experiments/asr/`-shaped set (needs `audio` + `transcript` logged) whose `.txt`
  drafts must be corrected by ear before `eval.asr_eval --dir` trusts them. `logs/`
  is gitignored. Never raises, fire-and-forget from `main.py`, never on the read's
  latency path.
- Clips are the one sanctioned retention of third-party audio: explicit
  user action, visible confirmation, one clip (a bounded id range) per save. `CLIPS_MAX_FILES`
  is load-bearing (a hostile client through the tunnel could otherwise fill
  the disk one `save` at a time); over it the save fails rather than evicting.
  `clips.py` validates the id's shape before touching the filesystem -- that
  regex is the whole path-traversal defence for `/api/clips/{id}/audio`. The
  CSP's `media-src 'self' blob:` exists so the dashboard can play a clip it
  fetched with the auth header, keeping the token out of an `<audio src>` URL.
- Transcripts reach the LLM through `interpreter._fence()`, which collapses
  whitespace so a spoken line cannot forge a new prompt section. Keep it
  **mechanical**: three prompt-text variants were measured and all scored worse
  (see the `_fence` docstring for the numbers). `SYSTEM_PROMPT` is byte-identical
  to its pre-hardening form on purpose.
- **The firmware is public, the server is not.** `Parth12358/emtext-firmware` is a
  generated, read-only mirror. One workflow, `.github/workflows/firmware.yml`: `main` is the dev
  branch and runs nothing; a PR into `prod` gets the compile check; merging it (a push to
  **`prod`**) compiles, mirrors and releases
  (`fw-v<firmware/VERSION>`, or a build-stamped tag if that one exists). The public tree
  comes from `.github/scripts/stage_firmware.sh` (sketch, the three firmware docs,
  `firmware/mirror/README.md`, `VERSION`) force-pushed as one orphan commit. Never edit the
  mirror; never let anything from `server/` into the staging script; `secrets.h` stays
  gitignored AND is excluded by the script. Needs `FIRMWARE_MIRROR_TOKEN` (fine-grained
  PAT: Contents + Workflows read/write). Details: `docs/guides/firmware.md`, "Releases".
- Cloudflare **quick tunnel** is the documented path out (`docs/guides/tunnel.md`):
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

## Dashboard / metrics

- `server/metrics.py` is memory-only and bounded (rolling deques). No database,
  per the brief. Nothing in it raises -- telemetry must never take the server down.
- `server/dashboard.py` holds the `/api/*` routes so `main.py` stays websocket
  wiring. All routes are token-gated on the SAME rule as `/stream` (enforced only
  when `AUTH_TOKEN` is set), so there is one auth rule in the project, not two.
- GPU stats come from Windows per-engine perf counters (vendor-neutral; there is
  no `nvidia-smi` for an Arc). A query costs ~2.8s and a PowerShell launch, so it is
  polled on a daemon thread and served from cache -- never query it in a request
  handler. The poller runs only while `/api/stats` has been called in the last 60 s
  (`metrics.gpu_wanted()`); polling unconditionally cost ~2.4 CPU-hours a day.
- `POST /api/model` switches the interpreter live and deliberately does NOT
  persist. It works because `Interpreter` reads `config.OLLAMA_MODEL` at call
  time; keep that property.
- Utterance tasks are fire-and-forget, so `_log_task_error` is attached as a done
  callback. Without it a raising task is completely silent and presents as "no
  transcript ever appears" -- keep it.

## Scope limits (from the brief — don't add unprompted)

No auth beyond the token, no database, no Docker, no `tests/` folder.

## Style

- **Commits are attributed to the author only.** No `Co-Authored-By` or other AI attribution
  trailers on commits, PRs, release notes or mirror commits; the public firmware repo's
  contributors must be people. The mirror workflow authors its commit as the shipping commit's
  author for the same reason.
Type hints, docstrings that explain the **why** of non-obvious choices. The
segmenter state machine, the executor pattern, and the worklet/downsampling are commented
generously on purpose (the author is learning from them) — preserve that when editing.
