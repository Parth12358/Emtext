"""Benchmark the three models in the pipeline: Whisper, SER, and the Ollama LLM.

Answers the practical question "where does the time actually go, and can this
keep up with a conversation?" -- run it after changing a model, a compute type,
or the hardware split.

    python -m eval.bench                 # everything
    python -m eval.bench --skip-llm      # audio stages only (no Ollama needed)
    python -m eval.bench --runs 5        # more repetitions per measurement

A note on "tokens per second", because it only means something for one of the
three stages:

  - The LLM is autoregressive, so tok/s is the natural unit and Ollama reports
    the real numbers itself (`eval_count` / `eval_duration`). We use its figures
    rather than wall-clock guesses, and we separate prompt processing (prefill,
    which scales with context length) from generation (decode), because they
    have very different costs and only decode grows with a longer answer.

  - Whisper as used here decodes text, so tok/s is meaningful in principle, but
    faster-whisper does not expose token counts through this wrapper. We report
    latency and a real-time factor instead.

  - SER is NOT autoregressive. It is a single encoder forward pass producing 7
    logits and 3 numbers -- there are no tokens to count and a tok/s figure
    would be meaningless. Latency is the number that matters.

So for the audio stages we report the **real-time factor** (RTF): audio seconds
processed per second of wall clock. RTF > 1 means the stage is faster than
real time and can keep up with live speech; RTF < 1 means it falls behind.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import wave
from pathlib import Path

import numpy as np

from server import config

ROOT = Path(__file__).resolve().parent.parent

# Utterance lengths to sweep. SER is expected to be flat across these (its
# feature extractor pads everything to a fixed 30 s window) while Whisper should
# scale with length -- the sweep is what makes that difference visible.
LENGTHS_S = (1, 2, 4, 8)

# Lines fed to the LLM. Short and long prompts are both represented because
# prefill cost scales with context while decode cost does not.
LLM_LINES = [
    "Oh great, another meeting that could have been an email.",
    "The train leaves at four fifteen from platform two.",
    "Fine. Whatever you think is best.",
    "No, please, take your time. It's not like I have anywhere to be.",
    "Thanks for finally getting back to me.",
]


def _fmt(values: list[float]) -> str:
    """mean / median / p95 for a list of seconds."""
    if not values:
        return "n/a"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return (
        f"{statistics.mean(values):6.2f}s  "
        f"{statistics.median(values):6.2f}s  "
        f"{p95:6.2f}s"
    )


def _load_audio() -> np.ndarray:
    """Load the SAPI test WAV, or synthesise noise if it hasn't been generated."""
    path = ROOT / "experiments" / "speech.wav"
    if path.exists():
        with wave.open(str(path), "rb") as w:
            if w.getframerate() != config.SAMPLE_RATE:
                raise SystemExit(f"{path} must be {config.SAMPLE_RATE} Hz")
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            audio = pcm.astype(np.float32) / 32768.0
            if w.getnchannels() > 1:
                audio = audio.reshape(-1, w.getnchannels()).mean(axis=1)
        print(f"audio: {path.relative_to(ROOT)} ({audio.size / 16000:.1f}s)")
        return audio
    print("audio: experiments/speech.wav missing -- using synthetic noise "
          "(timings are still valid; transcripts will be empty)")
    rng = np.random.default_rng(0)
    return (0.05 * rng.standard_normal(16000 * 16)).astype(np.float32)


def bench_audio(audio: np.ndarray, runs: int) -> None:
    """Time Whisper and SER across a sweep of utterance lengths."""
    from server import ser
    from server.transcriber import transcribe

    print("\n" + "=" * 74)
    print("AUDIO STAGES (CPU)")
    print("=" * 74)
    print(f"  whisper : {config.WHISPER_MODEL} "
          f"({config.WHISPER_DEVICE}, {config.WHISPER_COMPUTE_TYPE})")
    print(f"  ser     : {config.SER_MODEL if ser.available() else 'UNAVAILABLE'} "
          f"({config.SER_DEVICE})")

    # One untimed call each: the first pass pays lazy allocation and cache
    # warming that would otherwise be charged to the first measurement.
    warm = audio[: 16000 * 2]
    transcribe(warm)
    ser.analyze(warm)

    print(f"\n{'utterance':>10} | {'stage':>8} | {'mean':>7} {'median':>7} {'p95':>7} | {'RTF':>6}")
    print("-" * 74)

    for secs in LENGTHS_S:
        clip = audio[: int(16000 * secs)]
        if clip.size < 16000 * secs:
            continue  # source audio too short for this length

        for name, fn, enabled in (
            ("whisper", transcribe, True),
            ("ser", ser.analyze, ser.available()),
        ):
            if not enabled:
                continue
            times = []
            for _ in range(runs):
                start = time.perf_counter()
                fn(clip)
                times.append(time.perf_counter() - start)
            rtf = secs / statistics.mean(times)
            print(f"{secs:>9}s | {name:>8} | {_fmt(times)} | {rtf:>5.1f}x")
        print("-" * 74)

    if ser.available():
        print("RTF = audio seconds processed per wall-clock second (>1 beats real time).")
        print("SER is expected to be flat across lengths: its feature extractor pads")
        print("every input to a fixed 30s window, so short clips cost the same as long.")


