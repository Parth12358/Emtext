# Small interpreter, Stage 1: the distilled Qwen3-0.6B (2026-11-24)

Stage 1 of [`design/small-interpreter.md`](../design/small-interpreter.md): a full fine-tune
of Qwen3-0.6B on the distillation set (spec v2, `docs/guides/distill.md`), served through
Ollama with the production `SYSTEM_PROMPT`, schema and logprobs, and scored on the same two
suites as [Stage 0](small-interpreter-stage0.md). Neither suite contributed training rows:
`distill/pipeline.py build` rejects any row near an eval transcript.

**Verdict: it clears the gate and beats `qwen3:14b` on the voice-driven metrics, at a
quarter of the latency and ~1/8 of the VRAM.** Literal and low-confidence stay at 100%. It
is still behind the 14B on sarcasm and passive aggression **when there is no voice hint**,
and it reads plain praise with no voice as emotion `neutral`. Not yet switched on as the
default: the confidence Platt parameters were fitted on the 14B and must be refitted on
the student first (`python -m eval.confidence_eval`).

## What was run

| | |
|---|---|
| model | `Qwen/Qwen3-0.6B`, full fine-tune, bf16 + Kahan AdamW, 2 epochs, lr 1e-5 cosine, batch 32 |
| data | `Jwbpk/emtext-distill` tag `v2`: 6,287 train / 346 val, built at git `83b1166` |
| code | `distill/train.py` at git `0fedb8a`; final val loss 0.471, token accuracy 89.3% |
| export | `llama.cpp` `convert_hf_to_gguf.py --outtype q8_0` (633 MB), Ollama `emtext-qwen3-0.6b:v2` |
| template | `distill/Modelfile.student`: the training format exactly (see below) |
| hardware | Arc B580 (Ollama 0.40.16, Vulkan), desktop on the iGPU |

## Results

### `tone_cases.jsonl` (34 cases x 3 runs)

| model | tone | emotion | mismatch pairs | literal | low-conf (tone) | sarcasm | passive aggr. | read, mean |
|---|---|---|---|---|---|---|---|---|
| **emtext-qwen3-0.6b:v2** | **86%** | 92% | **5/6** | 100% | 100% | 50% | 50% | 0.20 s |
| qwen3:0.6b (stock) | 63% | 61% | 1/6 | 93% | 100% | 25% | 0% | 0.15 s |
| qwen3:14b | 83% | 95% | 2/6 | 100% | 100% | 75% | 83% | 0.73 s |
| *(copy voice)* | | 85% | | | | | | |

The stock and 14B rows were re-run beside the student on the same day; they match Stage 0
within run-to-run noise. The student moved the voice label on 9 reads, all 9 to an accepted
emotion (stock 0.6B: 3 of 21; 14B: 9 of 12).

### RAVDESS, end to end (280 clips, emotion2vec, parakeet:v3; SER alone 89%)

| model | shown emotion | tone | voice sensitivity | labels changed | of which wrong | read, median |
|---|---|---|---|---|---|---|
| **emtext-qwen3-0.6b:v2** | **89%** | **81%** | **+49%** | 0 | 0 | 0.16 s |
| qwen3:0.6b (Stage 0) | 51% | 17% | +4% | 118 | 108 | 0.18 s |
| qwen3:14b (Stage 0) | 85% | 65% | +30% | 12 | 10 | — |

Per acted emotion the student equals the SER exactly (angry 100, disgusted 85, fearful 80,
happy 88, neutral 100, sad 78, surprised 92), because it never moved a label. On RAVDESS
that is the right call: both sentences are neutral by construction, so the words give no
reason to override the voice, and every 14B override but two was a mistake. `tone_cases`
shows it does move the label when the words demand it.

## Where it is weak

All three failure groups are lines **with no voice hint**:

- **Text-only sarcasm and passive aggression.** "Wow, you actually showed up on time
  today. Miracle." → *positive, "genuinely impressed"*; "Sure, because that plan worked out
  so well last time." → *neutral, "plain agreement"*; "It's fine, I'll just do it myself
  like always." → *neutral*. The 14B gets these.
- **Plain praise reads as emotion `neutral`** (`positive` category, emotion 11%): the tone
  is right, but "Thanks so much for your help today" comes out `neutral`, and some reads
  invent doubt the words do not carry ("slight hesitation", "no real excitement").
- A plausible cause, not verified: the no-voice variant is 15% of rows in the oversampled
  sincere-positive and sarcasm cells (`_CONTRAST_HEAVY`), so the student learned to lean on
  the voice. A v3 spec would raise `none` there, then retrain and re-run both suites.

## Serving notes

- **Template.** The stock `qwen3` Ollama template appends ` /no_think` to the last user
  turn whenever a request sends `think: false`, which `interpreter.py` always does. Training
  used the HF template with `enable_thinking=False`, which adds no suffix and opens the
  answer with an empty `<think>\n\n</think>\n\n`. `distill/Modelfile.student` reproduces that
  exactly. Do not build the student from the stock template.
- **Ollama on the wrong GPU.** Ollama numbers Vulkan devices when it starts. With the
  monitor moved to the motherboard's iGPU after Ollama started, new runners opened
  "device 0" and got the AMD Radeon iGPU while `ollama ps` still said "100% GPU": both 0.6B
  models read in 2.2 s at 15-20 tok/s. Restarting Ollama re-ran discovery, found the B580,
  and the same reads took 0.15-0.2 s. **A restart is not enough:** an hour later, a newly
  loaded `qwen3:14b` landed on the iGPU again at 22.6 s per read. The device order is not
  stable between runners. Hiding the AMD Vulkan driver from Ollama's process alone
  (`VK_LOADER_DRIVERS_DISABLE=*amd-vulkan*`, then `ollama serve`) leaves the B580 as its only
  device; the 14B then labelled at 1.05 s per row. The server log line
  `using device Vulkan0 (...)` names the card actually used.

## Reproduce

```bash
.venv-train/Scripts/python models/tools/llama.cpp/convert_hf_to_gguf.py \
    models/student/Qwen3-0.6B-v2/final --outtype q8_0 \
    --outfile models/gguf/emtext-qwen3-0.6b-v2-q8_0.gguf
ollama create emtext-qwen3-0.6b:v2 -f distill/Modelfile.student
python -m eval.model_eval --models emtext-qwen3-0.6b:v2 qwen3:0.6b qwen3:14b \
    --runs 3 --csv eval/results/model_student_v2.csv
python -m eval.pipeline_eval --llm emtext-qwen3-0.6b:v2 --cells 1 --limit 280 \
    --speech-rms 150 --csv eval/results/pipeline_student_v2.csv
```

Run the converter **without** `python -I`: it imports the `conversion/` package beside it.
