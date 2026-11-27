# TODO

Known issues and deferred work. Each entry records what was measured, so the
next person doesn't have to rediscover it.

---

## Long utterances: split into sentences after ASR (2026-11-26)

### The problem, from the real transcript log

Of the 256 utterances logged on real speech (2026-09-22 to 09-25), **140 (55%) ran into
the 15 s `MAX_UTTERANCE_MS` cap**. The median was 15 s and 25 words, and 48% held three or
more sentences. A read of a paragraph is too vague to help, and its SER label is an average
over every sentence in it. The cause is the VAD, not the talker: Parakeet's token timings
on one capped clip show 1.8 s and 3.5 s pauses that the energy VAD counted as speech,
because the room's background cleared the threshold.

### What was built

`server/sentences.py`: after ASR, cut at `.`/`?`/`!` using the per-token timestamps,
merge pieces under 1.5 s or 3 words into a neighbour, cap at 6, and slice the audio at the
gap midpoints. Each piece goes out as an ordinary utterance/read pair with its own id, SER
and read, the earlier pieces as context (`main.py`). Default on (`SENTENCE_SPLIT`).
`CLIP_RETENTION_N` went from 8 to 24 so a hold still covers ~45 s.

Checked on the 12 logged WAVs that kept audio, streamed through the wire protocol on a
scratch server: the 15 s clips became 2-5 pieces at the right sentence ends ("Oh. Oh my
God." stays one piece), short clips stayed whole, and the 5-piece argument got 5 distinct
reads instead of one. 12 clips is a smoke test, not an eval.

### Open: `voiced_s` undercounts quiet rooms, and the split makes it visible

`confidence.py`'s "too short to read the voice" uses `speaker.voiced_fraction`, which
counts frames above the FIXED `SPEECH_RMS` (500), not the segmenter's adaptive threshold.
These logged clips sit at RMS ~200-900, so a 15 s conversational clip counts as **2.2 s
voiced**, and 3-4 s sentence pieces as 0.2-1.6 s, under the 1.5 s floor. Most piece reads
therefore carry the R_SHORT penalty. The bug predates the split (it hit whole utterances
too) but now bites on nearly every piece. Fix candidate: pass the segmenter's threshold at
close into `voiced_fraction`, then re-run `eval.confidence_eval` (RAVDESS runs use
`--speech-rms 150`, so its `voiced_s` AUROC was measured on a different threshold).

### Not done

- The first read still waits for the utterance to close (up to 15 s). Shortening that is a
  VAD change (accept a shorter pause once an utterance runs long, or find why the room
  clears the threshold) and was left for after the split is lived with.
- Saved clips of a split utterance hold only the pieces' audio; long pauses between
  sentences (non-speech by ASR) are not in the WAV.

---

## The VAD cannot hear quiet speech (`SPEECH_RMS`)

**Status:** IMPLEMENTED 2026-09-20 (noise-floor-relative threshold); needs a
real-microphone confirmation · **Found:** 2026-09-03, while building
`eval/pipeline_eval.py` · **Confirmed on real hardware** during phone testing
· **Affects:** `server/segmenter.py`, `server/config.py`

### What was built (2026-09-20)

The threshold now tracks the room instead of assuming a level:

```
threshold = clamp(noise_floor * NOISE_MARGIN, SPEECH_RMS_MIN, SPEECH_RMS)
```

`noise_floor` is an exponential moving average of frame RMS over frames the
machine classes as NON-speech (idle frames and trailing silence), with two
rates: it falls fast (`NOISE_FLOOR_FALL` 0.3, so a quiet room is trusted within
a few frames) and rises slowly (`NOISE_FLOOR_RISE` 0.02, so a cough is a
transient, not a new room). Speech frames never update it, so a sentence cannot
lift the bar under itself. `SPEECH_RMS` (500) keeps its meaning as the ceiling,
`SPEECH_RMS_MIN` (150) is the floor -- digital silence settles there, which is
the measured RAVDESS workaround -- and `NOISE_MARGIN` (3.0, ~+9.5 dB) is the
one number that means "a margin above what this mic hears when nobody talks".
`VAD_ADAPTIVE=0` restores the constant threshold byte for byte, and
`pipeline_eval --speech-rms N` sets that automatically, because an explicit
number should mean a constant.

Measured: `python -m eval.pipeline_eval --vad-check` with no override now
segments **10/10** clips (was 5/10; the four sad/neutral drops included). The
segmenter self-test gained three cases: an RMS-212 tone segments adaptively and
is still dropped at a fixed 500; white noise at RMS ~400 clamps the threshold to
500 and produces no false trigger; a loud burst leaves the floor unchanged.
`remote.html`'s VAD strip shows the live floor next to the threshold.

Still open: none of this has been watched on the pendant's mic in a real room.
`MIN_UTTERANCE_MS` is unchanged -- a lower threshold makes more frames count as
voiced, which is what that second filter needed. The analysis below is kept
for the measurements.

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

### Options that were considered (the first one is what was built)

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

**Status:** default switched to `parakeet:v3` on 2026-09-22; the real-room
measurement is still owed · **Affects:** `server/config.py` (`ASR_MODEL`),
`server/transcriber.py`

Update 2026-09-22: `ASR_MODEL` now defaults to `parakeet:v3` (multilingual; it
was briefly `parakeet:v2` the same day). On the SAPI set v3 scored 6.0% WER at
0.10 s per utterance, level with `whisper:base`'s 6.0% at 0.26 s and one
numeral-formatting error behind v2's 4.8%; as a transducer it does not
hallucinate on silence, and it covers 25 languages at the same cost. `WHISPER_MODEL`
set explicitly still wins (`whisper:<name>`), so nobody's start script changes
behaviour silently. The caveat below is unchanged: this was decided on clean
synthetic speech, and a real recording must confirm it.

Update 2026-09-19: `transcriber.py` now carries a catalogue -- Whisper `base` /
`distil-small.en` / `small.en` / `distil-large-v3` / `large-v3-turbo`, Parakeet TDT
0.6B v2/v3, Moonshine tiny/base, SenseVoice small -- switchable live from the
dashboard. `python -m eval.asr_eval` scores all of them. Record real-room
utterances into `experiments/asr/` and re-score before trusting the choice.

`base` was the default and the weakest link once SER dropped to ~0.12 s --
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

**Status:** PARTLY IMPLEMENTED 2026-09-20 -- arousal and dominance are now
derived from the class distribution and the mismatch rule is rebuilt on them;
the guard rail fires on label-only voice; valence is deliberately left to the
LLM · **Affects:** `server/ser.py`, `server/interpreter.py`, `server/config.py`

### What was built (2026-09-20)

Following the "valence may be the wrong target" conclusion below:

- `ser.py` (`_analyze_emotion2vec`) now returns `arousal` and `dominance` as the
  probability-weighted mean of per-emotion anchors over the 7-class distribution
  it already computed (`_AROUSAL_ANCHOR` / `_DOMINANCE_ANCHOR`, textbook PAD
  placements), tagged `dims_source: "derived"`. `valence` stays `None`.
- `interpreter.py`: the prompt gains a dominance scale and the four-row
  arousal+dominance mismatch table (positive words + low/low = masking; +
  high/high = genuine; negative + high/high = real anger; negative + low/low =
  resignation), phrased to work with or without valence. `_describe_voice`
  passes dominance and appends "label only, no scales measured" when a label
  arrives with no numbers, and the closing caution now fires for "no voice
  field OR label only" -- the 2026-09-05 finding below is closed.
- `config.py`: `DOMINANCE_LOW/HIGH` (0.4/0.6). The `read` frame's `voice` object
  carries `dominance` (additive).

Profiled on 210 balanced RAVDESS clips (`python -m eval.ser_eval
--profile-valence`, which now prints all three axes and the two separations the
rule depends on):

| emotion | arousal | dominance |
|---|---|---|
| angry | 0.85 | 0.80 |
| surprised | 0.80 | 0.40 |
| happy | 0.67 | 0.59 |
| fearful | 0.67 | 0.24 |
| neutral | 0.30 | 0.50 |
| sad | 0.26 | 0.26 |

angry - sad: arousal +0.60, dominance +0.55; angry - fearful dominance +0.53 (the
axis arousal cannot see). Ranges 0.25-0.85 / 0.20-0.80, so the 0.4/0.6 glosses
are all reachable. The caveat stands: because emotion2vec is usually confident,
values sit at the anchors and only grade when the model is torn -- this is a
function of the label, not an acoustic measurement. `eval/tone_cases.jsonl`
gained two valence-free mismatch pairs (`mm-05`, `mm-06`) so the shipped shape
is finally scoreable.

`python -m eval.model_eval` on the revised prompt (34 cases x 3 runs, 3 models;
`eval/results/model.csv` is now this run):

| model | old 30 cases (was) | new mm-05/06 | literal / low-conf | mismatch pairs (of 6) |
|---|---|---|---|---|
| gemma3:12b | 90% (93%) | 50% | 100% / 100% | 3 |
| qwen3:8b | 80% (83%) | 50% | 100% / 100% | 4 |
| qwen3:14b | 90% (93%) | 75% | 100% / 100% | 2 |

The over-reading guard rails held. The 3-point dip on the old cases is one
case per model, mostly `mm-04a` ("It's fine.", neutral label, valence 0.62,
arousal 0.35, no dominance): the new low-arousal rule reads calm as masking
even after the prompt was tightened to require both axes and to let valence
decide direction. On the new valence-free pairs every model gets the
"deflated voice = masking" and "angry voice = real complaint" halves right;
what fails is `mm-06a` (stock sarcastic wording, bright high-arousal voice),
which all three still call sarcastic/negative -- the same over-firing shape as
the valence-era `mm-01a`/`mm-03a`. Open: a prompt that separates "calm" from
"deflated" without a dominance value, and whether `mm-06a`'s expectation is
fair.

Still open: the expected-value idea is only as good as the anchors, which are
literature placements rather than fitted values; nothing has been checked on a
real microphone; and the "does valence help over the transcript" experiment
proposed below has still not been run.

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

### The compensating guard rail does not fire (found 2026-09-05)

Worse than "no valence": the prompt paragraph that exists to cover for a missing
voice signal is skipped in exactly this case. `interpreter.py` ends with *"When
there is no `voice:` field you are reading text alone... be especially careful
not to over-read sarcasm into plain sentences."* On emotion2vec the `voice:`
field **is** present -- it just has no numbers. So the five mismatch rules that
key on valence are inert, *and* the caution that compensates for their absence
never triggers. The model is handed rules it cannot apply and denied the warning
that would make it fall back safely.

Concretely, what reaches the LLM:

```
default (emotion2vec):  voice sounded like: angry (conf 0.77)
MERaLiON:               voice sounded like: angry (conf 0.77), valence 0.13 (negative), arousal 0.68 (elevated)
```

`VALENCE_LOW/HIGH` and `AROUSAL_LOW/HIGH` are therefore unreachable on the
shipped default; only `SER_MIN_CONFIDENCE` does any work.

### Cheaper candidate than a second model: use the distribution we already have

`_analyze_emotion2vec()` computes a **full 7-class probability distribution** and
then discards everything but the argmax. Expected valence over that distribution
-- map each label to a valence anchor, take the probability-weighted mean --
would restore a *continuous* signal for no download, no extra forward pass and no
new dependency. It is not a true acoustic valence measurement (it is a function
of the categorical posterior, so it cannot see anything the labels don't already
encode), but it turns a hard label into a graded one, which is what the mismatch
rule needs to reason about degree.

Worth trying before `audeering`, and it can be compared against it on the same
eval. Either way `VALENCE_LOW/HIGH` must be re-profiled against the actual output
range first -- see the MERaLiON failure above.

### Measure the marginal gain before assuming valence is worth it (2026-09-10)

Two literature findings say the expected payoff is smaller than it looks, and both
argue for measuring rather than adopting:

1. **Acoustic valence may partly be laundered text.** The transformer gains that
   "closed the valence gap" come substantially from models implicitly encoding
   *linguistic* content, not from better acoustic modelling. A system that already
   has a transcript may therefore gain much less than the published CCC suggests.
2. **Text beat audio outright on the closest real-world analogue.** Continuous
   satisfaction/frustration tracking on real call-centre speech: linguistic-only
   **CCC 0.924**, acoustic-only **0.806**, best fusion **0.920** -- fusion did not
   beat text (arXiv 2310.04481).

So the experiment worth running is not "does valence help" but **"does valence help
*over the transcript the interpreter already has*"**. `eval/model_eval` can answer
this directly: the `mm-*` pairs hold text constant and vary only voice. See
`docs/design/RESEARCH.md` §4.2 and §4.4.

### STRONGER: valence may be the wrong target entirely (2026-09-10)

Wagner et al. (IEEE TPAMI) closed the valence gap to CCC 0.638 and then showed
*why* it worked -- verbatim from the abstract: their success on valence "is based
on **implicit linguistic information** learnt during fine-tuning of the transformer
layers, which explains why they perform on-par with recent multimodal approaches
that explicitly utilise textual information."

The cleanest confirmation is a distillation table. Wav2Small compresses a VAD
teacher into 72K parameters:

| | arousal | dominance | **valence** |
|---|---|---|---|
| teacher | ~0.73 | ~0.63 | **0.676** |
| 72K student | ~0.66 | ~0.56 | **~0.37** |
| relative loss | -10% | -11% | **-45%** |

Arousal survives compression; valence collapses. You cannot distil lexical
understanding into 72K parameters -- which is exactly the prediction if acoustic
valence is mostly implicit ASR.

**Two consequences, and the second is the important one:**

1. An acoustic valence model spends CPU re-deriving, worse, a signal the
   interpreter LLM already extracts from the transcript.
2. **A partly-lexical valence head tends to AGREE with the text, so it will
   under-fire on precisely the sarcasm cases the mismatch rule exists to catch.**
   That is a plausible mechanism for MERaLiON's measured 0.143 happy-vs-angry
   separation, and it is directly testable: check whether its valence output
   correlates more with a text sentiment score than with any prosodic feature.

**Revised recommendation: keep emotion2vec, let the LLM own valence from the
transcript, and rebuild the mismatch rule on arousal + dominance** -- CCC 0.745
and 0.655, genuinely paralinguistic, and unavailable from text:

| words say | arousal | dominance | likely reading |
|---|---|---|---|
| positive | low | low | masking / flat compliance / "fine" that isn't |
| positive | high | high | genuine enthusiasm |
| negative | high | high | real anger or complaint |
| negative | low | low | sadness, resignation |

The expected-valence-from-distribution idea above is still the cheapest way to get
a graded signal, but it should now target **arousal and dominance anchors**, not
valence. Full argument and ranked alternatives in `docs/design/RESEARCH.md` §4.5-4.6 and §6.2.

Licence note for whoever evaluates `audeering/...-msp-dim`: it is
**CC-BY-NC-SA-4.0, research only**. Fine as an offline measurement instrument,
fatal if emtext ever ships as more than a personal project. The Odyssey 2024
WavLM baselines (`3loi/SER-Odyssey-Baseline-WavLM-*`) are **MIT** but 0.3B
parameters -- slower than the MERaLiON already rejected.

---

## A typed classifier between SER and the LLM does not help (Laya)

**Status:** measured 2026-09-21, NOT adopted; kept as a test bed · **Affects:**
nothing in `server/`; `experiments/laya/` (worker, bench, results, `docs/reports/laya.md`)

Tried `convaiinnovations/laya` (Apache-2.0, ModernBERT-large + decision head,
421M, one non-autoregressive pass, typed answers with probabilities, no
generation) in three roles between `ser.py` and `interpreter.py`: a words-only
polarity channel fused with SER arousal/dominance by a fixed table, an arbiter
that vetoes the LLM's tone, and a gate that skips the LLM on lines with no
subtext. Seven runs over `eval/tone_cases.jsonl`, `eval/results/model.csv` and
`eval/results/pipeline.csv`; the voice hint shown to Laya is
`interpreter._describe_voice`'s text, byte-identical to the LLM's. Full
write-up with method and per-row findings: `docs/reports/laya.md`.
The harness (worker + bench + per-row CSVs) was removed after the decision
(commit `e04d440` has it, if a fine-tuned checkpoint or a smaller sentiment
model ever needs scoring the same way); the report is what stays.

### What was measured

Tone cases (34; 6 mismatch pairs):

| run | acc | pairs | literal | low-conf | mismatch | sarcasm | PA |
|---|---|---|---|---|---|---|---|
| words only, Laya tone | 0.559 | 0/6 | 5/5 | 2/2 | 5/12 | 0/4 | 0/4 |
| words polarity x SER table | 0.735 | 4/6 | 5/5 | 2/2 | 10/12 | 1/4 | 0/4 |
| words + voice hint, Laya tone | 0.588 | 2/6 | 5/5 | **0/2** | 8/12 | 0/4 | 0/4 |

p(sarcastic) never exceeds 0.24 on any case (ECE 0.29; 31/34 answers in the
0-0.2 bin, where 39% are sarcastic). "Wow, you actually showed up on time
today" is `positive` 0.62. The hint breaks the low-confidence guard: "angry
(conf 0.22) -- low confidence" makes a meeting reschedule `negative` 0.64 --
Laya reads the label and ignores the caveat.

Veto over `model.csv` (rule a: LLM says sarcastic, Laya p(sarc) < theta ->
Laya's tone; rule b: LLM says neutral, Laya says negative/sarcastic/mixed >
theta -> Laya's tone):

| model | before | theta 0.3 | 0.5 | 0.7 | sarcasm before -> 0.7 | pairs before -> 0.7 |
|---|---|---|---|---|---|---|
| gemma3:12b | 0.853 | 0.676 | 0.706 | 0.735 | 9/12 -> 0/12 | 3/6 -> 2/6 |
| qwen3:14b | 0.882 | 0.765 | 0.794 | 0.824 | 12/12 -> 3/12 | 2/6 -> 4/6 |
| qwen3:8b | 0.765 | 0.647 | 0.676 | 0.706 | 9/12 -> 6/12 | 4/6 -> 3/6 |

Every theta lowers every model. Because p(sarc) is always low, rule (a) fires
on every LLM `sarcastic` answer and deletes true sarcasm; theta <= 0.6 also
zeroes the low-confidence guard through rule (b). The qwen3:14b pair gain
(the over-calling this was meant to fix) costs 9 sarcasm cases.

Pipeline (RAVDESS, 872 rows, theta 0.7, majority-class baseline 0.57): fused
== LLM in every cell, no rule ever fires (neutral text, so the LLM never says
sarcastic; Laya never crosses 0.7). Laya alone: 0.25 on the three emotion2vec
cells (LLM 0.405 / 0.795 / 0.312) -- it answers `sarcastic` on 100/259 rows,
"Kids are talking by the door" with an angry/fearful/sad hint, the exact
over-reading the prompt guards against. The 0.703 on meralion+qwen3:8b (LLM
0.242) is mostly majority class: 90/91 hints gloss valence "(negative)"
(MERaLiON range 0.12-0.41), Laya says `negative` 60/91, right on 48/51 true
negatives and 6/14 happy.

Gate on p(worth_reading): lost tracks skip ~1:1 on every source (tone cases
with voice theta 0.3: skip 0.676 / lost 0.75; pipeline theta 0.5: 0.273 /
0.179, theta 0.7: 0.716 / 0.685) -- near random at separating subtext from
plain speech, and a missed sarcastic line is the product's worst error.

Cost (CPU, warm, n = 100): one question p50 189 ms; three p50 449 / p95 582
ms; three while Whisper base transcribes concurrently p50 625 / p95 727 ms.
The whole audio stage is 0.30 s and the LLM 0.6-0.9 s, so Laya would be the
long pole of the audio stage or +50% on the serial path. 1991 MB RSS, 846 MB
fp32 download, 16.5 s cold load, second venv (transformers 5 vs the server's
`<5` pin). Deterministic across calls -- the one property the LLM lacks.

### What is open

- The two problems this targeted are unchanged: `qwen3:14b` over-calls
  sarcasm on a confidently happy voice (2/6 pairs), gemma / qwen3:8b miss
  `sar-04`. Both remain prompt and data work, not arbiter work.
- Zero-shot only. A fine-tuned tone/sarcasm head on our labels was not tried
  because there are 34 labelled lines and no real sarcastic speech; more
  labelled mismatch pairs are the prerequisite (see "Sarcasm is never tested
  on real speech").
- If a words-polarity channel is ever wanted (the fixed table did recover
  4/6 pairs), a ~20-60M sentiment model at ~10x less CPU in the server venv
  is the right shape, not a 421M zero-shot QA model. Any candidate must
  clear p(sarcastic) > 0.5 on `sar-01..04` before the veto run is worth
  repeating (restore the harness from `e04d440`).

---

## Sarcasm is never tested on real speech

**Status:** open, undocumented until now · **Found:** 2026-09-05, auditing what
the server actually checks · **Affects:** `eval/tone_cases.jsonl`,
`eval/pipeline_eval.py`

Sarcasm is the product's headline capability and **no test anywhere uses a real
human being sarcastic**. Three separate gaps stack up.

### The mismatch pairs test an input production never emits

The four `mm-*` pairs are the only check that the voice signal is used at all.
Their `voice` dicts are hand-typed JSON literals, and their valence separation is
far wider than any real measurement:

| pair | valence a / b | gap |
|---|---|---|
| mm-01 | 0.86 / 0.13 | 0.73 |
| mm-02 | 0.83 / 0.18 | 0.65 |
| mm-03 | 0.79 / 0.11 | 0.68 |
| mm-04 | 0.62 / 0.15 | 0.47 |

Measured MERaLiON valence over all 1440 RAVDESS clips (`eval/results/ser.csv`):
happy mean **0.329**, angry mean **0.187** -- a real separation of **0.143**,
three to five times narrower than the synthetic pairs. And on the *default*
backend there is no valence at all, so these cases exercise a code path the
shipped configuration never reaches.

So the suite proves the LLM reasons correctly over a clean, well-separated voice
field. It cannot show the field is ever that clean -- and `ser.csv` says it isn't.

### RAVDESS is the wrong shape for sarcasm, permanently

Eight *acted* basic emotions over two semantically neutral sentences ("Kids are
talking by the door"). Sarcasm needs words that assert something and prosody that
contradicts them; RAVDESS is emotional prosody over emotionally null words -- the
opposite arrangement. No amount of tuning makes it a sarcasm benchmark.

The numbers confirm it. Across all **942** end-to-end audio rows in
`eval/results/pipeline.csv`, `llm_tone` was:

| neutral | negative | positive | mixed | **sarcastic** |
|---|---|---|---|---|
| 581 | 300 | 60 | 1 | **0** |

and `tone_expected` only ever contains negative/positive/neutral. Sarcasm has
never been emitted on real audio and is not even scoreable there.

### `experiments/make_mismatch.ps1` is a demo, not an eval

Three unlabeled, unscored SAPI TTS lines, referenced by nothing under `eval/`.
Note that the prosody literature backs two of its three knobs -- sarcasm
correlates with reduced pitch and slower rate -- but reports that **amplitude
does not** differentiate, so its `volume="soft"` is not evidence-based.

### What would actually close this

A corpus of real sarcastic speech. **MUStARD** (MIT licence, 690 audiovisual
utterances) and **MUStARD++** (1,202 utterances, 601 sarcastic / 601 not) are the
obvious candidates, both sourced from sitcoms. Caveat before anyone downloads
them: sitcom audio carries laugh tracks, music and overlapping speakers, so it is
usable for scoring *the interpreter* on known-sarcastic clips and a poor fit for
judging the VAD or WER. Even 20-30 self-recorded clips of one sentence said
sincerely vs sarcastically would be a stronger test of the core claim than
anything currently in the repo.

Cheap interim step that needs no audio: add mismatch pairs whose valence gap is
~0.14 rather than ~0.7, and label-only pairs with no valence at all, so the suite
can score the default configuration instead of an idealised one.

### What "good" actually looks like (2026-09-10) -- set the bar honestly

Before building a sarcasm benchmark, know the ceilings, because the current eval's
93% invites a badly miscalibrated target:

- **Humans reach ~81-85%** on text with full conversation context, and only
  **~68% on sarcasm specifically** in naturalistic video *with audio, video and
  context all available* (RISC). There is no regime where humans are near-perfect.
- **Sarcasm labels are themselves unreliable.** MUStARD's inter-annotator
  agreement is Cohen's **k = 0.15**; MUStARD++ later corrected **343 of its 690**
  labels. On iSarcasm, third-party annotators **missed 30% of author-intended
  sarcasm** and **45% of what they called sarcastic was not intended as such**.
- **Spontaneous sarcasm is not acoustically marked.** Rockwell found listeners
  could discriminate *posed* sarcasm but **not spontaneous** sarcasm; Bryant &
  Fox Tree found only amplitude variability differed in real spontaneous speech.
  "There is no single ironic tone of voice" is replicated across labs.
- **Adding audio makes current sarcasm models worse**, not better: +8-13 points of
  false positives with only 7-10 points of false-negative reduction, and
  prosody-only collapses to 14-22% F1. Manipulating *only* pitch and pause length
  on non-sarcastic clips drove false positives to 60%.

That last point names a failure emtext already exhibits: `qwen3:14b` scores 100%
on the sarcasm category and 2/4 on mismatch pairs **because it fails the sincere
halves**. That is sarcasm over-firing, which for this product is the more damaging
error -- it teaches the listener to distrust ordinary speech. Full citations in
`docs/design/RESEARCH.md` §2.

---

## The model table hides a deterministic sarcasm failure (`sar-04`)

**Status:** DONE 2026-09-18 -- `README.md` now carries the sarcasm column, names
`sar-04` as the deterministic miss, describes the two opposite failure shapes,
and says the shipped default is `qwen3:14b` (it used to say gemma). Kept for the
numbers · **Found:** 2026-09-05 · **Affects:** `README.md` model comparison table

Recomputed from `eval/results/model.csv` (270 rows = 30 cases x 3 models x
3 runs):

| model | overall | sarcasm | mismatch pairs |
|---|---|---|---|
| gemma3:12b | 93.3% | **9/12 (75%)** | 4/4 |
| qwen3:8b | 83.3% | **9/12 (75%)** | 4/4 |
| qwen3:14b | 93.3% | **12/12 (100%)** | 2/4 |

The README table carries overall / mismatch / speed but **no sarcasm column**,
and describes `gemma3:12b`'s weakness as an "occasional passive-aggression miss".
Every sarcasm miss in the file is the same case -- `sar-04`, *"No, please, take
your time. It's not like I have anywhere to be."* -- and both `gemma3:12b` and
`qwen3:8b` fail it on **all three runs**. It is deterministic, not occasional,
and it is the most conversationally common form in the set (sarcasm carried by an
explicit negation rather than by an intensifier like "oh great").

Note the shape of the two failures is opposite, and the table currently makes
them look alike: `qwen3:14b` detects sarcasm perfectly and **over-applies** it
(its 2/4 comes from failing the `a` halves, where a confidently happy voice
should rule sarcasm out), while the other two **under-detect** it. For this
product those are not equally bad -- a false positive teaches the listener to
distrust ordinary speech.

The README used to omit the sarcasm column and call `gemma3:12b` "the right
default" while `config.py` shipped `qwen3:14b`. Both are fixed; the case itself
(`sar-04` failing deterministically on two of three models) is still open and
lives under "Sarcasm is never tested on real speech" above.

---

## Irony is not modelled, and an "ironic" answer is silently lost

**Status:** the silent loss is FIXED 2026-09-20 (`_TONE_SYNONYMS` in
`interpreter.py` maps `ironic` / `irony` / `sardonic` / `sarcasm` to `sarcastic`
and `ambivalent` / `conflicted` to `mixed` before the enum check); a real
`irony` tone remains deliberately unmodelled · **Found:** 2026-09-05 ·
**Affects:** `server/interpreter.py`

There is no mention of irony anywhere in the repo -- a case-insensitive search
for `iron(y|ic)` across `.py`, `.html`, `.md`, `.jsonl` and `.txt` returns
nothing. `TONES` is `("positive", "negative", "neutral", "sarcastic", "mixed")`,
so irony is folded entirely into "sarcastic".

That is a defensible product decision, but it has an undefended edge. The
response validator does:

```python
if tone not in TONES:
    tone = "neutral"
```

So a model that answers `"ironic"` -- a plausible thing for an LLM to volunteer,
and the `RESPONSE_SCHEMA` enum only constrains models whose Ollama build honours
schemas -- is recorded as **neutral**, the maximally wrong bucket, rather than
the adjacent "sarcastic". A one-line synonym map (`ironic`/`sardonic` ->
`sarcastic`) would fail safe instead.

Adding a real `irony` tone is a bigger decision: it would invalidate the eval
baseline and require re-running `python -m eval.model_eval`, and the
sarcasm/irony boundary is subtle enough that labelling it consistently in
`tone_cases.jsonl` is its own project.

---

## The server's memory is dominated by one checkpoint (mostly fixed)

**Status:** largely fixed 2026-09-10, one avenue left · **Affects:**
`server/ser.py`, `server/config.py`, `eval/slim_ser_ckpt.py`

The running server was using **3,055 MB working set / 4,041 MB private**. Staged
measurement (reproducing the live process to within 4 MB):

| stage | RSS | delta |
|---|---|---|
| bare python | 25 MB | |
| + torch | 198 MB | +162 |
| + whisper `base` int8 | 368 MB | +156 |
| + funasr import | 552 MB | +184 |
| **+ emotion2vec SER model** | **3,048 MB** | **+2,496** |
| + fastapi/uvicorn/httpx | 3,051 MB | +4 |

The SER load was **82% of the process**, against a live model of **358 MB**. Two
causes, multiplying:

1. **The checkpoint is two-thirds training baggage.** `model.pt` is 1,066 MB:
   `model` 355.4 MB, `last_optimizer_state` **710.9 MB** (Adam momentum +
   variance, exactly 2x the weights), everything else ~0.
2. **FunASR deep-copies it.** `load_pretrained_model.py` does `torch.load(path)`
   then `copy.deepcopy(ori_state)` before extracting the one key it wants. Peak
   1066 + 1066 + 355 = 2,487 MB, matching the measured +2,496 MB.

And **the peak is permanent** -- `del` + `gc.collect()` frees nothing measurable,
because the Windows allocator does not return the pages. Peak allocation *is*
steady-state RSS, so the only lever is allocating less.

### Fixed

- `python -m eval.slim_ser_ckpt` writes `models/emotion2vec_plus_base_slim/`
  with the optimizer state stripped: 1,066 MB -> 355 MB. `config.SER_MODEL_DIR`
  picks it up automatically when present, falls back to the hub id when absent.
  The ModelScope cache is never modified.
- `ser._patch_funasr_deepcopy()` removes the redundant copy, re-deriving the
  safety invariant (`ori_state` dead after the copy, `src_state` never assigned
  into) from the installed source each time, and failing open.

**Result: 3,055 MB -> 1,278 MB working set, 4,041 -> 2,086 MB private.** SER
output is **bit-identical**: 160 RAVDESS clips, 0 prediction mismatches, max
confidence delta 0.000000 against the original checkpoint.

### Still open: FunASR imports its entire package

`funasr/__init__.py` ends with `import_submodules(__name__)`, which walks and
imports **all 431 of its modules** to give you one class. That drags in
transformers, modelscope, librosa, numba, llvmlite, scipy and sklearn -- none of
which the emotion2vec inference path uses. Measured cost: **+184 MB**.

Note this defeats `ser.py`'s careful deferral of `transformers` (it is imported
only inside `_load_meralion()`): modelscope's lazy-import shim imports it anyway,
transitively, on the emotion2vec path. FunASR does have a lazy `__getattr__`, but
`import_submodules` runs unconditionally first, so it buys nothing.

Not obviously fixable without either vendoring the two modules we need or
pre-empting `sys.modules`, both uglier than 184 MB is worth. Recorded so the next
person does not re-derive it.

### The emergency lever

`SER_ENABLED=0` drops the whole thing -- torch, funasr, transformers, modelscope
and the model -- for about **1.1 GB**, at the cost of the `voice` field entirely.
The guard returns before `import torch`, so nothing heavy is imported at all.

---

## Audio-native LLM could replace Whisper entirely

**Status:** MEASURED 2026-09-22, NOT adopted · **Affects:** architecture

### Measured (2026-09-22)

Three local audio LLMs on llama.cpp (Vulkan, Arc B580), same 280 RAVDESS clips
and baselines as `pipeline.csv`, production prompt verbatim. Full report:
`docs/reports/audio-llm.md`. The harness was removed with the decision; the
report records the method in enough detail to rebuild it.

| model | audio tone / v.sens | `neutral` share | follows wrong hint / audio | RAVDESS WER | audio latency |
|---|---|---|---|---|---|
| gemma4-e4b (Q4_0) | 14% / +0 | 100% | 91% / 0% | 11% | 0.90 s |
| voxtral-mini (Q4_K_M) | 14% / -4 | 96% | 99% / 0% | 9% | 0.92 s |
| qwen25-omni-7b (Q4_K_M) | 16% / -1 | 98% | 25% / 4% (72% neither) | 3% | 1.27 s |
| qwen3:14b + emotion2vec (current) | 81% / +48 | -- | -- | 2% (parakeet) | 0.30 + 0.89 s |

They do not hear tone (audio is indistinguishable from the text-only floor),
two of three follow a five-word wrong transcript over the delivery every time,
and none is faster than the CPU ASR + GPU LLM split or fits beside qwen3:14b
in 12 GB. The prediction below held locally, and worse than the published
numbers. Original entry kept for the record:

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

### Downgraded 2026-09-10: there is now evidence against this

Audio LLMs largely do not listen. On 2,000 adversarial items pairing audio with a
transcript asserting the *wrong* paralinguistic attribute, **GPT-4o Audio scored
8.6% on ground truth while agreeing with the misleading transcript 81.6% of the
time**; the mean across 12 models was 15.3% vs 64.3% (arXiv 2605.27772,
corroborated by the independent LISTEN benchmark). Layer probing found the
acoustic information is present in the encoder and degrades at the encoder-LLM
interface.

That is fatal to the specific gain this entry proposes. The mismatch rule needs
**two independent channels**. If the voice reading comes from an audio LLM it is
largely a laundered transcript -- and since the interpreter already receives the
transcript, the rule would be comparing the text against itself and reporting
confident agreement rather than information. A separate acoustic-only SER model
is architecturally correct *because it cannot cheat*.

Revised status: **not a cost/latency question, a signal-independence question.**
If this is ever spiked, the thing to measure first is whether the audio path
disagrees with the transcript when the transcript is wrong. See `docs/design/RESEARCH.md` §4.3.

---

## No diarization: every voice in the room is treated as one speaker

**Status:** IMPLEMENTED 2026-09-18 as `server/speaker.py` (user / other / mixed
labelling; user lines get no read; `/enroll.html` -> `POST /api/enroll`,
`DELETE /api/speaker`; scored by `python -m eval.spk_eval`) -- see "What was
built" below. What remains open is measurement, not code: the four questions at
the end still stand and none has been attempted · **Found:** 2026-09-10, investigating `docs/design/RESEARCH.md` §5.2 · **Affects:**
`server/speaker.py`, `server/main.py`, `server/interpreter.py`, the `utterance`
and `read` frames, and every conversational-dynamics feature `docs/design/RESEARCH.md` §6.3
gates on diarization

emtext currently interprets the microphone, not a speaker. The user's own voice,
their conversational partner's, and a television are one undifferentiated stream,
so the interpreter is regularly handed the user's own words and asked what the
"speaker" meant by them. `docs/design/RESEARCH.md` §1.5 says pointing the interpreter at the
speaker rather than the user is the thing emtext gets *right*; without speaker
identity that is true only by luck.

### The segmenter cannot see turn changes (this is the load-bearing finding)

Measured with the real `Segmenter` (`SPEECH_RMS=150`, RAVDESS pairs, 40 trials
per gap): two different actors, A then B, separated by a gap.

| gap between turns | utterances produced | one glued utterance |
|---|---|---|
| 100 ms | 1.07 | **92%** |
| 200 ms | 1.05 | **95%** |
| 400 ms | 1.02 | **92%** |
| 600 ms | 1.15 | **85%** |
| 800 ms | 1.80 | 15% |
| 1000 ms | 1.80 | 18% |

`END_SILENCE_MS` is 650 and Stivers et al. put the cross-language response-gap
mode at **0-200 ms** (§5.1). So a *normally fast* reply is glued to the question
it answers, and the cliff sits just above the range where real conversation
lives. **One speaker label per utterance is therefore not sufficient**, and
neither is lowering `END_SILENCE_MS` on its own -- 200 ms of trailing silence is
also mid-sentence breathing, so that trades this bug for sentence fragments.

### CAM++ is already installed and costs almost nothing

`funasr` (already a dependency for the emotion2vec SER backend) ships the CAM++
speaker-embedding model, the spectral-clustering backend, and the `sv_chunk` /
`distribute_spk` helpers. `AutoModel(model="cam++")` resolves to
`iic/speech_campplus_sv_zh-cn_16k-common` (~27 MB) and emits a 192-d embedding.
It loads with `trust_remote_code: False`, i.e. the same safety posture `ser.py`
already documents for emotion2vec.

Measured on this machine (CPU, 12 cores):

- **20 ms per 2 s utterance** -- against 87 ms for emotion2vec SER and ~300 ms
  for Whisper `base`. It disappears inside the existing `asyncio.gather`.
- **No measurable peak-RSS increase.** The weights are ~30 MB and torch/funasr
  are already resident because SER loaded them.
- One new dependency: `kaldi-native-fbank` (308 KB wheel). FunASR needs an fbank
  backend for CAM++ and emotion2vec does not, so this path is currently missing
  it. `torchaudio` also satisfies it and is much larger.

### How well it actually separates speakers (RAVDESS, 24 actors, 1407 clips)

RAVDESS is the right adversarial test: every actor speaks the *same two
sentences*, so lexical content is controlled and any separation is speaker
identity. Mean trimmed clip length is 1.7 s -- emtext's real utterance length.

Textbook verification (one utterance vs one enrolled profile):

| enrollment | EER |
|---|---|
| 6 calm/neutral clips | 14.8% |
| 8 clips spanning all 8 emotions | 11.8% |
| + AS-norm against a 200-clip impostor cohort | **6.3%** |

**Emotion degrades own-voice match, and this is the trap.** Same-speaker cosine
against a calm-enrolled profile, by the emotion of the test clip:

```
calm 0.758 | happy 0.606 | disgust 0.600 | sad 0.600
surprised 0.596 | angry 0.543 | fearful 0.522
```

Enrol only on calm speech and a fixed threshold rejects the user *precisely when
they are upset* -- the state the app exists to be useful in. Enrollment must
span the emotional range; it is worth 3 EER points on its own.

### The decision rule matters more than the model

emtext is not doing open-set verification. It hears a conversation, almost
always two people, and only has to answer "is this the user". Classifying each
utterance by **nearest centroid**, where the partner's centroid is accumulated
online from utterances already rejected as not-the-user (120 random actor pairs):

| enrollment | rule | accuracy | user kept | partner kept |
|---|---|---|---|---|
| calm | fixed threshold 0.45 | 88.2% | 89.4% | 87.1% |
| calm | online partner model | 89.5% | 88.7% | 90.4% |
| spread | fixed threshold 0.45 | 87.5% | 90.0% | 85.0% |
| **spread** | **online partner model** | **94.3%** | **95.9%** | **92.7%** |

Same-sex pairs 91.6%, opposite-sex 97.7%. The online partner centroid needs no
enrollment from anyone but the user and is worth ~7 points.

### Do NOT try to cut a glued utterance into pieces

Two rules were measured on utterances known to contain a turn change:

1. **Blind 2-means over 1 s / 0.25 s windows.** Given that there are two
   speakers it finds the seam in 60/60 clips, median boundary error 215 ms, for
   212 ms of CPU. But *deciding* there are two speakers fails badly: on
   single-speaker audio the cluster distance is 0.428 (sd 0.137) against 0.594
   (sd 0.121) for two speakers, so at any usable threshold **24-84% of
   single-speaker utterances get falsely cut in half.** One person shifting
   emotion mid-utterance looks exactly like a second person.
2. **Windows scored against the enrolled user centroid** (a supervised 1-vs-rest
   question instead of an unsupervised one). Better, still not good enough: at
   thr 0.40 / 3-window run, 19% false splits against 61-74% of real turn changes
   caught.

Splitting the audio means handing the interpreter half a sentence and inventing
a speaker who was never there. The failure is silent and unfalsifiable from the
transcript, which is the same shape as the VAD bug at the top of this file.

### What does work: label the utterance, don't cut it

The *fraction of 1 s windows matching the enrolled user* separates cleanly:

```
user only 0.75 | mixed (contains a turn change) 0.43 | partner only 0.08
```

Thresholding that fraction at lo=0.15 / hi=0.75 gives a 3-way label:

```
              user  partner  mixed
user           65%      7%     28%
partner         1%     84%     14%
mixed           4%     13%     83%
```

78.9% overall, and the number that actually matters -- **a partner's line
mislabelled as the user's, i.e. silently dropped, is 1.4%.** The errors are
concentrated in the safe direction: 28% of the user's own lines land in `mixed`
and simply get interpreted anyway, which is exactly today's behaviour.

### What was built (2026-09-18)

`server/speaker.py`, same contract as `ser.py` (loaded once, CPU, never raises).
A third job in the `asyncio.gather` in `main.py`; windows are gated on the VAD
threshold (a window on the pre-roll / trailing silence embeds as noise: cosine
0.0 and -0.09 to the speaker's own centroid, measured) and the utterance label
is the fraction of voiced windows matching the enrolled user. Enrolment is a
guided page (`/enroll.html` -> `POST /api/enroll`, segmented with the real
`Segmenter`, embeddings only on disk); the partner centroid is per connection.
`user` lines go into the interpreter context via `Interpreter.remember()` and
get no read; `mixed` and `unknown` are interpreted as before.

Re-measured with the shipped code (`python -m eval.spk_eval`, seed 7):

| check | measured |
|---|---|
| EER, calm enrolment / all-8-emotion enrolment | 12.4% / 9.2% |
| same-speaker cosine vs calm profile, calm -> fearful | .76 -> .54 (the emotion trap, unchanged) |
| pairs, spread + online partner | **94.5%** (user kept 94.7, partner kept 94.2) |
| pairs, same-sex / opposite-sex | 90.6% / 97.6% |
| 3-way on glued turns | 80.7% overall; partner-as-user **2.2%**; mixed-as-user 15% |

That last number is the one to keep an eye on: an utterance that holds a turn
change and gets labelled `user` is dropped along with the partner's half of it.
User-only window fraction measured 0.86, so `SPEAKER_USER_FRAC_HI` could go up
to ~0.8 at the cost of more `mixed` -- not tuned, because none of this has been
measured on a real room yet. Speaker-id latency in the live gather measured
83 ms mean / 247 ms p95 (3-4 window embeddings, contending with Whisper).

`eval/model_eval` was re-run after the change: the unlabelled prompt is
byte-identical (asserted in code), so the tone numbers are unchanged within
run-to-run variance.

### Proposed shape (as implemented; kept for the reasoning)

- New `server/speaker.py` with the same contract as `ser.py`: loaded once, CPU,
  **never raises**, returns None when unavailable, and the rest of the codebase
  knows only `speaker.identify()` and `speaker.available()`.
- A third job in the existing `asyncio.gather` in `_process_utterance`, so it
  costs max(), not sum().
- Additive `"speaker": {"label": "user"|"other"|"mixed"|"unknown", "score": f}`
  on the `read` frame. Optional and omittable -- same rule as `voice`.
- Enrollment is a new `/api/enroll` route storing a centroid, plus online
  adaptation of the partner centroid per connection (it must reset per
  connection; it is a property of the conversation, not of the user).
- The interpreter prompt gets told whose line it is reading, and `own-voice`
  lines stay in the rolling context (they are what the partner is responding to)
  but do not get a read of their own.

### Still open (none attempted as of 2026-09-18)

- All of the above is clean studio audio, one voice per file. A real room adds
  reverberation, overlap, and a shared mic. **Nothing here has been measured on
  a real two-person recording** and it should be before any of it is trusted.
- On a head-worn ESP32 mic the user's own voice is close-miked and far louder
  than anyone else's. Plain RMS plus spectral tilt may do much of this work for
  free, and should be measured as a baseline *before* adding a model.
- CAM++ `zh-cn` was used on English speech. ModelScope publishes
  `speech_campplus_sv_en_voxceleb_16k`, but funasr's `AutoModel` does not
  register it (`RuntimeError: model ... is not registered`); the weights would
  have to be loaded into `funasr.models.campplus.model.CAMPPlus` by hand. All
  numbers above are therefore a floor, not a ceiling.
- Speaker embeddings are **biometric data**. §6.7's note on EU AI Act Art. 5(1)(f)
  applies with more force to a stored voiceprint than to a transient SER call.

Reproduce: `python -m eval.spk_eval` (the keeper version of the original
throwaway probes; `--real me.wav conv.wav` is the tool for the real-room
measurement above).

---

## Confidence on every read (built 2026-09-22)

Every `read` frame now carries `"confidence": {"score", "level", "reasons"}`,
computed in `server/confidence.py` (pure, self-tested) from signals the pipeline
already had and threw away: the probability Ollama assigns to the first token
of the chosen tone (`logprobs`, pre-grammar-mask, so it is a real distribution
over the enum), the transcriber's own confidence, SER confidence, voiced
seconds, and the speaker label. RESEARCH.md §4.8 / §4.10 / §6.5: calibrate, abstain
on short utterances, and suppress rather than hedge. Browser hides a `low` read
behind a "not sure" chip; the pendant renders it faint; the server always sends it.

### Measured (`eval/results/pipeline_conf.csv`, emotion2vec x qwen3:14b, 280 RAVDESS clips, parakeet:v3)

Reproduce: `python -m eval.confidence_eval --csv eval/results/pipeline_conf.csv --fit`.
"Correct" means the tone matched the acted emotion; overall tone accuracy 76.4%.

| signal              | AUROC vs tone_correct | verdict |
|---------------------|-----------------------|---------|
| `tone_prob` (LLM)   | 0.812                 | the base score; wrong reads sit at 0.6-0.9, right ones at ~1.0 |
| composite score     | 0.842                 | penalties help a little over tone_prob alone |
| `voiced_s`          | 0.610                 | weak but real; keeps the §4.10 short-clip penalty |
| `ser_confidence`    | 0.547                 | coin flip: emotion2vec is confident whether or not the LLM is right |
| `asr_conf`          | 0.452                 | coin flip here (RAVDESS transcripts are near-perfect); kept only as a misheard-words flag at 0.35 |

- **Platt scaling gains nothing.** The fit (a=0.35, b=-0.94) moves ECE 0.187 to
  0.186. The identity is shipped; `--fit` only recommends values when ECE
  improves by 0.01 or more.
- **Shipped cut-offs** `CONFIDENCE_HIGH=0.9`, `CONFIDENCE_LOW=0.6` give the
  listener: high 63 reads (22%) at 96.8% accuracy, medium 176 (63%) at 83.0%,
  low 41 (15%) at 17.1%. The old defaults (0.7 / 0.4) put 8 reads in `low` at
  12.5% and 207 in `medium` at 72.9%, i.e. nearly everything looked the same.
- Composite ECE is 0.145; the score is over-confident in the 0.6-0.8 bins, which
  is the "too short" band (see below), and honest above 0.9.

### Caveats

- **68% of RAVDESS clips have under 1.5 s of voiced audio**, so nearly every
  RAVDESS read carries the `too short to read the voice` penalty and the bulk
  of scores land at 0.6-0.7. Live speech is longer; expect more `high` reads
  there than the table suggests. The 1.5 s floor is §4.10's number, not fitted.
- `asr_conf` is backend-specific (exp of Whisper's mean `avg_logprob`, exp of
  Parakeet's minimum token log-prob) and has only been profiled on clean
  speech. Measured on `experiments/asr`: parakeet:v3 0.93 on a clean line,
  0.53 on the "415" line; whisper:base 0.77. Noisy-room numbers are unknown.
- The SER threshold `SER_MIN_CONFIDENCE=0.4` is also written as "about 0.4"
  **inside `SYSTEM_PROMPT`**; the env var does not change the prompt text.
- `interpreter offline` now scores 0 / `low` with its own reason, so the client
  can finally tell "unsure" from "dead" without `neutral` changing meaning.

### Open

- Fit the cut-offs on real conversation once the transcript log has a few
  hundred labelled lines; RAVDESS is acted, studio, single-speaker.
- `mixed` speaker label and `mixed` tone penalties are untested (RAVDESS never
  produces either).

---

## The headline is the SER's emotion; the interpreter adjusts it (built 2026-09-22)

The listener found the five-way tone verdict (positive / negative / neutral /
sarcastic / mixed) less useful than the seven emotions the voice model already
produces, so the roles were swapped: **the SER label is the primary source and
the LLM supports it**. The `read` frame now carries
`"emotion": {"label", "voice", "adjusted"}` in `ser.EMOTIONS`; the LLM's
schema requires an `emotion` and the prompt tells it to START from the voice
label and move it only when the words make it untenable. Interpreter offline =
the voice label untouched; SER off = words alone. `tone` stays for sarcasm and
mixed lines and for older clients. Every client colours by `emotion.label`;
the pendant (fw 0.2.0) maps the six non-neutral emotions onto the six chromatic
Undertale souls.

### Measured (`eval/results/pipeline_emotion.csv`, emotion2vec x qwen3:14b, 280 RAVDESS clips)

Reproduce: `python -m eval.pipeline_eval --llm qwen3:14b --cells 1 --limit 280 --speech-rms 150 --csv eval/results/pipeline_emotion.csv`
then `python -m eval.confidence_eval --csv eval/results/pipeline_emotion.csv --fit`.

| | accuracy vs acted emotion |
|---|---|
| SER alone (`ser_correct`) | 88.9% |
| shown emotion, after the interpreter (`emotion_correct`) | 88.2% |

- **The interpreter defers almost always.** 3 of 280 rows were adjusted, all
  happy -> neutral, and all three were wrong (the SER had 2 of 3 right). That is
  the expected shape on RAVDESS: the words are neutral by construction, so the
  LLM has nothing to add and can only hurt. The case for the adjustment is real
  speech, where the words carry information -- RAVDESS cannot measure it.
- Per emotion the SER and the shown label agree everywhere except happy
  (87.5% -> 82.5%, the three adjustments).
- The old tone channel got worse under the new prompt and is now secondary:
  RAVDESS tone accuracy (vs the EXPECTED_TONE heuristic) 76.4% -> 63%, and
  `eval/tone_cases.jsonl` 88% -> 82% on one run (passive-aggression 50%,
  sarcasm 75%; `mm-01a`/`mm-03a` now over-call sarcasm on a warm voice,
  `pa-03`/`pa-04` fall to neutral). Nothing here is tuned yet: the prompt
  gained one paragraph and the JSON gained one field, and the model's
  attention moved with them. Open.

### Confidence had to change with it

With the LLM copying the voice label, its own probability for the emotion says
nothing about whether the label is right: `emotion_prob` AUROC 0.59 against
correctness, while `ser_confidence` scores 0.76. So `confidence.base_prob()`
makes the base of the score the **voice model's confidence for an unadjusted
read** and the interpreter's probability only for an adjusted one. Composite
AUROC went 0.62 -> 0.78 with that rule; with the shipped cut-offs (0.9 / 0.6):
high 79 reads at 97.5%, medium 182 at 87.9%, low 19 at 52.6%. The `--fit`
Platt line in `confidence_eval` now applies only to adjusted reads.

### Open

- Re-measure on real conversation once the transcript log has labelled lines;
  RAVDESS can only show that the adjustment does no harm, not that it helps.
- The tone regression above. Either tune the prompt for both fields or accept
  that `tone` is now a sarcasm flag. (`tone_cases.jsonl` now carries
  `expect_emotion` and `model_eval` scores the headline, as of 2026-10-24; see
  "Stage 0 measured" below.)
- Website copy and demo still speak in the five tones.

---

## A fine-tuned ≤1.5B interpreter (researched 2026-10-03)

**Status:** researched, nothing built or measured · **Found:** 2026-10-03 ·
**Affects:** `server/interpreter.py` and `server/config.py` (only if adopted);
full write-up in `docs/design/small-interpreter.md`

`qwen3:14b` costs 9.3 GB of the B580's 12 GB and ~0.89 s per read, most of it
decode (~26 tokens at ~41 tok/s). A distilled 0.6-1.5B model would be several
times faster and could run on the CPU, but its quality ceiling is the teacher's
unless it is trained on real labelled speech, which does not exist yet.
Compute is not the constraint: a full H100 sweep is under $15.

### Stage 0 measured (2026-10-24)

Full write-up, with per-emotion tables and the Stage 1 implications:
`docs/reports/small-interpreter-stage0.md`. Today's prompt, unchanged, against the
three current models. `tone_cases.jsonl`
now carries `expect_emotion`, and `model_eval` scores it beside two no-LLM floors.
"Always neutral" scores 59% and "copy the voice label" 85%, so the emotion number
means something only as a margin over 85%. `eval/results/model_stage0.csv` (34
cases x 3 runs) and `eval/results/pipeline_stage0.csv` (280 RAVDESS clips,
emotion2vec, `--speech-rms 150`; SER alone 89%):

| model | tone | emotion | pairs | literal / low-conf | sarcasm | PA | RAVDESS emotion | voice sens | read |
|---|---|---|---|---|---|---|---|---|---|
| qwen3:0.6b | 63% | 60% | 1/6 | 100% / 100% | 17% | 0% | 51% | +4% | 0.18 s |
| qwen3.5:0.8b | 26% | 66% | 0/6 | **13% / 0%** | 0% | 0% | 34% | -41% | 0.24 s |
| granite4:1b-h | 60% | 79% | 1/6 | 100% / 100% | 0% | 0% | 18% | +0% | 0.36 s |
| qwen3:8b | 77% | 96% | 4/6 | 100% / 100% | 58% | 0% | 64% | +18% | 0.53 s |
| gemma3:12b | 79% | 88% | 3/6 | 100% / **0%** | 75% | 75% | 62% | +36% | 1.29 s* |
| qwen3:14b | 84% | 96% | 2/6 | 100% / 100% | 75% | 92% | 85% | +30% | 0.85 s** |

\* and \*\*: games, Wallpaper Engine and a browser held ~2.2 GB of VRAM during the
run, so qwen3:14b spilled ~2.4 GB into shared memory and measured 3.6-6.2 s while
Ollama still said "100% GPU". 0.85 s is the same day's clean run. gemma's 1.29 s
may be inflated too. Re-measure both with those apps closed.

- **No small model is usable zero-shot.** None beats "copy the voice" on emotion.
  All three score 0% on passive aggression, and qwen3.5:0.8b also fails the
  listener-safety gate (it calls plain logistics `positive`).
- **On real audio, only qwen3:14b keeps the voice label.** RAVDESS words are
  neutral, so every move is a loss. 14b moves 12 of 280 (happy -> neutral). qwen3:8b
  moves 73 and gemma 82, mostly towards `neutral`. granite moves 216, all towards
  `neutral`, and qwen3:0.6b turns angry into happy 50 times. The prompt's "start
  from the voice label" is followed only by the 14B. A small interpreter would need
  either a fine-tune that learns to defer, or the emotion taken out of its hands.
- gemma3:12b's tone low-confidence guard is 0% here. Earlier runs had it at 100%,
  so it looks like a regression since the emotion paragraph joined the prompt.
- `qwen3:14b`'s RAVDESS emotion is 85%, against 88.2% on 2026-09-22.

### Open

- Re-time gemma3:12b and qwen3:14b on a clear GPU
  (`python -m eval.model_eval --models gemma3:12b qwen3:14b --runs 3`).
- The existing reads cannot be training data: 280 RAVDESS reads have 45 distinct
  strings ("noticing kids" x54).
- A consented real-speech test set is the long pole, and it is also the
  missing test for the current 14B.
- Training on the B580 under Windows is blocked by torch-xpu-ops#5220 (bf16 NaN);
  Linux/WSL2 has a working recipe.
- Published speed (2026-10-07, design doc §3.1): llama.cpp CPU Q4_0 decode is
  ~100-210 tok/s for the ≤1.2B candidates, which estimates to ~0.2-0.5 s per read
  on the 9600X (vs 0.89 s today). There are no B580 numbers for any model under 3B, and
  none for Granite-H at all. Run `llama-bench -p 300 -n 25` locally to replace the estimates.