def bench_pipeline(audio: np.ndarray, runs: int) -> None:
    """Measure what main.py actually does: whisper and SER gathered, not summed."""
    from server import ser
    from server.transcriber import transcribe

    if not ser.available():
        print("\n(skipping concurrency check -- SER unavailable)")
        return

    clip = audio[: 16000 * 4]
    loop = asyncio.new_event_loop()

    async def gathered() -> float:
        start = time.perf_counter()
        await asyncio.gather(
            loop.run_in_executor(None, transcribe, clip),
            loop.run_in_executor(None, ser.analyze, clip),
        )
        return time.perf_counter() - start

    seq, con = [], []
    for _ in range(runs):
        start = time.perf_counter()
        transcribe(clip)
        ser.analyze(clip)
        seq.append(time.perf_counter() - start)
        con.append(loop.run_until_complete(gathered()))
    loop.close()

    print("\n" + "=" * 74)
    print("CONCURRENCY (4s utterance: whisper + ser)")
    print("=" * 74)
    print(f"{'sequential':>12} | {_fmt(seq)}")
    print(f"{'gathered':>12} | {_fmt(con)}")
    saved = statistics.mean(seq) - statistics.mean(con)
    print(f"\ngathering saves {saved:.2f}s per utterance "
          f"({saved / statistics.mean(seq) * 100:.0f}%)")


async def bench_llm(runs: int) -> None:
    """Measure Ollama prefill and decode using the timings Ollama itself reports.

    We call the real Interpreter so the prompt (system prompt, rolling context,
    voice hint) is exactly what production sends -- a benchmark on a toy prompt
    would understate prefill cost.
    """
    import httpx

    from server.interpreter import SYSTEM_PROMPT

    print("\n" + "=" * 74)
    print("LLM (Ollama, GPU)")
    print("=" * 74)
    print(f"  model   : {config.OLLAMA_MODEL}")
    print(f"  endpoint: {config.OLLAMA_URL}")

    async with httpx.AsyncClient() as client:
        try:
            tags = await client.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
            names = [m["name"] for m in tags.json().get("models", [])]
        except httpx.HTTPError as exc:
            print(f"\n  Ollama unreachable ({exc}) -- skipping.")
            return
        if config.OLLAMA_MODEL not in names:
            print(f"\n  {config.OLLAMA_MODEL} not pulled (have: {names or 'none'}).")
            print(f"  Run: ollama pull {config.OLLAMA_MODEL}")
            return

        # A voice hint is included so the prompt matches the SER-enabled path,
        # which is the longer (and therefore slower to prefill) of the two.
        voice = "angry (conf 0.72), valence 0.21 (negative), arousal 0.65 (elevated)"

        rows = []
        for i in range(runs):
            for line in LLM_LINES:
                prompt = (
                    f"voice sounded like: {voice}\n"
                    f'Newest line, interpret only this one: "{line}"'
                )
                payload = {
                    "model": config.OLLAMA_MODEL,
                    "system": SYSTEM_PROMPT,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "keep_alive": -1,
                    "options": {
                        "temperature": config.OLLAMA_TEMPERATURE,
                        "num_predict": config.OLLAMA_NUM_PREDICT,
                    },
                }
                start = time.perf_counter()
                resp = await client.post(
                    f"{config.OLLAMA_URL}/api/generate",
                    json=payload,
                    timeout=config.OLLAMA_TIMEOUT_S,
                )
                wall = time.perf_counter() - start
                resp.raise_for_status()
                body = resp.json()

                # Ollama reports durations in nanoseconds. Guard against zeros:
                # a cached or degenerate response can report 0 and would blow up
                # the division.
                def rate(count_key: str, dur_key: str) -> float:
                    n = body.get(count_key) or 0
                    d = body.get(dur_key) or 0
                    return n / (d / 1e9) if n and d else 0.0

                rows.append({
                    "wall": wall,
                    "load": (body.get("load_duration") or 0) / 1e9,
                    "prompt_tokens": body.get("prompt_eval_count") or 0,
                    "prompt_tps": rate("prompt_eval_count", "prompt_eval_duration"),
                    "gen_tokens": body.get("eval_count") or 0,
                    "gen_tps": rate("eval_count", "eval_duration"),
                })
                if i == 0:
                    print(f"\n  [{line[:44]:<44}] -> {body['response'].strip()[:60]}")

    if not rows:
        return

    def col(k: str) -> list[float]:
        return [r[k] for r in rows]

    print("\n" + "-" * 74)
    print(f"{len(rows)} generations, {config.OLLAMA_MODEL}")
    print("-" * 74)
    print(f"{'wall clock':>18} | {_fmt(col('wall'))}")
    print(f"{'model load':>18} | {_fmt(col('load'))}   (0 = already resident)")
    print()
    print(f"{'prompt tokens':>18} | {statistics.mean(col('prompt_tokens')):8.0f} avg")
    print(f"{'prefill speed':>18} | {statistics.mean(col('prompt_tps')):8.1f} tok/s")
    print(f"{'generated tokens':>18} | {statistics.mean(col('gen_tokens')):8.0f} avg")
    print(f"{'decode speed':>18} | {statistics.mean(col('gen_tps')):8.1f} tok/s")
    print()
    print("prefill = processing the prompt (scales with context length);")
    print("decode  = generating the reply (scales with answer length).")
    print("Both come from Ollama's own eval_count/eval_duration counters.")


