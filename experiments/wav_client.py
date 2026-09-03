"""Headless test client: streams a WAV file over the /stream wire protocol.

Exercises the exact contract the browser page (and the future ESP32) uses, so
the pipeline can be tested end to end with no microphone and no browser:

    TEXT token  ->  BINARY PCM 16 kHz mono int16 LE  ->  JSON frames back

Frames are paced in real time (a 30 ms chunk every 30 ms) because the segmenter
measures silence in wall-clock-shaped frame counts; blasting the file at full
speed would still segment correctly, but pacing keeps the run representative of
a live mic.

Usage:  python experiments/wav_client.py [wav] [--url ws://...] [--token dev]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave

import websockets

CHUNK_MS = 30


async def run(path: str, url: str, token: str, realtime: bool) -> int:
    with wave.open(path, "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
            print(
                f"error: {path} is {w.getnchannels()}ch/"
                f"{w.getsampwidth() * 8}bit/{w.getframerate()}Hz; "
                "the protocol requires mono/16-bit/16000Hz",
                file=sys.stderr,
            )
            return 2
        pcm = w.readframes(w.getnframes())

    chunk = int(16000 * CHUNK_MS / 1000) * 2  # bytes per 30 ms frame
    reads: dict[int, dict] = {}

    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(token)  # handshake: first frame is TEXT

        async def receive() -> None:
            """Print server frames as they arrive, concurrently with sending."""
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "utterance":
                    print(f"\n[{msg['id']}] {msg['transcript']}")
                elif kind == "read":
                    reads[msg["id"]] = msg
                    # Reads arrive out of order -- one asyncio task per
                    # utterance -- so echo the id to keep them correlated.
                    print(f"    -> [{msg['id']}] ({msg['tone']}) {msg['read']}")
                    # "voice" is optional: present only when SER is available.
                    v = msg.get("voice")
                    if v:
                        # valence/arousal are null on the emotion2vec backend
                        # (no dimensional head), so omit rather than print
                        # "None" -- same rule the web client follows.
                        bits = [str(v["emotion"])] if v.get("emotion") else []
                        bits += [
                            f"{k} {v[k]:.2f}"
                            for k in ("valence", "arousal")
                            if isinstance(v.get(k), (int, float))
                        ]
                        if bits:
                            # ASCII separator on purpose: the Windows console
                            # defaults to cp1252 and mangles a middle dot.
                            print(f"       voice: {', '.join(bits)}")
                elif kind == "ready":
                    print("ready")

        rx = asyncio.create_task(receive())
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i : i + chunk])
            if realtime:
                await asyncio.sleep(CHUNK_MS / 1000)

        # Trailing silence so the segmenter closes the final utterance, then
        # time for the last transcribe+interpret round trip to come back.
        await ws.send(b"\x00" * chunk * 40)
        try:
            await asyncio.wait_for(asyncio.shield(rx), timeout=60)
        except asyncio.TimeoutError:
            pass
        rx.cancel()

    offline = [r for r in reads.values() if r["read"] == "(interpreter offline)"]
    if offline:
        print(f"\nwarning: {len(offline)} read(s) came back '(interpreter offline)' "
              "-- is Ollama up and the model pulled?", file=sys.stderr)
    print(f"\ndone: {len(reads)} utterance(s) interpreted")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("wav", nargs="?", default="experiments/speech.wav")
    p.add_argument("--url", default="ws://localhost:8000/stream")
    p.add_argument("--token", default="dev")
    p.add_argument("--fast", action="store_true", help="send as fast as possible")
    a = p.parse_args()
    raise SystemExit(asyncio.run(run(a.wav, a.url, a.token, realtime=not a.fast)))


if __name__ == "__main__":
    main()
