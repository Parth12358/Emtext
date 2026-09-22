# Laya as a middle stage between SER and the interpreter

**Date:** 2026-09-21 -- **Status:** measured, not adopted -- **Touches:** nothing
in `server/`

> The test bed this report describes (`worker.py`, `bench.py`, `results/`,
> `.venv-laya/`) was removed once the decision was made. It exists intact in
> commit `e04d440` and can be restored from there to score another candidate.

## Verdict

Laya does not earn a place in the pipeline in any of the three roles tried.
As a words-only channel it is a fair polarity classifier (literal /
low-confidence / positive / refusal all 100%) but reads no sarcasm at all
(0/4 sarcasm, 0/4 passive-aggression; p(sarcastic) never exceeds 0.24 on any
of the 34 cases). As an arbiter over the LLM every veto threshold lowers
every model, because the rule that would catch qwen3:14b's over-calling also
deletes its 12/12 true-sarcasm detections. As a gate its p(worth_reading)
tracks the skip fraction almost 1:1, i.e. it cannot tell subtext from plain
speech. It costs 0.45 s idle / 0.62 s with Whisper contending for the CPU per
line (three questions), 2 GB RSS, and a second venv because it needs
transformers 5 while MERaLiON pins `<5`. The one number that looks good
(0.703 on the MERaLiON pipeline cell) is mostly the majority class echoed
from a hint that says "negative" on 90 of 91 lines.

## What Laya is

`convaiinnovations/laya` (Apache-2.0): ModernBERT-large plus a decision
head, 421M parameters, fp32 safetensors (846 MB). One non-autoregressive
forward pass returns typed answers -- `choice` (label + probabilities),
`score`, `noul` (a yes-probability) -- with claimed calibration; it never
generates text. Environment on this machine: pip install 92 s, cold load
16.5 s, RSS after load 1991 MB, `head_max_len` 192 (option descriptions over
that come back as an error), `max_len` 512, CPU only (the Arc B580 is the
LLM's; Laya's published 33 ms is a T4 CUDA number). Outputs are
deterministic across calls. The checkpoint ships one temperature outside its
own [0.5, 5] sanity range and the library clamps it at load, warning that
confidence from that bucket is uncalibrated.

## Test bed

- `experiments/laya/worker.py` -- runs in `.venv-laya/` (transformers 5.17),
  one resident checkpoint, JSON lines over stdin/stdout.
- `experiments/laya/bench.py` -- server venv, spawns the worker, seven runs,
  writes `experiments/laya/results/*.csv` and `summary.json`. The voice hint
  it shows Laya comes from `interpreter._describe_voice`, so it is
  byte-identical to what the LLM sees. Re-run:
  `.venv/Scripts/python experiments/laya/bench.py` (`--quick` for runs 1, 3,
  7; `--dry-run` exercises the scoring with a fake worker).
- Data: `eval/tone_cases.jsonl` (34 cases, 8 categories, 6 mismatch pairs,
  mm-05/06 valence-free); `eval/results/model.csv` (3 LLMs x 3 runs x 34,
  current prompt); `eval/results/pipeline.csv` (942 RAVDESS rows, 872 used
  after dropping `vad_dropped` and empty transcripts, 166 unique requests
  after dedup). `tone_expected` there is 57% negative / 29% positive / 14%
  neutral, so the majority-class baseline is 0.57 -- keep that beside every
  per-cell number below.
- Questions asked (all in `bench.QUESTION_SETS`): `tone5` (the five
  `interpreter.TONES`), `polarity3` (words only), `sarcastic` (noul),
  `worth_reading` (noul). The emotion2vec pipeline rows predate the derived
  arousal/dominance, so their voice is rebuilt from the label's anchor in
  `server/ser.py` (`voice_source=label-anchor`); MERaLiON rows are measured.

Full run: 267 s wall.

## R1: words channel

Method: transcript + context, no voice. Laya answers all four questions;
`r1_fusion` then takes only `polarity3` and combines it with the case's
voice through a fixed table (`bench.FUSION_TABLE`: positive words + low
arousal/dominance -> mixed, negative + high -> mixed, neutral + low ->
negative, everything else literal; valence wins when measured). A
`sarcastic` noul override at 0.5 / 0.7 was also tried.

| run | acc | pairs | literal | low-conf | mismatch | sarcasm | PA | positive |
|---|---|---|---|---|---|---|---|---|
| r1_words (tone5) | 0.559 | 0/6 | 5/5 | 2/2 | 5/12 | 0/4 | 0/4 | 3/3 |
| r1_fusion (table) | 0.735 | 4/6 | 5/5 | 2/2 | 10/12 | 1/4 | 0/4 | 3/3 |
| + sarc noul > 0.5 or 0.7 | 0.735 | 4/6 | same | same | same | same | same | same |

