# emtext

A live conversation interpreter. It transcribes speech as you hear it and, for
each sentence, adds a one-line read of the *emotional subtext* — is this
positive, negative, sarcastic, passive-aggressive, or just literal? It's built
for a neurodivergent listener who wants tone made explicit.

A browser page streams your microphone to a local server over a websocket. The
server slices the audio into utterances and then, for each one, works out both
*what* was said (Whisper, on CPU) and *how it sounded* (a speech emotion model,
also on CPU) before asking a local LLM (via Ollama, on the GPU) what the line
really means. Later, an ESP32 device will replace the browser using the exact
same wire protocol.

```
mic ─► browser (AudioWorklet, 16k int16) ─websocket─► server
                                                        ├─ segmenter  (VAD)
                                                        ├─ transcriber (faster-whisper, CPU) ─┐
                                                        ├─ ser         (emotion2vec, CPU) ────┤ (concurrent)
                                                        └─ interpreter (Ollama LLM, GPU) ◄────┘
```

## Where this is right now

Working end to end, on a laptop and on a phone over the internet. Every number
below is measured on this hardware (Ryzen 5 9600X, 6c/12t; Intel Arc B580, 12 GB
VRAM), not estimated.

**Stack as configured**

| stage | model | device | cost per utterance |
|---|---|---|---|
| VAD | energy segmenter | — | negligible |
| transcription | Whisper `base` (int8) | CPU | ~0.24 s |
| speech emotion | `emotion2vec_plus_base` | CPU | ~0.12 s |
| interpretation | `qwen3:14b` via Ollama | GPU | ~0.89 s |

**Stop talking → read on screen: ~1.94 s**, of which 0.65 s is the VAD's
end-of-speech wait (`END_SILENCE_MS`) and the rest is compute. It was 4.63 s
before the SER swap.

**What's built**

- Live app at `/`, streaming mic → transcript → tone read, with the voice label.
- **Remote access** over a Cloudflare quick tunnel — one command, no account, no
  domain. Also the only way to test a microphone on a phone, since browsers
  require HTTPS for `getUserMedia`.
- **`/dashboard.html`** — live CPU/RAM/GPU, per-stage latency percentiles, tone
  and voice-emotion distributions, recent utterances, and runtime switching of
  the interpreter model.
- **`/remote.html`** — connection diagnostics plus a live VAD strip showing which
  audio frames cleared `SPEECH_RMS`, which is how segmentation gets tuned.
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

**Known rough edges** — see [TODO.md](TODO.md) for the detail:

- **Segmentation needs tuning for real rooms.** `SPEECH_RMS` is an absolute
  threshold; quiet speech is dropped and sentences split at soft consonants. The
  VAD strip in `/remote.html` is the tool for this.
- **Whisper `base` could be better.** `distil-small.en` matches `small`'s accuracy
  for less CPU, but that was measured on clean synthetic speech, so it needs a
  real-microphone comparison first.
- The current SER backend supplies **no valence/arousal**, only an emotion label,
  which weakens the words-vs-voice mismatch rule the prompt is built around.

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

Note the default backend (emotion2vec) is **categorical only** — it returns an
emotion label with a confidence, and no valence/arousal numbers. See
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
| Whisper `base` (faster-whisper) | ~150 MB | transcription, CPU |
| `emotion2vec/emotion2vec_plus_base` | ~900 MB | speech emotion, CPU |

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

The browser client and the future ESP32 firmware both speak this:

1. Connect to `ws://host:8000/stream`.
2. Send **one TEXT frame**: the auth token. (Any value is accepted unless the
   `AUTH_TOKEN` env var is set on the server.)
3. Then send **BINARY frames**: raw PCM, 16 kHz, mono, int16 little-endian.

The server replies with TEXT JSON frames:

```jsonc
{"type":"ready"}
{"type":"status","state":"listening|heard|thinking"}
{"type":"utterance","id":1,"transcript":"..."}
{"type":"read","id":1,"tone":"positive|negative|neutral|sarcastic|mixed","read":"one line"}
```

