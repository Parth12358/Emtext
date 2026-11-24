# Building the distillation dataset

How to make the teacher-student set for a small interpreter adapter (Stage 1 of
[`../design/small-interpreter.md`](../design/small-interpreter.md) §9). The code is the
`distill/` package. The output is gitignored under `data/distill/<name>/`. The design
reasons are in the design doc §5; this page covers how to run it and what comes out.

## What a row is

One row is one interpreter call: earlier lines (sometimes speaker-tagged), an optional
voice hint, and the newest line, mapped to the teacher's `{"emotion","tone","read"}`.
The input is rendered by `Interpreter._build_prompt` itself, and the label comes from
the real `Interpreter.interpret`. The training distribution is therefore production's
prompt, schema and request options byte for byte, and an edit to `interpreter.py`
between phases is caught rather than silently mixed in.

## Running it

From the project root, with Ollama up:

```bash
python -m distill.pipeline generate --calls 40          # 6 scenarios per call
python -m distill.pipeline expand                       # 2 rows per scenario
python -m distill.pipeline label                        # teacher: qwen3:14b
python -m distill.pipeline label --teacher gemma3:12b   # optional second teacher
python -m distill.pipeline build --second gemma3:12b    # filter, dedup, split, export
python -m distill.pipeline status
```

- **Resumable.** `generate` and `label` append. Ctrl+C is a clean stop, and re-running
  the same command continues. `expand` and `build` are pure rewrites and safe to re-run.
- **Growing the set.** Raise `--calls` and re-run `generate`; the existing calls are
  kept. Then run `expand`, `label` (only the new rows are labelled) and `build`.
- **Sizing.** The design doc's planning figure is 5-20k rows (§5.3). Under spec v2,
  5k kept rows needs ~760 calls and 10k needs ~1,500 (projected from the pilot's
  per-cell yield). Measured costs are in the pilot section below.
- **GPU.** Every phase that loads a model unloads it on exit. Close GPU-heavy desktop
  apps before labelling with `qwen3:14b`. With ~2 GB of VRAM taken it spills into
  shared memory and runs several times slower, while `ollama ps` still says
  "100% GPU" (`docs/reports/small-interpreter-stage0.md`).
- `--name` picks the folder (default: the spec version), so a pilot and a full set
  can sit side by side. `generate` and `expand` refuse a folder made under another
  spec version, because the plan and the variant picks both depend on it.

## How rows are made

1. **generate**. `gemma3:12b` writes scenarios, one category per call. The category is
   assigned by a weighted round-robin, so the mix follows the weights at any size;
   random draws gave a 40-call pilot no teasing at all. The weights favour the
   categories the teacher gets wrong most often (see "Oversampling" below). Each scenario is pinned to a
   sampled setting, relationship, feeling and an exact number of earlier lines,
   enforced by the output schema; asked in prose, the generator wrote under half.
   Speakers alternate by position, ending on the listener. Eval phrases are banned,
   and generated "…", dashes and curly quotes are turned into ASR-style punctuation
   at expand. The generator comes from a different model
   family from the teacher, so the teacher is not grading text in its own style.
2. **expand**. Each scenario becomes two rows under two different voice variants,
   drawn by `VARIANT_WEIGHTS` or a category's own `variant_weights`:
   - `congruent`: a confident voice that sounds the way the line is meant;
   - `contrast`: a confident voice that opposes the words (the mismatch rules);
   - `low_conf`: a random label under 0.4 (the guard rail);
   - `none`: no voice line.

   A quarter of rows with context get `listener:`/`speaker:` tags. Voice hints come from
   `distill.voice`: a sampled 7-class distribution with arousal and dominance from
   `ser._expected()` over the real anchors, and valence None. Every hint is one the
   emotion2vec backend could have produced.
3. **label**. The teacher answers each row, along with `tone_prob` and `emotion_prob`
   from Ollama logprobs.