def verdict(audio: np.ndarray, runs: int) -> None:
    """The question that actually matters: can this keep up with live speech?

    Per-stage numbers are easy to read optimistically. What decides whether the
    app feels live is (a) how long after someone stops talking a read appears,
    and (b) whether the slowest stage can consume utterances at least as fast as
    a person produces them. (b) is the one that bites: if a stage takes longer
    than the audio it describes, a sustained conversation queues up behind it
    and the backlog grows without bound.
    """
    from server import ser
    from server.transcriber import transcribe

    if not ser.available():
        return

    clip = audio[: 16000 * 3]  # 3s: a typical conversational utterance
    ser.analyze(clip)
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        ser.analyze(clip)
        times.append(time.perf_counter() - start)
    ser_t = statistics.mean(times)

    transcribe(clip)
    start = time.perf_counter()
    transcribe(clip)
    whisper_t = time.perf_counter() - start

    print("\n" + "=" * 74)
    print("VERDICT (3s utterance -- typical conversational length)")
    print("=" * 74)
    audio_s = 3.0
    slowest = max(ser_t, whisper_t)
    print(f"  slowest audio stage : {slowest:.2f}s for {audio_s:.0f}s of speech")
    if slowest > audio_s:
        print(f"  -> SLOWER than real time. Back-to-back speech arrives faster than")
        print(f"     it can be scored, so reads fall progressively further behind.")
        print(f"     Fine for conversation with natural pauses; not for a monologue.")
        print(f"     Mitigations: SER_ENABLED=0, or raise SER_TORCH_THREADS.")
    else:
        print(f"  -> keeps up with continuous speech "
              f"({audio_s / slowest:.1f}x real time).")
    print("\n  Note this is throughput, not correctness: audio is never dropped.")
    print("  The read for a line simply arrives later under sustained speech.")


SER_CANDIDATES = [
    ("MERaLiON/MERaLiON-SER-v1", "0.8B, 7 emotions + valence/arousal/dominance"),
    ("emotion2vec/emotion2vec_plus_large", "~300M, 9 classes, categorical only"),
    ("emotion2vec/emotion2vec_plus_base", "~90M, 9 classes, categorical only"),
]


