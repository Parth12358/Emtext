"""End-to-end pipeline eval over RAVDESS: audio in, {tone, read} out.

    python -m eval.pipeline_eval                       # the full 2x3 matrix
    python -m eval.pipeline_eval --vad-check           # pre-flight, ~10 clips
    python -m eval.pipeline_eval --cells 1 --limit 28  # sanity pass, ~1 min
    python -m eval.pipeline_eval --evict-only          # just free the GPU

No env vars needed -- the HF symlink workaround is applied internally, and
SPEECH_RMS has a --speech-rms flag, because PowerShell cannot do `VAR=x cmd`.

Every other eval in this repo tests one stage alone. This one runs the whole
workflow -- Segmenter -> Whisper + SER -> Interpreter -- so it can measure
whether the stages *compose*: whether a mis-heard word ruins the read, whether
SER's valence actually reaches the LLM's judgement, whether the VAD copes with
real speech at all.

Why RAVDESS suits this specifically
-----------------------------------
Both spoken sentences are semantically neutral ("Kids are talking by the door",
"Dogs are sitting by the door"). So the text is known ground truth for WER, and
every scrap of emotional signal lives in the delivery. That is exactly the
words-vs-voice setup the interpreter's mismatch rule exists for: if the LLM
ignores the voice, every clip reads `neutral` no matter which emotion was acted.
That failure is measurable without needing any tone labels at all -- see
"voice sensitivity" in `summarise()`.

Shape of a run
--------------
The matrix is 2 SER backends x 3 LLMs, smallest models first so useful results
land in minutes rather than at the end. ASR and SER are computed ONCE per SER
backend and replayed against all three LLMs: the transcript and voice dict do
not depend on which LLM is under test, so recomputing them per cell would be 3x
redundant CPU work. That is also more correct -- all three LLMs then see
byte-identical inputs, so the comparison is not confounded by SER variation.

Stopping, pausing, resuming
---------------------------
Ctrl+C once finishes the in-flight clip, flushes, evicts the model from VRAM and
exits cleanly. Re-running the same command resumes: rows are keyed on
(ser_backend, llm_model, file) and completed ones are skipped, so *stop is
pause*. Creating the file `eval/results/PAUSE` pauses between clips and frees
the GPU without exiting; delete it to continue.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import csv
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from eval.asr_eval import wer
from eval.ser_eval import STATEMENTS, load_clips
from server import config

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
CSV_PATH = RESULTS / "pipeline.csv"
STATUS_PATH = RESULTS / "pipeline_status.json"
PAUSE_PATH = RESULTS / "PAUSE"

# Smallest first, deliberately: the cheap cells finish in minutes, so a mistake
# in the setup shows up early instead of two hours in.
SER_MODELS = [
    ("emotion2vec/emotion2vec_plus_base", "e2v-base"),
    ("MERaLiON/MERaLiON-SER-v1", "meralion"),
]
LLM_MODELS = ["qwen3:8b", "gemma3:12b", "qwen3:14b"]

# Derived from the acted emotion. This is a HEURISTIC, not ground truth: the
# words are neutral, so it only holds if the model reads the voice. Reported
# alongside the label-free voice-sensitivity metric, which does not rely on it.
EXPECTED_TONE = {
    "happy": "positive",
    "surprised": "positive",
    "neutral": "neutral",
    "sad": "negative",
    "angry": "negative",
    "fearful": "negative",
    "disgusted": "negative",
}

COLUMNS = [
    "cell_id", "ser_backend", "ser_model", "whisper_model", "llm_model", "row_ts",
    "file", "actor", "gender", "ravdess_emotion", "true_emotion", "intensity",
    "statement", "repetition", "reference_text",
    "vad_segments_n", "vad_dropped", "vad_original_ms", "vad_utterance_ms",
    "transcript", "wer_errors", "wer_ref_words", "wer_pct", "whisper_latency_s",
    "ser_emotion", "ser_correct", "ser_confidence", "ser_valence", "ser_arousal",
    "ser_dominance", "ser_latency_s",
    "llm_tone", "llm_read", "tone_expected", "tone_correct", "llm_offline",
    "llm_latency_s",
    "prompt_eval_count", "eval_count", "prompt_eval_ms", "eval_ms", "load_ms",
    "total_ms", "decode_tps",
    "cpu_stage_s", "total_latency_s", "error",
]

# Set by the SIGINT handler. 1 = finish this clip then stop; 2 = abort now.
_STOP = 0
_LOADED_MODEL: str | None = None   # what we last asked Ollama to hold resident


# ---------------------------------------------------------------------------
# VRAM hygiene. Ollama pins models with keep_alive:-1 (interpreter.py), so
# without an explicit unload a finished run leaves 8-9 GB occupied indefinitely,
# and switching models mid-matrix can push Ollama into offloading layers to CPU
# -- which looks like "this model is slow" rather than "this model was evicted".
# ---------------------------------------------------------------------------

def evict(model: str | None, quiet: bool = False) -> None:
    """Unload `model` from VRAM. Never raises -- Ollama may already be gone."""
    global _LOADED_MODEL
    if not model:
        return
    try:
        httpx.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={"model": model, "keep_alive": 0, "prompt": "", "stream": False},
            timeout=30,
        )
        if not quiet:
            print(f"  evicted {model} from VRAM", flush=True)
    except httpx.HTTPError as exc:
        if not quiet:
            print(f"  could not evict {model}: {exc}", flush=True)
    finally:
        if _LOADED_MODEL == model:
            _LOADED_MODEL = None


def _evict_on_exit() -> None:
    """atexit backstop, so even an unhandled exception frees the GPU."""
    evict(_LOADED_MODEL, quiet=True)


atexit.register(_evict_on_exit)


def resident() -> list[dict]:
    """What Ollama currently holds, with VRAM residency per model."""
    try:
        body = httpx.get(f"{config.OLLAMA_URL}/api/ps", timeout=10).json()
    except (httpx.HTTPError, ValueError):
        return []
    return body.get("models", [])


def report_resident(prefix: str = "  ") -> None:
    models = resident()
    if not models:
        print(f"{prefix}GPU clear -- no model resident", flush=True)
        return
    for m in models:
        size, vram = m.get("size", 0), m.get("size_vram", 0)
        where = "GPU" if vram and vram >= size else ("partly CPU" if vram else "CPU")
        print(f"{prefix}{m['name']}: {size / 1e9:.2f}GB resident ({where})", flush=True)


async def load_llm(client: httpx.AsyncClient, model: str) -> None:
    """Make `model` the resident one, then warm it.

    Evicting the previous model first matters: the Arc B580 has 12 GB and
    gemma3:12b (8.1) + qwen3:14b (9.3) cannot coexist. Without an explicit
    unload Ollama may keep part of the new model on CPU, and every latency
    number for that cell would be quietly wrong.
    """
    global _LOADED_MODEL
    if _LOADED_MODEL and _LOADED_MODEL != model:
        evict(_LOADED_MODEL)

    print(f"  loading {model} ...", flush=True)
    from server.interpreter import Interpreter

    original = config.OLLAMA_MODEL
    config.OLLAMA_MODEL = model
    try:
        # Untimed warm-up: the first real clip must not be charged with a cold
        # VRAM load (~8s on a 12B), which would corrupt its latency row.
        await Interpreter(client).interpret("Warming up the model.")
    finally:
        config.OLLAMA_MODEL = original
    _LOADED_MODEL = model

    for m in resident():
        if m["name"] == model:
            size, vram = m.get("size", 0), m.get("size_vram", 0)
            if vram < size:
                print(f"  WARNING: {model} is only {vram / 1e9:.1f}/{size / 1e9:.1f}GB "
                      f"in VRAM -- the rest is on CPU, so its timings will be "
                      f"slow for the wrong reason.", flush=True)
            else:
                print(f"  {model} resident, {vram / 1e9:.1f}GB fully in VRAM",
                      flush=True)
            break


# ---------------------------------------------------------------------------
# Persistence: append-only, flushed per row, tolerant of a truncated last line.
# ---------------------------------------------------------------------------

def load_done() -> set[tuple[str, str, str]]:
    """(ser_backend, llm_model, file) triples already in the CSV."""
    done: set[tuple[str, str, str]] = set()
    if not CSV_PATH.exists():
        return done
    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                done.add((row["ser_backend"], row["llm_model"], row["file"]))
            except (KeyError, TypeError):
                # A hard kill mid-write can leave a partial final row. Treat it
                # as not-done rather than crashing: worst case we redo one clip.
                continue
    return done


def load_cache(slug: str) -> dict[str, dict]:
    """Cached ASR+SER results for one SER backend, keyed by filename."""
    path = RESULTS / f"_cache_asrser_{slug}.jsonl"
    cache: dict[str, dict] = {}
    if not path.exists():
        return cache
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # truncated final line; it will simply be recomputed
            cache[obj["file"]] = obj
    return cache


_status_failures = 0


def write_status(**fields) -> None:
    """Atomic status write. NEVER raises -- this is telemetry, not data.

    A run died at clip 75 of 280 because os.replace hit
    `PermissionError: [WinError 5]`: on Windows the rename fails if anything else
    holds the destination open, and an editor, an antivirus scanner, the search
    indexer or someone simply reading the file is enough. Losing a status update
    costs nothing; losing forty minutes of eval costs a lot.

    The write is still atomic (tmp + replace) so a watcher never sees a
    half-written file, and brief retries absorb the usual sub-second locks.
    """
    global _status_failures

    fields["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fields["pid"] = os.getpid()
    tmp = STATUS_PATH.with_suffix(".tmp")

    for attempt in range(3):
        try:
            tmp.write_text(json.dumps(fields, indent=2), encoding="utf-8")
            os.replace(tmp, STATUS_PATH)
            _status_failures = 0
            return
        except OSError:
            if attempt < 2:
                time.sleep(0.15)

    # Exhausted retries. Say so once per streak so a permanently broken status
    # path is still visible, without spamming a line per clip.
    _status_failures += 1
    if _status_failures == 1:
        print(f"  (status file not writable, continuing: {STATUS_PATH})", flush=True)


async def wait_if_paused(client: httpx.AsyncClient, model: str) -> None:
    """Block while eval/results/PAUSE exists, freeing the GPU meanwhile."""
    if not PAUSE_PATH.exists():
        return
    print(f"\n  PAUSED ({PAUSE_PATH.name} present). Freeing the GPU; "
          f"delete the file to resume.", flush=True)
    evict(_LOADED_MODEL)
    write_status(phase="paused", llm_model=model)
    while PAUSE_PATH.exists() and not _STOP:
        await asyncio.sleep(2)
    if _STOP:
        return
    print("  resuming ...", flush=True)
    await load_llm(client, model)


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------

def build_row(cell_id, ser_model, ser_backend, llm_model, meta, cached,
              llm_out, llm_s) -> dict:
    """Merge RAVDESS labels + cached CPU results + LLM output into one CSV row."""
    reference = meta["statement"]          # RAVDESS statement text == ground truth
    transcript = cached.get("transcript") or ""
    errors, ref_words = wer(reference, transcript)
    voice = cached.get("voice") or {}
    ser_emotion = voice.get("emotion")
    tone = (llm_out or {}).get("tone")
    expected = EXPECTED_TONE.get(meta["true_emotion"])

    return {
        "cell_id": cell_id,
        "ser_backend": ser_backend,
        "ser_model": ser_model,
        "whisper_model": config.WHISPER_MODEL,
        "llm_model": llm_model,
        "row_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file": cached["file"],
        "actor": meta["actor"],
        "gender": meta["gender"],
        "ravdess_emotion": meta["ravdess_emotion"],
        "true_emotion": meta["true_emotion"],
        "intensity": meta["intensity"],
        "statement": meta["statement"],
        "repetition": meta["repetition"],
        "reference_text": reference,
        "vad_segments_n": cached.get("vad_segments_n"),
        "vad_dropped": cached.get("vad_dropped"),
        "vad_original_ms": cached.get("vad_original_ms"),
        "vad_utterance_ms": cached.get("vad_utterance_ms"),
        "transcript": transcript,
        "wer_errors": errors,
        "wer_ref_words": ref_words,
        "wer_pct": round(100 * errors / ref_words, 1) if ref_words else "",
        "whisper_latency_s": cached.get("whisper_latency_s"),
        "ser_emotion": ser_emotion,
        "ser_correct": int(ser_emotion == meta["true_emotion"]) if ser_emotion else "",
        "ser_confidence": voice.get("confidence"),
        "ser_valence": voice.get("valence"),
        "ser_arousal": voice.get("arousal"),
        "ser_dominance": voice.get("dominance"),
        "ser_latency_s": cached.get("ser_latency_s"),
        "llm_tone": tone,
        "llm_read": (llm_out or {}).get("read"),
        "tone_expected": expected,
        "tone_correct": int(tone == expected) if tone and expected else "",
        "llm_offline": int((llm_out or {}).get("read") == "(interpreter offline)"),
        "llm_latency_s": round(llm_s, 3),
        "prompt_eval_count": (llm_out or {}).get("prompt_eval_count"),
        "eval_count": (llm_out or {}).get("eval_count"),
        "prompt_eval_ms": (llm_out or {}).get("prompt_eval_ms"),
        "eval_ms": (llm_out or {}).get("eval_ms"),
        "load_ms": (llm_out or {}).get("load_ms"),
        "total_ms": (llm_out or {}).get("total_ms"),
        "decode_tps": (llm_out or {}).get("decode_tps"),
        "cpu_stage_s": cached.get("cpu_stage_s"),
        "total_latency_s": round((cached.get("cpu_stage_s") or 0) + llm_s, 3),
        "error": cached.get("error", ""),
    }


def log_row(prefix: str, i: int, total: int, row: dict, acc: float,
            eta_s: float) -> None:
    ser_mark = "?" if row["ser_correct"] == "" else ("y" if row["ser_correct"] else "n")
    tone_mark = "?" if row["tone_correct"] == "" else ("y" if row["tone_correct"] else "n")
    drop = " VAD-DROP" if row["vad_dropped"] else ""
    # The prefix carries the cell -- which SER backend and which LLM produced
    # this line -- so a scrollback or a grepped log is self-describing. Without
    # it every line looks alike and you cannot tell which model said what.
    print(
        f"{prefix}[{i:>4}/{total}] {row['file'].replace('.wav', ''):<24} "
        # 10 wide, not 9: "disgusted"/"surprised" are exactly 9 chars and the
        # correctness mark collided with them ("->disgustedy").
        f"ser:{row['true_emotion']:<10}->{str(row['ser_emotion']):<10}{ser_mark} "
        f"wer {str(row['wer_pct']):>5}% "
        f"llm:{str(row['llm_tone']):<8}(exp {str(row['tone_expected'])[:8]:<8}){tone_mark} "
        f"cpu {row['cpu_stage_s']:>5}s llm {row['llm_latency_s']:>5}s "
        f"acc {acc:>3.0f}% eta {eta_s / 60:>4.1f}m{drop}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Phase 1: collect -- run the CPU child while the LLM works on earlier clips
# ---------------------------------------------------------------------------

async def collect_and_interpret(client, ser_model, slug, llm_model, clips, cache,
                                done, writer, fh, cell_id, quiet, counters):
    """Stream clips through the CPU subprocess and the LLM concurrently.

    This is where the cross-clip pipeline lives. Within one clip the LLM needs
    SER's output, so they cannot overlap -- but while clip N is on the GPU the
    child process is already computing clip N+1 on the CPU. The bounded queue is
    what keeps that look-ahead from running away: full queue -> parent stops
    reading -> the child's stdout pipe fills -> the child blocks. Backpressure
    for free, and no unbounded memory.
    """
    from server.interpreter import Interpreter

    todo = [(p, m) for p, m in clips if p.name not in cache]
    meta_by_name = {p.name: m for p, m in clips}
    cache_path = RESULTS / f"_cache_asrser_{slug}.jsonl"

    env = {
        **os.environ,
        "SER_MODEL": ser_model,
        "HF_HUB_DISABLE_SYMLINKS": "1",
        "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }
    print(f"  starting CPU worker for {ser_model} ({len(todo)} clips to compute) ...",
          flush=True)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "eval.pipeline_child", str(ROOT)],
        cwd=str(ROOT), env=env, text=True, bufsize=1,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,   # these libraries are very chatty on load
    )

    handshake = None
    for line in proc.stdout:
        if line.strip().startswith("{"):
            handshake = json.loads(line)
            break
    if not handshake or not handshake.get("ok"):
        proc.kill()
        raise RuntimeError(f"CPU worker failed to start for {ser_model}")
    backend = handshake.get("backend") or "?"
    print(f"  CPU worker ready (backend={backend}, "
          f"whisper={handshake.get('whisper_model')})", flush=True)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=3)

    def feed() -> None:
        try:
            for path, _meta in todo:
                if _STOP:
                    break
                proc.stdin.write(f"{path}\n")
                proc.stdin.flush()
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def pump() -> None:
        """Blocking reader thread -> async queue. Sentinel None on EOF.

        Every cross-thread submit is guarded on `loop.is_closed()`. If the
        consumer dies, asyncio.run() closes the loop underneath this thread and
        both the submit and the sentinel in `finally` raise
        "Event loop is closed" -- two tracebacks that bury the ACTUAL failure
        exactly when you need to read it. Bailing out quietly instead leaves the
        real exception as the only thing on screen.
        """
        try:
            for line in proc.stdout:
                if loop.is_closed():
                    return
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("event") == "row":
                    # .result() applies backpressure to this thread, which in
                    # turn backs up the child. Finite timeout so a dead consumer
                    # fails loudly instead of hanging the run forever.
                    try:
                        asyncio.run_coroutine_threadsafe(
                            queue.put(obj), loop).result(timeout=1800)
                    except RuntimeError:
                        return  # loop closed mid-submit; consumer is gone
        finally:
            if not loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(queue.put(None), loop)
                except RuntimeError:
                    pass

    threading.Thread(target=feed, daemon=True).start()
    threading.Thread(target=pump, daemon=True).start()

    total = len(clips)
    with cache_path.open("a", encoding="utf-8") as cache_fh:
        while True:
            item = await queue.get()
            if item is None:
                break
            if _STOP >= 2:
                break

            cache[item["file"]] = item
            cache_fh.write(json.dumps(item) + "\n")
            cache_fh.flush()

            await _interpret_and_write(
                client, Interpreter, llm_model, item, meta_by_name, done,
                writer, fh, cell_id, ser_model, backend, total, quiet, counters)

            if _STOP:
                break
            await wait_if_paused(client, llm_model)

    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    return backend


async def _interpret_and_write(client, Interpreter, llm_model, cached, meta_by_name,
                               done, writer, fh, cell_id, ser_model, backend,
                               total, quiet, counters):
    """Run the LLM on one clip and append its row. Failures become CSV rows."""
    name = cached["file"]
    meta = meta_by_name.get(name)
    if meta is None:
        return
    if (backend, llm_model, name) in done:
        return

    original = config.OLLAMA_MODEL
    config.OLLAMA_MODEL = llm_model
    start = time.perf_counter()
    try:
        # Fresh Interpreter per clip: the rolling context is per-conversation
        # and must not bleed between unrelated RAVDESS clips.
        interp = Interpreter(client)
        llm_out = await interp.interpret(cached.get("transcript") or "",
                                         cached.get("voice"))
        err = ""
    except Exception as exc:  # noqa: BLE001
        llm_out, err = None, f"{type(exc).__name__}: {exc}"
    finally:
        config.OLLAMA_MODEL = original
    llm_s = time.perf_counter() - start

    row = build_row(cell_id, ser_model, backend, llm_model, meta, cached,
                    llm_out, llm_s)
    if err:
        row["error"] = (row["error"] + " | " + err).strip(" |")
    writer.writerow(row)
    fh.flush()
    done.add((backend, llm_model, name))

    # --- everything past this point is bookkeeping, not data -----------------
    # The row is written and flushed above, so it is safe on disk. Progress
    # counters, the console line and the status file are all observability, and
    # a failure in any of them must not abort a run that is minutes or hours in.
    # A telemetry write is exactly what killed a run at clip 75 of 280.
    try:
        counters["done"] += 1
        counters["elapsed"] = time.perf_counter() - counters["t0"]
        if row["tone_correct"] != "":
            counters["tone_n"] += 1
            counters["tone_ok"] += int(row["tone_correct"])
        acc = 100 * counters["tone_ok"] / counters["tone_n"] if counters["tone_n"] else 0
        remaining = total - counters["done"]
        eta = (counters["elapsed"] / counters["done"]) * remaining if counters["done"] else 0

        if not quiet:
            log_row(counters["prefix"], counters["done"], total, row, acc, eta)
        elif counters["done"] % 25 == 0:
            print(f"{counters['prefix']}{counters['done']}/{total}  tone acc {acc:.0f}%  "
                  f"eta {eta / 60:.1f}m", flush=True)

        write_status(phase=counters["phase"], cell_id=cell_id, llm_model=llm_model,
                     ser_model=ser_model, done=counters["done"], total=total,
                     tone_accuracy=round(acc, 1), eta_s=round(eta),
                     elapsed_s=round(counters["elapsed"]), last_file=name,
                     last_error=row["error"] or None)
    except Exception as exc:  # noqa: BLE001 -- see comment above
        print(f"  (progress bookkeeping failed, row still saved: "
              f"{type(exc).__name__}: {exc})", flush=True)


# ---------------------------------------------------------------------------
# Phase 2: replay -- CPU results already cached, so this is GPU-only
# ---------------------------------------------------------------------------

async def replay(client, ser_model, backend, llm_model, clips, cache, done,
                 writer, fh, cell_id, quiet, counters):
    """Run a second/third LLM over the cached ASR+SER results.

    No subprocess and no queue: the CPU work for this SER backend is already
    done and identical for every LLM, which is the whole point of caching it --
    all three models see byte-identical inputs, so the comparison isolates the
    LLM.
    """
    from server.interpreter import Interpreter

    meta_by_name = {p.name: m for p, m in clips}
    total = len(clips)
    for path, _meta in clips:
        if _STOP:
            break
        cached = cache.get(path.name)
        if cached is None:
            continue  # never computed (crash or stop mid-collect); next run gets it
        await _interpret_and_write(
            client, Interpreter, llm_model, cached, meta_by_name, done,
            writer, fh, cell_id, ser_model, backend, total, quiet, counters)
        await wait_if_paused(client, llm_model)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarise(csv_path: Path) -> None:
    """Per-cell accuracy, and the metric that actually matters: voice sensitivity."""
    if not csv_path.exists():
        return
    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return

    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        cells[(r["ser_backend"], r["llm_model"])].append(r)

    print("\n" + "=" * 92)
    print("RESULTS BY CELL")
    print("=" * 92)
    print(f"{'ser':<12}{'llm':<14}{'n':>5}{'SER acc':>9}{'tone acc':>10}"
          f"{'WER':>7}{'voice sens':>12}{'cpu s':>8}{'llm s':>8}")
    print("-" * 92)

    for (backend, llm), group in sorted(cells.items()):
        n = len(group)

        def pct(key: str) -> float:
            vals = [int(r[key]) for r in group if r[key] not in ("", None)]
            return 100 * sum(vals) / len(vals) if vals else 0.0

        errs = sum(int(r["wer_errors"] or 0) for r in group)
        words = sum(int(r["wer_ref_words"] or 0) for r in group)

        # Voice sensitivity: does the tone actually move with the delivery?
        # The words are identical and neutral across every clip, so a model that
        # ignores the voice gives the same answer everywhere and scores ~0 here
        # regardless of how good its tone accuracy looks. No labels needed.
        def positive_rate(emotions: set[str]) -> float | None:
            sub = [r for r in group if r["true_emotion"] in emotions and r["llm_tone"]]
            if not sub:
                return None
            return sum(r["llm_tone"] == "positive" for r in sub) / len(sub)

        hi, lo = positive_rate({"happy", "surprised"}), positive_rate({"angry", "sad"})
        sens = f"{100 * (hi - lo):+.0f}%" if hi is not None and lo is not None else "-"

        def mean(key: str) -> float:
            vals = [float(r[key]) for r in group if r[key] not in ("", None)]
            return sum(vals) / len(vals) if vals else 0.0

        print(f"{backend:<12}{llm:<14}{n:>5}{pct('ser_correct'):>8.0f}%"
              f"{pct('tone_correct'):>9.0f}%"
              f"{(100 * errs / words if words else 0):>6.0f}%{sens:>12}"
              f"{mean('cpu_stage_s'):>7.2f}s{mean('llm_latency_s'):>7.2f}s")

    print("\nvoice sensitivity = P(tone=positive | happy/surprised)")
    print("                  - P(tone=positive | angry/sad)")
    print("Both RAVDESS sentences are neutral, so this is pure voice signal: a model")
    print("ignoring the `voice` field scores ~0 no matter its tone accuracy. It is the")
    print("large-sample successor to the 4 mismatch pairs in tone_cases.jsonl.")

    dropped = sum(1 for r in rows if r["vad_dropped"] == "1")
    if dropped:
        print(f"\nWARNING: the VAD found no speech in {dropped}/{len(rows)} rows and fell")
        print("back to the raw clip. Those rows are still scored, but SPEECH_RMS may be")
        print("too high for studio recordings -- check which emotions they cluster in.")

    offline = sum(1 for r in rows if r["llm_offline"] == "1")
    if offline:
        print(f"\nWARNING: {offline}/{len(rows)} rows came back '(interpreter offline)'.")
        print("Those tone values are fallbacks, not judgements -- exclude them.")


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def vad_check(clips) -> None:
    """Does the mic-tuned VAD survive studio recordings? Answer before a long run.

    SPEECH_RMS is tuned for a live microphone. If quiet RAVDESS classes (sad,
    fearful, calm) never cross it, the VAD stage silently mangles exactly the
    emotions the mismatch rule most depends on -- so this is worth ten seconds
    before committing to hours.
    """
    import numpy as np

    from eval.ser_eval import read_wav_16k
    from server.segmenter import Segmenter

    print("=" * 78)
    print(f"VAD PRE-FLIGHT  (SPEECH_RMS={config.SPEECH_RMS}, "
          f"MIN_UTTERANCE_MS={config.MIN_UTTERANCE_MS})")
    print("=" * 78)
    print(f"{'file':<32}{'emotion':<11}{'peak rms':>9}{'segs':>6}{'kept ms':>9}  verdict")
    print("-" * 78)

    dropped = 0
    dropped_peaks: list[float] = []
    for path, meta in clips:
        audio = read_wav_16k(path)
        frame = int(config.SAMPLE_RATE * config.FRAME_MS / 1000)
        usable = (audio.size // frame) * frame
        rms = np.sqrt(np.mean((audio[:usable].reshape(-1, frame) * 32768.0) ** 2, axis=1))
        seg = Segmenter()
        outs = list(seg.feed((audio * 32768.0).astype(np.int16).tobytes()))
        tail = seg.flush()
        if tail is not None:
            outs.append(tail)
        kept = max((a.size for a in outs), default=0) / config.SAMPLE_RATE * 1000
        ok = bool(outs)
        dropped += (not ok)
        if not ok:
            dropped_peaks.append(float(rms.max()))
        print(f"{path.name:<32}{meta['true_emotion']:<11}{rms.max():>9.0f}"
              f"{len(outs):>6}{kept:>8.0f}ms  {'ok' if ok else 'DROPPED'}")

    print("-" * 78)
    if not dropped:
        print("All clips segmented. The VAD copes with this material.")
        return

    print(f"{dropped}/{len(clips)} clips produced no segment.")
    print()
    print("The eval still scores these: it falls back to the raw clip and flags")
    print("vad_dropped=1, so the row is kept and SER is unaffected (it pads to a fixed")
    print("30s window regardless). But measured on real rows, dropped clips transcribe")
    print("far worse -- ~33% WER against ~0% for kept clips -- so their tone reads are")
    print("made on garbled words. Filter vad_dropped=1 out when analysing tone accuracy.")
    print()
    print("That WER gap is NOT caused by the fallback. These clips peak around -40 dBFS")
    print("and Whisper mis-hears them however they are fed to it: segmented, raw, or")
    print("amplified. Very quiet speech is simply hard to transcribe. The VAD drop and")
    print("the bad transcript share a cause rather than one causing the other.")
    print()
    print("The pattern is still worth reading as a PRODUCT finding:")
    print()
    print("  SPEECH_RMS is an absolute int16 threshold tuned for a live mic with gain.")
    print("  Loud emotions (angry, happy) clear it easily; quiet ones (sad, fearful,")
    print("  neutral) do not. So the VAD is least able to hear exactly the subdued,")
    print("  masked speech this app exists to interpret. A quiet real user would hit")
    print("  the same wall.")
    print()
    print("  Note MIN_UTTERANCE_MS is a second filter: a clip whose peak clears")
    print("  SPEECH_RMS can still be dropped if too few frames do, so lowering the")
    print("  threshold alone does not fix it.")
    print()
    # Suggest a threshold derived from the clips that actually failed, rather
    # than a fixed number -- and stay quiet if lowering it would not help, which
    # is the case once MIN_UTTERANCE_MS (not the threshold) is what is dropping
    # them. Use the flag, not `SPEECH_RMS=...`: PowerShell has no `VAR=x cmd`.
    workable = [p for p in dropped_peaks if p < config.SPEECH_RMS]
    if workable:
        suggestion = max(50, int(min(workable) * 0.6))
        print(f"Try: python -m eval.pipeline_eval --vad-check "
              f"--speech-rms {suggestion}")
    else:
        print(f"Lowering SPEECH_RMS will NOT recover these: every dropped clip already")
        print(f"peaks above {config.SPEECH_RMS}, so MIN_UTTERANCE_MS={config.MIN_UTTERANCE_MS}")
        print("is what discards them -- too few frames clear the bar, not none.")
    print("Do NOT normalise the clips to work around this -- loudness is itself an")
    print("emotional cue, and flattening it would corrupt the SER measurement, which")
    print("is the thing this eval is actually for.")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _install_signal_handler() -> None:
    """Ctrl+C once = finish this clip and stop cleanly. Twice = abort now.

    A hard kill mid-write can leave a truncated CSV row; the graceful path lets
    the in-flight clip finish, flushes it, tears down the child process and
    frees the GPU. Because resume is keyed on (backend, llm, file), a clean stop
    is indistinguishable from a pause -- just re-run the same command.
    """
    def handler(_signum, _frame):
        global _STOP
        _STOP += 1
        if _STOP == 1:
            print("\n\n  Ctrl+C -- finishing the current clip, then stopping.\n"
                  "  (press again to abort immediately; the GPU is freed either way)",
                  flush=True)
        else:
            print("\n  aborting.", flush=True)
    signal.signal(signal.SIGINT, handler)


async def run(args) -> None:
    clips = load_clips(args.limit, args.seed, exclude_calm=not args.include_calm)

    if args.vad_check:
        vad_check(clips[: args.vad_check])
        return

    cells = [(sm, slug, llm) for sm, slug in SER_MODELS for llm in LLM_MODELS]
    if args.cells:
        cells = cells[: args.cells]

    RESULTS.mkdir(parents=True, exist_ok=True)
    done = load_done()
    is_new = not CSV_PATH.exists()

    print("=" * 92)
    print("FULL PIPELINE EVAL over RAVDESS")
    print("=" * 92)
    print(f"  clips per cell : {len(clips)} (seed {args.seed}, "
          f"calm {'folded into neutral' if args.include_calm else 'excluded'})")
    print(f"  cells          : {len(cells)}  (smallest models first)")
    for i, (sm, _slug, llm) in enumerate(cells, 1):
        print(f"      {i}. {sm.split('/')[-1]:<26} + {llm}")
    print(f"  whisper        : {config.WHISPER_MODEL}")
    print(f"  csv            : {CSV_PATH}")
    print(f"  status         : {STATUS_PATH}")
    if done:
        print(f"  resuming       : {len(done)} rows already complete, will be skipped")
    print(f"\n  pause : touch {PAUSE_PATH}   (frees the GPU; delete to resume)")
    print(f"  stop  : Ctrl+C           (clean; re-run this command to continue)")
    print("\n  GPU before start:")
    report_resident("    ")

    started = time.perf_counter()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
            fh.flush()

        async with httpx.AsyncClient() as client:
            # Cache is per SER backend; the first LLM for a backend computes it
            # (pipelined against the GPU), the rest replay it.
            collected: dict[str, str] = {}
            for index, (ser_model, slug, llm_model) in enumerate(cells, 1):
                if _STOP:
                    break
                cell_id = f"{slug}+{llm_model}"
                print(f"\n{'#' * 92}")
                print(f"# cell {index}/{len(cells)}: {ser_model.split('/')[-1]} + {llm_model}")
                print("#" * 92, flush=True)

                await wait_if_paused(client, llm_model)
                if _STOP:
                    break
                await load_llm(client, llm_model)

                cache = load_cache(slug)
                counters = {
                    "done": 0, "tone_ok": 0, "tone_n": 0, "t0": time.perf_counter(),
                    "elapsed": 0.0,
                    # Name the models on every line, not just in the cell banner:
                    # a long log gets scrolled, split and grepped, and a line that
                    # does not say what produced it is not much use on its own.
                    "prefix": f"  [{index}/{len(cells)} {cell_id}] ",
                    "phase": "collect" if slug not in collected else "replay",
                }

                if slug not in collected:
                    backend = await collect_and_interpret(
                        client, ser_model, slug, llm_model, clips, cache, done,
                        writer, fh, cell_id, args.quiet, counters)
                    collected[slug] = backend
                    # collect() only sees clips the child had to COMPUTE, so on a
                    # warm cache it interprets just those and silently leaves the
                    # already-cached clips with no LLM row for this cell. Sweep
                    # them up here. A no-op on a cold cache, and _interpret_and_write
                    # skips anything already in `done`, so nothing is duplicated.
                    if not _STOP:
                        cache = load_cache(slug)
                        counters["phase"] = "replay"
                        await replay(client, ser_model, backend, llm_model,
                                     clips, cache, done, writer, fh, cell_id,
                                     args.quiet, counters)
                else:
                    await replay(client, ser_model, collected[slug], llm_model,
                                 clips, cache, done, writer, fh, cell_id,
                                 args.quiet, counters)

                print(f"  cell done: {counters['done']} rows in "
                      f"{counters['elapsed'] / 60:.1f} min", flush=True)

    print(f"\ntotal wall clock: {(time.perf_counter() - started) / 60:.1f} min")
    summarise(CSV_PATH)
    print(f"\nper-clip rows: {CSV_PATH}")


def main() -> None:
    # Declared up front: Python requires `global` before the name is used
    # anywhere in the function, and the --csv default reads CSV_PATH below.
    global CSV_PATH, STATUS_PATH

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=280,
                    help="clips per cell, balanced across emotions; 0 = all 1440")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cells", type=int, default=0,
                    help="run only the first N cells (smallest models first)")
    ap.add_argument("--include-calm", action="store_true",
                    help="fold RAVDESS 'calm' into neutral (default: excluded; "
                         "see eval/ser_eval.py for why)")
    ap.add_argument("--quiet", action="store_true",
                    help="progress every 25 clips instead of a line per clip")
    ap.add_argument("--vad-check", type=int, nargs="?", const=10, default=0,
                    metavar="N", help="pre-flight: segment N clips and exit")
    ap.add_argument("--speech-rms", type=int, default=None, metavar="RMS",
                    help=f"override SPEECH_RMS (default {config.SPEECH_RMS}). "
                         "RAVDESS is quieter than a live mic; try 150.")
    ap.add_argument("--evict-only", action="store_true",
                    help="unload everything Ollama is holding, then exit")
    ap.add_argument("--csv", default=str(CSV_PATH), metavar="PATH",
                    help="where rows go (default %(default)s). Point a demo or "
                         "experiment at its own file so it neither appends to your "
                         "real results nor inherits their resume state.")
    args = ap.parse_args()

    # Rebind the output paths before anything reads them. Resume is driven by
    # the CSV's contents, so a separate --csv is also a separate run history --
    # which is exactly what you want for a throwaway demo.
    CSV_PATH = Path(args.csv)
    STATUS_PATH = CSV_PATH.with_name(CSV_PATH.stem + "_status.json")

    # Set here rather than asking the operator to export env vars: PowerShell
    # cannot do `VAR=x command`, so anything env-based turns one command into
    # three. These are the two settings every run of this eval wants.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")          # Windows: WinError 1314
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    if args.speech_rms is not None:
        # Mutate config (which the Segmenter reads at construction) AND the env,
        # so the CPU subprocess inherits the same threshold.
        config.SPEECH_RMS = args.speech_rms
        os.environ["SPEECH_RMS"] = str(args.speech_rms)

    if args.evict_only:
        print("resident before:")
        report_resident()
        for m in resident():
            evict(m["name"])
        print("resident after:")
        report_resident()
        return

    _install_signal_handler()
    try:
        asyncio.run(run(args))
    finally:
        # Unconditional: normal finish, Ctrl+C, or an unhandled exception all
        # land here, so the GPU is never left holding 8-9GB after a run.
        if _LOADED_MODEL:
            print("\ncleaning up:", flush=True)
            evict(_LOADED_MODEL)
        report_resident()


if __name__ == "__main__":
    main()
