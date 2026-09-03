# TODO

Known issues and deferred work. Each entry records what was measured, so the
next person doesn't have to rediscover it.

---

## The VAD cannot hear quiet speech (`SPEECH_RMS`)

**Status:** open · **Found:** 2026-09-03, while building `eval/pipeline_eval.py`
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

### Interim workaround for evals

Run the pipeline eval with `--speech-rms 150`, and filter `vad_dropped=1` rows
out when analysing **tone** accuracy — those rows carry garbled transcripts, so
the LLM judged them on wrong words. SER and WER columns are still usable.