The `read` frame carries one **optional** field, `voice`, present only when SER
produced a result (so it is absent when `SER_ENABLED=0`, when the model failed
to load, or when that utterance couldn't be scored):

```jsonc
{"type":"read","id":1,"tone":"sarcastic","read":"one line",
 "voice":{"emotion":"angry","valence":0.21,"arousal":0.65}}
```

`valence` runs 0 (negative) to 1 (positive); `arousal` runs 0 (calm) to 1
(intense). Clients must treat `voice` — and each field inside it — as optional:
existing clients and firmware that ignore the key keep working unchanged, which
is why it was added this way rather than as a new frame type.

Reads arrive **out of order** relative to transcripts: each utterance is
processed in its own task, so a short line can overtake a long one. Correlate on
`id`, never on arrival order.

## Tuning the VAD

The segmenter decides where sentences start and stop using simple frame energy.
Every knob is in `server/config.py` and can be overridden with an environment
variable of the same name. The ones you'll actually touch:

| Env var             | Default | What it does |
|---------------------|---------|--------------|
| `SPEECH_RMS`        | 500     | Loudness (int16 RMS) needed to count a frame as speech. **Raise it** if a noisy room keeps triggering; **lower it** if quiet speech gets missed. |
| `END_SILENCE_MS`    | 650     | How much trailing quiet ends an utterance. Lower = snappier but may split sentences at pauses; higher = fewer splits but more lag. |
| `MIN_UTTERANCE_MS`  | 350     | Utterances with less voiced audio than this are discarded as blips (coughs, taps). |
| `MAX_UTTERANCE_MS`  | 15000   | Hard cap; force-cuts run-on audio so the buffer never grows without bound. |
| `PRE_ROLL_MS`       | 240     | Audio kept from just *before* speech starts, so the first sound isn't clipped. |

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

Other useful env vars: `PORT`, `AUTH_TOKEN`, `OLLAMA_URL`, `OLLAMA_MODEL`,
`WHISPER_MODEL`, `CONTEXT_LINES`.

## Testing without a microphone

The browser client needs a real mic and a user gesture, which makes it awkward
to check a change. `experiments/` has a headless path that speaks the same wire
protocol the ESP32 will:

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
flatly — the sarcasm case).

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
| `SER_ENABLED`        | `true`  | Set to `0` to turn SER off: skips the 2.9 GB download and ~2 GB of RAM, drops the `voice` field, and removes the added latency below. |
| `SER_MODEL`          | `emotion2vec/emotion2vec_plus_base` | Model id — see the table below. |
| `SER_BACKEND`        | `auto`  | `auto` infers from the model name; force with `meralion` or `emotion2vec`. |
| `SER_DEVICE`         | `cpu`   | Leave it — the Arc B580 is for Ollama. |
| `SER_MIN_CONFIDENCE` | `0.4`   | Below this the interpreter is told the voice signal is weak and to trust the words instead. |
| `SER_TORCH_THREADS`  | `0`     | `0` = torch's default. Raising it to your logical core count speeds up one SER pass but competes with Whisper; measure before changing. |

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

The emotion2vec models are far lighter, but they are **categorical only** — no
dimensional head, so `valence` and `arousal` come back `null`. That is not a
missing column so much as a missing capability: valence is the axis the
interpreter's sarcasm rule actually keys on, so mismatch detection gets weaker.
Treat them as a speed/quality trade, not a free win.

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

Measured on a 12-core CPU + Arc B580, `WHISPER_MODEL=base`, `gemma3:12b`:

| stage | model | device | per utterance |
|-------|-------|--------|---------------|
| transcription | Whisper `base` (faster-whisper, int8) | CPU | ~0.28 s |
| speech emotion | `emotion2vec/emotion2vec_plus_base` | CPU | ~0.12 s |
| whisper + SER, gathered | | CPU | ~2.6 s (vs ~3.7 s sequentially) |
| interpretation | `qwen3:14b` (Ollama) | GPU | ~0.89 s |