4. **build**. A row is kept only if all of these hold:
   - the teacher's tone and emotion fall inside the accepted sets that
     `distill.spec` derives from the production prompt's own rules for that
     category and variant;
   - the read is at most 5 words and does not restate the line;
   - the line is not a near-duplicate of another scenario (char 4-gram Jaccard
     ≥ 0.7), and not an echo of an eval transcript. An echo is any of: Jaccard ≥ 0.5
     against `tone_cases.jsonl` or the RAVDESS sentences, a shared word 3-gram that
     isn't all filler words, or a phrase from `spec.AVOID_PHRASES`. The first pilot
     needed the last two: with eval phrases in its briefs, the generator wrote
     "Thanks for finally noticing the fridge" (pa-03's opening), and character
     overlap alone let it through;
   - its read string is under the diversity cap (0.3% of rows).

   **Fallback:** when the primary teacher's answer breaks the rules but the second
   teacher's satisfies them, the row keeps the second teacher's label, recorded as
   `label_source` (`--no-fallback` turns this off). The rules still decide what is
   acceptable, so gemma's known failure on the low-confidence guard cannot get in.
   Every target read is normalised to one style (lowercase, comma-joined, no full
   stop), because gemma capitalises and punctuates and qwen3:14b doesn't. A label
   whose prompt hash no longer matches its row is rejected as `stale_label`.

   The split is train/val by scenario. `--require-agree` additionally drops rows where
   the second teacher disagrees. It is off by default because Stage 0 measured
   gemma3:12b at 0% on the low-confidence guard and qwen3:8b at 0% on passive
   aggression, so requiring agreement would empty exactly those categories.

The rule table is the part to review before trusting the set. It is
`distill/spec.py`, versioned by `SPEC_VERSION`, and its self-test is
`python -m distill.spec`. Changing a category, a weight or an accepted set means
bumping the version, because the rows stop being comparable.

## The pilot (2026-10-25, spec v1)

40 calls, `--name pilot`, the defaults above, on the B580 with desktop apps holding
~2.9 GB of VRAM:

- **Yield.** 240 scenarios planned, 232 generated (one call failed on truncated JSON,
  which is now retried), and 12 dropped as instruction echoes. That left 440 rows, of
  which **249 were kept** (233 train, 16 val, 190 distinct reads, 0 malformed).
- **Reject reasons.** tone outside the rules 95, read cap 55 (the cap is 3 per string
  at this size and ~30 at 10k), emotion outside the rules 31, eval echo 10. The
  fallback supplied 36 of the kept labels.
- **Kept share.** It is high for literal, refusal and sincere-negative lines (82-100%).
  It is lowest where the teacher has a known failure: warm-voice teasing 4/24 (both
  teachers call it negative/angry), sarcasm under a warm voice 3/13, masking 18/48
  and sincere-positive 20/48. Spec v2 oversamples these (next section).
- **Cost.** Generation ~15 s per call. Labelling 3.6 s per row with qwen3:14b spilled
  into shared memory (~0.85 s on a clear GPU) and 1.45 s per row with gemma3:12b.
  At v2's projected ~51% kept, 5k kept rows needs ~760 calls and ~9.1k labelled
  rows: about 3 h of generation, 2-9 h for the 14B (clear GPU vs spilled) and 3.7 h
  for gemma.

### Oversampling (spec v2)

The pilot ran under spec v1, which weighted categories by how often each kind of line
seemed worth teaching. Because build rejects every label outside the rules, equal
generation left the hard cells a small share of the kept set. v2 changes only
the weights. The accepted sets are unchanged.

- Categories: teasing 0.10 → 0.14, sarcasm 0.12 → 0.14, sincere_positive 0.11 → 0.12
  and masking 0.10 → 0.11. The share comes from the always-kept categories: literal
  0.13 → 0.10, sincere_negative 0.12 → 0.09 and refusal 0.07 → 0.05.
- Variants: sarcasm and sincere_positive put contrast in 77% of scenarios instead
  of 52% (`_CONTRAST_HEAVY`). Teasing has only two variants, so it is helped only
  by the category weight.

Projected kept rows at 700 calls (pilot yields × v2 plan, against v1):

| cell | v1 | v2 |
|---|---|---|
| sarcasm/contrast | 61 | 105 |
| sincere_positive/contrast | 101 | 162 |
| teasing/congruent | 70 | 98 |
| masking (all four) | 305 | 336 |
| literal (all three) | 1,092 | 841 |
| total | 4,997 | 4,611 |

The total falls because more of the budget goes to cells that keep less. The pilot
cells hold only 10-25 rows each, so this is a coarse correction. Re-read
`manifest.json`'s `per_cell` after a run and adjust. Oversampling has one limit:
the rows a weak cell keeps are the ones the teacher got right, so they lean towards
the easy end of that cell. If a student trained on the set is still weak there, the
next step is a stronger teacher, not more generation.

### Hosted teacher for the two weakest cells (2026-11-23)

The full v2 run kept 4,255 of 8,458 rows. Two cells stayed low because both local
teachers answer against the rules there. On warm-voice teasing they say negative/angry
(65 of 585 kept). On sincere-positive lines in a sad voice they ignore the voice (62 of 413).
These rows were relabelled by DeepSeek V4.1 Flash (`deepseek-flash`) through its API,
and that teacher is the third fallback at build, after gemma:

```bash
# key from the environment only; nothing the pipeline writes contains it
DEEPSEEK_API_KEY=... python -m distill.pipeline label --api deepseek --api-thinking \
    --teacher deepseek-flash --cells teasing/congruent,sincere_positive/contrast --concurrency 8
python -m distill.pipeline build --second gemma3:12b --also deepseek-flash
```

- **Same inputs.** The request carries `SYSTEM_PROMPT` and the row's stored prompt as
  chat messages, in `json_object` mode, since DeepSeek has no enum schema. Its answer goes
  through the interpreter's own synonym tables. An off-enum emotion counts as a failed
  label. It does not fall back to the voice label: that fallback would pass the
  sincere-positive gate, whose accepted emotion is the voice label.
- **Thinking on.** On a 40-row trial of the same rows, thinking off passed the rules on
  9/20 teasing and 5/20 sincere-positive rows, and thinking on on 12/20 and 12/20.
  `qwen3:14b` scored 3/20 and 12/20, and `gemma3:12b` 0 and 0.
- **Full cells:** across all 998 rows DeepSeek passed 396/585 teasing and 262/413
  sincere-positive rows, with no failed calls. About 1.1M tokens went in (~70% cache
  hits) and ~0.19M came out, under $0.50 at the posted Flash price.
- **Rebuilt set:** 4,660 kept (4,401 train, 259 val), and 375 labels come from DeepSeek.
  Warm-voice teasing went from 65 to **380**. Sincere-positive in a sad voice went from
  62 to **115**; it is now held back by the read cap ("genuinely pleased", "masking
  hurt"), not the teacher.
- These rows left the machine. That is allowed because the set is synthetic only (see
  below).

**Then DeepSeek became the primary teacher.** It labelled all 8,458 rows (no failures,
~8M tokens in at 83% cache hits, 1.2M out, about $1-2). It was scored first on the held-out
`tone_cases.jsonl` with the same prompts and scoring as `model_eval` (3 runs, thinking on),
against qwen3:14b's Stage 0 numbers:

| | qwen3:14b | deepseek-flash |
|---|---|---|
| tone, all | 84% | **92%** |
| emotion, all | 96% | 96% |
| mismatch pairs | 2/6 | **5/6** |
| sarcasm | 75% | **92%** |
| literal / low-confidence | 100% / 100% | 100% / 100% |
| passive aggression | **92%** | 67% |

DeepSeek's one weak category is passive aggression, where it says `neutral`. The rules
reject `neutral` there, so those rows fall back to the 14B:

```bash
python -m distill.pipeline build --teacher deepseek-flash --second qwen3:14b --also gemma3:12b
```

The result: **6,633 kept** (6,287 train, 346 val) against 4,660, with 5,252 distinct
reads. Labels come from deepseek-flash 5,622, qwen3:14b 701 and gemma3:12b 310. Every cell
now keeps 62-97%. The read cap dropped from 1,766 rejects to 247, because DeepSeek varies
its reads where the local teachers repeated "genuinely pleased" and "masking hurt". Agreement
with the 14B on kept rows is 52%. The two teachers differ within the accepted sets, for
example `sarcastic` against `negative`.

### No-voice rebalance (spec v3, 2026-11-24)

The v2 student beat the 14B wherever a voice hint was present but missed text-only sarcasm
and passive aggression ([Stage 1](../reports/small-interpreter-stage1.md)). The v2 labels
there were right (sarcasm/none: 124 of 131 sarcastic). There were just few of them. Spec v3
changes only variant weights (`_CONTRAST_HEAVY`, new `_NONE_HEAVY` for passive aggression),
so it reuses v2's scenarios and needs no generator:

```bash
python -m distill.pipeline generate --adopt v2        # v2's 4,229 scenarios, restamped v3
python -m distill.pipeline expand
python -m distill.pipeline label --api deepseek --api-thinking --teacher deepseek-flash --concurrency 8
# local fallbacks only where they can matter: rows the teachers before them got wrong
python -m distill.pipeline label --teacher qwen3:14b --only-failed deepseek-flash
python -m distill.pipeline label --teacher gemma3:12b --only-failed deepseek-flash,qwen3:14b
python -m distill.pipeline build --teacher deepseek-flash --second qwen3:14b --also gemma3:12b
```

DeepSeek labelled all 8,458 rows with no failures (9.1M tokens in at 83% cache hits, 1.33M
out, under $2) and broke the rules on 2,515. The 14B then labelled those 2,515 and gemma the
1,580 both got wrong, about 55 minutes of GPU against ~4 h for full local passes.

**Result: 6,622 kept** (6,273 train, 349 val; deepseek-flash 5,546, qwen3:14b 724, gemma3:12b
352). No-voice rows: sarcasm 134 → 269, sincere positive 138 → 227, passive aggression
187 → 239. Contrast rows held (1,476 → 1,437). The easy congruent cells paid for it
(sarcasm 325 → 213, sincere positive 279 → 207). The build's "second teacher agrees"
figure (4%) means nothing here: the 14B labelled only the rows DeepSeek failed.

**DeepSeek-only comparison** (`data/distill/v3-ds`): the same rows and labels built with
no fallback teacher keep 5,531 (5,250 train, 281 val). The 1,091 missing rows are what the
local teachers rescued, most in teasing/contrast (433 → 262), passive aggression/none
(239 → 150) and sarcasm/contrast (294 → 222). Trained the same way, the two students tie
within noise ([Stage 1](../reports/small-interpreter-stage1.md)), so **the local fallbacks
are optional**: `build --teacher deepseek-flash` alone is enough, and it saves the ~55 minutes
of GPU the `--only-failed` passes cost.

Generator faults found and fixed in the pilot, so they are not rediscovered:
- category draws too noisy, now round-robin;
- the briefs quoted eval phrases and the generator copied them;
- context counts asked in prose came back under half, now pinned by the schema;
- "…", em dashes, curly quotes and `*emphasis*` in lines, now ASR-normalised;
- the plan text, true feeling included, echoed into the dialogue, now dropped.

## What comes out

| file | contents |
|---|---|
| `train.jsonl`, `val.jsonl` | TRL conversational **prompt/completion**: `prompt` = system (`SYSTEM_PROMPT`) + user (the rendered prompt), `completion` = the assistant's JSON. Loss lands on the completion only, with no `{% generation %}` tags needed (design §6.2). |
| `train.meta.jsonl`, `val.meta.jsonl` | Same order, one line per row: id, category, variant, voice dict, speaker label, accepted sets, teacher probabilities, the second teacher's answer and whether it agreed. For soft targets, weighting and per-category eval. |
| `rejects.jsonl` | Every dropped row with its reason and the teacher's answer. Read it: the `tone_outside_rules` rows are where the teacher and the prompt's rules disagree. |
| `manifest.json` | Spec version, `SYSTEM_PROMPT` sha256, models, counts per category × variant, reject reasons, read diversity, the voice-confidence ranges and the recorded decisions. |
| `scenarios.jsonl`, `rows.jsonl`, `labels_<model>.jsonl` | The intermediate phases. |

Training notes:

- **The JSON key order is `emotion`, `tone`, `read`.** It matches what the schema makes
  the teacher emit, and `_tone_prob` reads the first token of the value.
- **A shortened system prompt is possible** (design §1): replace the system message at
  train time. Serving must then use the same short prompt, through a per-model
  `config.py` knob.
- **For Qwen3, train and serve with thinking off** (an empty `<think></think>` in the
  template), matching the `think: false` the teacher was labelled with.

## Training locally on the B580 (`distill/train.py`)

Training runs in its own venv, `.venv-train` (gitignored). It needs torch built for Intel
GPUs, which the server's `.venv` must not get:

```bash
python -m venv .venv-train
.venv-train/Scripts/python -m pip install torch --index-url https://download.pytorch.org/whl/xpu
.venv-train/Scripts/python -m pip install transformers trl datasets accelerate torch-optimi
.venv-train/Scripts/python -m distill.train --check-mask     # what carries loss, one row
.venv-train/Scripts/python -m distill.train --max-steps 10   # timing / memory trial
.venv-train/Scripts/python -m distill.train                  # full run -> models/student/
```

Stop the emtext server and unload Ollama models first: training needs the GPU to itself.
Plugging the monitor into the motherboard's iGPU gives back about 1 GB of the B580's VRAM.

**First run (2026-11-24): Qwen3-0.6B, full fine-tune, data v2.** 2 epochs, 394 steps of 32
rows (2 per step × 16 accumulated), learning rate 1e-5 with cosine decay, about 81 minutes at
12.2 s per step. Peak memory 6.6 GB. Library versions: torch 2.15.0+xpu, transformers 5.24,
trl 1.18.2, torch-optimi 0.3.3. Validation loss went 0.601 → 0.536 → 0.512 → 0.481 → 0.480 →
0.475 → 0.472 → **0.471** (steps 50…394). It was still falling at the end, with no
overfitting, and the final token accuracy was 89.3%. Things found on the way, so they are
not rediscovered:

- **bf16 works on Windows with torch 2.15.** A one-layer forward/backward check against
  the CPU found no NaN or zeros, so torch-xpu-ops#5220 does not reproduce here. Raw bf16
  matmul measured 97 TFLOPS.
- **Pure bf16 with Kahan-summed AdamW** (`torch-optimi`). fp32 master weights would need
  ~9.6 GB before activations.
- **Triton is unusable without a C compiler** (Windows has none by default). Both
  optimi's kernels (`triton=False`) and TRL's chunked LM-head loss (patched to use its
  torch path) are kept off Triton.
- **TRL 1.x label smoothing costs an extra forward pass** per step, so it is off by default.
- **4 rows per step crashed** at step 18 with `UR_RESULT_ERROR_OUT_OF_RESOURCES` (peak
  7.3 GB). 2 × 16 has the same effective batch and ran clean. Rows are 999-1,153 tokens,
  so nothing is truncated at the 1,536 cap.
- **`--save-steps` must be set when a run starts.** On `--resume`, transformers keeps the
  checkpoint's interval and only warns.

Export and serve: convert `final/` to GGUF Q8_0 with llama.cpp's `convert_hf_to_gguf.py`, then
`ollama create emtext-qwen3-0.6b:v2 -f distill/Modelfile.student`. That Modelfile's template is
the training format, not the stock `qwen3` one (which appends ` /no_think`). Results, weak
spots and commands: [`reports/small-interpreter-stage1.md`](../reports/small-interpreter-stage1.md).

Model and data versions live in **private** Hugging Face repos: `Jwbpk/emtext-distill` (tag
`v2` = this set) and, once evaluated, `Jwbpk/emtext-interpreter-qwen3-0.6b`. The token is
the `HF_TOKEN` user environment variable. Keep both repos private until the provenance
questions below (Gemma terms, DeepSeek terms) are answered.

## Decisions recorded in every manifest

- **English only.** `parakeet:v3` transcribes 25 languages and the 14B reads them, so a
  student trained on this set will quietly lose that (design §8).
- **Emotion2vec-shaped hints only.** No valence, matching the default backend. The
  confidence ranges (0.50-0.95 confident, 0.15-0.38 low) are a design choice: real
  confidence on this mic has never been profiled.
- **No public corpora seeded yet.** Grounding generation in real lines beat generating
  from an empty prompt (design §5.1d). iSarcasmEval's sincere rephrases and GoEmotions
  (Apache-2.0) are the next sources, each a licence decision first.
- **Synthetic only, so it can go to a rented GPU.** Nothing here comes from a real
  recording or the transcript log (design §6.1 and §7).

Every headline number for a student trained on this must still come from held-out real
speech (design §5.2). This set teaches the format and the common cases.
