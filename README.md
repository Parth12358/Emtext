# emtext

A live conversation interpreter. It transcribes speech as you hear it and, for
each sentence, adds a one-line read of the *emotional subtext* — is this
positive, negative, sarcastic, passive-aggressive, or just literal? It's built
for a neurodivergent listener who wants tone made explicit.

A browser page streams your microphone to a local server over a websocket. The
server slices the audio into utterances and then, for each one, works out both
*what* was said (Whisper, on CPU) and *how it sounded* (a speech emotion model,
also on CPU) before asking a local LLM (via Ollama, on the GPU) what the line
really means. An ESP32 pendant is now being built to replace the browser over the
exact same wire protocol — see **[docs/guides/firmware.md](docs/guides/firmware.md)**.

```
mic ─► browser (AudioWorklet, 16k int16) ─websocket─► server
                                                        ├─ segmenter  (VAD)
                                                        ├─ transcriber (faster-whisper, CPU) ─┐
                                                        ├─ ser         (emotion2vec, CPU) ────┤ (concurrent)
                                                        └─ interpreter (Ollama LLM, GPU) ◄────┘
```

## Two halves: server + pendant

emtext is two codebases in one repo, joined only by the websocket **wire protocol**:

- **The server** — everything documented in *this* README: the browser test client, the
  segmenter → Whisper + SER → Ollama pipeline, the dashboard, the eval suite, and the
  Cloudflare tunnel. Runs on a workstation with a GPU.