**SER, not Whisper, decides how soon a read appears.** Transcription and SER are
deliberately gathered onto the executor rather than sequenced, so an utterance
costs about `max(whisper, ser)` rather than their sum — worth ~1 s per utterance
— but SER is ~12× Whisper here, so concurrency hides Whisper behind SER rather
than the reverse. Enabling SER adds roughly 2.3 s before a read arrives.

That cost is **flat**: a 1 s utterance and a 14 s utterance both take ~3.3 s,
because the Whisper feature extractor pads every input to a fixed 30 s window
(80 × 3000 mel frames) and the encoder always processes the whole thing. So SER
runs *slower than real time* for any utterance under ~3.3 s, which is most
conversational speech. Audio is never dropped — the read just arrives later —
but during sustained back-to-back speech the backlog grows. Natural pauses are
what keep it level. To fix it, set `SER_ENABLED=0` or raise `SER_TORCH_THREADS`.

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

Five stages, each runnable alone or all together, each writing a CSV of
per-item rows so you can slice the detail in a spreadsheet rather than squint at
a summary.

```
python -m eval.run_all --list      # what the stages are and what each needs
python -m eval.run_all --quick     # small samples, minutes
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

`WHISPER_MODEL` picks the faster-whisper model. Measured on 8 known-text SAPI
utterances (`python -m eval.asr_eval`):

| `WHISPER_MODEL` | size | WER | per utterance | when to use |
|---|---|---|---|---|
| `tiny` | ~39M | 6.0% | 0.12 s | CPU time is the problem |
| `base` *(default)* | ~74M | 6.0% | 0.26 s | the sensible starting point |
| `small` | ~244M | 4.8% | 0.80 s | accuracy is the problem |
| `distil-small.en` | ~166M | 4.8% | 0.67 s | `small`'s accuracy, ~20% cheaper, English-only |

Two things worth knowing before you read too much into that table. Much of the
residual WER is **numeral formatting** — every model writes "four fifteen" as
"4.15", which scores as errors but changes nothing for the interpreter — so real
semantic accuracy is better than these figures suggest. And the audio is clean,
accent-free SAPI speech over no microphone at all, so this is a *relative*
ranking; real-world WER will be higher for every model, probably by different
amounts.

Given that, `base` is a reasonable default and `distil-small.en` is the upgrade
worth trying first if transcripts annoy you — it matched `small`'s accuracy here
while being cheaper. Note that with SER on, transcription is a rounding error in
the latency budget (0.26 s against SER's 3.3 s), so paying for `small` costs
almost nothing end-to-end. With `SER_ENABLED=0` the choice actually matters.

## Choosing the interpreter model

`eval/tone_cases.jsonl` is a labeled suite for A/B-ing candidate LLMs, and
`eval/model_eval.py` scores them through the real `Interpreter` — same system
prompt, same context assembly, same JSON parsing the app uses.

```
python -m eval.model_eval                       # all candidates
python -m eval.model_eval --models gemma3:12b   # one model
python -m eval.model_eval --runs 3 --show-fails # variance + every miss
```

The suite has 30 cases across eight categories, and the categories matter more
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

30 cases × 3 runs, Arc B580, current system prompt:

| model | size | overall | mismatch pairs | speed | notable weakness |
|-------|------|---------|----------------|-------|------------------|
| `gemma3:12b` | 8.1 GB | 93% | **4/4** | 1.17 s | occasional passive-aggression miss |
| `qwen3:14b` | 9.3 GB | 93% | 2/4 | 0.85 s | over-calls sarcasm when voice says sincere |
| `qwen3:8b` | 5.2 GB | 83% | **4/4** | 0.52 s | **0% on passive aggression** |

**`gemma3:12b` remains the right default.** `qwen3:14b` ties it on overall
accuracy and is faster, but it is the only model that still ignores a confidently
positive voice on sarcastic-*looking* phrasing — the exact discrimination SER was
added to provide.

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

See [TODO.md](TODO.md) for the full write-up of each, including what was tested
and ruled out. In short:

- **The VAD cannot hear quiet speech.** `SPEECH_RMS` is an absolute threshold, so
  subdued delivery is never segmented and the app emits nothing for it — on
  RAVDESS that dropped 5 of 10 sample clips, and they were the *sad* and *neutral*
  ones. Backwards for a tool meant to make masked emotion legible. The VAD strip
  in `/remote.html` now diagnoses it; the fix (a noise-floor-relative threshold)
  is not written.
- **Whisper `base` is untested in a real room.** The ASR comparison used clean
  synthetic speech, which cannot see noise robustness. Do not switch models on
  that evidence alone.
- **The default SER backend has no valence**, only an emotion label — which
  weakens the words-vs-voice mismatch rule the whole prompt is built around.
- **Acted-emotion caveat.** RAVDESS is studio recordings by professional actors,
  so every accuracy figure here is an optimistic ceiling rather than what a cheap
  microphone in a busy room will give you.

## Remote access

The server can be reached from outside the LAN through a **Cloudflare quick
tunnel** — one command, no account, no domain, no port forwarding, works behind
NAT/CGNAT. Full instructions and troubleshooting are in
**[tunnel/README.md](tunnel/README.md)**.

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
| Configuration | active models, `SPEECH_RMS`, `END_SILENCE_MS`, auth state, Ollama reachability |
| Tone / voice emotion | distribution across the session |
| Recent utterances | last 40, with per-stage timings and the voice label |

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
> your CPU and GPU. The server logs a loud warning at startup when it is unset.

## Layout

```
TODO.md           open issues, with the measurements behind them
tunnel/           Cloudflare quick-tunnel docs + start scripts
server/
  main.py         FastAPI app + websocket route (wiring only)
  config.py       all tunables, env-overridable
  segmenter.py    pure VAD state machine: bytes in -> utterances out
  transcriber.py  faster-whisper wrapper (CPU)
  ser.py          speech emotion recognition: emotion2vec / MERaLiON (CPU)
  interpreter.py  Ollama wrapper + rolling conversation context
  metrics.py      in-memory rolling metrics for the dashboard (no DB)
  dashboard.py    token-gated /api/* : stats, model select, model evict
  static/index.html      browser client, the app itself (no build step)
  static/remote.html     tunnel diagnostics + VAD strip + mic test (phone)
  static/dashboard.html  monitoring + live model control
eval/results/           committed eval output (the evidence behind config)
eval/run_all.py         run every stage, one CSV each
eval/pipeline_eval.py   full workflow over RAVDESS (the matrix runner)
eval/pipeline_child.py    its CPU half: Segmenter -> whisper + SER
eval/pipeline_wire.py     the same, over the real websocket (spot check)
eval/ser_eval.py        SER accuracy vs RAVDESS (real emotional speech)
eval/asr_eval.py        whisper model A/B: WER + CPU cost on known text
eval/model_eval.py      A/B interpreter LLMs on the labeled tone cases
eval/tone_cases.jsonl   those labeled cases
eval/bench.py           per-stage latency + Ollama tok/s
eval/latency.py         end-to-end: stopped speaking -> read in hand
eval/sarcasm_lines.txt  tricky lines to eyeball the interpreter
data/ravdess/           RAVDESS dataset (gitignored, see below)
experiments/      scratch space; headless test client + SAPI audio generators
firmware/         (future) ESP32 client
```

### Endpoints

| path | what | auth |
|---|---|---|
| `/` | the app | token as `?token=` for the websocket |
| `/remote.html` | connection + VAD diagnostics, mic test | token |
| `/dashboard.html` | monitoring and model control | token |
| `/health` | `{"status":"ok"}` | none, deliberately |
| `/stream` | websocket: TEXT token, then binary PCM | first frame |
| `/stream?vad=1` | as above, plus VAD telemetry frames | first frame |
| `/api/stats` | everything the dashboard polls | token |
| `/api/models` · `POST /api/model` · `POST /api/evict` | model control | token |

Token auth follows one rule everywhere: **enforced when `AUTH_TOKEN` is set,
open when it is not.** `/health` is the deliberate exception, so a tunnel can be
checked without credentials — a 502 there means the tunnel is up but the server
is not.
