# A fine-tuned ≤1.5B interpreter, and the dataset it needs

**Date:** 2026-10-03 -- **Status:** researched, nothing built or measured -- **Touches:**
nothing in `server/` yet

Research for replacing `qwen3:14b` with a model of at most 1.5B parameters, fine-tuned on a
dataset we build ourselves. The model would produce the five-word read (the "summary"), the
tone, and the emotion adjustment. Every emtext number below is from a file in this repo,
named next to it. External claims carry a link and the evidence markers from
[`RESEARCH.md`](RESEARCH.md): **[STRONG]** / **[SINGLE]** / **[PREPRINT]** / **[GAP]**, plus
**[EST]** for an estimate made here and not measured anywhere.

---

## Verdict

**Worth doing, in stages. The latency and VRAM win is close to certain; the quality win is
not.** Stage 0 costs nothing, Stage 1 costs under ~$30 of rented H100 time, and either can
stop the effort.

- **What we gain.** `qwen3:14b` takes 9.3 GB of the B580's 12 GB (`README.md` model table)
  and ~0.89 s of the ~1.94 s from stop-talking to read-on-screen. Most of that 0.89 s is
  **decode, not prefill**: ~26 output tokens at ~41 tok/s ≈ 0.63 s, against ~505 prompt
  tokens at ~1050 tok/s ≈ 0.48 s (`README.md`, "LLM throughput"). A 0.6-1.5B model decodes
  several times faster, takes 0.6-1.6 GB at Q8_0, and could run on the CPU, freeing the GPU
  entirely **[EST]**.
- **Where a small model has its best chance.** Fine-tuned small models routinely beat
  prompted giants on narrow classification and short-generation tasks: Bucher & Martini
  report it for sentiment, emotion and stance **[PREPRINT]**, and distilling step-by-step
  reports it for NLI **[SINGLE]** (sources in §4). Our task is that shape: three fields,
  two of them enums, and a read of at most 5 words.