- **The pendant firmware** — `firmware/`, documented in **[docs/guides/firmware.md](docs/guides/firmware.md)**.
  Public mirror and flashable releases: **[Parth12358/emtext-firmware](https://github.com/Parth12358/emtext-firmware)**
  (this repository is private; the firmware is not).
  An ESP32-S3 (M5StickS3) worn as a pendant that replaces the browser: a mic + a small
  display that streams 16 kHz PCM over the same `wss://…/stream` contract and renders the
  server's `read` frames. All transcription/interpretation stays on the server — the device
  is just ears and a screen.

The wire protocol is the contract between them (raw PCM 16 kHz mono int16 in;
`ready`/`status`/`utterance`/`read`/`saved` JSON out, plus keepalive pings), so the
two are developed independently.
**Firmware progress:** board bring-up, config/NVS + serial console, the display + controls
UX, mic capture with an energy gate, connectivity (authenticated `wss://` on a dedicated core
so audio capture never stalls), and — the milestone — **the full loop working on the device**: the pendant streams its mic to
this server, survives a ~3 s WiFi drop with no audio loss, and **shows the tone read on its own
screen** (Stages 0–6 of 9). A **phone-based Wi-Fi setup portal** is also done — toggle a WPA2
setup hotspot from the device and reconfigure Wi-Fi/host/token from a captive page that pops up
automatically. **Hold-to-record clips** are also done — hold the button to save a moment (an
utterance-id range) to the server for later review on the dashboard. **Power management (Stage 8)**
is now built too — a staged cycle of *awake → idle → low-power standby (Wi-Fi kept alive, wakes on a
button) → full power off*, plus visible boot stages and a low-battery warning (no ESP deep sleep, and
no firmware power-off gesture: on the M5StickS3 the PWR button is a hardware off). Remaining:
on-hardware validation of Stage 8 (incl. the ≥2.5 h runtime target) and acceptance/compliance
(Stage 9). (Audio cues, the former Stage 7, are dropped — the pendant
has no speaker; the emotional signal is visual only.) Full module architecture and the
staged plan live in [docs/guides/firmware.md](docs/guides/firmware.md).

Everything below documents the **server half**.

## Where this is right now

Working end to end, on a laptop and on a phone over the internet. Every number
below is measured on this hardware (Ryzen 5 9600X, 6c/12t; Intel Arc B580, 12 GB
VRAM), not estimated.

**Stack as configured**

| stage | model | device | cost per utterance |
|---|---|---|---|
| VAD | energy segmenter | — | negligible |
| transcription | Parakeet TDT 0.6B v3 (int8, sherpa-onnx) | CPU | ~0.10 s |
| speech emotion | `emotion2vec_plus_base` | CPU | ~0.12 s |
| interpretation | `qwen3:14b` via Ollama | GPU | ~0.89 s |

**Stop talking → read on screen: ~1.94 s**, of which 0.65 s is the VAD's
end-of-speech wait (`END_SILENCE_MS`) and the rest is compute. It was 4.63 s
before the SER swap.

**What's built**

- Live app at `/`, streaming mic → transcript → tone read, with the voice label,
  a speaker chip on lines that are yours or hold a turn change, a **save** link per
  read (the browser twin of the pendant's hold-to-record clip), and a link to
  `/enroll.html` to enrol your own voice so your lines are never interpreted.
- **Remote access** over a Cloudflare quick tunnel — one command, no account, no
  domain. Also the only way to test a microphone on a phone, since browsers
  require HTTPS for `getUserMedia`.
- **`/dashboard.html`** — live CPU/RAM/GPU, per-stage latency percentiles, tone,
  voice-emotion and speaker distributions, connected clients, recent utterances,
  runtime switching of the interpreter model, and the **saved clips** review list
  with playback and delete.
- **`/remote.html`** — connection diagnostics plus a live VAD strip showing which
  audio frames cleared `SPEECH_RMS`, which is how segmentation gets tuned.
- **A transcript log** (`logs/transcripts/`, gitignored), **off by default**: the
  dashboard's "Transcript log" card switches it on for a session and picks what to
  keep per utterance -- audio (the WAV), transcription, result (tone + read), voice,
  speaker id, timings -- as one JSON line each, with a rolling cap on the audio. It
  exists so the ASR choice can finally be made on this microphone and room instead
  of synthetic speech: `python -m server.translog --export DIR` writes an
  `experiments/asr/`-style set whose draft `.txt` references you correct by ear,
  then `python -m eval.asr_eval --dir DIR` scores every backend on it. Ticking
  `audio` retains other people's speech; the card says so.
- **An eval suite** (`python -m eval.run_all`) scoring SER, ASR, the interpreter
  LLM, and the whole pipeline against RAVDESS. Results are committed under
  `eval/results/`.

**Why these models** — both defaults were chosen on measurements, not vibes:

- `emotion2vec` over MERaLiON: **86% vs 61.3%** accuracy on 1440 RAVDESS clips,
  at **0.17 s vs 2.72 s**. MERaLiON's cost is also *flat* (it pads every clip to
  30 s), which put SER below real time and made sustained speech build a backlog.
- `qwen3:14b` over `gemma3:12b`: **81% vs 43%** tone accuracy on 280 clips where
  the words are deliberately neutral, so all signal is in the voice. gemma
  answers `neutral` 68% of the time there — it largely ignores a bare emotion
  label. (It scores 93% on `eval/tone_cases.jsonl`, where the hint carries
  *numeric* valence. Different inputs, both results real.)

**Known rough edges** — see [docs/status/TODO.md](docs/status/TODO.md) for the detail:

- **Segmentation is unverified on the pendant's mic.** The speech threshold now
  tracks the room (`noise_floor × NOISE_MARGIN`, clamped to
  `[SPEECH_RMS_MIN, SPEECH_RMS]`) and segments 10 of 10 RAVDESS test clips where a
  fixed threshold dropped 5, but nobody has watched it in a real room yet. The VAD
  strip in `/remote.html` shows the live floor and threshold for exactly that.
- **The ASR default was chosen on synthetic speech.** Parakeet v3 replaced Whisper
  `base` on the SAPI set (the same 6.0% WER at under half the cost, plus 25
  languages), but that set cannot see noise robustness; a real-microphone
  comparison is still owed.
- The current SER backend supplies **no valence**; arousal and dominance are
  derived from its class distribution, and the mismatch rule is built on those
  two axes (valence is left to the LLM, which reads it from the words).

## Why the voice matters

The transcript for a genuine "oh, wonderful" and a bitter one is identical, so
text alone cannot tell them apart. The speech emotion recognition (SER) stage
listens to the delivery and hands the interpreter a second, independent signal.
What the interpreter actually keys on is the **mismatch** between the two:

| words | voice | usual meaning |
|-------|-------|---------------|
| positive | sounds negative | sarcasm, or masking that they're upset |
| negative | sounds positive | teasing, joking, banter |
| agree | agree | take it literally |

Measured on 280 RAVDESS clips where the spoken words are deliberately neutral, so
every bit of signal is in the delivery: `qwen3:14b` reaches 81% tone accuracy and
48% voice sensitivity — the gap between how often it calls a happy voice positive
versus an angry one. A model ignoring the voice field scores ~0 there.

Note the default backend (emotion2vec) has no dimensional head: `valence` is
`null` by design, and `arousal` / `dominance` are **derived** as the
anchor-weighted mean over its 7-class distribution (`dims_source: "derived"`).
The prompt's mismatch rule is stated on arousal + dominance as well as on
valence, so it works with either backend. See
[Choosing a SER model](#choosing-a-ser-model).

**SER labels are hints, not truth.** The model is guessing from acoustics and it
is noisy — short utterances, unfamiliar accents and cheap microphones all
degrade it. Each reading carries a confidence, and below ~0.4 the interpreter is
instructed to lean on the words and treat the voice as weak evidence at most. A
voice label alone never turns a plain sentence into sarcasm. Don't treat a
displayed emotion as a fact about how the speaker actually feels.

## Requirements

- Python 3 (any reasonably recent version; whatever `python` points at is fine)
- [Ollama](https://ollama.com) running locally
- A microphone, opened from **localhost or over HTTPS** (browsers only allow
  `getUserMedia` on secure origins — `http://localhost:8000` counts as secure)

## Setup

From the project root (the directory that contains `server/`):

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
# Linux / macOS
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Then pull the interpreter model (the default; override with `OLLAMA_MODEL`):

```
ollama pull qwen3:14b
```

The first run also downloads the models, which are cached after that:

| model | size | notes |
|-------|------|-------|
| Parakeet TDT 0.6B v3 (sherpa-onnx, int8) | ~640 MB | transcription, CPU. Fetched into `models/asr/` on first start; `ASR_MODEL=whisper:base` restores the old default |
| `emotion2vec/emotion2vec_plus_base` | ~1.07 GB | speech emotion, CPU. Two thirds of that file is optimizer state inference never touches; `python -m eval.slim_ser_ckpt` writes a 355 MB copy under `models/` that `SER_MODEL_DIR` picks up automatically and saves ~1.4 GB of RAM |
| CAM++ (`cam++`, via funasr) | ~27 MB | speaker id, CPU. Skipped with `SPEAKER_ENABLED=0` |

That download happens the first time the server starts. To skip it entirely,
start with `SER_ENABLED=0` (see [Speech emotion
recognition](#speech-emotion-recognition)) — the app runs exactly as it did
before SER existed, just without the voice line.

**On Windows, set `HF_HUB_DISABLE_SYMLINKS=1` before the first run.** The Hugging
Face cache uses symlinks by default, which need Developer Mode or an admin
shell; without it some downloads fail outright with
`WinError 1314: A required privilege is not held by the client`.

```powershell
$env:HF_HUB_DISABLE_SYMLINKS = "1"
```

Note the pinned `transformers<5` in `requirements.txt`. It only matters for the
optional MERaLiON backend, which ships custom model code that builds layers with
`torch.logspace(...).item()` at construction time; transformers 5 builds models
on the meta device, where that raises. The default emotion2vec backend is
unaffected.

## Run

Start Ollama (if it isn't already running as a service):

```
ollama serve
```

Start the server from the project root (the directory that contains `server/`):

```
python -m server.main
```

Then open <http://localhost:8000/?token=dev>, press **Start**, allow microphone
access, and talk. Transcripts appear newest-first; a tone-colored read shows up
beneath each one once the LLM responds.

- Green = positive · Red = negative · Grey = neutral · Purple = sarcastic ·
  Amber = mixed
- If Ollama is down, transcripts still appear with the read
  `(interpreter offline)` — the app degrades instead of breaking.

## Wire protocol

The browser client and the pendant firmware both speak this:

1. Connect to `ws://host:8000/stream` (`wss://` through the tunnel).
2. Send **one TEXT frame**: the auth token. (Any value is accepted unless the
   `AUTH_TOKEN` env var is set on the server.) It must arrive within
   `WS_AUTH_TIMEOUT_S` (5 s) and be at most 512 characters, and it must be a
   TEXT frame; otherwise the server closes with code `1008`.
3. Then send **BINARY frames**: raw PCM, 16 kHz, mono, int16 little-endian. A
   frame is capped at `WS_MAX_MESSAGE_BYTES` (64 KiB, two seconds of audio) and
   per-message compression is refused; a real client sends 20-100 ms per frame.

The server replies with TEXT JSON frames:

```jsonc
{"type":"ready"}
{"type":"status","state":"listening|heard|thinking"}
{"type":"utterance","id":1,"transcript":"..."}
{"type":"read","id":1,"emotion":{"label":"neutral|happy|sad|angry|fearful|disgusted|surprised","voice":"...","adjusted":false},
 "tone":"positive|negative|neutral|sarcastic|mixed","read":"one line"}
{"type":"ping","t":1789794138.7}          // keepalive after WS_IDLE_PING_S (30 s) with no audio
{"type":"pong","t":...}                   // reply to a client ping, `t` echoed untouched
{"type":"saved","id":2,"ok":true,"clip":"20260918T230722_1_eb19"}   // reply to a save
{"type":"saved","id":2,"ok":false,"error":"no audio"}
```

Two **client → server** TEXT frames are accepted between the PCM frames. Any
other JSON type is ignored, never fatal -- the protocol is additive:

```jsonc
{"type":"ping","t":<your timestamp>}      // answered by pong; use it for RTT and to hold an idle socket open
{"type":"save","from":3,"to":5}           // keep a clip: the utterances with ids 3..5 (inclusive)
{"type":"save","id":3}                    // older spelling, same as from == to
```

`id` is the per-connection utterance counter (it restarts at 1 on every
connection), so a `save` only makes sense for ids from the current socket. The
server keeps the last `CLIP_RETENTION_N` (8) utterances or `CLIP_RETENTION_S`
(45 s) in memory per connection; a `save` naming something still retained
concatenates that audio into one WAV under `clips/` with a JSON sidecar holding
the transcripts, tones, reads, voice and speaker labels, and replies `saved`
with `ok:true` and the clip's id. Nothing else is ever written to disk. Failure
replies carry `error`: `unknown id` (non-integer or inverted range), `no audio`
(nothing in that span is retained -- a stale id after a reconnect, or a hold
that ended before the pipeline produced anything), `clip store full` (over
`CLIPS_MAX_FILES`, delete some from the dashboard), `clips disabled`, or
`write failed`. The pendant sends the range of everything it heard while its
button was held; the browser's save link sends a one-utterance range.

The server closes a connection that has sent no audio for `WS_IDLE_CLOSE_S`
(900 s) with code `1000`, and keeps at most `MAX_INFLIGHT_UTTERANCES` (3) per
connection in the pipeline -- extra ones are dropped and counted, never queued.
Conversational speech never reaches either limit.

Connect with `?vad=1` and the server also streams `vad` (~10/s: current RMS,
threshold, state) and `vad_close` (why each utterance ended) frames for the
diagnostics page. Normal clients and the pendant never ask for these.

The headline of a `read` is `emotion`: the voice model's label, in its own
seven-word vocabulary, **as adjusted by the interpreter**. The SER is the
primary source; the LLM starts from its label and moves it only when the words
make it untenable (`adjusted: true`, with `voice` holding the label it started
from). With SER off, `voice` is null and the label comes from the words alone;
with the interpreter offline, the voice label ships untouched. `tone` stays for
what an emotion cannot carry -- `sarcastic` and `mixed` -- and for older clients.

The `read` frame also carries one **optional** field, `voice`, present only when SER
produced a result (so it is absent when `SER_ENABLED=0`, when the model failed
to load, or when that utterance couldn't be scored):

```jsonc
{"type":"read","id":1,"emotion":{"label":"angry","voice":"angry","adjusted":false},
 "tone":"sarcastic","read":"one line",
 "voice":{"emotion":"angry","confidence":0.77,"valence":0.21,"arousal":0.65,"dominance":0.7},
 "speaker":{"label":"other","score":0.0},
 "confidence":{"score":0.47,"level":"medium","reasons":["too short to read the voice"]}}
```

`valence` runs 0 (negative) to 1 (positive); `arousal` runs 0 (calm) to 1
(intense); `voice.confidence` is the emotion model's own probability for its
label. `confidence` is **how sure the server is of the whole read**
(`server/confidence.py`): a 0-1 score, a `level` clients render as weight (`high`
as normal, `medium` de-emphasised, `low` suppressed behind a "not sure" marker in
the browser and drawn faint on the pendant), and plain-language `reasons` for
anything below high. The score starts from the interpreter's own token
probability for the emotion it chose and is knocked down for a short utterance, an
unclear voice, a doubtful transcript, two voices in one line, or a `mixed` tone;
`(interpreter offline)` is always score 0. Clients must treat `voice`, `speaker`
and `confidence` — and each field inside them — as optional:
existing clients and firmware that ignore the key keep working unchanged, which
is why it was added this way rather than as a new frame type.

Both `utterance` and `read` carry a second **optional** field, `speaker`,
present once the listener has enrolled their voice at `/enroll.html`
(`server/speaker.py`):

```jsonc
{"type":"utterance","id":2,"transcript":"...","speaker":{"label":"user","score":1.0}}
```

`label` is `user` (the listener's own voice), `other`, `mixed` (the utterance
holds a turn change -- the segmenter glues a fast reply onto the question it
answers) or `unknown`; `score` is the fraction of the utterance's 1 s windows
that matched the enrolled voice. **A `user` utterance gets no `read` frame at
all**: it is transcribed because it is what the other person is replying to,
but never interpreted -- the app must not explain the listener's own feelings
back to them. Clients must not wait for a read on a `user` utterance, and, as
with `voice`, must keep working when `speaker` is absent.

Reads arrive **out of order** relative to transcripts: each utterance is
processed in its own task, so a short line can overtake a long one. Correlate on
`id`, never on arrival order.

## Tuning the VAD

The segmenter decides where sentences start and stop using simple frame energy.
Every knob is in `server/config.py` and can be overridden with an environment
variable of the same name. The ones you'll actually touch:

| Env var             | Default | What it does |
|---------------------|---------|--------------|
| `SPEECH_RMS`        | 500     | Ceiling of the adaptive speech threshold (int16 RMS); with `VAD_ADAPTIVE=0` it is the constant threshold. **Raise it** if a noisy room keeps triggering; the adaptive floor already handles quiet speech, so lower `SPEECH_RMS_MIN` rather than this if quiet speech is still missed. |
| `END_SILENCE_MS`    | 650     | How much trailing quiet ends an utterance. Lower = snappier but may split sentences at pauses; higher = fewer splits but more lag. |
| `MIN_UTTERANCE_MS`  | 350     | Utterances with less voiced audio than this are discarded as blips (coughs, taps). |
| `MAX_UTTERANCE_MS`  | 15000   | Hard cap; force-cuts run-on audio so the buffer never grows without bound. |
| `PRE_ROLL_MS`       | 240     | Audio kept from just *before* speech starts, so the first sound isn't clipped. |
| `VAD_ADAPTIVE`      | `true`  | Track the room: the threshold is `noise_floor × NOISE_MARGIN`, clamped to `[SPEECH_RMS_MIN, SPEECH_RMS]`, with the floor learned from non-speech frames only. `0` restores the constant `SPEECH_RMS`. |
| `SPEECH_RMS_MIN`    | 150     | Floor of the adaptive threshold. Digital silence settles here; on RAVDESS that took VAD drops from 5/10 to 1/10 (10/10 now segment). |
| `NOISE_MARGIN`      | 3.0     | How far above the room a frame must be to count as speech (~+9.5 dB). |
| `NOISE_FLOOR_RISE` / `NOISE_FLOOR_FALL` | 0.02 / 0.3 | EMA rates for the floor: rises slowly (a cough is not a new room), falls fast (a quiet room is trusted within a few frames). |

Example — retune for a loud room and pull a bigger Whisper model:

```powershell
# Windows PowerShell -- env vars are set as their own statements
$env:SPEECH_RMS = 900; $env:WHISPER_MODEL = "small"; python -m server.main
```

```bash
# Linux / macOS
SPEECH_RMS=900 WHISPER_MODEL=small python -m server.main
```

Check your tuning with the segmenter's built-in self-test (synthetic audio, no
mic needed):

```
python -m server.segmenter
```

### Every other knob

All of these live in `server/config.py`, each with a comment on why its default
is what it is. Override any of them with an environment variable of the same
name.

| group | env vars | notes |
|---|---|---|
| server | `PORT` (8000), `HOST` (`127.0.0.1`), `AUTH_TOKEN`, `ALLOW_NO_AUTH` | `HOST` is loopback on purpose: the tunnel reaches it there, and `0.0.0.0` adds a LAN entrance that bypasses the tunnel. With `AUTH_TOKEN` unset the server **refuses to start** on any non-loopback bind unless `ALLOW_NO_AUTH=1`. |
| audio | `SAMPLE_RATE` (16000), `FRAME_MS` (30) | fixed by the wire protocol; named, not tunable in practice |
| whisper | `WHISPER_MODEL` (`base`), `WHISPER_DEVICE` (`cpu`), `WHISPER_COMPUTE_TYPE` (`int8`), `WHISPER_LANGUAGE` (`en`) | |
| interpreter | `OLLAMA_URL`, `OLLAMA_MODEL` (`qwen3:14b`), `CONTEXT_LINES` (12), `OLLAMA_TEMPERATURE` (0.2), `OLLAMA_NUM_PREDICT` (80), `OLLAMA_TIMEOUT_S` (30) | |
| SER | `SER_ENABLED`, `SER_MODEL`, `SER_BACKEND`, `SER_DEVICE`, `SER_MODEL_DIR`, `SER_REVISION`, `SER_MIN_CONFIDENCE`, `SER_TORCH_THREADS`, `VALENCE_LOW/HIGH` (0.4/0.6), `AROUSAL_LOW/HIGH` (0.4/0.6) | see [Speech emotion recognition](#speech-emotion-recognition). The gloss thresholds only matter on the MERaLiON backend; emotion2vec has no dimensions. |
| speaker id | `SPEAKER_ENABLED` (true), `SPEAKER_MODEL` (`cam++`), `SPEAKER_DEVICE` (`cpu`), `SPEAKER_PROFILE_PATH` (`models/speaker_user.json`), `SPEAKER_MATCH_THRESHOLD` (0.45), `SPEAKER_USER_FRAC_LO/HI` (0.15/0.75), `SPEAKER_WINDOW_S` (1.0), `SPEAKER_HOP_S` (0.5), `SPEAKER_MIN_ENROLL` (3), `ENROLL_MAX_BYTES` (4 MiB) | the profile is embeddings only, never audio; `DELETE /api/speaker` forgets it |
| clips | `CLIPS_ENABLED` (true), `CLIPS_DIR` (`clips`), `CLIP_RETENTION_N` (8), `CLIP_RETENTION_S` (45), `CLIPS_MAX_FILES` (200) | the retention ring is memory-only and dies with the connection; `CLIPS_MAX_FILES` stops a hostile client filling the disk one save at a time |
| keepalive | `WS_IDLE_PING_S` (30), `WS_KEEPALIVE_CHECK_S` (5), `VAD_TELEMETRY_INTERVAL_S` (0.1) | Cloudflare culls a proxied websocket after ~100 s idle |
| exposure limits | `WS_AUTH_TIMEOUT_S` (5), `WS_MAX_MESSAGE_BYTES` (65536), `MAX_CONNECTIONS` (32), `MAX_INFLIGHT_UTTERANCES` (3), `WS_IDLE_CLOSE_S` (900), `WS_FLUSH_TIMEOUT_S` (8), `MODEL_SWITCH_COOLDOWN_S` (10) | each closes a measured amplification; a conforming client never approaches any of them |

## Testing without a microphone

The browser client needs a real mic and a user gesture, which makes it awkward
to check a change. `experiments/` has a headless path that speaks the same wire
protocol the pendant does:

```powershell
# 1. synthesize test speech (Windows SAPI -> 16 kHz mono WAV, the wire format)
powershell -NoProfile -ExecutionPolicy Bypass -File experiments\make_speech.ps1

# 2. stream it to a running server and print the JSON frames coming back
python experiments\wav_client.py
```

`wav_client.py` takes an optional WAV path plus `--url` and `--token`, and
`--fast` to skip real-time pacing. Two other generators produce audio aimed at
SER specifically: `make_prosody.ps1` (one sentence at three deliveries, to check
the voice signal actually moves) and `make_mismatch.ps1` (positive words said
flatly — the sarcasm case). `make_asr_set.ps1` builds the known-text set the
`asr` eval stage scores against, and `experiments/spk/` holds the clips
`eval.spk_eval --real` uses.

To score a single WAV and print the raw SER dict without running the server:

```
python -m server.ser experiments/speech.wav
```

Note that SAPI is a flat, unemotional speaker, so it's a good test of the
plumbing and a poor test of emotional accuracy. Use real recorded speech to
judge whether the reads are any good.

## Speech emotion recognition

SER reads *how* a line sounded and passes it to the interpreter alongside the
transcript. It runs on CPU, like Whisper — the GPU stays reserved for the LLM.

| Env var              | Default | What it does |
|----------------------|---------|--------------|
| `SER_ENABLED`        | `true`  | Set to `0` to turn SER off: skips the model download and ~1.1 GB of RAM (it is the largest single thing in the process), drops the `voice` field, and removes the added latency below. |
| `SER_MODEL`          | `emotion2vec/emotion2vec_plus_base` | Model id — see the table below. |
| `SER_BACKEND`        | `auto`  | `auto` infers from the model name; force with `meralion` or `emotion2vec`. |
| `SER_DEVICE`         | `cpu`   | Leave it — the Arc B580 is for Ollama. |
| `SER_MIN_CONFIDENCE` | `0.4`   | Below this the interpreter is told the voice signal is weak and to trust the words instead, and the read's confidence is knocked down ("voice unclear"). The prompt text hardcodes "about 0.4"; change both together. |
| `CONFIDENCE_HIGH` / `CONFIDENCE_LOW` | `0.9` / `0.6` | Level cut-offs for the read's confidence: at or above HIGH renders as normal, below LOW is suppressed. Measured on 280 RAVDESS clips: high 97% accurate, medium 83%, low 17%. Set them from `python -m eval.confidence_eval`, not by hand. |
| `CONFIDENCE_PLATT_A` / `_B` | `1.0` / `0.0` | Platt scaling of the interpreter's tone probability. The fitted values gained nothing on RAVDESS (ECE 0.187 to 0.186), so the identity is shipped. |
| `CONFIDENCE_PENALTY` | `0.7` | Multiplier applied per weak input (short utterance, unclear voice, doubtful transcript, two voices, mixed tone). |
| `CONFIDENCE_MIN_VOICE_S` / `CONFIDENCE_MIN_ASR` | `1.5` / `0.35` | Voiced seconds below which the voice reading is distrusted; transcriber confidence below which the words are (0.35 is Whisper's own "failed segment" line). |
| `OLLAMA_LOGPROBS` | `1` | Ask Ollama for token probabilities (needs 0.12.11+); off, the confidence score loses its base signal and says so. |
| `SER_TORCH_THREADS`  | `0`     | `0` = torch's default. Raising it to your logical core count speeds up one SER pass but competes with Whisper; measure before changing. |
| `SER_MODEL_DIR`      | `models/emotion2vec_plus_base_slim` | If this directory exists, load the slimmed checkpoint from it instead of the ModelScope cache. Build it with `python -m eval.slim_ser_ckpt`; worth ~1.4 GB of RAM, bit-identical output. |
| `SER_REVISION`       | *(empty)* | Pin the MERaLiON backend to a hub commit (it executes downloaded code). Empty = the vetted revision in `ser.py`. |

### Choosing a SER model

Two backends sit behind the same `ser.analyze()` call, so swapping is a config
change and nothing else in the codebase notices:

| `SER_MODEL` | size | classes | valence/arousal | needs |
|---|---|---|---|---|
| `emotion2vec/emotion2vec_plus_base` *(default)* | ~900 MB | 9 → 7 | no | `funasr` |
| `emotion2vec/emotion2vec_plus_large` | ~1.2 GB | 9 → 7 | no | `funasr` |
| `MERaLiON/MERaLiON-SER-v1` | ~2.9 GB | 7 | yes | `transformers<5` |

**Measured over all 1440 RAVDESS clips, emotion2vec beats MERaLiON on both axes:**
86% accuracy vs 61.3% macro recall, at ~0.17 s vs 2.72 s per utterance. MERaLiON's
cost is also *flat* — it pads every clip to 30 s, so a 1 s utterance costs the same
2.7 s as a 14 s one, which put SER below real time and made sustained speech build
a backlog.

MERaLiON is the only backend with a dimensional head, but its valence measured a
**+0.085** pleasant/unpleasant separation on this data — too compressed to be
useful, and against the default gloss thresholds every clip in 1440 was described
to the LLM as "negative". If you enable it, run `--profile-valence` first and set
`VALENCE_LOW`/`VALENCE_HIGH` to match.

The emotion2vec models are far lighter and have **no dimensional head**. Rather
than fabricate valence, `ser.py` derives `arousal` and `dominance` from the
class distribution (per-emotion anchors, probability-weighted) and leaves
`valence` at `null`: acoustic valence is largely implicit linguistic content
(see `docs/status/TODO.md`), so it would agree with the transcript exactly where the
mismatch rule needs an independent channel. The LLM reads valence from the
words; the voice contributes the two axes the words cannot. Profile the derived
range with `python -m eval.ser_eval --profile-valence` before changing
`AROUSAL_*` / `DOMINANCE_*`.

Their 9 classes are normalised onto the same 7 the app uses, with `other` and
`unknown` folded into `neutral` — those mean "the model didn't commit", and
inventing an emotion from a non-answer is the exact false positive to avoid.
They're published on Hugging Face as `emotion2vec/…` and fetched by FunASR from
ModelScope as `iic/…`; `ser.py` accepts either spelling.

Compare them yourself on the same audio:

```
python -m eval.bench --ser-models
```

If the model fails to load for any reason — missing weights, no network, a
dependency conflict — the server logs a warning and runs with SER disabled. It
never crashes over it. Check which mode you're in at startup:

```
INFO:     __main__: speech emotion recognition enabled (emotion2vec/emotion2vec_plus_base)
```

### Latency, measured

Reproduce any of this with the benchmark — run it after changing a model, a
compute type, or the hardware split:

```
python -m eval.bench              # all three models
python -m eval.bench --skip-llm   # audio stages only (no Ollama needed)
```

Measured on a 12-core CPU + Arc B580, `qwen3:14b`. Transcription was Whisper
`base` when these were taken; `parakeet:v3`, the default since 2026-09-22,
measured ~0.10 s per utterance on the same set:

| stage | model | device | per utterance |
|-------|-------|--------|---------------|
| transcription | Whisper `base` (faster-whisper, int8) | CPU | ~0.28 s |
| speech emotion | `emotion2vec/emotion2vec_plus_base` | CPU | ~0.12 s |
| speaker id | CAM++ (3-4 window embeddings) | CPU | ~0.08 s mean, 0.25 s p95 |
| whisper + SER + speaker, gathered | | CPU | ~0.30 s |
| interpretation | `qwen3:14b` (Ollama) | GPU | ~0.89 s |

**Whisper decides how soon a transcript appears, and the LLM decides the read.**
The three audio stages are gathered onto the executor rather than sequenced, so
an utterance costs about `max(whisper, ser, speaker)` rather than their sum, and
on the default backend that maximum is Whisper.

It was not always so. The original SER backend, MERaLiON, cost ~2.7-3.3 s per
utterance **flat** -- a 1 s and a 14 s utterance took the same time, because its
Whisper-derived feature extractor pads every input to a fixed 30 s window and
the encoder always processes the whole thing -- which put SER below real time on
sustained speech and made it the stage that set read latency. Switching to
emotion2vec removed that ceiling (see the SER section). MERaLiON is still
available via `SER_MODEL`, and that flat cost comes back with it.

### End-to-end: stop talking -> read on screen

`eval/bench.py` times stages in isolation. `eval/latency.py` measures what a
user actually feels, over the real websocket, with **every model warmed up
before the timer starts** so no weight-loading is counted:

```
python -m eval.latency                     # full run (needs the server up)
python -m eval.latency --model qwen3:8b    # stage breakdown for another LLM
python -m eval.latency --skip-wire         # no server needed
```

| configuration | stop -> read |
|---|---|
| **current defaults** (emotion2vec + qwen3:14b) | **1.94 s** |
| old defaults (MERaLiON + gemma3:12b) | 4.63 s |
| SER off entirely | 1.82 s |

Switching the SER backend took 2.7 s out of the budget. SER is now 0.12 s against
Whisper's 0.24 s, so **SER is no longer the bottleneck** — the LLM is, at 0.89 s.

Budget for the default configuration:

| | |
|---|---|
| VAD end-of-speech wait (`END_SILENCE_MS`) | 0.65 s — pure waiting, a tuning choice |
| Whisper + SER, gathered | 0.30 s |
| LLM interpret | 0.89 s |
| **total** | **1.94 s** |

The audio stages are now cheap enough that the two remaining levers are the LLM
(0.89 s) and `END_SILENCE_MS` (0.65 s of pure waiting). `SER_ENABLED=0` saves only
0.06 s now — it is no longer worth turning off.

Note `latency.py` trims trailing silence from its test clips before sending. It
has to: SAPI writes seconds of silence into every file, which lets the server's
VAD close the utterance and start working *before* the clock starts — that
produced a wire latency lower than the compute it contains, which is the
giveaway for the bug.

LLM throughput, from Ollama's own counters (`prompt_eval_count` /
`eval_count`) rather than wall-clock estimates:

| | |
|---|---|
| prompt (prefill) | ~505 tokens @ ~1050 tok/s |
| generation (decode) | ~26 tokens @ ~41 tok/s |

Reads are capped at `OLLAMA_NUM_PREDICT` (80) tokens and typically use ~26, so
decode is cheap; most of the LLM's ~1.1 s is prefill of the system prompt. Note
the SER instructions made that prompt noticeably longer — that trade is why
prefill is broken out separately in the benchmark.

Tokens per second is only meaningful for the LLM. SER is a single encoder
forward pass producing 7 logits and 3 numbers — there are no tokens to count —
so the audio stages report a real-time factor instead.

## The eval suite

Six stages, each runnable alone or all together, each writing a CSV of
per-item rows so you can slice the detail in a spreadsheet rather than squint at
a summary.

```
python -m eval.run_all --list      # what the stages are and what each needs
python -m eval.run_all --quick     # small samples, minutes
python -m eval.run_all --only ser asr   # a subset
python -m eval.run_all             # the full run (slow: ~1-2 hours)
```

| stage | question it answers | CSV |
|---|---|---|
| `pipeline` | Does the **whole workflow** work end to end? | `eval/results/pipeline.csv` |
| `ser` | Is the voice signal actually *right*? | `eval/results/ser.csv` |
| `asr` | Which `WHISPER_MODEL`? | `eval/results/asr.csv` |
| `model` | Which Ollama model reads tone best? | `eval/results/model.csv` |
| `bench` | Where does the time go? | — (printed) |
| `latency` | What does the user actually feel? | — (printed) |

Outside `run_all`: `python -m eval.spk_eval` scores speaker id against RAVDESS
(`--eer`, `--pairs`, `--label`; `--real me.wav conv.wav` is the harness for a
real-room recording, which has not been made yet), and `python -m
eval.slim_ser_ckpt` builds the slimmed SER checkpoint described above. An
audio-input LLM as a one-shot replacement for the whole pipeline was measured
and not adopted; the write-up is
[docs/reports/audio-llm.md](docs/reports/audio-llm.md).

Every stage streams progress as it goes — the SER stage prints a line per clip
with a running ETA — because the full run takes long enough that a silent
terminal is indistinguishable from a hang. Pass `--quiet` to any stage for
periodic progress instead of per-item lines.

### Full pipeline: audio in, read out

Every other stage tests one component alone. `pipeline` runs the real workflow —
`Segmenter` → Whisper + SER (concurrent) → `Interpreter` — over RAVDESS, so it
measures whether the stages **compose**: whether a mis-heard word ruins the read,
whether SER's valence actually reaches the LLM's judgement, whether the VAD copes
with real speech.

```bash
python -m eval.pipeline_eval                       # the full 2x3 matrix
python -m eval.pipeline_eval --vad-check           # pre-flight, ~10s
python -m eval.pipeline_eval --cells 1 --limit 28  # sanity pass, ~1 min
python -m eval.pipeline_eval --evict-only          # free the GPU now
```

No env vars required — the Windows HF-symlink workaround is applied internally,
and `SPEECH_RMS` is a `--speech-rms` flag, since PowerShell can't do `VAR=x cmd`.
Every progress line names the cell that produced it:

```
[1/6 e2v-base+qwen3:8b] [  6/280] 03-01-04-01-01-02-09  ser:sad ->sad y  wer 50.0%
                                  llm:neutral (exp negative)n  cpu 0.32s llm 0.55s  acc 50% eta 12.4m VAD-DROP
```

It sweeps 2 SER backends × 3 LLMs, **smallest models first** so useful results
land in minutes. ASR+SER are computed once per SER backend and replayed against
all three LLMs — 3× less CPU work, and more correct, since every LLM then sees
byte-identical inputs.

**Concurrency.** Within a clip the LLM needs SER's output, so they can't overlap.
The overlap is *across* clips: a subprocess does Whisper+SER for clip N+1 while
the parent awaits the GPU on clip N, with a bounded queue providing backpressure.

**Pause and stop.** `Ctrl+C` finishes the current clip, flushes, evicts the model
from VRAM and exits; re-running the same command resumes, because rows are keyed
on `(ser_backend, llm_model, file)`. **Stop is pause.** For a pause without
exiting, `touch eval/results/PAUSE` — it frees the GPU and waits; delete the file
to continue.

**VRAM hygiene.** `interpreter.py` pins models with `keep_alive: -1`, so a naive
run leaves 8–9 GB occupied forever and model-swapping can silently push layers to
CPU (which reads as "this model is slow"). The runner evicts explicitly between
cells, asserts the incoming model is fully in VRAM, and frees the GPU on every
exit path. A `SIGKILL` is the exception — nothing can run on it, so use
`--evict-only` afterwards.

**Monitoring a long run:** a per-clip line with running accuracy and ETA, plus
`eval/results/pipeline_status.json` (atomically rewritten every clip) for a
watcher to poll.

### SER accuracy on real emotional speech

Every other test in this repo uses Windows SAPI speech, which is deliberately
flat and carries no emotion — fine for plumbing, useless for accuracy. The `ser`
stage uses **RAVDESS**: 1440 clips, 24 professional actors, eight emotions.

```bash
curl -L -o ravdess.zip   https://www.kaggle.com/api/v1/datasets/download/uwrfkaggler/ravdess-emotional-speech-audio
# extract the Actor_* folders into data/ravdess/
python -m eval.ser_eval --limit 56          # quick pass
python -m eval.ser_eval --limit 0           # all 1440
python -m eval.ser_eval --models MERaLiON/MERaLiON-SER-v1 emotion2vec/emotion2vec_plus_base
```

The dataset is ideal for this specific job because **both sentences are
semantically neutral** — "Kids are talking by the door" and "Dogs are sitting by
the door" — so the words carry no emotion and every bit of signal has to come
from delivery. That is exactly the situation the words-vs-voice mismatch rule
depends on.

The report gives per-emotion recall, a confusion matrix, and — for backends with
a dimensional head — **mean valence per true emotion**, which is the most useful
number in the file. Valence is the axis the sarcasm rule keys on, so if valence
doesn't separate angry from happy, mismatch detection can't work no matter how
good the categorical labels look. The stage prints a warning if that separation
collapses.

Two caveats. These are *acted* emotions recorded in a studio, so treat the
numbers as an optimistic ceiling rather than what a cheap mic in a real room
gives you. And RAVDESS has a `calm` class our 7-label vocabulary lacks; it folds
into `neutral` by default, which is a judgement call — `--exclude-calm` shows the
numbers without it. Scores are reported as macro-average recall rather than raw
accuracy, because the merged neutral+calm class is twice the size of the others
and raw accuracy would reward a model that just leans neutral.

## Choosing the transcription model

`ASR_MODEL` picks the resident speech-to-text model by **catalogue key**, and the
dashboard's **Transcription model** card switches it live (`POST /api/asr`, not
persisted -- a restart returns to `ASR_MODEL`). Exactly one model is resident at a
time; choosing another builds it first (downloading its files on first use, under
`models/asr/`), then drops the previous one. `WHISPER_MODEL=<name>` still works
and means `whisper:<name>`.

| key | family | size | why it is here |
|---|---|---|---|
| `whisper:base` | faster-whisper | ~75 MB | the previous default, 6.0% WER / 0.26 s on the SAPI set |
| `whisper:distil-small.en` | faster-whisper | ~170 MB | `small`'s accuracy for less CPU (4.8% / 0.67 s measured) |
| `whisper:small.en` | faster-whisper | ~250 MB | 4.8% / 0.80 s measured |
| `whisper:distil-large-v3` | faster-whisper | ~750 MB | best Whisper on noisy speech; **seconds** per utterance on CPU |
| `whisper:large-v3-turbo` | faster-whisper | ~810 MB | as above, slightly worse on noise |
| `parakeet:v2` | sherpa-onnx (NVIDIA Parakeet TDT 0.6B, English) | ~640 MB | 4.8% WER / 0.10 s on the SAPI set; ~6% on the Open ASR leaderboard vs ~10% for Whisper base; cost scales with audio |
| `parakeet:v3` *(default)* | sherpa-onnx (Parakeet TDT 0.6B, 25 languages) | ~640 MB | 6.0% WER / 0.10 s on the SAPI set (one numeral-formatting miss more than v2); same speed, multilingual |
| `moonshine:base` | sherpa-onnx (Useful Sensors Moonshine) | ~270 MB | built for short utterances; the fastest here |
| `moonshine:tiny` | sherpa-onnx | ~120 MB | smallest |
| `sensevoice:small` | sherpa-onnx (FunAudioLLM SenseVoice) | ~230 MB | also tags language / emotion / laughter events (logged, not used yet) |

Whisper pads every utterance to a 30 s window, so its cost is **flat**: a large
Whisper costs the same seconds for "yeah" as for a paragraph. The transducer and
CTC families (Parakeet, Moonshine, SenseVoice) scale with the audio and do not
invent text on silence. Compare them all with `python -m eval.asr_eval`
(downloads on demand; add real-microphone recordings to `experiments/asr/` first --
the SAPI set there is clean, accent-free speech that cannot separate these models,
and much of its residual WER is numeral formatting like "four fifteen" vs "4.15").

## Choosing the interpreter model

`eval/tone_cases.jsonl` is a labeled suite for A/B-ing candidate LLMs, and
`eval/model_eval.py` scores them through the real `Interpreter` — same system
prompt, same context assembly, same JSON parsing the app uses.

```
python -m eval.model_eval                       # all candidates
python -m eval.model_eval --models gemma3:12b   # one model
python -m eval.model_eval --runs 3 --show-fails # variance + every miss
```

The suite has 34 cases across eight categories, and the categories matter more
than the headline number. Two of them exist purely as **false-positive guards**:

- `literal` — plain sentences ("The train leaves at four fifteen"). Reading tone
  into these is the most damaging failure this app can make, because it teaches
  the listener to distrust ordinary speech.
- `low-confidence` — a weak SER guess (conf ~0.2) attached to a neutral line.
  Tests that the model respects `SER_MIN_CONFIDENCE` instead of letting a noisy
  acoustic label override plain words.

The headline metric is **mismatch discrimination**. Eight cases form four pairs
with *identical transcripts* and opposite voice data — "Oh wonderful. That is
just perfect." with a bright voice versus a hostile one. Both halves must be
right to score the pair, so a model that ignores the `voice` field scores 0 no
matter how good its overall accuracy looks. It is the only metric that proves
the SER signal is actually being used.

Accepted tones are deliberately sets rather than single answers: several
readings are often genuinely defensible, and scoring against one "correct" label
would mostly measure agreement with whoever wrote the labels.

### Results

34 cases × 3 runs, Arc B580, current system prompt (the arousal + dominance
revision of 2026-09-20; `mm-05`/`mm-06` are valence-free pairs in the shipped
backend's shape):

| model | size | overall | sarcasm | mismatch pairs | speed | notable weakness |
|-------|------|---------|---------|----------------|-------|------------------|
| `gemma3:12b` | 8.1 GB | 85% | 9/12 (75%) | 3/6 | 1.01 s | misses `sar-04` on every run; reads a calm "it's fine" as masking |
| `qwen3:14b` | 9.3 GB | 88% | **12/12** | 2/6 | 0.62 s | over-calls sarcasm when voice says sincere |
| `qwen3:8b` | 5.2 GB | 76% | 9/12 (75%) | **4/6** | 0.43 s | **0% on passive aggression**, misses `sar-04` |

On the 30 cases the earlier prompt was scored on, each model is 3 points
below its old figure (93 / 93 / 83), almost entirely one case per model; the
`literal` and `low-confidence` guard rails are 100% for all three. The full
before/after is in `docs/status/TODO.md` under the SER entry.

The sarcasm column is the axis the product is named for, and it shows two
opposite failure shapes that the overall score hides. Every sarcasm miss in
`eval/results/model.csv` is the same case, `sar-04` (*"No, please, take your
time. It's not like I have anywhere to be."*): `gemma3:12b` and `qwen3:8b` fail it
on all three runs, so it is deterministic, not occasional, and it is the most
conversationally common form in the set (sarcasm carried by a negation rather
than an intensifier like "oh great"). `qwen3:14b` detects every sarcasm case and
**over-applies** it: its 2/6 on the mismatch pairs comes from the sincere halves,
where a confidently happy voice should rule sarcasm out. For this product a false
positive is the more damaging error, because it teaches the listener to distrust
ordinary speech.

**The shipped default is `qwen3:14b`**, chosen on the end-to-end RAVDESS pipeline
eval rather than this text suite (see `server/config.py`): there the spoken text
is always neutral, so every bit of signal is in the voice field, and `qwen3:14b`
was the only model that used a bare categorical label at all (81% tone accuracy
against 43% and 33%). On this suite `gemma3:12b` ties it overall and wins the
mismatch pairs, so if a backend with usable valence ever lands, re-run both.

`qwen3:8b` is half the latency and matches on the voice pairs, but it scores 0%
on passive aggression across every run, reading "Thanks for finally getting back
to me" as *"They're relieved to hear from you again."* Passive aggression is a
core use case here, so the speed isn't worth it. (Its failures are often a
mislabel rather than a misunderstanding — for pa-02 it wrote the correct read,
*"They're frustrated but trying to sound calm"*, and still labelled the tone
`neutral` — but tone drives the colour coding, so the mislabel still misleads.)

Two findings from building this suite are baked back into the code:

- **Bare `format: "json"` is not enough.** `{}` is valid JSON, and `qwen3:14b`
  returned exactly that on every call — which looked like a model answering
  "neutral" to everything (38% overall) rather than one not complying.
  `interpreter.py` now sends a JSON *schema* with required fields and a `tone`
  enum, which fixed it outright (38% → 93%) and left the other two unchanged.
- **The mismatch rule needed to be stated in both directions.** Every model
  applied "positive words + negative voice = sarcasm" but not its converse, so
  sarcastic-*sounding* phrasing said in a genuinely warm voice was still called
  sarcasm. Spelling out the reverse took `gemma3:12b` and `qwen3:8b` from 2/4 to
  4/4 on the voice pairs (+6 and +7 points overall).

## Known limitations

See [docs/status/TODO.md](docs/status/TODO.md) for the full write-up of each, including what was tested
and ruled out. In short:

- **The adaptive VAD is unconfirmed on real hardware.** A fixed `SPEECH_RMS`
  used to drop subdued speech entirely — 5 of 10 RAVDESS sample clips, the *sad*
  and *neutral* ones, backwards for a tool meant to make masked emotion legible.
  The threshold is now noise-floor-relative and segments all 10, but that is a
  studio-audio result; the pendant's mic in a real room has not been checked, and
  `MIN_UTTERANCE_MS` was left as it was pending that.
- **Parakeet v3 is untested in a real room.** It became the default on the SAPI
  comparison, which cannot see noise robustness; re-run `python -m eval.asr_eval`
  on real recordings before trusting it over the Whisper keys.
- **The default SER backend has no measured dimensions.** Arousal and dominance
  are derived from the label distribution, so they grade only when the model is
  torn; valence is left to the LLM. Whether that beats a measured head on real
  speech is untested.
- **Sarcasm is never tested on real speech.** The mismatch pairs use hand-typed
  valence gaps far wider than any backend measures, RAVDESS has no sarcastic
  speech at all, and the default backend has no valence for those cases to
  exercise. The suite proves the LLM reasons correctly over an idealised voice
  field; it cannot show the field is ever that clean.
- **Speaker id has only been measured on studio audio.** ~94% on RAVDESS actor
  pairs, but nothing has been recorded in a real room with two people and one
  mic; `python -m eval.spk_eval --real` is the harness waiting for that clip.
- **Acted-emotion caveat.** RAVDESS is studio recordings by professional actors,
  so every accuracy figure here is an optimistic ceiling rather than what a cheap
  microphone in a busy room will give you.

## Remote access

The server can be reached from outside the LAN through a **Cloudflare quick
tunnel** — one command, no account, no domain, no port forwarding, works behind
NAT/CGNAT. Full instructions and troubleshooting are in
**[docs/guides/tunnel.md](docs/guides/tunnel.md)**.

```powershell
$env:AUTH_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
python -m server.main                              # terminal 1
cloudflared tunnel --url http://localhost:8000     # terminal 2
```

cloudflared prints a random `https://<random>.trycloudflare.com` URL. **It
changes every restart** — that is the trade for needing no account. `tunnel/start.ps1`
(Windows) and `tunnel/start.sh` start both and print the URL prominently.

This is also the only way to test the microphone on a phone: browsers require a
**secure origin** for `getUserMedia`, and `localhost` is the only exception. The
tunnel supplies the HTTPS.

### Dashboard

`/dashboard.html` is the monitoring and control panel:

```
https://<random>.trycloudflare.com/dashboard.html?token=<AUTH_TOKEN>
```

| panel | what it shows |
|---|---|
| Right now | utterances, live clients, median total/LLM latency, VAD discards, LLM-offline count |
| Machine | CPU (per physical/logical cores), RAM, this process's RSS and threads, 60s sparkline |
| GPU | per-engine utilisation and GPU memory in use, 60s sparkline |
| Pipeline latency | whisper / ser / cpu-stage / llm / total — last, mean, median, p95, n |
| Interpreter model | every pulled model, which is selected, which is resident and whether it is **fully on GPU** — with **Use** and **Evict** buttons |
| Transcription model | the ASR catalogue (Whisper, Parakeet, Moonshine, SenseVoice): which is resident, which is downloaded, with a **Use** / **Download & use** button and live download progress |
| Configuration | active models, speaker-id enrolment state, `SPEECH_RMS`, `END_SILENCE_MS`, LLM sampling, auth state, Ollama reachability |
| Tone / voice emotion / who spoke | distributions across the session |
| Connected clients | one row per open `/stream` socket: address (`CF-Connecting-IP` through the tunnel), client, uptime, utterances, bytes, idle/streaming |
| Recent utterances | last 40, with per-stage timings, the voice label and the speaker label |
| Saved clips | every clip on disk, newest first, with **Play** (fetched with the auth header, played from a Blob URL so the token never lands in a URL) and **Delete** |

**Switching models is live** — it takes effect on the next utterance with no
restart and without disturbing connected clients, because `Interpreter` reads
`OLLAMA_MODEL` at call time. It evicts the previous model first, since two
8–9&nbsp;GB models do not fit in 12&nbsp;GB and a partly-resident model runs
partly on CPU (which reads as "this model is slow"). The change is **not
persisted**; a restart returns to the configured default.

All `/api/*` routes are token-gated on the same rule as `/stream`: enforced when
`AUTH_TOKEN` is set, open when it is not. That matters more here than for the
websocket — `/stream` only lets a stranger burn CPU, while `POST /api/model`
lets them change which model the server runs.

GPU figures come from Windows per-engine performance counters, which work for
any vendor (there is no `nvidia-smi` equivalent for an Arc). A query costs ~2.6s,
so it is sampled on a background thread and served from cache; a dashboard poll
never waits on it. On non-Windows the panel degrades to "unavailable" and
Ollama's own VRAM figures carry the GPU story instead.

### Tunnel diagnostics

`/remote.html` is a standalone page for verifying a tunnel from another device:

```
https://<random>.trycloudflare.com/remote.html?token=<AUTH_TOKEN>
```

It checks secure origin, `/health` with round-trip time, websocket scheme,
connect, auth, keepalive latency and whether the `voice` field is arriving — then
**Stream mic** runs the whole path (mic → tunnel → Whisper → Ollama → read) and
reports end-to-end latency. Server URL and token are editable, since the tunnel
hostname changes on every restart. Built for a phone: large touch targets,
responsive, and a timestamped log of every frame.

> **Set `AUTH_TOKEN` before starting a tunnel.** Unset, the server accepts *any*
> client — there is no rate limit, connection cap or origin check. A quick-tunnel
> hostname is random, but random is not secret: it passes through Cloudflare and
> sits in your shell history, and anyone who obtains it gets unmetered use of
> your CPU and GPU. The server logs a loud warning at startup when it is unset,
> and **refuses to start** on any bind other than loopback (`HOST` defaults to
> `127.0.0.1`) unless `ALLOW_NO_AUTH=1` is set explicitly. `python tunnel/token.py`
> generates one.

## Layout

```
docs/             everything except this file and CLAUDE.md (index: docs/README.md)
  status/TODO.md          open issues, with the measurements behind them
  design/                 RESEARCH.md, firmware UI/UX + requirements, website brief
  guides/                 firmware.md, tunnel.md, website.md
  reports/                one-off measurements (laya.md)
tunnel/           Cloudflare quick-tunnel start scripts + token helper
server/
  main.py         FastAPI app + websocket route (wiring only)
  config.py       all tunables, env-overridable
  segmenter.py    pure VAD state machine: bytes in -> utterances out
  transcriber.py  speech-to-text behind one transcribe(); catalogue of faster-whisper + sherpa-onnx backends, one resident, switchable (CPU)
  ser.py          speech emotion recognition: emotion2vec / MERaLiON (CPU)
  speaker.py      speaker id: is this the listener's own voice? CAM++ (CPU)
  clips.py        clips: per-connection retention ring + wav/json save + listing
  translog.py     transcript log: every utterance's text + audio under logs/transcripts/, for re-scoring ASR on real speech
  interpreter.py  Ollama wrapper + rolling conversation context
  metrics.py      in-memory rolling metrics for the dashboard (no DB)
  dashboard.py    token-gated /api/* : stats, model select/evict, clips, speaker enrol/forget
  static/index.html      browser client, the app itself (no build step)
  static/remote.html     tunnel diagnostics + VAD strip + mic test (phone)
  static/dashboard.html  monitoring, live model control, saved clips
  static/enroll.html     guided voice enrolment for speaker id
clips/                  saved clips (gitignored; user-initiated retention only)
models/                 slimmed SER checkpoint + speaker profile (gitignored)
eval/results/           committed eval output (the evidence behind config)
eval/run_all.py         run every stage, one CSV each
eval/pipeline_eval.py   full workflow over RAVDESS (the matrix runner)
eval/confidence_eval.py does the read's confidence track correctness? AUROC per signal, ECE, Platt fit -> CONFIDENCE_* values
eval/pipeline_child.py    its CPU half: Segmenter -> whisper + SER
eval/pipeline_wire.py     the same, over the real websocket (spot check)
eval/ser_eval.py        SER accuracy vs RAVDESS (real emotional speech)
eval/asr_eval.py        ASR backend A/B (whisper / parakeet / moonshine / sensevoice): WER + CPU cost
eval/model_eval.py      A/B interpreter LLMs on the labeled tone cases
eval/tone_cases.jsonl   those labeled cases
eval/bench.py           per-stage latency + Ollama tok/s
eval/latency.py         end-to-end: stopped speaking -> read in hand
eval/spk_eval.py        speaker id vs RAVDESS: EER, actor pairs, glued turns
eval/slim_ser_ckpt.py   strip optimizer state from the SER checkpoint (-1.4 GB RAM)
eval/sarcasm_lines.txt  tricky lines to eyeball the interpreter
data/ravdess/           RAVDESS dataset (gitignored, see below)
experiments/      scratch space; headless test client + SAPI audio generators
firmware/         the M5StickS3 pendant: mic, display, wss client, hold-to-record clips
website/          the project site (Vite + three.js pendant render)
```

### Endpoints

| path | what | auth |
|---|---|---|
| `/` | the app | token as `?token=` for the websocket |
| `/remote.html` | connection + VAD diagnostics, mic test | token |
| `/dashboard.html` | monitoring, model control, saved clips | token |
| `/enroll.html` | guided voice enrolment | token |
| `/health` | `{"status":"ok"}` | none, deliberately |
| `/stream` | websocket: TEXT token, then binary PCM (+ `ping` / `save` JSON) | first frame |
| `/stream?vad=1` | as above, plus VAD telemetry frames | first frame |
| `/api/stats` | everything the dashboard polls | token |
| `/api/models` · `POST /api/model` · `POST /api/evict` | model control; only already-pulled names, one change per `MODEL_SWITCH_COOLDOWN_S` (429 otherwise) | token |
| `/api/asr` · `POST /api/asr` | transcription model: catalogue + state, and switch (catalogue keys only; returns at once, poll `GET` for download/load progress; 409 while one is running, 429 inside the cooldown) | token |
| `/api/clips` · `/api/clips/{id}/audio` · `DELETE /api/clips/{id}` | list saved clips, fetch one's WAV, delete one | token |
| `/api/speaker` · `POST /api/enroll` · `DELETE /api/speaker` | speaker-id state, enrol from an uploaded WAV/PCM (segmented with the real VAD, embeddings only stored), forget the profile | token |

Token auth follows one rule everywhere: **enforced when `AUTH_TOKEN` is set,
open when it is not.** The HTTP routes accept it as an `X-Auth-Token` header
(what the pages send, so it stays out of URLs, logs and history) or as
`?token=` for a hand-pasted link. `/health` is the deliberate exception, so a tunnel can be
checked without credentials — a 502 there means the tunnel is up but the server
is not.