The fusion table is the whole gain: the words channel alone is worse than
every LLM, and the table recovers the mismatch pairs because the pairs are
built to be separable by a table (same text, opposite voice). The override
never fires because p(sarcastic) never crosses 0.5. Where the table fails it
fails as designed: mm-01b ("Oh wonderful. That is just perfect." + angry
voice) fuses to `mixed`, which the case does not accept, and mm-06b ("Great,
another meeting." + bright voice) has no row that reads it as sarcasm. The
one sarcasm hit, sar-04, is an accident: Laya's polarity for "It's not like I
have anywhere to be" is `negative`, which the case accepts.

The text-only failures are the informative part. Sarcasm cases score
`positive` with confidence ("Wow, you actually showed up on time today" 0.62
positive; "Sure, because that plan worked out so well" 0.56 positive) and
every passive-aggression case reads as `neutral` or `positive`. Calibration:
ECE 0.293 on `sarcastic` (31/34 answers sit in the 0-0.2 bin, where 39% are
actually sarcastic) and 0.683 on `worth_reading` (26/34 in the 0-0.2 bin,
where 81% do carry subtext). The probabilities are not usable as thresholds;
they are almost always low.

Verdict: a words channel this blind to sarcasm cannot feed the mismatch rule
-- it would report "positive words" for every sarcastic line, which is the
same thing the LLM already extracts from the transcript, and the LLM gets
sar-01..03 right without help.

## R2: arbiter / veto

Method: `r2_voice` re-asks all four questions with the voice hint appended,
then `r2_fusion_llm` replays `model.csv` through two rules (`bench.veto`):
(a) LLM said `sarcastic`, Laya's p(sarcastic) < theta and its tone is not
sarcastic -> take Laya's tone; (b) LLM said `neutral`, Laya says
sarcastic/negative/mixed with probability > theta -> take Laya's tone. theta
swept 0.3-0.7.

Laya with the hint (`r2_voice`): acc 0.588, pairs 2/6, mismatch 8/12,
sarcasm 0/4, PA 0/4, and **low-confidence 0/2 (was 2/2)**: the "angry (conf
0.22) -- low confidence" hint turns "The meeting is moved to Thursday at ten"
into `negative` 0.64. Laya reads the label word and ignores the caveat that
follows it. On the b-halves the hint moved p(sarcastic) up on 5/6 but by
0.04-0.15 (mm-01b 0.075 -> 0.227; mm-05b fell 0.159 -> 0.117) and changed the
tone on 2/6 (mm-03b, mm-04b, both to `negative`, both correct).

Veto over the recorded LLM answers (102 rows per model = 34 x 3 runs):

| model | before | 0.3 | 0.4 | 0.5 | 0.6 | 0.7 | pairs before -> 0.7 | sarcasm before -> 0.7 | rule (a) fires |
|---|---|---|---|---|---|---|---|---|---|
| gemma3:12b | 0.853 | 0.676 | 0.676 | 0.706 | 0.706 | 0.735 | 3/6 -> 2/6 | 9/12 -> 0/12 | 18 |
| qwen3:14b | 0.882 | 0.765 | 0.765 | 0.794 | 0.794 | 0.824 | 2/6 -> 4/6 | 12/12 -> 3/12 | 24 |
| qwen3:8b | 0.765 | 0.647 | 0.647 | 0.676 | 0.676 | 0.706 | 4/6 -> 3/6 | 9/12 -> 6/12 | 6 |

Every theta lowers every model. Rule (a) is the damage: since Laya's
p(sarcastic) never exceeds 0.24, "p < theta" holds for every LLM `sarcastic`
answer at every theta, so (a) fires on all of them and replaces true sarcasm
with Laya's `positive` / `neutral`. The qwen3:14b pair gain (2 -> 4/6, the
over-calling on the a-halves that motivated this role) is real but bought
with 9 lost sarcasm cases. Rule (b) at theta <= 0.6 zeroes the low-confidence
guard (6/6 -> 0/6 or 3/6) for the reason above; 0.7 is the only theta where
it never fires. Literal stays 15/15 throughout -- the false-positive guard
survives, but only because Laya agrees with the LLM on plain sentences.

Real audio (`r2_pipeline`, theta 0.7, RAVDESS transcripts + real SER):

| cell | n | LLM | Laya | fused | rules fired |
|---|---|---|---|---|---|
| emotion2vec + gemma3:12b | 259 | 0.405 | 0.255 | 0.405 | 0 |
| emotion2vec + qwen3:14b | 259 | 0.795 | 0.255 | 0.795 | 0 |
| emotion2vec + qwen3:8b | 263 | 0.312 | 0.251 | 0.312 | 0 |
| meralion + qwen3:8b | 91 | 0.242 | 0.703 | 0.242 | 0 |

Fused equals LLM everywhere: RAVDESS text is emotionally null, so no LLM
ever answers `sarcastic` (rule (a) has nothing to veto), and Laya's tone
probability never exceeds 0.7 (rule (b) never fires). Two things in the
per-row file matter more than the cell means:

- In the emotion2vec cells Laya answers **`sarcastic` on 100 of 259 rows**
  -- "Kids are talking by the door" with an angry / disgusted / fearful / sad
  hint. Neutral words plus an unpleasant voice reads as sarcasm to it. That
  is exactly the over-reading the product must not produce and the prompt's
  guard rails exist to prevent.
