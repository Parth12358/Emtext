# Audio-input LLMs as a replacement for Whisper + SER + interpreter

**Date:** 2026-09-22 -- **Status:** measured, not adopted -- **Touches:** nothing
in `server/`

> The test bed this report describes (`eval/audio_llm_eval.py`,
> `experiments/audio_llm/`, the per-item CSV and the ~15 GB of model files under
> `models/`) was removed once the decision was made. The method below is
> complete enough to rebuild it.

## Verdict

None of the three local audio LLMs earns the pipeline. They do not hear tone:
on 280 RAVDESS clips whose words are neutral, every model answers `neutral` on
96-100% of clips and voice sensitivity is 0 to -4 points, against +48 for the
current emotion2vec + qwen3:14b pair. They follow a wrong transcript over their
ears: told "the speaker sounded happy" on a sad clip, Gemma and Voxtral adopt
the hint on 91% and 99% of clips and side with the audio on 0%; Qwen2.5-Omni
resists the hint only by staying `neutral` (72%). And they are not faster: 0.9-1.4 s
per clip end to end, the same as the current 0.3 s CPU audio stage plus 0.9 s
GPU read, while two of the three transcribe worse than the CPU ASR (WER 9-11% vs
Whisper base's 2% on the same clips; parakeet:v2 beats Whisper on every set it has
been scored on) and all three need the whole GPU that qwen3:14b already occupies.

## What was tried

