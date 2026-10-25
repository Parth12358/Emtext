# Small interpreter, Stage 0: today's prompt on ≤1B models

**Date:** 2026-10-24 -- **Status:** measured; no small model adopted, Stage 1 not
started -- **Touches:** nothing in `server/` except an unrelated DLL fix found
on the way (below); the eval changes are in `eval/`. Plan and candidates:
[`../design/small-interpreter.md`](../design/small-interpreter.md) §9.

## Verdict

Zero-shot, no model at or under 1B can stand in for `qwen3:14b`. All three are
2-5x faster per read on the B580 (0.18-0.36 s against 0.85 s). None beats the
no-LLM floor of copying the voice label on emotion, and all three score **0% on
passive aggression**. `qwen3.5:0.8b` also fails the listener-safety gate: it calls
plain logistics `positive`.

The finding that matters most for Stage 1 comes from real audio. Since 2026-09-22
the prompt tells the model to *start from the voice label and keep it*. On RAVDESS
the words are neutral, so every change is a loss, and only the 14B keeps the
label. It changed 12 reads of 280. `qwen3:8b` changed 73, `gemma3:12b` 82,
`granite4:1b-h` 216 and `qwen3:0.6b` 118. The small models therefore cut the
voice model's 89% to 18-51%. A small interpreter needs one of two things. Either
a fine-tune that learns to defer to the voice label, or the emotion taken out of
its hands, with the label passed through and the LLM kept for tone and the read.

## What was run

- **Models.** Three candidates from the design doc's shortlist: `qwen3:0.6b`,
  `qwen3.5:0.8b` and `granite4:1b-h` (Granite-4.0-H-1B). The three current models
  ran as the comparison: `qwen3:8b`, `gemma3:12b` and `qwen3:14b`. All are stock
  Ollama tags, `SYSTEM_PROMPT` is unchanged, the request options are production's
  (`think: false`, `load_options()`), and everything ran on the B580.
- **`python -m eval.model_eval`.** 34 cases in `eval/tone_cases.jsonl`, 3 runs
  each, written to `eval/results/model_stage0.csv`.
- **`python -m eval.pipeline_eval`.** The emotion2vec cell, 280 balanced RAVDESS
  clips (40 per emotion, calm excluded), `--speech-rms 150`, parakeet:v3. These
  are the same flags as `pipeline_emotion.csv` (2026-09-22). Written to
  `eval/results/pipeline_stage0.csv`. ASR and SER are cached and replayed, so all
  six models saw byte-identical transcripts and voice dicts.

### Prerequisites built for this

- **`expect_emotion` on all 34 cases.** This was owed since the headline became
  the SER's emotion. These are generous lists, like `expect`. With a voice, the
  emotion to expect is the voice label unless the words or a low confidence make
  it untenable. So `lc-01`/`lc-02` expect `neutral`, the opposite of copying.
  Text-only sarcasm and passive aggression also accept `neutral`, because the
  prompt says to prefer it without a voice and `tone` carries the sarcasm. The
  labels are one person's judgement, so read them before trusting a small
  difference. `mm-04b` ("It's fine." with an angry voice) expects
  angry/disgusted, and the 14B's `sad` is arguably the better reading.
- **`model_eval` scores the emotion,** with two policies that call no LLM printed
  beside it. "Always `neutral`" scores 59% and "copy the voice label, else
  `neutral`" scores 85%. Both are high by construction, so the emotion number
  means something only as a margin over 85%. The emotion cases that discriminate
  are few: `low-confidence`, where copying is wrong, `positive` (text-only), and
  the masking/complaint halves of the mismatch pairs.

## Results

### `tone_cases.jsonl` (34 cases x 3 runs)

| model | tone | emotion | mismatch pairs | literal | low-conf (tone) | sarcasm | passive aggr. | read, mean |
|---|---|---|---|---|---|---|---|---|
| qwen3:0.6b | 63% | 60% | 1/6 | 100% | 100% | 17% | 0% | 0.18 s |
| qwen3.5:0.8b | 26% | 66% | 0/6 | **13%** | **0%** | 0% | 0% | 0.24 s |
| granite4:1b-h | 60% | 79% | 1/6 | 100% | 100% | 0% | 0% | 0.39 s |
| qwen3:8b | 77% | 96% | 4/6 | 100% | 100% | 58% | 0% | 0.57 s |
| gemma3:12b | 79% | 88% | 3/6 | 100% | **0%** | 75% | 75% | see below |
| qwen3:14b | 84% | 96% | 2/6 | 100% | 100% | 75% | 92% | 0.85 s |
| *(neutral)* | | 59% | | | | | | |
| *(copy voice)* | | 85% | | | | | | |