def bench_ser_models(models: list[str], runs: int) -> None:
    """A/B the SER backends on speed AND on whether they react to delivery.

    Speed alone would pick the smallest model every time. What actually matters
    is whether a model still *discriminates*: the three prosody clips are the
    same sentence said flat, bright and subdued, so a backend whose answer never
    moves across them is fast but useless here. The dimensional columns matter
    too -- valence is the axis the sarcasm rule keys on, and the emotion2vec
    models simply do not have it.

    Each model is loaded in a subprocess: ser.py loads once at import by design,
    so swapping models in-process would mean reaching past its public interface.
    """
    import json
    import subprocess
    import sys

    clips = sorted((ROOT / "experiments" / "prosody").glob("*.wav"))
    if not clips:
        print("\n(skipping SER comparison -- run experiments/make_prosody.ps1 first)")
        return

    print("\n" + "=" * 74)
    print("SER BACKENDS (same audio, same interface: ser.analyze)")
    print("=" * 74)

    notes = dict(SER_CANDIDATES)
    # Child prints one JSON line per clip so the parent stays immune to the
    # loading noise these libraries write to stdout/stderr.
    child = (
        "import json,sys,time,wave;import numpy as np;from server import ser\n"
        "out=[]\n"
        "for p in sys.argv[1:]:\n"
        "    w=wave.open(p,'rb');a=np.frombuffer(w.readframes(w.getnframes()),dtype=np.int16)\n"
        "    a=a.astype(np.float32)/32768.0;w.close()\n"
        "    ser.analyze(a)\n"
        "    ts=[]\n"
        f"    for _ in range({runs}):\n"
        "        t=time.perf_counter();r=ser.analyze(a);ts.append(time.perf_counter()-t)\n"
        "    out.append({'clip':p.replace(chr(92),'/').split('/')[-1],'t':min(ts),'r':r})\n"
        "print('@@@'+json.dumps({'ok':ser.available(),'rows':out}))\n"
    )

    for model in models:
        # Announce before loading: emotion2vec_plus_large takes minutes to pull
        # and initialise, and a silent terminal looks identical to a hang.
        print(f"\n  loading {model.split('/')[-1]} ...", flush=True)
        env = {"SER_MODEL": model, "HF_HUB_DISABLE_SYMLINKS": "1",
               "HF_HUB_DISABLE_SYMLINKS_WARNING": "1", "PYTHONIOENCODING": "utf-8"}
        import os
        proc = subprocess.run(
            [sys.executable, "-c", child, *[str(c) for c in clips]],
            cwd=str(ROOT), capture_output=True, text=True,
            env={**os.environ, **env}, timeout=1800,
        )
        line = next((l for l in proc.stdout.splitlines() if l.startswith("@@@")), None)
        short = model.split("/")[-1]
        if not line:
            print(f"\n  {short:<28} FAILED to load or run")
            continue
        data = json.loads(line[3:])
        if not data["ok"]:
            print(f"\n  {short:<28} UNAVAILABLE")
            continue

        print(f"\n  {short}  ({notes.get(model, '')})")
        print(f"    {'clip':<12}{'time':>8}  {'emotion':>10}{'conf':>7}"
              f"{'valence':>9}{'arousal':>9}")
        for row in data["rows"]:
            r = row["r"] or {}
            def fmt(key: str) -> str:
                v = r.get(key)
                return f"{v:9.2f}" if isinstance(v, (int, float)) else f"{'--':>9}"
            print(f"    {row['clip']:<12}{row['t']:>7.2f}s  {str(r.get('emotion')):>10}"
                  f"{r.get('confidence', 0):>7.2f}{fmt('valence')}{fmt('arousal')}")

    print("\n  '--' means the model has no dimensional head. valence is the axis the")
    print("  interpreter's sarcasm rule keys on, so losing it is the real cost of the")
    print("  lighter backends -- not just a missing column.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=3, help="repetitions (default 3)")
    ap.add_argument("--skip-llm", action="store_true", help="audio stages only")
    ap.add_argument("--skip-audio", action="store_true", help="LLM only")
    ap.add_argument("--ser-models", nargs="*",
                    help="A/B these SER models instead of the normal benchmark")
    args = ap.parse_args()

    if args.ser_models is not None:
        models = args.ser_models or [m for m, _ in SER_CANDIDATES]
        bench_ser_models(models, args.runs)
        return

    print("=" * 74)
    print("emtext model benchmark")
    print("=" * 74)

    if not args.skip_audio:
        audio = _load_audio()
        bench_audio(audio, args.runs)
        bench_pipeline(audio, args.runs)

    if not args.skip_llm:
        asyncio.run(bench_llm(args.runs))

    if not args.skip_audio:
        verdict(audio, args.runs)


if __name__ == "__main__":
    main()