- **Runtime:** llama.cpp b11104 win-vulkan-x64 `llama-server`, `--device
  Vulkan0` (the Arc B580; `-ngl 99` alone splits layers with the Ryzen iGPU),
  `-ngl 99 --jinja -c 4096`. Requests go to `/v1/chat/completions` with an
  `input_audio` part, `response_format` json_schema, temperature 0.2, max_tokens
  120, `chat_template_kwargs {"enable_thinking": false}` (Gemma otherwise spends
  the whole budget on a "Thinking Process:" preamble). The Gemma-on-Vulkan
  garbling bug (llama.cpp #24560) did not reproduce with this build and device.
- **Models** (`ggml-org` GGUFs): `gemma4-e4b` = Gemma 4 E4B-it Q4_0 4.59 GB +
  mmproj Q8_0 0.56 GB; `voxtral-mini` = Voxtral-Mini-3B-2507 Q4_K_M 2.47 GB +
  mmproj Q8_0 0.72 GB (no Q8_0 model file published); `qwen25-omni-7b` =
  Qwen2.5-Omni-7B Q4_K_M 4.68 GB + mmproj Q8_0 1.55 GB. Three architectures
  (Google, Mistral, Alibaba), all with published audio mmprojs and all fitting a
  12 GB card beside their KV cache.
- **Why not** Ollama (no `input_audio` on its OpenAI route, no control of mmproj
  or quant; #15333, #15427), Qwen3-Omni (every variant is 30B, 59-69 GB), or
  Gemma 4 12B (would leave no VRAM headroom and E4B already carries the same
  audio encoder).
- Wall time for the full matrix: 17, 17 and 22 minutes; qwen3:14b was evicted from
  Ollama during each run and restored afterwards.

## Test bed

Six conditions, all through `eval/audio_llm_eval.py`, prompt = the production
`SYSTEM_PROMPT` verbatim behind a one-sentence preamble saying the newest line
arrives as audio; the model returns `{transcript, tone, read}`.

| condition | input | n | asks |
|---|---|---|---|
| `audio` | clip only | 280 | can it read tone from delivery? |
| `audio+text` | clip + Whisper transcript (e2v cache) | 280 | does a transcript help or replace listening? |
| `audio+wrong` | clip + transcript + "The speaker sounded <opposite>." | 280 | does it follow the words or the ears? |
| `text` | transcript only | 280 | the floor: words alone |
| `cases` | `eval/tone_cases.jsonl` (text + voice dict) | 34 | same suite as `model.csv` |
| `asr` | the 8 SAPI lines in `experiments/asr` | 8 | WER vs `asr.csv` |

The 280 clips are the balanced, seed-0, calm-excluded RAVDESS sample that
`eval/results/pipeline.csv` used, silence-trimmed at RMS 150 to ~1.1-1.3 s.
`tone_expected` maps 4 of 7 acted emotions to `negative` (160 clips), 2 to
`positive` (80), 1 to `neutral` (40), so **~14% tone accuracy means "answered
`neutral` on everything"**. Voice sensitivity is P(positive | happy/surprised) -
P(positive | angry/sad); both RAVDESS sentences are neutral, so any movement is
voice signal. Baselines come from the existing CSVs, no re-runs.

To rerun: evict qwen3:14b from Ollama (`/api/generate` with `keep_alive: 0`;
the two cannot share the 12 GB card), start llama.cpp `llama-server` with the
model GGUF and its `mmproj`, `-ngl 99 --jinja -c 4096 --device Vulkan0` (the box
has a second Vulkan device, the AMD iGPU), send each item to
`/v1/chat/completions` with a `text` part and an `input_audio` part (base64
16 kHz WAV), `response_format` json_schema, temperature 0.2, `max_tokens` 120 and
`chat_template_kwargs {"enable_thinking": false}` (Gemma 4 otherwise spends the
budget on a thinking preamble), and score as described above. Restore qwen3:14b
afterwards with a warm `/api/generate` call using `keep_alive: -1`.

## Results

### 1. Transcription

| model | RAVDESS WER (audio) | SAPI WER (asr) | asr latency |
|---|---|---|---|
| gemma4-e4b | 11% (4% of rows not JSON) | 4% | 1.27 s |
| voxtral-mini | 9% | 5% | 1.00 s |
| qwen25-omni-7b | 3% | 6% | 1.35 s |
| whisper:base | 2% (pipeline.csv, the ASR of record for that run) | 6% | 0.26 s |
| parakeet:v2 (current default) | not run on RAVDESS | 5% | 0.10 s |

Only Qwen transcribes RAVDESS as well as Whisper base did, at 5x the latency, and
13x parakeet's.
Gemma's 12 failed rows are the model answering with prose instead of JSON despite
the schema.

### 2. Tone from audio

| model | audio tone / v.sens | audio+text | text | `neutral` share (audio) |
|---|---|---|---|---|
| gemma4-e4b | 14% / +0 | 14% / +0 | 14% / +0 | 100% (267/268) |
| voxtral-mini | 14% / -4 | 15% / -5 | 16% / +0 | 96% |
| qwen25-omni-7b | 16% / -1 | 18% / +1 | 14% / +0 | 98% |
| qwen3:14b + emotion2vec | **81% / +48** | -- | -- | -- |

Per acted emotion the picture is flat: on `audio`, Gemma answers `neutral` on
40/40 angry, 40/40 disgusted, 38/38 happy; Voxtral's 7 `positive` answers land
on angry (1), fearful (2), happy (1) and sad (3); Qwen's 5 `positive` include 3
sad clips. The audio condition is indistinguishable from the text-only floor for
all three, which is the definition of not listening.

### 3. Does it listen? (`audio+wrong`)

| model | follows hint | follows audio | neither | tone acc | v.sens |
|---|---|---|---|---|---|
| gemma4-e4b | 91% | 0% | 9% | 0% | -94 |
| voxtral-mini | 99% | 0% | 1% | 0% | -100 |
| qwen25-omni-7b | 25% | 4% | 72% | 4% | -4 |

Tone by hint (n = 160 "happy", 80 "angry", 40 "sad"):

| model | hint happy -> | hint angry -> | hint sad -> |
|---|---|---|---|
| gemma4-e4b | positive 147, neutral 13 | negative 80 | negative 29, neutral 11 |
| voxtral-mini | positive 160 | negative 80 | negative 36, sarcastic 3, neutral 1 |
| qwen25-omni-7b | neutral 154, positive 3, negative 3 | negative 66, neutral 14 | neutral 40 |

A five-word sentence of text flips Gemma and Voxtral from 100% `neutral` to
whatever the sentence says; the audio never wins once. Qwen's 72% "neither" is
not resistance but inertia: it stays `neutral` under both a wrong hint and the
real delivery, and the 25% it does follow are all the "angry" hints.

### 4. `tone_cases.jsonl` (text + voice dict, no audio)

| model | acc | pairs | literal | low-conf | sarcasm | PA | mismatch |
|---|---|---|---|---|---|---|---|
| gemma4-e4b | 88% | 3/6 | 100% | 100% | 75% | 100% | 75% |
| voxtral-mini | 76% | 3/6 | 80% | 50% | 50% | 75% | 75% |
| qwen25-omni-7b | 74% | 3/6 | 100% | 100% | 50% | 0% | 75% |
| qwen3:14b (3-run) | 88% | 2/6 | -- | -- | 100% | -- | -- |

As a text interpreter Gemma 4 E4B matches qwen3:14b on this suite at a third of
the weights, which is the one interesting number in the run and is a text-LLM
finding, not an audio one. Voxtral loses the low-confidence guard rail and Qwen
reads no passive-aggression.

### 5. Cost

| model | audio latency mean (p95) | weights + mmproj | prompt tokens, 3.5 s clip / ~1.2 s clip | fits beside qwen3:14b (9.6 GB)? |
|---|---|---|---|---|
| gemma4-e4b | 0.90 s (1.97) | 5.2 GB | ~111 / +46 over text | no |
| voxtral-mini | 0.92 s (0.98) | 3.2 GB | ~1428 / +183 | no |
| qwen25-omni-7b | 1.27 s (1.32) | 6.2 GB | ~786 / +750 | no |
| current split | 0.30 s CPU + 0.89 s GPU | 9.6 GB LLM (GPU) + CPU models | -- |

Latencies are single-request, warm, `-c 4096`. Gemma's audio encoder is the
cheapest by far (~25 tokens/s of audio); Voxtral's and Qwen's audio tokens dwarf
the text prompt. None of the three can share the B580 with qwen3:14b, so
adopting one means replacing the interpreter, not adding to it.

## Interpretation

This is the local reproduction of RESEARCH.md §4.3: VoxParadox (arXiv
2605.27772) measured GPT-4o Audio at 8.6% ground truth / 81.6% transcript
agreement and a 12-model mean of 15.3% / 64.3%; LISTEN (arXiv 2510.10444)
found the same collapse to text. The quantised local models are worse than the
published ones: two of three follow the wrong transcript at 91-99% with 0%
audio wins, and the third simply never leaves `neutral`. The acoustic
information may exist in the encoder, but at the encoder-LLM interface it is
laundered into words, and words on RAVDESS are null. emotion2vec on the same
clips separates the voice at +48 points precisely because it cannot read the
transcript.

## What adoption would have cost

- **Progressive disclosure:** today the transcript ships when Whisper finishes
  (~0.3 s) and the read follows; one model means nothing until the JSON closes.
- **Graceful degradation:** an Ollama outage still yields transcripts; a single
  audio LLM outage yields nothing.
- **Two channels become one:** the mismatch rule compares words against an
  acoustic reading that cannot see the words. Sourced from an audio LLM the
  voice channel is the transcript again, and the rule reports agreement with
  itself.

## What could change the verdict

- A checkpoint tuned for paralinguistics (e.g. the community Gemma-4 E4B
  emotion DoRA): the harness accepts any GGUF + mmproj, and `audio` v.sens over
  +20 with `follows_audio` over `follows_hint` is the bar.
- A two-pass design that hides the transcript: audio-only tone in pass one,
  text read in pass two. It would recover channel independence but at two GPU
  calls, and pass one still scored 0 to -4 here.
- Real-room audio and sarcastic speech. RAVDESS is acted and neutral in words;
  a mismatch pair recorded live (see "Sarcasm is never tested on real speech"
  in `docs/status/TODO.md`) is the prerequisite for every approach.

## Sources

- VoxParadox, arXiv 2605.27772 -- audio LLMs vs misleading transcripts.
- LISTEN benchmark, arXiv 2510.10444 -- paralinguistic listening, corroborating.
- llama.cpp `docs/multimodal.md` -- `input_audio` on `llama-server`.
- llama.cpp #24560 -- Gemma 4 garbling on Vulkan (not reproduced here).
- ollama #15333, #15427 -- audio input status.
- `ggml-org/gemma-4-E4B-it-GGUF`, `ggml-org/Voxtral-Mini-3B-2507-GGUF`,
  `ggml-org/Qwen2.5-Omni-7B-GGUF` on Hugging Face.
- Data: 3,486 scored rows (one per model x condition x item), removed with the
  harness; every number above is taken from them.
