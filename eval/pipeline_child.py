"""CPU half of the pipeline eval: Segmenter -> Whisper + SER, one clip at a time.

Run as a subprocess by `eval/pipeline_eval.py`, never directly by a human:

    SER_MODEL=... WHISPER_MODEL=... python -u -m eval.pipeline_child <repo_root>

Why a subprocess at all
-----------------------
`server/transcriber.py` and `server/ser.py` both load their model at *import*.
That is deliberate (the server pays the cost once at startup, not on the first
utterance), but it means a model cannot be swapped inside a live process. To
evaluate more than one SER backend we therefore need one process per backend,
configured by env vars before import.

It also buys real concurrency: this process does CPU work in its own OS process
while the parent awaits the GPU on the previous clip, with no GIL contention.

Protocol (newline-delimited JSON on stdout)
-------------------------------------------
1. One handshake line: {"event":"loaded", "ok":bool, "backend":str, ...}
2. Then, for each clip path read from stdin, one {"event":"row", ...} line.

stdout is JSON only; the model libraries are extremely chatty, so the parent
sends our stderr to DEVNULL and we never print anything unstructured.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _emit(obj: dict) -> None:
    """One JSON object per line, flushed, so the parent sees it immediately."""
    print(json.dumps(obj), flush=True)


def main() -> None:
    # The parent passes the repo root so `server` and `eval` are importable
    # regardless of the working directory it was launched from.
    if len(sys.argv) > 1:
        sys.path.insert(0, sys.argv[1])

    import numpy as np

    from eval.ser_eval import read_wav_16k
    from server import config, ser
    from server.segmenter import Segmenter
    from server.transcriber import transcribe

    _emit({
        "event": "loaded",
        "ok": True,
        "ser_available": ser.available(),
        "backend": getattr(ser, "_backend", None),
        "ser_model": config.SER_MODEL if ser.available() else None,
        "whisper_model": config.WHISPER_MODEL,
    })

    # Two workers so transcribe() and analyze() overlap exactly as they do in
    # main.py's asyncio.gather(run_in_executor(...), run_in_executor(...)).
    # Both release the GIL inside their native inference, so this is real
    # parallelism, not just interleaving.
    pool = ThreadPoolExecutor(max_workers=2)

    for line in sys.stdin:
        path_s = line.strip()
        if not path_s:
            continue
        path = Path(path_s)
        row: dict = {"event": "row", "file": path.name}

        try:
            audio = read_wav_16k(path)
            original_ms = round(1000 * audio.size / config.SAMPLE_RATE, 1)

            # --- VAD -------------------------------------------------------
            # Run the app's real first stage rather than skipping it, so the
            # CSV can show whether the energy VAD (tuned for a live mic) copes
            # with studio recordings -- quiet emotions like sad and fearful are
            # the ones at risk of never crossing SPEECH_RMS.
            t0 = time.perf_counter()
            segmenter = Segmenter()
            pcm_bytes = (audio * 32768.0).astype(np.int16).tobytes()
            utterances = list(segmenter.feed(pcm_bytes))
            tail = segmenter.flush()
            if tail is not None:
                utterances.append(tail)
            vad_s = time.perf_counter() - t0

            row["vad_segments_n"] = len(utterances)
            row["vad_original_ms"] = original_ms
            row["vad_s"] = round(vad_s, 4)

            if utterances:
                # Longest segment: a clip occasionally splits at a pause, and
                # the longest piece is the one carrying the sentence.
                speech = max(utterances, key=lambda a: a.size)
                row["vad_dropped"] = 0
            else:
                # The VAD found nothing. Fall back to the raw clip so the
                # downstream stages still produce data -- a dropped row would
                # silently shrink the sample for exactly the quiet emotions we
                # most need to measure. The flag makes the fallback visible.
                speech = audio
                row["vad_dropped"] = 1
            row["vad_utterance_ms"] = round(1000 * speech.size / config.SAMPLE_RATE, 1)

            # --- Whisper + SER, concurrently --------------------------------
            # Each worker times ITSELF. Timing around .result() instead would
            # measure "submitted until collected", which folds queue wait into
            # whichever future is collected first and undercounts the second --
            # SER would look free simply because Whisper was awaited first.
            def timed(fn, arg):
                start = time.perf_counter()
                value = fn(arg)
                return value, time.perf_counter() - start

            t0 = time.perf_counter()
            fut_asr = pool.submit(timed, transcribe, speech)
            fut_ser = pool.submit(timed, ser.analyze, speech)

            transcript, asr_s = fut_asr.result()
            voice, ser_s = fut_ser.result()
            row["whisper_latency_s"] = round(asr_s, 3)
            row["ser_latency_s"] = round(ser_s, 3)
            # Wall time for both together. This is the number that matters --
            # it is what main.py actually costs per utterance, and it should be
            # close to max(whisper, ser) rather than their sum.
            row["cpu_stage_s"] = round(time.perf_counter() - t0, 3)

            row["transcript"] = transcript
            row["voice"] = voice
            row["error"] = ""
        except Exception as exc:  # noqa: BLE001
            # One bad clip must not kill the run. Report it as a row so the
            # parent still records it and resume does not retry forever.
            row["error"] = f"{type(exc).__name__}: {exc}"
            row.setdefault("transcript", "")
            row.setdefault("voice", None)

        _emit(row)

    pool.shutdown(wait=False)


if __name__ == "__main__":
    main()
