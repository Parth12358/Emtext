# TODO

Known issues and deferred work. Each entry records what was measured, so the
next person doesn't have to rediscover it.

---

## The VAD cannot hear quiet speech (`SPEECH_RMS`)

**Status:** open, but now *diagnosable* · **Found:** 2026-09-03, while building
`eval/pipeline_eval.py` · **Confirmed on real hardware** during phone testing
· **Affects:** `server/segmenter.py`, `server/config.py`

### The problem

`SPEECH_RMS` (default 500) is an **absolute int16 RMS threshold**. Speech quieter
than that is never segmented, so the pipeline emits nothing at all — no
transcript, no read. Measured on RAVDESS, peak frame RMS spans a ~130× range
across emotions:

| emotion | peak frame RMS |
|---|---|
| angry | 13456 |
| happy | 11970 |
| fearful | 411 |
| surprised | 461 |
| **sad** | **103–168** |

At the default threshold, **5 of 10 sample clips produced no segment**, and they
were the quiet emotions (4 sad, 1 neutral). Loud emotions sail through.

This is the wrong way round for this product. emtext exists to make subdued and
masked emotion legible for a neurodivergent listener — and the VAD is least able
to hear exactly the speech that matters most. A quiet real user, or anyone
speaking at a distance from the mic, hits the same wall.

### `MIN_UTTERANCE_MS` is a second, independent filter

Lowering `SPEECH_RMS` alone does not fix it. A clip whose *peak* clears the
threshold can still be discarded because too few individual frames do, leaving
less than `MIN_UTTERANCE_MS` (350) of voiced audio. One sad clip peaking at RMS
168 was still dropped at `SPEECH_RMS=150`. Any fix has to consider both knobs.

### What is *not* the problem

Two things were tested and ruled out, so they don't get re-litigated:

1. **The raw-clip fallback is not what corrupts transcription.** Dropped rows
   show ~33% WER against ~0% for kept rows, but that gap is not caused by the
   fallback. Those clips peak around **−40 dBFS** and Whisper mis-hears them
   whatever you do — segmented, raw, or amplified to −6 dBFS all produce
   different wrong answers ("Kids are talking *about it all*", "*Tox* is sitting
   by the door"). Very quiet speech is simply hard to transcribe. The VAD drop
   and the bad transcript share a cause rather than one causing the other.

2. **SER is unaffected.** It scored 100% on dropped rows — its feature extractor
   pads every input to a fixed 30 s window, so leading/trailing silence and low
   level don't bother it.

So the end-to-end failure for quiet speech is: VAD emits nothing → nothing
downstream runs. If it *did* emit, ASR would still be unreliable.

### Options for a fix (none implemented)

| approach | note |
|---|---|
| **Noise-floor-relative threshold** — track ambient RMS, trigger at a margin above it | Probably the right fix. Adapts to room and mic instead of assuming a level. Keeps `segmenter.py` pure/testable, but the state machine gains a calibration phase. |
| **AGC / input gain at capture** — normalise in the browser worklet and on the ESP32 | Fixes ASR too, since the level itself is the transcription problem. But it's a wire-protocol-adjacent change: both clients must do it consistently. |
| **Just lower the defaults** | Cheapest. `SPEECH_RMS=150` takes drops from 5/10 to 1/10. But an absolute threshold is still wrong in principle, and a lower one will false-trigger on room noise. |

**Do not "fix" this by loudness-normalising audio before SER.** Loudness is
itself an emotional cue — normalising flattens the difference between angry and
sad and would corrupt the emotion measurement to make a secondary stage look
better.

### Reproduce

```bash
python -m eval.pipeline_eval --vad-check              # 5/10 dropped at the default
python -m eval.pipeline_eval --vad-check --speech-rms 150   # 1/10
```

The pre-flight prints per-clip peak RMS, segment count, and a data-derived
threshold suggestion.

### Tooling now exists for this

`/remote.html` has a live VAD strip: per-frame RMS against the threshold, a
rolling speech/silence view, and the reason each utterance closed
(`end_silence` / `max_length` / `too_short`). It turns the numbers into an
instruction -- e.g. *"only 420ms voiced before it closed, that looks like a
sentence cut at a soft consonant"*, or *"nothing has cleared SPEECH_RMS=500 in
88 frames, peak was 153"*. Server-side it is opt-in via `/stream?vad=1`.

Confirmed in real use: splitting mid-sentence **and** clipping starts turn out to
be the same root cause, an absolute threshold set for a louder microphone than
the one in use.

### Interim workaround for evals

Run the pipeline eval with `--speech-rms 150`, and filter `vad_dropped=1` rows
out when analysing **tone** accuracy — those rows carry garbled transcripts, so
the LLM judged them on wrong words. SER and WER columns are still usable.


---

## Whisper accuracy in a noisy room

**Status:** open · **Affects:** `server/config.py` (`WHISPER_MODEL`)

`base` is the default and is the weakest link now that SER dropped to ~0.12 s --
Whisper is the largest CPU cost in the pipeline at ~0.24 s, and the transcript
sets a ceiling on how good any read can be.

Measured on 8 known-text utterances (`python -m eval.asr_eval`):

| model | WER | per utterance |
|---|---|---|
| `tiny` | 6.0% | 0.12 s |
| `base` *(current)* | 6.0% | 0.29 s |
| `small` | 4.8% | 0.93 s |
| `distil-small.en` | 4.8% | 0.83 s |

`distil-small.en` looks like the upgrade: `small`'s accuracy for ~11% less CPU.

**But do not switch on this evidence.** That set is Windows SAPI speech --
clean, accent-free, no microphone, no background noise. It cannot see the thing
that actually matters in a busy room, and `tiny` matching `base` there is almost
certainly an artefact of how easy the audio is. Record ~20 real utterances
through the browser client in a realistic environment and score those first.

Note also that much of the residual WER above is numeral formatting (every model
writes "four fifteen" as "4.15"), which scores as errors but changes nothing for
the interpreter.

---

## The default SER backend has no valence

**Status:** accepted trade-off, worth revisiting · **Affects:** `server/ser.py`

`emotion2vec_plus_base` is categorical only: it returns an emotion label and a
confidence, and `valence`/`arousal` come back `None`. The interpreter's
words-vs-voice mismatch rule is built around *valence*, so it currently reasons
from the label alone.

That measurably matters: on the pipeline eval, `qwen3:14b` uses a bare label well
(48% voice sensitivity) but `gemma3:12b` largely ignores it (6%) -- while gemma
scores 4/4 on the mismatch pairs when given numeric valence. So the current setup
depends on one model's willingness to act on a categorical hint.

Candidate: **`audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim`** -- 0.2B,
12 layers, variable-length (no 30 s padding), outputs valence/arousal/dominance
in our exact 0-1 format, trained on naturalistic podcast speech rather than acted
emotion. Dimensional-only (no categorical label) and CC-BY-NC-SA licensed.

**Check its valence distribution before trusting it**
(`python -m eval.ser_eval --profile-valence`). MERaLiON's spanned only 0.12-0.41
with a +0.085 pleasant/unpleasant separation, which made every utterance read as
"negative" against the default gloss thresholds -- the failure this check exists
to catch.

---

## Audio-native LLM could replace Whisper entirely

**Status:** idea, blocked on tooling · **Affects:** architecture

An audio-in LLM could take the waveform directly and produce transcript + tone in
one pass, removing Whisper and the valence hand-off.

Blocked: **Ollama does not support audio input** (tracking issue #15333), and the
whole server is built on its `/api/generate`. Gemma 4 does audio at E2B/E4B/12B
and accepts 16 kHz mono float32 -- exactly what the segmenter already emits, at
~25 tokens/sec, so a 4 s utterance is ~100 audio tokens, cheaper than the current
~600-token text prompt. But it needs `llama-server` instead of Ollama, and a
hands-on report calls E2B's transcription "far from a practical level".
Qwen3-Omni is far better at audio but every variant is 30B needing ~59-69 GB.

Two properties would be given up: **progressive disclosure** (today the transcript
ships as soon as Whisper finishes, with the read following) and **graceful
degradation** (today an Ollama outage still yields transcripts). Worth a measured
spike, not adoption on principle.
