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
`RESEARCH.md` §4.2 and §4.4.

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
valence. Full argument and ranked alternatives in `RESEARCH.md` §4.5-4.6 and §6.2.

Licence note for whoever evaluates `audeering/...-msp-dim`: it is
**CC-BY-NC-SA-4.0, research only**. Fine as an offline measurement instrument,
fatal if emtext ever ships as more than a personal project. The Odyssey 2024
WavLM baselines (`3loi/SER-Odyssey-Baseline-WavLM-*`) are **MIT** but 0.3B
parameters -- slower than the MERaLiON already rejected.

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
`RESEARCH.md` §2.

---

## The model table hides a deterministic sarcasm failure (`sar-04`)

**Status:** open, documentation fix · **Found:** 2026-09-05 · **Affects:**
`README.md` model comparison table

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

Add the sarcasm column and correct the note. Cheap, and it prevents the next
model choice being made on a table that omits the axis the product is named for.

---

## Irony is not modelled, and an "ironic" answer is silently lost

**Status:** open, low priority · **Found:** 2026-09-05 · **Affects:**
`server/interpreter.py`

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
disagrees with the transcript when the transcript is wrong. See `RESEARCH.md` §4.3.

---

## No diarization: every voice in the room is treated as one speaker

**Status:** open, scoped and measured, nothing implemented · **Found:**
2026-09-10, investigating `RESEARCH.md` §5.2 · **Affects:** `server/segmenter.py`,
`server/main.py`, the `read` frame, and every conversational-dynamics feature
`RESEARCH.md` §6.3 gates on diarization

emtext currently interprets the microphone, not a speaker. The user's own voice,
their conversational partner's, and a television are one undifferentiated stream,
so the interpreter is regularly handed the user's own words and asked what the
"speaker" meant by them. `RESEARCH.md` §1.5 says pointing the interpreter at the
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

### Proposed shape (not implemented)

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

### Open questions before building

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

Reproduce: the probes are throwaway and live in the session scratchpad. A keeper
version belongs in `eval/spk_eval.py`, alongside `ser_eval.py`, reusing
`data/ravdess/`.
