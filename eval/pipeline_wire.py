"""Cross-check the in-process pipeline eval against the REAL wire protocol.

    python -m eval.pipeline_wire                  # 40 clips (server must be up)
    python -m eval.pipeline_wire --limit 8        # quicker

`pipeline_eval.py` calls `transcribe`/`ser.analyze`/`Interpreter` directly. That
is the right way to get per-stage timings and a resumable matrix, but it does
bypass the thing an ESP32 will actually speak: the websocket contract. This
script closes that gap on a small sample -- if the two disagree, the in-process
numbers are measuring something the real client never experiences.

Deliberately kept small and separate. Audio is paced in real time (the VAD
measures end-of-speech in frames), so every clip costs ~4 s of wall clock no
matter how fast the models are. Forty clips is a spot check, not a second matrix.

One fresh connection per clip, rather than one long stream: `main.py` documents
that reads can arrive out of order across utterances, and a per-clip connection
removes that ambiguity from what is meant to be a smoke test.

Limitation worth knowing: this cannot introspect which SER_MODEL / OLLAMA_MODEL
the running server was started with. For an apples-to-apples comparison against
a specific `pipeline.csv` cell, start the server with matching env vars first.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from pathlib import Path

import numpy as np
import websockets

from eval.asr_eval import wer
from eval.ser_eval import load_clips, read_wav_16k
from server import config

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
CSV_PATH = RESULTS / "pipeline_wire.csv"
CHUNK_MS = 30

COLUMNS = [
    "file", "actor", "gender", "true_emotion", "intensity", "reference_text",
    "wire_transcript", "wer_errors", "wer_ref_words", "wer_pct",
    "wire_tone", "wire_read",
    "voice_emotion", "voice_valence", "voice_arousal",
    "stop_to_read_s", "error",
]


async def one_clip(url: str, token: str, path: Path) -> dict:
    """Stream one clip and collect the frames the server sends back."""
    audio = read_wav_16k(path)
    pcm = (audio * 32768.0).astype(np.int16).tobytes()
    chunk = int(config.SAMPLE_RATE * CHUNK_MS / 1000) * 2

    out: dict = {"wire_transcript": "", "wire_tone": "", "wire_read": "",
                 "voice_emotion": "", "voice_valence": "", "voice_arousal": "",
                 "stop_to_read_s": "", "error": ""}

    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(token)                      # handshake: TEXT token first
        done = asyncio.get_running_loop().create_future()

        async def receive() -> None:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "utterance":
                    out["wire_transcript"] = msg.get("transcript", "")
                elif msg.get("type") == "read":
                    out["wire_tone"] = msg.get("tone", "")
                    out["wire_read"] = msg.get("read", "")
                    voice = msg.get("voice") or {}
                    out["voice_emotion"] = voice.get("emotion") or ""
                    # valence/arousal are null on the emotion2vec backend.
                    for key in ("valence", "arousal"):
                        value = voice.get(key)
                        out[f"voice_{key}"] = value if isinstance(value, (int, float)) else ""
                    if not done.done():
                        done.set_result(time.perf_counter())

        rx = asyncio.create_task(receive())
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i : i + chunk])
            await asyncio.sleep(CHUNK_MS / 1000)   # real-time pacing; the VAD needs it

        # Clock starts here: everything after is the system thinking, which is
        # what a user actually feels.
        spoke_at = time.perf_counter()
        for _ in range(60):                        # ~1.8s, over END_SILENCE_MS
            await ws.send(b"\x00" * chunk)
            await asyncio.sleep(CHUNK_MS / 1000)

        try:
            got = await asyncio.wait_for(done, timeout=90)
            out["stop_to_read_s"] = round(got - spoke_at, 2)
        except asyncio.TimeoutError:
            out["error"] = "timed out waiting for a read frame"
        rx.cancel()
    return out


async def main_async(args) -> None:
    clips = load_clips(args.limit, args.seed, False)
    RESULTS.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("WIRE CROSS-CHECK (real websocket, real protocol)")
    print("=" * 78)
    print(f"  url   : {args.url}")
    print(f"  clips : {len(clips)} (paced in real time, so ~4s each regardless of models)")
    print(f"  csv   : {CSV_PATH}")
    print("\n  The server's own SER/LLM config decides these results -- start it with")
    print("  the env vars matching whichever pipeline.csv cell you are comparing to.\n")

    rows = []
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()

        for i, (path, meta) in enumerate(clips, 1):
            try:
                got = await one_clip(args.url, args.token, path)
            except OSError as exc:
                raise SystemExit(f"cannot reach {args.url}: {exc}\nIs the server running?")

            reference = meta["statement"]
            errors, words = wer(reference, got["wire_transcript"])
            row = {
                "file": path.name, "actor": meta["actor"], "gender": meta["gender"],
                "true_emotion": meta["true_emotion"], "intensity": meta["intensity"],
                "reference_text": reference,
                "wer_errors": errors, "wer_ref_words": words,
                "wer_pct": round(100 * errors / words, 1) if words else "",
                **got,
            }
            writer.writerow(row)
            fh.flush()
            rows.append(row)

            print(f"  [{i:>3}/{len(clips)}] {path.name:<30} {meta['true_emotion']:<10} "
                  f"tone={row['wire_tone']:<9} voice={row['voice_emotion'] or '-':<10} "
                  f"wer {row['wer_pct']}%  {row['stop_to_read_s']}s"
                  f"{'  ' + row['error'] if row['error'] else ''}", flush=True)

    scored = [r for r in rows if r["stop_to_read_s"] != ""]
    if scored:
        lat = sorted(r["stop_to_read_s"] for r in scored)
        errs = sum(r["wer_errors"] for r in rows)
        words = sum(r["wer_ref_words"] for r in rows)
        print(f"\n  {len(scored)}/{len(rows)} clips got a read")
        print(f"  stop->read : {sum(lat) / len(lat):.2f}s mean, "
              f"{lat[len(lat) // 2]:.2f}s median, {lat[-1]:.2f}s max")
        print(f"  WER        : {100 * errs / words:.1f}%" if words else "")
        got_voice = sum(1 for r in rows if r["voice_emotion"])
        print(f"  voice field present on {got_voice}/{len(rows)} reads")
    print(f"\nrows written to {CSV_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--url", default=f"ws://localhost:{config.PORT}/stream")
    ap.add_argument("--token", default="dev")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