- The MERaLiON 0.703 is mostly an artefact, checked in `r2_pipeline.csv`:
  90/91 hints gloss valence as "(negative)" (MERaLiON's range there is
  0.12-0.41), Laya answers `negative` on 60/91 and is right on 48/51 true
  negatives but only 6/14 happy and 4/14 surprised. Against the cell's
  majority-class baseline of 0.56 it is a small gain, and the gain comes from
  the label word in the hint ("happy", "surprised") pulling some lines to
  `positive` against the valence gloss. It says nothing about reading tone
  from words.

Verdict: the veto cannot be made to fire only on false positives because
Laya's sarcasm probability is uninformative -- low on plain speech AND on
sarcasm. An arbiter that overrides the LLM's correct answers as often as its
wrong ones is noise.

## R3: gate

Method: skip the LLM when p(worth_reading) < theta. `skip_frac` is the LLM
compute saved, `lost_frac` the fraction of subtext-carrying lines (sarcasm,
PA, refusal, mismatch b-halves; 16/34 on cases, 761/872 on the pipeline)
that would never get a read.

| source | theta 0.3 | theta 0.5 | theta 0.7 |
|---|---|---|---|
| tone cases, words only | skip 0.853 / lost 0.875 | 0.971 / 1.0 | 1.0 / 1.0 |
| tone cases, with voice | skip 0.676 / lost 0.75 | 0.765 / 0.812 | 0.941 / 0.938 |
| pipeline (real SER) | skip 0.091 / lost 0.033 | 0.273 / 0.179 | 0.716 / 0.685 |

Lost tracks skip within a few points at every theta on every source, which
is what a random gate does. The pipeline curve looks slightly better than
random at low theta (skip 0.27, lost 0.18 at 0.5), but 761/872 of those rows
are "subtext" by construction (any non-neutral acted emotion), so the gate is
being scored on whether it can spot the 13% of neutral clips, and it mostly
cannot. A missed sarcastic line is the product's worst error; a gate that
drops 18% of them to save 27% of LLM calls is not a trade.

## Cross-cutting

- **Latency** (n = 100, warm, sequential, CPU): `tone5` alone p50 189 / p95
  238 ms; three questions p50 449 / p95 582 ms; three questions while
  `whisper:base` transcribes on another thread p50 625 / p95 727 ms (85
  Whisper calls during the pass). Pipe + JSON overhead is < 1 ms (wall and
  worker-reported times agree). For scale: the audio stage (Whisper + SER +
  speaker, gathered) is 0.30 s and the LLM interpret 0.6-0.9 s. In series
  before the LLM, Laya adds ~0.6 s to a ~1.2 s path; in parallel with the
  audio stage it becomes the long pole and slows Whisper.
- **Memory:** 1991 MB RSS for the worker alone, on a server whose own budget
  was just fought down (see "The server's memory is dominated by one
  checkpoint" in `TODO.md`). fp32 weights; no int8 export was tried.
- **Dependency conflict:** Laya needs transformers 5.x; the server venv pins
  `<5` for MERaLiON's remote code. The subprocess worker is the only way to
  run both, and it is a second Python, a second torch, and a process to
  supervise.
- **Calibration:** ECE 0.27-0.29 on `sarcastic`, 0.55-0.68 on
  `worth_reading`, all mass in the bottom two bins. "Calibrated
  probabilities" is the feature the veto and the gate both depend on, and it
  is the one that failed.
- **Determinism:** identical answers on repeated calls, which is a real
  advantage over the LLM (`model.csv` shows run-to-run disagreement on
  mm-04a). It does not help when the deterministic answer is wrong.

## What would change the answer

- **A fine-tuned checkpoint on our labels.** Everything above is zero-shot
  through natural-language criteria. ModernBERT-large is a reasonable base
  for a sarcasm/tone head, but `tone_cases.jsonl` is 34 lines and the RAVDESS
  text is null, so there is nothing to fine-tune on yet. More labelled
  mismatch pairs on real speech (see "Sarcasm is never tested on real
  speech" in `TODO.md`) are the prerequisite for this and for every other
  approach.
- **A smaller sentiment model for the words channel.** If a polarity-only
  signal is ever wanted (R1's table did recover 4/6 pairs), a ~20-60M
  sentiment classifier would give the same three-way polarity at roughly 10x
  less CPU and ~200 MB, inside the server venv. It would still not read
  sarcasm; the value would be a deterministic mismatch table rather than the
  LLM deciding, which the prompt already does at 85-88%.
- **A sarcasm head that actually fires.** Any replacement must beat
  p(sarcastic) > 0.5 on sar-01..04 before a veto rule is worth re-running;
  `bench.py --runs 3,4` (from `e04d440`) is the check.

## Recommendation

Do not integrate. This report is the record; the measurement harness lives
in git history (`e04d440`) rather than in the tree, and the numbers are in
`TODO.md`.
The open problems Laya was aimed at -- qwen3:14b over-calling sarcasm on a
confidently happy voice, gemma / qwen3:8b missing sar-04 -- remain prompt and
data problems, not arbiter problems.