- **Where it doesn't.** The product's hard cases are sarcasm, masking and the
  voice-vs-words mismatch, and synthetic data is weakest on exactly those. Models trained on
  generated sarcasm "collapse to random guessing on organic human speech" (§5.2). And we have
  **no real conversational speech labelled for any of it** (TODO, "Sarcasm is never tested on
  real speech"). A distilled model can match the teacher at best; beating it needs real
  labels.
- **The blocker is the data, not the GPU.** One full H100 sweep across every candidate costs
  under $15 (§6.1). Building a held-out set of *real* speech that is honestly labelled and
  legally clean is the long pole, and it is needed anyway to judge the current 14B.

**Cheapest first experiment:** Stage 0 in §9. Run `eval.model_eval` and `eval.pipeline_eval`
on `qwen3:0.6b` and `granite4` exactly as they are, with today's prompt. That gives the floor
a fine-tune has to clear, and it costs an afternoon.

---

## 1. The task, precisely

A fine-tune is easy to over-scope, so here is everything the interpreter does today
(`server/interpreter.py`).

**Input**, built by `_build_prompt`:

- up to `CONTEXT_LINES = 12` earlier lines;
- speaker tags (`listener:` / `speaker:`), rendered only once a voiceprint is enrolled;
- optionally a voice hint line, for example
  `voice sounded like: angry (conf 0.77), arousal 0.70 (elevated), dominance 0.55 (moderate)`.
  On the default SER backend valence is absent, and a label-only hint says
  `label only, no scales measured`;
- the newest line, fenced by `_fence()` (whitespace collapsed so a spoken line cannot forge
  a prompt section).

The ~2,600-character `SYSTEM_PROMPT` carries all the rules: start from the voice label, the
mismatch rules on arousal and dominance and on valence, the "stock sarcastic phrase with a
warm voice is sincere" rule, discounting voice confidence below ~0.4, and the caution when
there is no voice.

**Output**, schema-constrained through Ollama `format` (`RESPONSE_SCHEMA`):

```json
{"emotion": "neutral|happy|sad|angry|fearful|disgusted|surprised",
 "tone": "positive|negative|neutral|sarcastic|mixed",
 "read": "<at most 5 telegraphic words>"}
```

The call itself is `temperature 0.2`, `num_predict 80` and `keep_alive -1`, with
`logprobs` and `top_logprobs 5`.

**What a replacement model must keep:**

1. **Schema-constrained JSON through Ollama `format`.** The sampler enforces this, not the
   model, so it works with any imported GGUF ([Ollama structured
   outputs](https://docs.ollama.com/capabilities/structured-outputs)).
2. **Per-token logprobs, with the enum value starting on a token boundary.** `_tone_prob()`
   walks the tokens to the character where the value starts. Measured on qwen3:14b, Ollama's
   logprobs are **pre-grammar-mask** (`interpreter.py`, `_tone_prob` docstring), so the
   first token of the value carries a real distribution over the labels. A new tokenizer can
   split `"sarcastic"` differently; check that the boundary still lands. Ollama has had
   logprobs on `/api/generate` since v0.12.11
   ([release](https://newreleases.io/project/github/ollama/ollama/release/v0.12.11)).
3. **The confidence contract.** `confidence.base_prob()` uses the interpreter's
   probability only when it *adjusted* the SER label, where `emotion_prob` scores AUROC
   0.59. Unadjusted reads use the SER's confidence, which scores 0.76, and the composite is
   0.78 (TODO, "Confidence had to change with it"). The Platt parameters and cut-offs are
   fitted to qwen3:14b; **a new model needs them refitted** with
   `python -m eval.confidence_eval --fit`. Instruction tuning is known to damage calibration
   (§4).
4. **Graceful degradation, `user` lines get no read, `_fence`, speaker-tag rendering.**
   These all live in `interpreter.py` and `main.py`, not in the model. They carry over
   unchanged as long as the model is still served through Ollama.

**The opportunity and its risk.** A fine-tune can drop most of `SYSTEM_PROMPT` and keep only
a short task header, because the behaviour moves into the weights. That cuts prefill.
Training and serving must then use byte-identical templates. The risk is that the guard rails
(the `literal` and `low-confidence` categories, "never let a low-confidence voice alone turn
a plain sentence into sarcasm") **then exist only as training examples**. Each rule needs
enough examples to survive, and editing the prompt becomes "regenerate the data and
retrain" (§8).

---

## 2. What "the summary bit and more" should cover

The model produces three fields today. They are not equally suited to a 1B model.

| field | what it does today | small-model verdict |
|---|---|---|
| `read` (≤5 words) | The product's visible sentence. Free text, unconstrained by the schema. | **The main job.** Short, formulaic and reference-able. This is where distillation shines and where the data has to be built from scratch (below). |
| `tone` | Now effectively a sarcasm/mixed flag. The headline moved to `emotion` (TODO, "The headline is the SER's emotion"). | **Keep, as a 5-way head.** It is the field the hard cases live in, and the one most at risk from synthetic data. |
| `emotion` adjustment | Start from the SER label and move only if the words make it untenable. On 280 RAVDESS clips the LLM moved 3 labels, **all wrongly** (SER alone 88.9%, shown 88.2%). | **Mostly a copy.** A small model will learn "copy the voice label" almost for free. The rare justified adjustment needs real speech, where the words carry information; RAVDESS words are neutral by construction. Teach it with explicit examples where text with strong polarity overrides a low-confidence label. |
| clip summary (new) | Not built. A saved clip's sidecar joins the reads with `" / "` (`server/clips.py`). | **Optional, later.** One line over a saved moment ("argued about plans, ended calm") is a natural second task. It is off the latency path, so a separate prompt to the same model is fine. Out of scope for Stage 1. |

**The existing reads are not training data.** `eval/results/pipeline_emotion.csv` has 280
reads with only **45 distinct strings**. "noticing kids" alone appears 54 times, because
RAVDESS has two sentences ("Kids are talking by the door", "Dogs are sitting by the door").
The read has to come from a new dataset.

---

## 3. Candidate base models ≤1.5B

As of 2026-10. "IFEval" mixes metrics across sources: compare within one source only.

| model | params | licence | ctx | languages | Ollama | notes |
|---|---|---|---|---|---|---|
| **Qwen3-0.6B** | 0.6B | Apache-2.0 | 32K | 119 | `qwen3:0.6b` official | Same family, tokenizer and template as the current qwen3:14b teacher. Plain architecture, so an imported fine-tune has no risk. IFEval 54.5 with thinking off ([Qwen3 report](https://arxiv.org/html/2505.09388v1)). Train and serve with an empty `<think></think>`. |
| Qwen3.5-0.8B | 0.87B | Apache-2.0 | 262K | 201 | `qwen3.5:0.8b` | Newest. Hybrid Gated-DeltaNet vision-language model; **imported fine-tunes fail to load in Ollama** unless exported text-only ([#14730](https://github.com/ollama/ollama/issues/14730)). The card warns of thinking loops. Side variant only. |
| Qwen2.5-0.5B / 1.5B | 0.5 / 1.5B | Apache-2.0 | 32K | ~29 | yes | Older; IFEval 42.5 at 1.5B. A baseline, not a pick. |
| **Granite-4.0-H-1B** | 1.5B (hybrid: 4 attention + 36 Mamba2 layers) | Apache-2.0 | 128K | 12, incl. de/es/fr/it/nl/cs/pt | `granite4` family | Strongest instruction following under the cap with an unrestricted licence: IFEval 78.5 avg ([HF](https://huggingface.co/ibm-granite/granite-4.0-h-1b)). Hybrid, so **an imported fine-tune must be checked in Ollama**. |
| **LFM2.5-1.2B-Instruct** | 1.17B | LFM Open Licence: commercial use only under $10M revenue ([licence](https://www.liquid.ai/lfm-license)) | 32K | 8 | community tags only; official GGUF on HF | Best IFEval (86.2) and fastest on CPU (below). Misses most of the 25 languages `parakeet:v3` transcribes. |
| Gemma 3 1B / 270M | 1B / 0.27B | Gemma Terms + Prohibited Use Policy | 32K | 140+ | `gemma3:1b`, `gemma3:270m` | Google positions the 270M for fine-tuned classification ([card](https://ai.google.dev/gemma/docs/core/model_card_3)). No system role. The licence is the cost. |
| Llama 3.2 1B | 1.23B | Llama 3.2 Community Licence + AUP | 128K | 8 | yes | IFEval 59.5. Licence terms and only 8 languages. |
| *over the cap* | | | | | | Qwen3-1.7B, SmolLM2-1.7B, Granite-4.0-1B dense (1.6B), Gemma 4 E2B (5.1B with embeddings), Phi-4-mini (3.8B), SmolLM3 (3B). |

**Try first:** Qwen3-0.6B, then Granite-4.0-H-1B, then LFM2.5-1.2B. The 0.6B is the
cheapest to distil into, because teacher and student share a tokenizer and chat template.
Granite is the quality bet. LFM is the speed bet, if the licence and languages are
acceptable.

**Where it runs.** On the CPU, a Zen 5 laptop chip (Ryzen AI 9 HX 370, llama.cpp Q4_0) does
LFM2.5-1.2B at 2,975 tok/s prefill / 116 tok/s decode, and Qwen3-1.7B at 2,008 / 62
([Liquid](https://www.liquid.ai/blog/introducing-lfm2-5-the-next-generation-of-on-device-ai))
**[SINGLE, vendor]**. On our 6-core 9600X, a ~300-token prompt plus ~25 output tokens comes
out at roughly **0.2-0.6 s for 1-1.5B, less for 0.6B** **[EST]**. That would free the GPU
completely and overturn the hardware split (LLM on GPU, everything else on CPU). But it
competes with ASR, SER and speaker id for the same six cores, which run in parallel per
utterance. Measure with `llama-bench -p 300 -n 25` while `eval.latency` runs, before
deciding. On the B580 it would decode far faster than 41 tok/s **[EST]**.

---

## 4. What the literature says about small fine-tuned models

- **Fine-tuned small models beat prompted giants on narrow labelling tasks.**
  - Distilling step-by-step: a 770M T5 beat 540B PaLM few-shot on ANLI with 80% of the
    data, and rationales cut the data needed (12.5% of e-SNLI)
    ([ACL Findings 2023](https://aclanthology.org/2023.findings-acl.507/)) **[SINGLE]**.
  - Fine-tuned small LLMs beat zero-shot GPT-3.5/4 and Claude Opus on sentiment, emotion and
    stance, with the gap largest on the less standard tasks such as emotion
    ([Bucher & Martini](https://arxiv.org/abs/2406.08660)) **[PREPRINT]**.
  - Prompted LLMs reach ~97% of a fine-tuned T5 on plain sentiment but lag on complex or
    fine-grained phenomena ([NAACL Findings 2024](https://aclanthology.org/2024.findings-naacl.246.pdf)) **[SINGLE]**.
- **Rationales help at training time only.** Step-by-step trains label and rationale as two
  tasks under separate prefixes, so inference emits only the label. That suits a latency
  budget: have the teacher write a one-line "why" and train on it as an auxiliary task, but
  serve only the JSON.
- **Calibration gets worse.** Instruction tuning degrades calibration; RLHF tends to
  preserve it ([Zhu et al.](https://arxiv.org/abs/2311.13240)) **[SINGLE]**. Fine-tuning on
  hard labels increases overconfidence; label smoothing reduces calibration error at little
  accuracy cost ([2601.01362](https://arxiv.org/html/2601.01362v1),
  [2505.20903](https://arxiv.org/pdf/2505.20903)) **[PREPRINT]**. For us: train the enum
  heads with label smoothing, or on soft targets (the teacher's top-k probabilities or the
  rater distribution, §5.4). Then refit Platt; the old parameters do not transfer.
- **[GAP]** No published emotion, sentiment or sarcasm result for any of the specific ≤1.5B
  models above. No paper on suppressing one false-positive label at this size.

---

## 5. Building the dataset

One row = one interpreter call: `context[]` (with optional speaker tags) + `voice` hint (or
null) + `transcript` → `{emotion, tone, read}`, plus training-only fields: `rationale`,
`source`, `licence`, `split`, and soft label distributions where available. The **input side
must be byte-for-byte what `_build_prompt` renders**. Generate rows through that function
rather than re-implementing it, so the training distribution cannot drift from production.

### 5.1 Sources, in order of how much they should be trusted

**(a) Real speech we record ourselves. The gold, and the only honest test set.**

- Scripted, consented sessions: two or more people enacting everyday exchanges, including
  sincere and sarcastic readings of the *same* line, masking ("it's fine") and passive
  aggression. Recorded through the pendant's own mic, so the voice hints come from our own
  SER on our own audio.
- Even 20-30 sincere/sarcastic pairs beat anything in the repo as a test of the core claim
  (TODO, "What would actually close this").
- Spontaneous sarcasm is not acoustically marked the way posed sarcasm is (RESEARCH §2.1),
  so scripted sessions are a ceiling. Record some free conversation too.

**(b) The transcript log.** `logs/transcripts/` holds 256 rows over three days, with 12
utterance WAVs. It is real, and it is the right *shape* (real ASR, real SER, real context
order). But:

- it records third parties who did not consent to training (§7);
- it was never designed as a dataset: the rendered prompt is not stored, only row order;
- it is gitignored and must stay off any public artifact or rented machine (§6.1).

Use it only with the speakers' consent, labelled by hand, and preferably as **test** data
rather than training data.

**(c) Public corpora mapped onto our schema.**

| corpus | licence | what it gives us | fit |
|---|---|---|---|
| **MELD** | GPL-3.0 repo over *Friends* clips; whether trained weights are a derivative is unanswered ([#19](https://github.com/declare-lab/MELD/issues/19)) | ~13k utterances, multi-party dialogue, **same 7 emotions as ours**, audio | Closest match, and we can run our own SER over its audio to get realistic voice hints. Licence risk for anything shipped. Sitcom acting with a laugh track. |
| **EmoryNLP** | Apache-2.0 | 12.6k *Friends* utterances, text, dialogue | Willcox labels (powerful/peaceful) map poorly to ours. |
| **GoEmotions** | Apache-2.0 | 58k Reddit comments, 27+neutral, single line | Folds cleanly into our 7. No context and no voice. Good for emotion-from-words examples. |
| **iSarcasmEval** | MIT repo, tweets under X's terms | 3.5k tweets with **the author's own sincere rephrase** | Ready-made minimal pairs: same meaning, said sincerely. Author-intended labels. |
| **MUStARD** | MIT repo, TV clips copyrighted | 690 clips, audio, context | κ = 0.15 (RESEARCH §2.5). Test, never train. |
| **MUStARD++** | **no licence stated**, so assume all rights reserved | 1,202 clips, audio | Evaluation only, if at all. |
| **PodSarc** (Interspeech 2025) | dataset licence unchecked | 29.4 h podcast audio, 4k sarcastic / 7k not | **Spoken, naturalistic sarcasm we can run our SER over.** Labels are partly LLM-made, with humans resolving the ~26% of segments where GPT-4o and LLaMA-3 disagreed ([ISCA](https://www.isca-archive.org/interspeech_2025/li25ba_interspeech.pdf)). |
| **MSP-Podcast** | free academic licence; commercial terms unpublished | 409 h natural speech, isolated turns | No context. Best for checking what our SER says on natural speech, not for interpreter rows. |
| IEMOCAP | "internal research purposes only", no derivatives | dyadic acted, audio | Internal evaluation only. |
| DailyDialog, EmpatheticDialogues | CC BY-NC(-SA) | text dialogue | Non-commercial; skip unless the project stays non-commercial. |
| K-EmoCon | restricted, user agreement | Korean debates, self + partner + observer labels | The only corpus found that separates felt from perceived emotion, which is what masking is. Research reference. |

**[GAP]** No passive-aggression dataset exists; it appears only as a sub-type in abuse
taxonomies ([2504.17653](https://arxiv.org/pdf/2504.17653)). We build those examples
ourselves.

**(d) Teacher distillation over generated scenarios. The bulk.** A large model writes
everyday dialogue scenarios, and a teacher (qwen3:14b today, or a 32B / frontier model)
labels them through the **exact current prompt**. Distillation works (§4), but it copies the
teacher's known failures:

- qwen3:14b over-calls sarcasm on a confidently happy voice (2/6 mismatch pairs, TODO);
- under the emotion-first prompt, `mm-01a` / `mm-03a` call a warm voice sarcastic and
  `pa-03` / `pa-04` fall to neutral.

Containment:

- **Multi-teacher voting.** Keep only rows where two different teachers agree on tone and
  emotion; candidate-label distillation is the published version of this
  ([CanDist, ACL 2025](https://aclanthology.org/2025.acl-long.139.pdf)) **[SINGLE]**.
- **Ground generation in real lines.** For synthetic sarcasm, generating from real examples
  beat filtering or taxonomy prompting ([2305.15041](https://arxiv.org/abs/2305.15041)).
  Seed from iSarcasmEval, MELD and our own recordings rather than from an empty prompt.
- **Deduplicate inputs and reads**, exact and embedding-near
  ([2107.06499](https://arxiv.org/abs/2107.06499), [SemDeDup](https://arxiv.org/abs/2303.09540)).
  Cap any single read string at a small share of the set, or the model collapses onto the
  RAVDESS-style "noticing kids" stock (§2).

**(e) Controlled voice hints.** Text corpora have no voice. Hints have to be sampled, and
sampled **the way production actually emits them**:

- **Realistic gaps.** The current mismatch pairs use valence gaps of 0.47-0.73. Real SER
  separates pleasant from unpleasant by only ~0.14 (TODO, "The mismatch pairs test an input
  production never emits"). Train on the realistic gap or the model learns a cue that never
  occurs.
- **The hint mix production sees**: mostly label-only or label + arousal/dominance (the
  emotion2vec default, `dims_source: "derived"`); valence rarely; a large share of
  low-confidence hints; no voice line at all for some rows.
- **Prefer real SER over real audio** wherever a corpus has audio (MELD, PodSarc, our own
  recordings). Sampled hints are the fallback.

### 5.2 Synthetic data is weakest exactly where the product lives

- The more subjective the task, the more training on synthetic data hurts, with humour and
  sarcasm worst ([EMNLP 2023](https://aclanthology.org/2023.emnlp-main.647/)) **[SINGLE]**.
- Models score near-perfectly on synthetic sarcasm and "collapse to random guessing on
  organic human speech", because they learn the generator's signature
  ([ACL 2026](https://aclanthology.org/2026.acl-long.1346/)) **[SINGLE]**.
- Multimodal models flag sarcasm from a stereotype of elevated pitch and irregular pausing:
  manipulating only those drove false positives to 60%
  ([2608.30204](https://arxiv.org/abs/2608.30204)) **[PREPRINT]**. RESEARCH §2.4 already
  records the same finding.

**Consequence:** synthetic rows may teach the *format* and the common cases, but **every
headline number must come from real held-out speech**. Two kinds of hard negatives get a
deliberate share of the data: sincere lines that look sarcastic (iSarcasmEval rephrases,
stock phrases said warmly), and sincere lines with expressive delivery.

### 5.3 How many rows

- LIMA aligned a 65B model with 1,000 curated examples ([2305.11206](https://arxiv.org/abs/2305.11206)).
  AlpaGasus beat Alpaca's 52k with a filtered 9k ([2307.08701](https://arxiv.org/abs/2307.08701)).
  Both are general instruction following, not our task.
- **Planning figure [EST]:** 5-20k rows for Stage 1, with **several hundred per hard
  category** (sarcasm, sincere-that-looks-sarcastic, masking, passive aggression, mixed,
  low-confidence voice, no voice, `listener:`-tagged context). Below ~5k the read will not
  diversify; above ~50k we are only buying more teacher noise.

### 5.4 Labelling protocol, for the real-speech set

- **Raters.** About 4 non-expert labels per item matched expert quality on affect
  ([Snow et al. 2008](https://aclanthology.org/D08-1027/)) **[SINGLE]**. Use ≥3, ideally 5,
  per item for tone and emotion. **Keep the full distribution**, not a majority label:
  disagreement is signal ([LeWiDi](https://arxiv.org/pdf/2510.08460)), gives soft training
  targets, and gives calibration targets for `confidence.py`.
- **Report κ or α, never percent agreement.** The majority class inflates percent agreement.
  For calibration of expectations: MUStARD reached κ = 0.15, and humans hit ~68% on
  naturalistic sarcasm (RESEARCH §2.5-2.6).
- **Who labels sarcasm matters.** Third-party annotators miss 30% of author-intended sarcasm
  (RESEARCH §2.5). In our own recordings the speaker can label their own intent. Take that
  as gold, with third-party labels alongside.
- **The read has no single right answer.** Collect 2-3 acceptable reads per item and score a
  candidate with a rubric:
  1. fits the line, context and voice;
  2. actionable rather than clinical;
  3. **does not over-read** (yes/no, scored on its own);
  4. at most 5 words.

  An LLM judge can scale this only after humans have graded a slice and the judge's κ
  against them is measured. Even top judges are up to 5 points off on absolute scores, and
  small judges are much worse ([Thakur et al.](https://arxiv.org/abs/2406.12624)) **[SINGLE]**.

### 5.5 Splits and leakage

- **Never train on the eval suites.** `eval/tone_cases.jsonl` (34 cases),
  `eval/results/model.csv` and every RAVDESS-derived CSV stay held out. Dedup the training
  set against them, by text and by near-duplicate embedding. Generated scenarios will
  reinvent "oh wonderful, just perfect" on their own.
- Split real recordings **by speaker and by session**, not by row, or speaker voice leaks
  across the split.
- Keep a frozen **real-speech test set** that no training stage, prompt edit or teacher ever
  sees. It is also the missing test for the current 14B.

---

## 6. Fine-tuning

### 6.1 Compute: rented H100 vs the local Arc B580

**Rented H100.** On-demand for one GPU, 2026-10. Prices move: Hyperbolic doubled this year.

| provider | 1× H100 | note |
|---|---|---|
| RunPod Community | PCIe $1.99 / SXM $2.69 | peer-hosted machines |
| RunPod Secure | PCIe $2.89 / SXM $3.49 | Tier 3/4 datacentres; SOC 2 claims cover Secure only ([docs](https://docs.runpod.io/pods/storage/types)) |
| Lambda | PCIe $3.29 / SXM $3.99 | |
| Vast.ai | $1.50-2.27 verified, ~$0.90 unverified | unverified hosts risk interruption and data loss |
| Modal | SXM ≈ $3.95/h + CPU/RAM | per-second billing ([pricing](https://modal.com/pricing)) |
| CoreWeave | 8-GPU nodes only (~$49/h) | wrong size for this job |

Throughput **[EST]**: Unsloth full fine-tuning of Qwen2.5-0.5B was measured at ~11.7k tok/s
on an A100-40GB ([2601.02609](https://arxiv.org/html/2601.02609v1)). Taking an H100 as
~2.5× that gives ~30k tok/s for 0.5B and ~10k tok/s for 1.5B; small models with short
sequences are bound by overhead, not compute. Plan for ±2×. The budget is 400 tokens ×
3 epochs at ~$3/h, with only the ~25 answer tokens carrying loss:

| run | 5k rows (6M tok) | 20k (24M) | 50k (60M) |
|---|---|---|---|
| 0.5B full FT or LoRA | ~3 min / $0.20 | ~13 min / $0.70 | ~35 min / $1.70 |
| 1.5B full FT | ~10 min / $0.50 | ~40 min / $2 | ~1.7 h / $5 |
| 1.5B LoRA | ~8 min / $0.40 | ~30 min / $1.50 | ~1.3 h / $4 |

Add ~0.5 h per session for setup, downloads, merging and GGUF conversion.

**Teacher labelling on the same box** **[EST]**: Qwen3-32B FP8 in vLLM measured
2.35-4.28k tok/s total on one H100 ([gpustack](https://docs.gpustack.ai/2.0/performance-lab/qwen3-32b/h100/)).
50k prompts ≈ 20M input + 1.25M output tokens comes to ~1.5-2.5 h, **~$5-9**; Qwen3-14B under
an hour. The workload is mostly input, and prefix caching on the shared system prompt helps.
Turn Qwen3 thinking **off**, or output tokens balloon. A 70B-class teacher needs 2× H100
(~$30-50).

**Data on a rented box:**

- Synthetic and permissively licensed public data can go anywhere.
- **Real recordings and the transcript log** (third-party speech) go only to a Secure-tier or
  datacentre provider, and only with the speakers' consent. Otherwise they stay local.
- Terminate the pod rather than stopping it. On RunPod, `/workspace` survives a stop.
  Whether deletion is a secure wipe was not verified.

**Local Intel Arc B580 (12 GB):**

- **IPEX is finished.** Intel Extension for PyTorch is end-of-life since March 2026; native
  PyTorch `xpu` (Windows, Linux, WSL2 since 2.7) is the path
  ([PyTorch blog](https://pytorch.org/blog/pytorch-2-7-intel-gpus/)).
- **A working recipe exists on Linux.** Ubuntu 24.04, torch 2.9.1+xpu, TRL + PEFT, bf16 +
  gradient checkpointing: a Qwen3-0.6B LoRA on 844 examples in ~13 min on a B580
  ([write-up](https://roger.lol/blog/fine-tuning-llms-on-any-intel-arc-gpu)) **[SINGLE]**.
  No published B580 training throughput exists **[GAP]**.
- **The Windows blocker.** [torch-xpu-ops#5220](https://github.com/intel/torch-xpu-ops/issues/5220)
  is open since 2026-09-05: on a B580 under Windows, `nn.Linear` returns **NaN in bf16 and
  zeros in fp16**. Run a one-layer bf16-vs-CPU check before any run; on Windows that may mean
  fp32 or no B580 at all.
- **Tooling.** Unsloth supports Intel GPUs on Windows 11 and Linux for LoRA and QLoRA
  ([docs](https://unsloth.ai/docs/get-started/install/intel)). bitsandbytes lists Arc B-series
  for 4-bit and 8-bit optimizers.
- **What fits** (AdamW mixed precision ≈ 18 bytes/param, [HF](https://huggingface.co/docs/transformers/main/en/model_memory_anatomy)):

  | job | memory | fits in 12 GB? |
  |---|---|---|
  | full FT 0.5B | ~9 GB before activations (~6 GB with 8-bit Adam) | tight |
  | full FT 1.5B | ~28 GB | no |
  | LoRA 1.5B | ~3 GB + adapters | yes |

  Qwen's 151k vocabulary makes the logits big: ~1.9 GB at batch 8 × 400 tokens in fp32.
- **It shares the GPU with the live interpreter.** The server pins the model with
  `keep_alive: -1`. Evict it first (`ollama stop <model>`, or a `keep_alive: 0` request) and
  keep the server off while training. That is the same eviction `pipeline_eval` already does
  on exit (CLAUDE.md).

**Which for what:**

| stage | where |
|---|---|
| teacher labelling (Qwen3-14B/32B, vLLM) | H100, Secure tier, ~$3-9 |
| SFT sweeps (0.6B and 1.5B, full FT and LoRA, up to 50k) | H100, under $15 total; also sidesteps the Windows bf16 bug |
| DPO/ORPO follow-up | H100, minutes |
| quick LoRA iterations on 0.5-0.6B, and anything on private recordings that must stay on-prem | B580, under Linux/WSL2 with torch 2.9.x, after the bf16 check, with Ollama evicted |
| merge, GGUF, quantize, Ollama serving, all evals | local |

### 6.2 Method

- **Start with SFT; preference tuning only if needed.** For a 0.6B model full fine-tuning is
  cheap enough to be the default; LoRA is the 1.5B and B580 option.
- **Loss on the answer only.** With TRL `SFTTrainer`, set `completion_only_loss` /
  `assistant_only_loss`. `assistant_only_loss` needs `{% generation %}` tags in the chat
  template and is silently ignored with Liger kernels on
  ([TRL#3781](https://github.com/huggingface/trl/issues/3781)). Verify the mask on one
  batch. Unsloth is faster on one GPU; Axolotl and LLaMA-Factory are not needed at this
  scale.
- **Label smoothing or soft targets** on the enum fields, for calibration (§4).
- **Rationale as an auxiliary task**, distilling step-by-step style, never emitted at
  inference.
- **Against sarcasm over-firing**, first try more literal and sincere-looking negatives in
  the SFT mix. If that is not enough, add ORPO (no reference model; beat DPO at 1.3B,
  [EMNLP 2024](https://aclanthology.org/2024.emnlp-main.626.pdf) **[SINGLE]**) or DPO at a
  low β:
  - chosen = literal or neutral, rejected = sarcastic, on the `literal` and `low-confidence`
    shapes;
  - gated on the `mismatch` category so that true sarcasm detection does not collapse, which
    is exactly how the Laya veto failed (`docs/reports/laya.md`).

  The choice of preference objective swings results on classification
  ([2604.00773](https://arxiv.org/abs/2604.00773)) **[PREPRINT]**.

### 6.3 Export and serving

1. Merge the LoRA, run `convert_hf_to_gguf.py`, and quantize to **Q8_0** (0.6-1.6 GB at this
   size). Q4_K_M saves almost nothing, small models are more quantization-sensitive, and it
   shifts the first-token logprobs that `confidence.py` uses
   ([llama.cpp#2094](https://github.com/ggml-org/llama.cpp/discussions/2094)).
2. Write an Ollama `Modelfile` with `FROM`, `TEMPLATE` copied from the same family, and
   `PARAMETER`s, then `ollama create`. A wrong `TEMPLATE` is the most common cause of
   garbled output ([import docs](https://docs.ollama.com/import)). Import the GGUF, not the
   safetensors (`Qwen3ForCausalLM unsupported` has been reported on the safetensors route).
3. **Switch it in without code changes.** `POST /api/model` swaps `config.OLLAMA_MODEL`
   live, and `pipeline_eval` replays cached ASR+SER across LLMs. The one change is a
   per-model system prompt: the shortened training prompt must reach this model and only
   this model. That is a `config.py` knob, not a code path in `main.py`.
4. Check on the imported model that `format` is still enforced, that `logprobs` arrive, and
   that `_tone_prob` finds the value start on a token boundary.

---

## 7. Privacy and law

- **The fine-tuned LLM alone is not an emotion recognition system** under the AI Act.
  Art. 3(39) requires inference "on the basis of their biometric data", and the Commission's
  Feb 2025 guidelines say inferring emotion "from a written text does not fall within the
  scope" ([FPF summary](https://fpf.org/blog/red-lines-under-eu-ai-act-unpacking-the-prohibition-of-emotion-recognition-in-the-workplace-and-education-institutions/)).
  **The deployed pipeline still is one**: it feeds the LLM a voice-derived SER label, and the
  headline emotion is the SER's. A text-only model does not move emtext out of
  Art. 5(1)(f)'s workplace/education ban, or out of Annex III high-risk and Art. 50(3)
  disclosure elsewhere. RESEARCH §6.7 already notes this; whether an assistive aid counts as
  the "medical" exception is untested **[GAP]**.
- **Bystander audio as training data is personal data.** The GDPR household exemption is
  read narrowly ([Ryneš](https://www.hunton.com/privacy-and-cybersecurity-law-blog/cjeu-adopts-strict-approach-use-cctv))
  and almost certainly does not cover building a training set. About nine US states require
  all-party consent to record, including California
  ([RCFP](https://www.rcfp.org/reporters-recording-guide/california/)).
- **Voiceprints are special-category** (GDPR Art. 9, biometric data for unique
  identification) and **never belong in a training set**. Strip `speaker.score` and any
  embedding, and keep only the rendered `listener:` / `speaker:` tag.
- **Norm to adopt:**
  - written opt-in from every recorded speaker, limited to training this model;
  - scripted enactments rather than captured bystanders;
  - the transcript log is test data only, and only for consented speakers;
  - nothing real leaves the machine except to a Secure-tier provider, under the same consent;
  - the trained weights are never published until the provenance of every source is known.
    MELD's GPL question and MUStARD++'s missing licence are reasons enough on their own.

---

## 8. Risks and open questions

- **The teacher is the ceiling.** Distillation reproduces qwen3:14b's sarcasm over-firing
  unless real labels correct it. Beating the teacher needs §5.1(a), not more synthetic rows.
- **Label noise.** κ is low for sarcasm everywhere (RESEARCH §2.5), so plan for ~25% of LLM
  sarcasm labels needing a human (the PodSarc disagreement rate).
- **Calibration.** Small SFT models are overconfident (§4). Until the Platt fit is redone on
  the new model, the confidence levels shown to the listener are not trustworthy.
- **Narrowing.** The model becomes a single-prompt tool: any change to the prompt or schema
  means regenerating data and retraining. Keep the generation pipeline scripted and
  versioned like the eval suite.
- **Multilingual loss.** `parakeet:v3` transcribes 25 languages and qwen3:14b reads them. An
  English-only fine-tune quietly breaks that. Either include a multilingual slice, or record
  English-only as a decision.
- **CPU contention**, if the model moves off the GPU (§3).
- **Open:** whether Granite's hybrid architecture and LFM2.5 import cleanly into Ollama as
  fine-tunes; whether a short-prompt fine-tune keeps the `literal` and `low-confidence`
  guard rails at 100%; whether `tone` survives as anything more than a sarcasm flag.

---

## 9. Staged plan

Each stage has a cost and a result that stops the effort.

| stage | what | cost | stop if |
|---|---|---|---|
| **0. Baseline** | `qwen3:0.6b`, `granite4` (and `qwen3.5:0.8b`) through `eval.model_eval` and `eval.pipeline_eval`, unchanged prompt. Add `expect_emotion` to `tone_cases.jsonl` first (TODO, already owed) so the headline is scored. | an afternoon, local | never: this is the floor every later number is measured against |
| **1. Distil** | Scripted generator + 2-teacher labelling of 5-20k rows; dedup; SFT Qwen3-0.6B (full) and Granite-H-1B (LoRA); Q8_0 → Ollama; refit Platt. | ~$10-30 H100 + generation | the fine-tune scores below Stage 0's best on `literal` / `low-confidence`, **or** below qwen3:14b by more than a few points overall on `tone_cases` |
| **2. Real speech** | Record consented scripted + free sessions; label with ≥3 raters (soft labels, self-labelled intent); a frozen real test set; then real rows into training; optional ORPO against over-firing. | weeks of recording and labelling; H100 cost negligible | the real test set shows no small model within reach of the 14B on the mismatch/sarcasm categories |
| **3. Ship** | Per-model prompt knob in `config.py`; `eval.latency` with the model on CPU and on GPU; README model table; a `docs/reports/` write-up. | local | latency or VRAM gain smaller than the measured quality loss |

**Gate before anything replaces the default:** the listener-safety categories (`literal`,
`low-confidence`; no over-reading) must not regress at all. Overall accuracy comes second. A
small model that calls plain speech sarcastic teaches the listener to distrust ordinary
speech, which is worse than a slow one (TODO, "What 'good' actually looks like").

---

## Sources

The repo-internal sources are `server/interpreter.py`, `server/confidence.py`, `README.md`
(stack, LLM throughput, model table), `docs/status/TODO.md`, `docs/design/RESEARCH.md`,
`docs/reports/laya.md`, `docs/reports/audio-llm.md`, `eval/tone_cases.jsonl` and
`eval/results/pipeline_emotion.csv`. External sources are linked inline. Research method:
web search on 2026-10-03, across model cards, licences, dataset repos and papers, and
toolchain issue trackers. Licences and prices change; re-check them before relying on them.
Items flagged uncertain during the search:

- the MSP-Podcast commercial terms;
- the licences of MUStARD++, SARC, PodSarc and MC-EIU;
- the CPED licence, which conflicts between sources;
- the MELD weights question;
- whether the Windows bf16 bug also hits Linux;
- the H100 throughput, extrapolated from A100 numbers;
- the Ollama import of hybrid architectures.