- **The small models fail in two different ways.** `granite4:1b-h` and
  `qwen3:0.6b` answer `neutral` on every sarcasm and passive-aggression case.
  That is safe, but it is no use to the listener. `qwen3.5:0.8b` answers
  `positive` on almost everything, including "The train leaves at four fifteen",
  which is the over-reading the guard rails exist to catch.
- **`qwen3:0.6b` reads sarcasm as delight on emotion.** It answered `happy` on
  sar-01, sar-02, sar-03 and pa-03. For this listener that is worse than
  `neutral`.
- **`gemma3:12b` lost the low-confidence guard.** It turns both "weak voice on a
  plain sentence" cases negative on every run. Earlier runs before the emotion
  paragraph scored 100% here (TODO, "The default SER backend has no valence"),
  so this looks like a regression from the 2026-09-22 prompt change. Nothing
  current ships gemma.
- **`qwen3:8b` matches the 14B on emotion (96%) and wins on pairs (4/6).** It
  misses all four passive-aggression cases.
- `sar-04` ("No, please, take your time...") and `pa-03` ("Thanks for finally
  getting back to me") are still the two cases the 14B misses.

### RAVDESS, end to end (280 clips, emotion2vec; SER alone 89%)

| model | shown emotion | tone | voice sensitivity | labels changed | of which wrong | read, median |
|---|---|---|---|---|---|---|
| qwen3:0.6b | 51% | 17% | +4% | 118 | 108 | 0.18 s |
| qwen3.5:0.8b | 34% | 20% | -41% | 172 | 155 | 0.24 s |
| granite4:1b-h | 18% | 18% | +0% | 216 | 199 | 0.36 s |
| qwen3:8b | 64% | 43% | +18% | 73 | 70 | 0.53 s |
| gemma3:12b | 62% | 76% | +36% | 82 | 75 | see below |
| qwen3:14b | 85% | 65% | +30% | 12 | 10 | see below |

Changing the voice label almost never fixed a wrong one: 2 changes did, both
from `qwen3:0.6b`, and none did in any other cell. Nearly every other change moved
a right label to `neutral` or a neighbouring class. Shown-emotion accuracy per acted emotion, against the
voice alone:

| | neutral | happy | sad | angry | fearful | disgusted | surprised |
|---|---|---|---|---|---|---|---|
| SER alone | 100% | 88% | 78% | 100% | 80% | 85% | 92% |
| qwen3:0.6b | 100% | 92% | 78% | 0% | 0% | 20% | 68% |
| qwen3.5:0.8b | 100% | 8% | 2% | 0% | 0% | 40% | 85% |
| granite4:1b-h | 100% | 0% | 2% | 22% | 0% | 0% | 0% |
| qwen3:8b | 100% | 35% | 78% | 100% | 32% | 10% | 92% |
| gemma3:12b | 100% | 45% | 5% | 100% | 80% | 38% | 68% |
| qwen3:14b | 100% | 62% | 78% | 100% | 80% | 85% | 92% |

- **`granite4:1b-h` answers `neutral` regardless of the voice.** That gives
  100% on neutral clips and nearly zero elsewhere. It holds the guard rails on
  the tone cases for the same reason.
- **`qwen3:0.6b`'s most common changes were angry -> happy (50 clips),
  fearful -> sad (32) and disgusted -> sad (26).**
- **The 14B's whole loss is on `happy`** (88% -> 62%, all happy -> neutral). It
  scored 88.2% on the same clips on 2026-09-22 against 85% now, with SER
  identical, so the difference is LLM run-to-run variance or the `think: false`
  request change in between.
- `tone` stays a weak channel on RAVDESS for every model, because the words carry
  nothing. `voice sensitivity` (how much `positive` moves with the acted
  emotion) is the label-free check: the 14B and gemma use the voice, while
  granite and qwen3:0.6b ignore it. qwen3.5:0.8b's -41% means it reads the
  voice backwards.
- 18 clips per cell (7 sad, 5 neutral) fell back to the raw clip at the fixed
  `--speech-rms 150`. That is the known quiet-RAVDESS behaviour, and the same
  18 were used in every cell.

### Speed and memory

| model | VRAM resident | read, median (`tone_cases`) | read, median (RAVDESS) |
|---|---|---|---|
| qwen3:0.6b | 0.9 GB | 0.18 s | 0.18 s |
| qwen3.5:0.8b | 0.6 GB | 0.24 s | 0.24 s |
| granite4:1b-h | 1.7 GB | 0.39 s | 0.36 s |
| qwen3:8b | 5.6 GB | 0.56 s | 0.53 s |
| gemma3:12b | 8.1 GB | 1.41 s (?) | 1.30 s (?) |
| qwen3:14b | 9.6 GB | 0.85 s (first, clean run) | 6.21 s (spilled) |

**The 12B and 14B timings are not clean.** During the runs, desktop
applications held ~2.2 GB of the B580's VRAM: a game, Wallpaper Engine, Steam's
web helper, a browser, and 1.08 GB for `dwm`. The runner got only 7.15 GB of
dedicated memory, and ~2.4 GB of `qwen3:14b` sat in shared system memory, while
`ollama ps` still reported "100% GPU". The 14B measured 3.6-6.2 s per read in
that state, against 0.85 s in a run earlier the same day before those
applications took memory. That matches `README.md`'s 0.89 s. gemma's 1.3-1.4 s
may be inflated the same way. The small models and `qwen3:8b` fit with room to
spare, so their numbers stand. Accuracy is unaffected: spilling into shared
memory slows a model down but doesn't change its answers.

All of these timings are **on the GPU**. The design doc's 0.2-0.5 s estimates
(§3.1) are for the **CPU**, which is the case that would free the GPU entirely,
and nothing here measures them. `llama-bench -p 300 -n 25` on the 9600X is
still owed.

## Found on the way

- **The server could not start on the default ASR.** sherpa-onnx's extension
  loads `onnxruntime.dll` by name. Windows 11 ships its own 1.17.1 copy in
  System32, the loader picks that one instead of the bundled 1.29, and every
  sherpa key fails with "requested API version [28] is not available". That
  includes `parakeet:v3`, which the server loads at import. The fix is
  `transcriber._pin_sherpa_onnxruntime()`: on Windows only, it loads the bundled
  DLL by full path before the import.
- **`pipeline_eval` hid its worker's errors.** The CPU worker's stderr went to
  DEVNULL, so the DLL failure read only as "CPU worker failed to start". It now
  goes to `eval/results/_worker_<slug>.log`, which is gitignored, and the error
  names that file.
- **`model_eval` never unloaded a model.** The interpreter pins with
  `keep_alive: -1`, so a multi-model run left every model resident. In one run
  that put 14B and 12B side by side, and the 14B timed at 5.5 s. It now unloads
  each model when it finishes, with the same `load_options()`.
- **`pipeline_eval --llm` only filtered the default three.** Any other name was
  silently dropped, and the run did nothing. It now runs the models named.

## What this means for Stage 1

- **The quality bar is the 14B, and the floor is "copy the voice".** A fine-tune
  that scores 85% on emotion has learned nothing the pipeline didn't already do
  for free.
- **Pick the fine-tune target on this evidence.** The failure is mostly not
  understanding. Small models change the voice label when told to keep it. Two
  ways out, and the cheaper one needs no training:
  - **Take the emotion out of the small model's hands.** Pass the SER label
    through, and let the LLM own `tone` (the sarcasm flag) and the read.
    Measuring that means a prompt or schema variant and a re-run of both evals,
    and it changes what the listener sees on real speech, where the words do
    carry emotion.
  - **Train it to defer.** That needs distillation rows where keeping the label
    is the answer. RAVDESS-style neutral-words rows are exactly that, though the
    existing 280 reads are too repetitive to train on (TODO).
- **Passive aggression is the gap no prompt closed.** All three small models
  score 0%, and so does `qwen3:8b`. It is the category the distillation data
  must cover most densely.
- **Candidates.** `qwen3.5:0.8b` drops out on the safety gate. `granite4:1b-h`
  is the safest zero-shot model: it never over-reads, because it barely reads at
  all. `qwen3:0.6b` is the fastest at 0.18 s. Both stay candidates for Stage 1.

## Reproduce

```bash
ollama pull qwen3:0.6b && ollama pull qwen3.5:0.8b && ollama pull granite4:1b-h
python -m eval.model_eval --models qwen3:0.6b qwen3.5:0.8b granite4:1b-h qwen3:8b gemma3:12b qwen3:14b \
    --runs 3 --show-fails --csv eval/results/model_stage0.csv
python -m eval.pipeline_eval --llm qwen3:0.6b qwen3.5:0.8b granite4:1b-h qwen3:8b gemma3:12b qwen3:14b \
    --cells 6 --limit 280 --speech-rms 150 --csv eval/results/pipeline_stage0.csv
```

Close GPU-heavy desktop applications first, or the 12B/14B timings will spill
into shared memory as above. To check, look at dedicated vs shared usage per
process: `Get-Counter '\GPU Process Memory(*)\Dedicated Usage'`.
