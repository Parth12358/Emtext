# Server compute requirements: RAM and GPU, active and idle

**Date:** 2026-10-03 -- **Status:** measured; **1.1 (no mmap), 1.3 (on-demand GPU
poller) and 1.4 (context guard) adopted 2026-10-03**, the rest open -- **Touches:**
the measurements touched nothing in `server/` (scratch probes outside the repo, the
live server process, and Ollama's own logs)

## Verdict

The emtext stack on this machine holds about **10.5 GB of system RAM and 9.3 GB of
VRAM**. It holds roughly the same amount whether someone is talking or the server has
sat idle since yesterday's conversation. Three findings dominate, and two of them need
no change to the server's logic.

1. **The LLM is in RAM twice.** With Ollama's default memory-mapped load, the
   `llama-server` runner keeps **8.6 GB of system RAM** in its working set after the
   weights have gone to the GPU. One request option, `use_mmap: false`, brought that
   to **0.6 GB** and cut the load time **from 10-12 s to 2.7 s** in the same test.
   This is the largest single saving available, about 8 GB.
2. **Idle and active cost the same.** `interpreter.py` sends `keep_alive: -1`, so after
   the first read the model stays pinned until someone presses Evict or restarts Ollama:
   9.3 GB of VRAM (and, with mmap, 8.6 GB of RAM) held with no client connected.
   Tying residency to connections would make an idle server cost ~0 GPU. With
   finding 1 applied, a warm-up costs 2.7 s, which hides behind the listener's first
   sentence.
3. **The Python process is the right size, but it ratchets.** It starts at 1.56 GB
   working set / 2.25 GB private (byte-for-byte what the live server shows) and grows
   to **1.96 GB / 2.80 GB after one 15 s utterance**, and that growth never comes back.
   The ASR backend (Parakeet, ~0.7 GB) and the SER stack (torch + funasr +
   emotion2vec, ~1.1 GB) are the two big blocks. Both can be shrunk, but each shrink
   changes a measured model and has to clear the evals first.

Smaller items: the dashboard's GPU poller spawns PowerShell every ~8 s whether or not
anyone is looking, which comes to **~2.4 CPU-hours a day**. The desktop composites on
the Arc and costs ~0.7 GB of the LLM's VRAM. The 4096-token context is about 2.5x
larger than any prompt the server has sent. A ranked list is in
[Recommendations](#recommendations).

## The machine, and how this was measured

- **Host:** Ryzen 5 9600X (6 cores / 12 threads, AVX-512 incl. VNNI and BF16),
  31.1 GB RAM, 22 GB pagefile (commit limit 53.1 GB), Windows 11.
- **GPU:** Intel Arc B580, 11.8 GiB as Ollama sees it. 11.1 GiB was free at Ollama
  start, because the desktop (dwm, browser, terminal, wallpaper) runs on the same
  card. A Radeon iGPU is also present, and Ollama drops it ("dropping integrated GPU").
- **Ollama 0.34.4**, Vulkan backend, runner is `llama-server` launched with
  `-c 4096 -np 1 --flash-attn auto -b 512 -ub 512 --context-shift --keep 4`.
  Server env: `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=0` (auto),
  `OLLAMA_FLASH_ATTENTION=false` (flash attention is still enabled per-runner by
  `--flash-attn auto`), `OLLAMA_KV_CACHE_TYPE` unset (f16), `OLLAMA_CONTEXT_LENGTH=0`
  ("vram-based default context", which resolved to 4096 on this card).
- **Server config:** defaults: `ASR_MODEL=parakeet:v3`, emotion2vec_plus_base from the
  slimmed local checkpoint, CAM++ speaker id, `qwen3:14b` (Q4_K_M).

Method:

- **Python side.** A scratch probe imported the server modules one stage at a time in a
  fresh process and printed working set, private bytes and peak working set after each
  stage. It then ran ASR, SER and speaker id three times each on 1 s, 4 s and 15 s
  stretches of concatenated RAVDESS audio. The probe set `TRANSCRIPT_LOG_ENABLED=0` and
  `HF_HUB_OFFLINE=1`, so it neither wrote logs nor downloaded anything. Its startup
  numbers match the running server (PID 13868: 1,561 MB WS / 2,250 MB private) to
  within 5 MB, so the staged table describes the real process.
- **Ollama side.** VRAM breakdown and prompt sizes came from the runner's own log lines
  (`load_tensors`, `llama_kv_cache`, `sched_reserve`, `print_timing`). Host RAM came
  from loading `qwen3:14b` three times with a short `keep_alive` (90-120 s, then an
  explicit `keep_alive: 0`) and reading `\Memory\Available MBytes`,
  `\Memory\Committed Bytes` and the runner's process counters around each load. Ollama
  was left empty afterwards, as it was found.
- **Units:** Windows process and GPU counters are in binary megabytes (MiB), written
  here as MB. `ollama ps` uses decimal GB, which is why it says 9.6 GB for what the
  GPU counter calls 9,268 MB.

## Where the memory goes today

### The whole stack, by state

| state | Python server (WS / private) | LLM runner, host RAM (WS / private) | VRAM (emtext) |
|---|---|---|---|
| idle, fresh start, LLM never used | 1,564 / 2,254 MB | not running | 0 |
| idle, after any conversation (LLM pinned) | 1,600-1,960 / 2,350-2,800 MB | **8,585 / 9,469 MB** | **9,268 MB** |
| active conversation | same, +~0.1 GB transient | same | same |
| *active, after Tier 0-1 below* | same | ~580 / 9,900 MB (commit only) | ~9.1 GB |
| *idle, after Tier 0-1 below* | 1.6-2.0 GB / 2.3-2.8 GB | not running | 0 |

Idle and active are the same row today. Everything expensive is loaded once and never
released. On the Python side that is deliberate (models load at import). On the LLM side
it is `keep_alive: -1`.

### Python process, staged (default config)

| stage | WS | private | delta WS | threads |
|---|---|---|---|---|
| bare python | 26 | 11 | | 8 |
| + numpy / psutil | 38 | 374 | +12 | 19 |
| + torch (2.14 CPU) | 201 | 531 | +163 | 19 |
| + transcriber (`parakeet:v3`, int8 ONNX) | 910 | 1,232 | **+709** | 28 |
| + ser (emotion2vec, slim checkpoint, via funasr) | 1,856 | 2,659 | +946 | 45 |
| + speaker (CAM++ via funasr) | 1,559 | 2,249 | -297 | 47 |
| + fastapi / main | 1,564 | 2,254 | +5 | 47 |
| after 3x 1 s utterances | 1,606 | 2,345 | +42 | 47 |
| after 3x 4 s utterances | 1,659 | 2,437 | +53 | 47 |
| **after 3x 15 s utterances** | **1,962** | **2,801** | **+303** | 47 |
| after `gc.collect()` | 1,957 | 2,796 | -5 | 47 |

How to read it:

- **SER + speaker together cost ~+650 MB WS / ~+1,020 MB private.** The SER stage peaks
  at +946 MB WS. Windows then trims part of that working set while CAM++ loads, which
  is why the speaker row is negative. Private bytes are the honest number for that
  pair. Measured on its own (SER only, no ASR), the SER stack is **1,126 MB WS /
  1,601 MB private** above bare numpy. That agrees with the "+1,068 MB" in `config.py`.
- **The 15 s row is a high-water mark, and it stays.** Per-utterance cost at 15 s is
  ASR 0.36 s, SER 0.86 s, speaker 0.09 s. Those buffers, ONNX Runtime's arena and
  torch's CPU allocator cache, are kept after the call and `gc` does not touch them.
  This is the same behaviour `docs/status/TODO.md` recorded for the SER checkpoint:
  peak allocation becomes steady-state RSS. Note what this means in practice. The
  process size is set by the longest utterance it has ever seen, and
  `MAX_UTTERANCE_MS` (15 s) is the ceiling. One long monologue moves the server
  permanently from ~1.6 GB to ~2.0 GB.
- **numpy's +360 MB private** is OpenBLAS committing per-thread buffers for 12 threads.
  It is commit charge, not resident memory (WS moved by 12 MB), so it matters only
  against the commit limit.
- **47 threads**: torch's intra-op pool (6), ONNX Runtime (`ASR_THREADS=4` plus its
  own pools), OpenBLAS (12) and the asyncio executor. These cost CPU contention, not
  memory. See [CPU contention](#cpu-contention-during-active-runs).

### The LLM, in VRAM

From the runner's log, identical on every `qwen3:14b` load since 2026-09-21:

| buffer | where | size |
|---|---|---|
| model weights (41/41 layers offloaded) | Vulkan0 | 8,423 MB |
| KV cache, `n_ctx = 4096`, f16 | Vulkan0 | 640 MB |
| compute graph | Vulkan0 | 126 MB |
| **total on the GPU** | | **9,189 MB** (counter: 9,268 MB) |
| token-embedding table (`CPU_Mapped`) | host | 417 MB |
| host compute buffer | host | 24 MB |
| prompt cache (`--cache-ram`, limit 8 GiB, bounded by 4096 tokens) | host | 160-224 MB, one prompt |

With 11.1 GiB free at Ollama start, that leaves ~1.9 GiB of headroom. VRAM is not tight
today. It matters for three things: fitting a better model or quant, running on a card
smaller than 12 GB, and sharing the card with the desktop. The KV cache costs **160 KB
per token** (40 layers x 8 KV heads x 128 dims x 2 x f16), so it scales linearly with
`num_ctx`.

### The LLM, in system RAM: the doubled copy

Measured around one load each (both from a warm file cache, so neither load hit the disk):

| load mode | load time | system `Available` drop | runner WS | runner private | commit rise |
|---|---|---|---|---|---|
| default (`load_mode = mmap`) | 10.2 s, 12.3 s | **-8,698 MB** | **8,585 MB** | 9,469 MB | +9.6 GB |
| `options.use_mmap: false` (`load_mode = none`) | **2.7 s** | **-804 MB** | **579 MB** | 9,900 MB | +9.9 GB |

The weights end up 100% on the GPU either way (`ollama ps`: `100% GPU`). In mmap mode
the runner also keeps the entire 9.3 GB GGUF file mapped and resident in its working
set, even though every tensor except the 417 MB embedding table now lives in VRAM. The
most likely cause: llama.cpp releases the mapped ranges of offloaded tensors after
upload (`unmap_fragment`), and that release is only implemented on POSIX, so on Windows
the whole mapping stays resident. Those pages are clean and file-backed, so Windows
*can* reclaim them under pressure. Until it does, they count as in use, push the file
cache out, and show up in Task Manager as 8.6 GB taken by Ollama. With `use_mmap: false`
the file is read into the GPU and the host copy is gone, and the load was also 4x
faster.

The ~9.5-9.9 GB of commit appears in **both** modes and does not track physical RAM.
It is most likely the Vulkan driver charging commit for evictable device-local
allocations, which WDDM requires. On this box it is harmless (commit went 30 -> 40 GB
against a 53 GB limit). It would matter on a machine with a small pagefile: a 16 GB host
with a system-managed pagefile could refuse the load outright.

## Findings

### F1. `use_mmap: false` removes ~8 GB of RAM and 75% of the load time

See the table above. The only thing not yet re-measured is decode speed with the
weights loaded this way. All 41 layers are on the GPU in both modes, so no change is
expected, but `python -m eval.latency` and `python -m eval.bench` should confirm it
before adoption.

One trap, which applies equally to F3: `use_mmap` and `num_ctx` are **load-time**
options. Ollama reloads the runner whenever a request arrives with load options that
differ from the loaded model's. Today four call sites build their own Ollama payloads:
`interpreter.py`, the dashboard's warm call in `dashboard.select_model`,
`eval/latency.py`'s warm-up and `eval/bench.py`. If only one of them sends
`use_mmap: false`, every alternation between them triggers a full reload, and that
reads as "the model got slow". The options must come from one place, and every caller
has to use it.

### F2. `keep_alive: -1` pins 9.3 GB of VRAM forever

`interpreter.py` sends `keep_alive: -1` on every request so that no read ever pays a
load. That decision was right when a load cost 10-12 s. The price is that from the first
read until a manual Evict or an Ollama restart, the GPU holds 9.3 GB and (in mmap mode)
the host holds 8.6 GB, even with zero connections. The pinning is already acknowledged
in the code: the dashboard has an Evict button for exactly this, `pipeline_eval` evicts
on exit, and `MODEL_SWITCH_COOLDOWN_S` exists because swaps are expensive. Nothing
releases the model automatically.

The server already has a clean signal for "nobody needs the LLM". `metrics` counts live
connections, and `WS_IDLE_CLOSE_S` (900 s) already closes abandoned sockets. So
residency can follow connections: warm on the first authenticated `/stream`, unload a
grace period after the last one closes. With F1 in place, a warm from page cache costs
~2.7 s. A pendant takes at least that long to deliver its first utterance (speech, plus
the 650 ms end-silence, plus 0.3 s of CPU stage), so the listener would rarely see the
warm-up at all.

### F3. The context window is ~2.5x larger than any prompt sent

Prompt sizes from every `print_timing` line in Ollama's logs: **n = 1,240, mean 1,059,
max 1,559 tokens**. The RAVDESS pipeline evals sit at 891-1,012 (no rolling context).
Output is 13-25 tokens (`eval_count`, p95 25), and the 80-token `OLLAMA_NUM_PREDICT`
cap is never reached. Prefix caching works: 953 of ~1,008 tokens are reused per request
(`cached n_tokens = 953`). So `num_ctx` only needs to cover the worst prompt.

| `num_ctx` | KV f16 | KV q8_0 | headroom over 1,559 + 80 |
|---|---|---|---|
| 4096 (today) | 640 MB | ~340 MB | 2,457 |
| 3072 | 480 MB | ~255 MB | 1,433 |
| 2048 | 320 MB | ~170 MB | 409 |

**Do not go to 2048.** The runner is launched with `--context-shift --keep 4`. If a
prompt overflows, it discards tokens from position 4 onward, which means the start of
`SYSTEM_PROMPT`, and it does so silently. A worst case is twelve 15 s context lines of
~60 tokens each, plus the ~850-token system prompt, plus the voice and speaker
preamble. That comes to ~1,700 tokens, so 2048 is too close. **3072 saves 160 MB with
safe margin**, and q8_0 KV on top saves ~385 MB in total. These are small wins
next to F1 and F2. Their value is headroom: room for a larger quant, or a 12 GB card
that also drives a display.

### F4. The ASR backend is the largest Python-side lever

Each backend loaded alone in a fresh process (baseline 38 MB WS):

| backend | load, +WS | growth 1 s -> 15 s, WS | 15 s utterance | on disk |
|---|---|---|---|---|
| `parakeet:v3` (default) | +721 | +185 | 0.37 s | 640 MB |
| `parakeet:v2` | +699 | +184 | 0.34 s | 640 MB |
| `moonshine:base` | +300 | +162 | 0.23 s | 270 MB |
| `sensevoice:small` | +307 | **+29** | 0.15 s | 230 MB |
| `moonshine:tiny` | +165 | +102 | 0.12 s | 120 MB |
| `whisper:base` | +156 (peak +309) | +53 | 0.63 s | 75 MB |
| `whisper:distil-small.en` | +285 (peak +546) | ~0 | 3.0 s | 170 MB |
| `whisper:small.en` | +383 (peak +708) | +39 | 4.5 s | 250 MB |

The "growth" column comes from the 1 s and 15 s rows of the same run, so the probe's
own allocations cancel out. Whisper's 15 s timings are long because the test audio was
concatenated RAVDESS clips with their silences left in. Use them for memory, not
latency.

Switching Parakeet for Moonshine base would save ~420 MB resident. Switching to
SenseVoice would save ~410 MB and also stop the 15 s ratchet almost entirely. **But the
evidence cannot choose between them on accuracy.** `eval/results/asr.csv` scores every
backend on the same 8 SAPI lines, and they land within one error of each other (4-5
errors in ~80 words). Parakeet won on public benchmark WER and on not inventing text
over silence. The prerequisite for trading ASR accuracy for RAM is the step
`docs/status/TODO.md` already asks for: score real-room speech
(`python -m server.translog --export DIR`, corrected by ear, then
`python -m eval.asr_eval --dir DIR`). Until that exists, changing the default for RAM
would be a guess.

### F5. The SER stack is ~1.1 GB, and a third of it is the framework

The SER stack costs 1,126 MB WS standalone: torch (163 MB), funasr's import of all 431
of its modules (184 MB, already documented in TODO.md), the 358 MB fp32 model, plus
allocator and activation overhead. Two ways to shrink it were looked at:

- **Dynamic int8 quantisation** (`torch.ao.quantization.quantize_dynamic` on every
  `nn.Linear`), measured on 144 RAVDESS clips covering all 24 actors:

  | | fp32 (today) | dynamic int8 |
  |---|---|---|
  | model state | 358 MB | ~113 MB (82 MB packed int8 Linear + 31 MB rest) |
  | mean SER time / clip | 0.161 s | **0.081 s** |
  | label agreement with fp32 | | 140 / 144 (97.2%) |
  | correct vs RAVDESS label | 136 / 144 | 134 / 144 |
  | max confidence change | | **0.415** |

  Half the time and a third of the weight memory, with labels nearly unchanged. But
  confidence moved by up to 0.42 on some clip. SER confidence feeds three places: the
  prompt's `conf` and "low confidence" wording, the `SER_MIN_CONFIDENCE` gate, and
  `confidence.base_prob`. So this is a model change, not a free optimisation. It needs
  the full `eval.ser_eval`, `eval.pipeline_eval` and `eval.confidence_eval` before
  adoption. Second caveat: quantising after load does not guarantee the fp32 pages
  leave the working set, given the allocator behaviour TODO.md recorded. The safe
  version follows the slim-checkpoint pattern: save a pre-quantised artifact once and
  load it directly.

  **Full evals, 2026-11-26: not adopted.** Built as the opt-in `SER_QUANT=int8`
  (quantised after load; default off) and run through all three evals with
  `emtext-qwen3-0.6b:v3` as the interpreter:

  | | fp32 (default) | `SER_QUANT=int8` |
  |---|---|---|
  | `ser_eval`, 1,248 clips (calm excluded): macro recall | 93.9% | 92.3% |
  | `ser_eval`: time per clip | 0.20 s | 0.07 s |
  | `ser_eval`: same label as fp32 | | 97.1% |
  | `pipeline_eval`, 280 clips: shown emotion | 88.9% | **84.3%** |
  | `pipeline_eval`: tone / voice sensitivity | 90% / +79% | 89% / +80% |
  | `pipeline_eval`: CPU stage / end to end, median | 0.173 / 0.338 s | 0.138 / 0.327 s |
  | `confidence_eval`: composite AUROC | 0.78 | **0.71** |
  | `confidence_eval`: reads marked `high` (their accuracy) | 18 (100%) | 79 (95%) |

  Three reasons it stays off:
  - **It buys 11 ms.** SER runs in parallel with Parakeet (0.11 s), so a 3x faster SER
    only lowers the CPU stage to Parakeet's time.
  - **It loses more on the pipeline's own cuts than on whole clips.** On the
    segmenter's trimmed utterances it got 15 clips wrong that fp32 had right, and
    fixed 2. Most were disgusted read as angry (6) and sad read as neutral (3).
  - **It degrades the confidence signal.** `ser_confidence` is the composite's best
    input (AUROC 0.78 fp32, 0.71 int8). More reads reach `high` while being right less
    often, which is the one direction the listener-facing marker must not move.
  Resident memory was not measured: quantising after load may leave the fp32 pages in
  the working set. A pre-quantised checkpoint would settle the memory question but not
  the accuracy or confidence loss. Do not re-open this without a different
  quantisation scheme (static or QAT) or an ONNX export scored the same way.
  Per-clip rows: `eval/results/ser_fp32.csv`, `ser_int8.csv`,
  `pipeline_student_v3.csv`, `pipeline_student_v3_int8.csv`.
- **Dropping torch and funasr from the process.** CAM++ is published as ONNX for
  sherpa-onnx's speaker-embedding extractor (the 3D-Speaker
  `campplus_sv_zh-cn_16k-common` export), and `sherpa-onnx` is already a dependency.
  emotion2vec would need an ONNX export, then int8. Once neither model goes through
  funasr, the process sheds torch, funasr, transformers, modelscope, librosa, numba and
  scipy. That is an **estimated** 0.8-0.9 GB of resident memory (1,126 MB SER stack
  minus a ~150-250 MB ONNX Runtime session), and ~6 s of the 8.2 s cold start (SER
  loads in 3.8 s, speaker in 2.2 s). Nothing here was built or measured. Two parity
  checks would gate it: the speaker profile stores the model name, and embeddings must
  match or the user re-enrols; and SER output has to be re-scored on RAVDESS.

### F6. Idle CPU: the GPU poller never stops

`metrics.start_gpu_poller()` runs from server startup, whether or not a dashboard is
open. Each poll launches a fresh `powershell.exe` and queries two counter sets. Measured
three times: **2.8 s wall, 0.77 CPU-s, 130 MB peak working set** per poll. With the
5 s sleep, that is one poll every ~7.8 s: ~11,000 PowerShell launches a day, **~2.4
CPU-hours a day**, and a 130 MB process alive about a third of the time. The server
itself, by contrast, used 0.06 CPU-s over 10 s of idle (the 1 Hz psutil sampler plus
the keepalive tasks). The poller is the server's whole idle CPU bill, and nobody reads
its output unless `/api/stats` is being polled.

### F7. The desktop shares the LLM's GPU

At the time of measurement the Arc held ~0.9 GB for desktop processes: dwm 375 MB,
terminal 126 MB, wallpaper 117 MB, explorer, browser, editor. Ollama saw 11.1 of
11.8 GiB free at start. The CPU has a Radeon iGPU that Ollama already ignores. Driving
the monitor from the motherboard output gives the LLM the whole card and keeps
compositing out of its memory bandwidth. This is a cable move, not a code change.

A side note: the adapter-level `\GPU Adapter Memory\Dedicated Usage` counter read
5,435 MB with no model loaded, while the per-process counters summed to ~0.9 GB. The
dashboard sums the per-process counters, which agree with Ollama, so it reports the
right figure. The adapter counter is not trustworthy on this driver and should not be
used for VRAM accounting.

### F8. What is already fine

Everything per connection is bounded and small. The segmenter holds at most one
`MAX_UTTERANCE_MS` utterance (15 s: ~0.5 MB int16 plus ~1 MB float32 at finalise).
`clips.Recent` is at most 8 x 15 s x 32 KB/s ≈ 3.8 MB. At most 3 utterances are in
flight (`MAX_INFLIGHT_UTTERANCES`). `metrics` is fixed-size deques. Even the worst case
of `MAX_CONNECTIONS=32` is well under 200 MB. The exposure limits in `config.py` are
what keep it that way. `translog` and `clips` cost disk, not RAM. Nothing here needs
changing.

## Recommendations

Ranked by saving per unit of effort and risk. "Tier 0" needs no server code. "Tier 1"
is a small change behind a config knob. "Tier 2" is structural. Every item names what
it touches in `CLAUDE.md`'s invariants and what must be re-run.

### Tier 0: configuration and hardware

| # | change | saves | risk / cost | verify with |
|---|---|---|---|---|
| 0.1 | Monitor on the iGPU output | ~0.7-0.9 GB VRAM | none | Ollama's "available" at start, `ollama ps` |
| 0.2 | `OLLAMA_MAX_LOADED_MODELS=1` in Ollama's environment | prevents two models co-resident / partial CPU offload during switches and evals | an eval and the server using different models evict each other (already true at 12 GB) | dashboard "fully_on_gpu" |
| 0.3 | `OLLAMA_CONTEXT_LENGTH=3072` in Ollama's environment | 160 MB VRAM | silent truncation if a prompt ever exceeds it; see F3 and 1.4 | `eval.model_eval`, `eval.pipeline_eval` unchanged |
| 0.4 | `OLLAMA_KV_CACHE_TYPE=q8_0` (+ `OLLAMA_FLASH_ATTENTION=1`) | ~0.2-0.3 GB VRAM | tiny quality change in the KV cache; needs the eval | `eval.model_eval` (all categories, incl. `mismatch`), `eval.pipeline_eval` |

0.3 and 0.4 are set server-wide, so they apply to every caller at once and cannot
cause the reload ping-pong F1 warns about. They are still model-behaviour changes:
`CLAUDE.md` says the prompt is the product, and the eval is how that is checked.

### Tier 1: small code changes

**1.1 Load the LLM without mmap** (F1). Add `"use_mmap": False` to the options of every
Ollama request that can load a model. Build those options once, for example as a
`config`-driven dict that `interpreter.py` exports and `dashboard.py` and the eval
warm-ups import, so the load options can never diverge. **Saves ~8 GB of host RAM
whenever the model is loaded; load drops from 10-12 s to 2.7 s.** It touches nothing in
the wire protocol or the prompt. Re-run `eval.latency` and `eval.bench` for decode
speed, and watch the runner's WS in Task Manager.

**1.2 Residency follows connections** (F2). Replace "pinned forever" with:

- on the 0 -> 1 connection transition: fire-and-forget a warm request (empty prompt,
  `keep_alive: -1`, the same load options as 1.1) for `config.OLLAMA_MODEL`;
- on the 1 -> 0 transition: schedule an unload (`keep_alive: 0`) after a new knob,
  e.g. `LLM_IDLE_UNLOAD_S` (default 600 s), cancelled if a connection arrives first;
- `0` keeps today's behaviour (never unload), for anyone who wants it.

**Saves 9.3 GB of VRAM, plus the host copy if 1.1 is not adopted, for all the time
nobody is connected.** Design notes:

- The warm and unload calls are I/O on the event loop, fire-and-forget like the
  utterance tasks. Give them a `_log_task_error` done-callback so a failure is not
  silent, and keep the read loop untouched, per the "never block the websocket read
  loop" invariant.
- The interpreter's own payload keeps `keep_alive: -1`. The idle timer is the only
  thing that ever releases the model, so a conversation with long silences never pays
  a reload mid-way.
- The unload must respect `dashboard._vram_lock`. It also must not evict a model that
  something else is using. A `pipeline_eval` running against the same model while the
  server's timer fires would lose its model mid-run and read as "slow model". The
  simplest guard is to skip the unload if `/api/ps` reports the model's `expires_at`
  in the future, which means another client renewed it after the server's last
  request. The other option is to document that evals and an idle server do not mix.
- `WS_IDLE_CLOSE_S` (900 s) already closes an abandoned tab, so a forgotten browser
  window releases the GPU after 900 + 600 s instead of never.
- A cheaper alternative with no new state: send `keep_alive: "15m"` instead of `-1`.
  The GPU frees 15 minutes after the last *read*. The cost is that a quiet room with a
  connected pendant goes cold too, and the next read after a lull pays the warm-up:
  ~2.7 s with 1.1 from page cache, ~10 s or more from disk.

**1.3 Poll the GPU only while someone is looking** (F6). Start the poller lazily on
the first `/api/stats` request and let it stop after ~60 s without one. Alternatively,
read the same PDH counters in-process via `ctypes`/`win32pdh`: after the first query
that costs milliseconds instead of a 2.8 s process launch. **Saves ~2.4 CPU-hours a
day and ~11,000 process launches.** It keeps the existing rule that a dashboard poll
never waits on the GPU query, because the cache stays.

**1.4 A context-overflow guard** (pairs with 0.3). `_ollama_stats()` already extracts
`prompt_eval_count`. Log a warning, and count a `metrics` event, when
`prompt_eval_count + OLLAMA_NUM_PREDICT` exceeds ~85% of the configured context. That
turns F3's silent system-prompt truncation into something visible on the dashboard.
It costs nothing at runtime and is worth having even if `num_ctx` never changes.

**1.5 Pre-quantised SER weights** (F5), *only if the evals agree*. This uses the same
shape as `eval/slim_ser_ckpt.py`: an offline step writes an int8 artifact, and
`ser.py` loads it when present and falls back otherwise. **Saves ~245 MB of weights
and halves SER time (0.16 s -> 0.08 s).** The gate is `eval.ser_eval` on all 1440
clips (macro recall, valence/arousal separation lines), then `eval.pipeline_eval` and
`eval.confidence_eval`, because confidence moved by up to 0.42. The swap stays inside
`ser.py`, as the module boundary requires.

### Tier 2: structural (only if the host has to get smaller)

**2.1 Torch-free CPU stack** (F5): speaker id on sherpa-onnx CAM++, SER on an ONNX
export of emotion2vec. Estimated **0.8-0.9 GB** of resident memory and ~6 s of cold
start. Each model still sits behind its existing module (`speaker.py`, `ser.py`), so no
other file learns about the swap. The cost is real: an export pipeline to maintain,
parity work on two models, and possibly a re-enrolment.

**2.2 ASR downsizing** (F4): Moonshine base or SenseVoice instead of Parakeet, ~0.4 GB.
Blocked on real-room ASR scoring, not on engineering. The catalogue already supports
both, live from the dashboard.

**2.3 Cap the ratchet.** The 15 s high-water mark comes from activations on the
longest utterance. Two options. Lower `MAX_UTTERANCE_MS`, which is a segmenter knob
with VAD consequences, so read `docs/status/TODO.md` first. Or run SER on a bounded
window of a long utterance. Either saves up to ~0.3-0.4 GB, and both change measured
behaviour.

**2.4 CPU models in a worker process that can exit.** Freed memory does not return on
Windows, so the only way to give an idle server's ~2 GB back is to end the process
holding it. Spawn the ASR+SER+speaker worker on the first connection and stop it after
the same idle timer as 1.2: an idle server would sit at ~60 MB. The price is an **8.2 s
cold start** (measured with a warm disk cache: torch 0.9 s, ASR 1.2 s, SER 3.8 s,
speaker 2.2 s) on the first connection after idle, an IPC hop per utterance (~1 MB of
float32, negligible), and a much bigger change to `main.py`'s wiring and to the "loaded
once at import" contracts of three modules. With 31 GB of RAM this is not worth it. On a
16 GB machine shared with other work it might be.

### Not recommended

- **Moving ASR, SER or speaker id to the GPU** to save RAM. It breaks the hardware
  split invariant, and with 1.9 GiB of VRAM headroom it would push the LLM into partial
  CPU offload, which is exactly the "slow model" failure the dashboard warns about.
- **`qwen3:8b` or `gemma3:12b` to save VRAM.** They would save 4 GB and 1 GB, but
  `config.py` records 33% and 43% tone accuracy against 81% for `qwen3:14b` on the
  RAVDESS pipeline. A lower quant of the 14B (e.g. Q3_K_M) is the only plausible VRAM
  cut that keeps the model. It has not been measured, and it would need the full
  `pipeline_eval` and `model_eval`.
- **Unloading models in-process and calling `gc.collect()`.** TODO.md measured that
  freed memory does not leave the process on Windows, and the probe's `after gc` row
  (-5 MB) agrees. Only 2.4 (exiting a process) returns it.
- **Trimming the working set** (`SetProcessWorkingSetSize(-1, -1)`) at idle. It makes
  Task Manager look better without freeing commit, and it moves the cost onto the next
  utterance as page faults.
- **Shrinking `OLLAMA_NUM_PREDICT` or the JSON keys** to save GPU time. Output is
  already 13-25 tokens. The keys are part of `RESPONSE_SCHEMA` and the measured prompt,
  so changing them invalidates the eval baseline for a few tens of milliseconds.

## Requirements before and after

| | today | after Tier 0 + Tier 1 (1.1-1.4) | after Tier 2.1 + 2.2 as well |
|---|---|---|---|
| GPU | 12 GB class (9.3 GB used of 11.1 free) | 12 GB class (~9.0 GB used); 8.4 GB of weights is the floor for `qwen3:14b` Q4_K_M | unchanged |
| VRAM while idle | 9.3 GB once used | **0** | 0 |
| host RAM, active | ~10.5 GB (2.0 Python + 8.6 runner) | **~2.6 GB** (2.0 + 0.6) | ~1.4 GB (estimated) |
| host RAM, idle | ~10.5 GB once used | ~2.0 GB | ~0.8 GB (estimated) |
| commit while LLM loaded | ~12.5 GB | ~12.7 GB (driver commit stays) | ~11.5 GB |
| idle CPU | ~0.1 core (GPU poller) | ~0 | ~0 |
| practical host | 32 GB | **16 GB** (keep a system-managed pagefile for the ~10 GB driver commit) | 16 GB with room to spare |

No measured option today runs this product on an 8 GB GPU at its current quality. That
is an LLM question (F5's "not recommended" list), not a server one.

## CPU contention during active runs

This is not RAM or GPU, but it is the other half of "computer requirements". One
utterance runs three executor jobs concurrently on 6 cores: torch's 6 intra-op threads
for SER, ONNX Runtime's 4 for ASR (`ASR_THREADS`), and CAM++ on torch's pool as well.
The 15 s probe row (ASR 0.36 s, SER 0.86 s, speaker 0.09 s, run one after another
there) shows SER is the long pole on long utterances. `config.py` already warns against
raising `SER_TORCH_THREADS` blindly. If 1.5 lands, SER halves and the CPU stage becomes
ASR-bound again. A 6-core part is comfortably enough. The pipeline never needs more than
~1.3 CPU-s per 15 s of speech.

## Verification checklist for whoever implements this

1. **Before:** record `python -m eval.latency`, `python -m eval.bench --skip-llm`, the
   live server's WS/private from the dashboard, and the runner's WS/private with the
   model loaded.
2. **1.1:** confirm `load_mode = none` in `%LOCALAPPDATA%\Ollama\server.log`, runner WS
   < 1 GB, `ollama ps` shows `100% GPU`, decode tok/s unchanged (36 tok/s today), and no
   reload when the dashboard warms a model and the interpreter then uses it.
3. **1.2:** connect -> model loads, disconnect -> unloads after the grace period,
   reconnect inside the grace period -> no reload, `pipeline_eval` running during the
   grace period -> not evicted.
4. **0.3 / 0.4:** `python -m eval.model_eval` and `python -m eval.pipeline_eval` within
   noise of the current baselines, especially the `literal`, `low-confidence` and
   `mismatch` categories.
5. **1.5 / 2.1 / 2.2:** the eval named in each item, plus this report's staged RSS
   table re-run to confirm the saving actually lands in the working set.

## Appendix: raw measurements

**Ollama loads of `qwen3:14b`** (2026-10-03, each followed by an explicit evict):

```
mmap   #1  wall 14.5 s  load_duration 12,336 ms  runner WS 8,585 MB  private 9,499 MB  GPU 9,268 MB
mmap   #2               load_duration 10,183 ms  Available 12,777 -> 4,079 MB   Committed 29,891 -> 39,500 MB
no-mmap                 load_duration  2,660 ms  Available 12,713 -> 11,909 MB  Committed 30,138 -> 40,037 MB
                        runner WS 579 MB  private 9,900 MB
after evict             Available back to 12,5xx-12,6xx MB, runner process gone
```

**Runner timing for a typical read** (log, task 7809): 1,008 prompt tokens of which
953 cached; prefill of the remaining 55 took 198 ms (3.6 ms/token); 17 output tokens at
33.6 tok/s, 476 ms; total 674 ms. Across 280 `pipeline_emotion.csv` rows: prompt 1,007
tokens median, output 18 median / 25 max, decode 36 tok/s, `load_ms` 2-3 (warm).

**Cold start of the CPU models** (warm disk cache, fresh process): torch 0.9 s,
transcriber 1.2 s, SER 3.8 s, speaker 2.2 s, rest 0.1 s, **total 8.2 s**.

**GPU poller cost** (3 runs of the poller's two `Get-Counter` queries in a fresh
`powershell.exe`): 2.78 / 2.78 / 2.76 s wall, 0.78 / 0.81 / 0.72 CPU-s, 129-130 MB
peak working set.

**Reproducing the Python probe.** Run it from the project root with the venv python
and `TRANSCRIPT_LOG_ENABLED=0 HF_HUB_OFFLINE=1`. Import `server.transcriber`,
`server.ser`, `server.speaker` and `server.main` in that order, reading
`psutil.Process().memory_info()` (`rss` = working set, `private`, `peak_wset`) after
each. Then call `transcriber.transcribe_scored`, `ser.analyze` and
`speaker.embed_windows` three times each on 1 s / 4 s / 15 s slices of concatenated
RAVDESS audio resampled to 16 kHz. For a single ASR backend, set `ASR_MODEL=<key>
SER_ENABLED=0 SPEAKER_ENABLED=0` and import only `server.transcriber`. For the int8
SER comparison, apply `quantize_dynamic(ser._model.model, {nn.Linear}, qint8)` and
rerun `ser.analyze` on every 10th RAVDESS file (144 clips, all 24 actors).
