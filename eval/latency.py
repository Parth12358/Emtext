"""End-to-end latency: how long after someone stops talking does a read appear?

    python -m eval.latency                      # full run
    python -m eval.latency --runs 5             # more samples
    python -m eval.latency --model qwen3:8b     # in-process stages only
    python -m eval.latency --skip-wire          # no server needed

Every model is warmed up BEFORE any timer starts (see `warmup`), so none of the
numbers include weight loading, VRAM transfer, lazy allocation or first-call
JIT. Those costs are real but they are paid once at startup, not per utterance,
and folding them in would badly misrepresent steady-state behaviour.

Two measurements, because they answer different questions:

  1. STAGE BREAKDOWN (in-process) -- calls transcribe / ser.analyze / interpret
     directly to attribute time to each stage. Tells you *where* the time goes
     and what to optimise.

  2. WIRE END-TO-END (over the real websocket) -- streams paced audio to a
     running server and measures from the last sample of speech sent to the
     `read` frame arriving. This is the number a user actually feels: "I stopped
     talking; how long until the read shows up?" It includes everything the
     stage breakdown does, plus the VAD's end-of-speech wait, websocket
     framing, JSON, and scheduling.

The wire number is necessarily LARGER than the sum of the stages, and the gap is
mostly `END_SILENCE_MS` -- the segmenter cannot know a sentence has finished
until it has heard enough trailing quiet. That wait is a tuning decision, not
overhead, so it is reported as its own line rather than hidden.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import websockets

from server import config

ROOT = Path(__file__).resolve().parent.parent
CHUNK_MS = 30


def _stats(values: list[float]) -> str:
    if not values:
        return "     n/a"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return (f"{statistics.mean(values):7.2f}s {statistics.median(values):7.2f}s "
            f"{p95:7.2f}s {max(values):7.2f}s")


def _trim_trailing_silence(audio: np.ndarray) -> np.ndarray:
    """Cut everything after the last voiced frame.

    This matters more than it looks. The wire measurement starts its clock at
    "the last speech sample was sent", but SAPI writes several seconds of
    trailing silence into every file. Sending that silence unmodified lets the
    server's VAD close the utterance and start working *before* the clock
    starts, which produced a wire latency lower than the compute it contains --
    a physically impossible result that is the giveaway for this bug.

    Uses the same frame size and RMS threshold as the real segmenter, so "where
    speech ends" means the same thing here as it does in production.
    """
    frame = int(config.SAMPLE_RATE * config.FRAME_MS / 1000)
    usable = (audio.size // frame) * frame
    if usable == 0:
        return audio
    frames = audio[:usable].reshape(-1, frame)
    # int16-scale RMS per frame, to compare against SPEECH_RMS directly.
    rms = np.sqrt(np.mean((frames * 32768.0) ** 2, axis=1))
    voiced = np.flatnonzero(rms >= config.SPEECH_RMS)
    if voiced.size == 0:
        return audio
    return audio[: (int(voiced[-1]) + 1) * frame]


def _load_wavs() -> list[tuple[str, np.ndarray]]:
    """Single-utterance WAVs, so one file == one measurable call."""
    candidates = [
        ROOT / "experiments" / "prosody" / "flat.wav",
        ROOT / "experiments" / "prosody" / "up.wav",
        ROOT / "experiments" / "prosody" / "down.wav",
    ]
    out = []
    for path in candidates:
        if not path.exists():
            continue
        with wave.open(str(path), "rb") as w:
            if w.getframerate() != config.SAMPLE_RATE or w.getnchannels() != 1:
                continue
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        audio = _trim_trailing_silence(pcm.astype(np.float32) / 32768.0)
        out.append((path.name, audio))
    if not out:
        raise SystemExit(
            "no single-utterance WAVs found. Generate them first:\n"
            "  powershell -NoProfile -ExecutionPolicy Bypass -File "
            "experiments\\make_prosody.ps1"
        )
    return out


async def warmup(model: str, audio: np.ndarray) -> None:
    """Load and exercise every model once, untimed.

    This is the whole point of the `--warm` contract: a cold 12B costs ~8s to
    page into VRAM and Whisper/SER pay first-call allocation. Those belong to
    startup, not to per-utterance latency, so they are burned off here where no
    timer is running. `keep_alive: -1` then pins the LLM resident so it cannot
    be evicted between samples and silently reintroduce a load cost mid-run.
    """
    from server import ser
    from server.transcriber import transcribe

    print("warming up (untimed)...")

    clip = audio[: config.SAMPLE_RATE * 2]
    t0 = time.perf_counter()
    transcribe(clip)
    print(f"  whisper  {config.WHISPER_MODEL:<28} {time.perf_counter() - t0:5.1f}s")

    if ser.available():
        t0 = time.perf_counter()
        ser.analyze(clip)
        print(f"  ser      {config.SER_MODEL.split('/')[-1]:<28} "
              f"{time.perf_counter() - t0:5.1f}s")
    else:
        print("  ser      UNAVAILABLE -- voice stage will be skipped")

    async with httpx.AsyncClient() as client:
        t0 = time.perf_counter()
        await client.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": "hi", "stream": False, "keep_alive": -1},
            timeout=300,
        )
        print(f"  llm      {model:<28} {time.perf_counter() - t0:5.1f}s  (now resident)")
    print()


async def stage_breakdown(model: str, wavs, runs: int) -> dict[str, list[float]]:
    """Time each stage directly, attributing cost rather than just totalling it."""
    from server import ser
    from server.interpreter import Interpreter
    from server.transcriber import transcribe

    original = config.OLLAMA_MODEL
    config.OLLAMA_MODEL = model
    timings: dict[str, list[float]] = {
        "whisper": [], "ser": [], "audio_gathered": [], "llm": [], "total": []
    }

    try:
        async with httpx.AsyncClient() as client:
            loop = asyncio.get_running_loop()
            for _ in range(runs):
                for _name, audio in wavs:
                    start = time.perf_counter()

                    # Mirror main.py exactly: the two audio stages gathered, not
                    # sequenced. Timing them separately as well would change the
                    # thing being measured, so per-stage numbers come from the
                    # dedicated calls below.
                    t0 = time.perf_counter()
                    transcript, voice = await asyncio.gather(
                        loop.run_in_executor(None, transcribe, audio),
                        loop.run_in_executor(None, ser.analyze, audio),
                    )
                    timings["audio_gathered"].append(time.perf_counter() - t0)

                    t0 = time.perf_counter()
                    await Interpreter(client).interpret(transcript or "Hello.", voice)
                    timings["llm"].append(time.perf_counter() - t0)

                    timings["total"].append(time.perf_counter() - start)

                    # Isolated stage costs, measured outside the gather.
                    t0 = time.perf_counter()
                    transcribe(audio)
                    timings["whisper"].append(time.perf_counter() - t0)
                    if ser.available():
                        t0 = time.perf_counter()
                        ser.analyze(audio)
                        timings["ser"].append(time.perf_counter() - t0)
    finally:
        config.OLLAMA_MODEL = original
    return timings


async def wire_latency(url: str, token: str, wavs, runs: int) -> list[float]:
    """Measure what the user feels: last word spoken -> read frame in hand.

    Audio is paced in real time (a 30 ms frame every 30 ms), because the VAD
    measures end-of-speech in frames and blasting the file would collapse that
    wait into nothing and flatter the result.
    """
    samples: list[float] = []
    chunk = int(config.SAMPLE_RATE * CHUNK_MS / 1000) * 2

    for _ in range(runs):
        for _name, audio in wavs:
            pcm = (audio * 32768.0).astype(np.int16).tobytes()
            try:
                async with websockets.connect(url, max_size=None) as ws:
                    await ws.send(token)
                    done = asyncio.get_running_loop().create_future()

                    async def receive() -> None:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get("type") == "read" and not done.done():
                                done.set_result(time.perf_counter())

                    rx = asyncio.create_task(receive())

                    for i in range(0, len(pcm), chunk):
                        await ws.send(pcm[i : i + chunk])
                        await asyncio.sleep(CHUNK_MS / 1000)

                    # Speech is now fully delivered. The clock for "how long
                    # until I see a read" starts HERE -- everything after this is
                    # the system thinking, including the VAD's silence wait.
                    spoke_at = time.perf_counter()

                    # Trailing silence so the segmenter can close the utterance.
                    for _ in range(60):  # ~1.8s, comfortably over END_SILENCE_MS
                        await ws.send(b"\x00" * chunk)
                        await asyncio.sleep(CHUNK_MS / 1000)

                    try:
                        got = await asyncio.wait_for(done, timeout=60)
                        samples.append(got - spoke_at)
                    except asyncio.TimeoutError:
                        print("  (timed out waiting for a read frame)")
                    rx.cancel()
            except OSError as exc:
                raise SystemExit(f"cannot reach {url}: {exc}\nIs the server running?")
    return samples


async def main_async(args: argparse.Namespace) -> None:
    wavs = _load_wavs()
    model = args.model or config.OLLAMA_MODEL

    print("=" * 70)
    print("emtext end-to-end latency")
    print("=" * 70)
    print(f"  llm     : {model}")
    print(f"  whisper : {config.WHISPER_MODEL} ({config.WHISPER_DEVICE})")
    print(f"  ser     : {config.SER_MODEL if config.SER_ENABLED else 'disabled'}")
    print(f"  samples : {len(wavs)} utterance(s) x {args.runs} run(s)")
    for name, audio in wavs:
        print(f"            {name:<12} {audio.size / config.SAMPLE_RATE:5.2f}s speech "
              f"(trailing silence trimmed)")
    print()

    await warmup(model, wavs[0][1])

    print("=" * 70)
    print("STAGE BREAKDOWN (in-process, models warm)")
    print("=" * 70)
    t = await stage_breakdown(model, wavs, args.runs)
    print(f"{'stage':<20}{'mean':>8}{'median':>8}{'p95':>8}{'max':>8}")
    print("-" * 52)
    for key, label in (
        ("whisper", "whisper (alone)"),
        ("ser", "ser (alone)"),
        ("audio_gathered", "whisper+ser gathered"),
        ("llm", "llm interpret"),
        ("total", "TOTAL per utterance"),
    ):
        if t[key]:
            print(f"{label:<20}{_stats(t[key])}")

    if t["whisper"] and t["ser"]:
        seq = statistics.mean(t["whisper"]) + statistics.mean(t["ser"])
        gathered = statistics.mean(t["audio_gathered"])
        print(f"\ngathering the audio stages saves {seq - gathered:.2f}s "
              f"({(seq - gathered) / seq * 100:.0f}%) vs running them in sequence")

    if args.skip_wire:
        return

    print("\n" + "=" * 70)
    print("WIRE END-TO-END (real websocket: stopped speaking -> read in hand)")
    print("=" * 70)
    samples = await wire_latency(args.url, args.token, wavs, args.runs)
    if not samples:
        return
    print(f"{'measurement':<20}{'mean':>8}{'median':>8}{'p95':>8}{'max':>8}")
    print("-" * 52)
    print(f"{'stop -> read':<20}{_stats(samples)}")

    vad = config.END_SILENCE_MS / 1000
    wire = statistics.mean(samples)
    print("\n  budget:")
    print(f"    {vad:5.2f}s  VAD end-of-speech wait (END_SILENCE_MS) -- pure waiting")
    print(f"    {wire - vad:5.2f}s  everything after: compute + transport")
    print(f"    {wire:5.2f}s  total, stopped speaking -> read in hand")
    print()
    print(f"  The in-process stage total ({statistics.mean(t['total']):.2f}s) is measured on")
    print("  the whole clip; the server only ever sees the voiced audio the segmenter")
    print("  emits, so the two are close but not identical.")
    print()
    print(f"  END_SILENCE_MS ({config.END_SILENCE_MS} ms) is the one part of this budget")
    print("  that is a tuning choice rather than work. Lower it for a snappier read, at")
    print("  the cost of splitting sentences at pauses. See 'Tuning the VAD'.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", help=f"LLM to test (default {config.OLLAMA_MODEL})")
    ap.add_argument("--url", default=f"ws://localhost:{config.PORT}/stream")
    ap.add_argument("--token", default="dev")
    ap.add_argument("--skip-wire", action="store_true",
                    help="stage breakdown only; no running server needed")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
