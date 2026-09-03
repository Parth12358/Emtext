"""Compare faster-whisper models on accuracy (WER) and CPU cost.

    python -m eval.asr_eval                          # the default lineup
    python -m eval.asr_eval --models tiny base       # just these

Answers the practical question behind `WHISPER_MODEL`: is `small` worth its
extra CPU time, and is `tiny` too lossy to live with? Both halves matter here --
transcription accuracy sets a ceiling on how good the interpreter's read can
possibly be, since a misheard word is a misread line.

Ground truth comes from `experiments/asr/*.txt`, written by `make_asr_set.ps1`
at the same time as the audio, so the reference is exactly what was spoken.

Two caveats on reading these numbers:

  - The audio is Windows SAPI: clean, consistent, no background noise and no
    accent. Real WER over a live microphone in a real room will be worse for
    every model, and probably worse by *different* amounts. Treat this as a
    relative ranking, not an absolute accuracy figure.
  - WER is computed on normalised text (lowercased, punctuation stripped),
    because the interpreter reads meaning and does not care about a missing
    comma. A model penalised for punctuation would rank misleadingly low.
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import time
import wave
from pathlib import Path

import numpy as np

from server import config

ROOT = Path(__file__).resolve().parent.parent
ASR_DIR = ROOT / "experiments" / "asr"

DEFAULT_MODELS = [
    ("tiny", "~39M  -- when CPU time is the problem"),
    ("base", "~74M  -- the current default"),
    ("small", "~244M -- when accuracy is the problem"),
    ("distil-small.en", "~166M -- distilled, English-only"),
]


def normalise(text: str) -> list[str]:
    """Lowercase, strip punctuation, collapse whitespace -> word list."""
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return text.split()


def wer(reference: str, hypothesis: str) -> tuple[int, int]:
    """Levenshtein distance over words -> (errors, reference length).

    Returned as a pair rather than a ratio so the caller can pool errors across
    the whole set before dividing. Averaging per-utterance WERs would weight a
    four-word sentence the same as a fourteen-word one.
    """
    ref, hyp = normalise(reference), normalise(hypothesis)
    if not ref:
        return 0, 0
    # Standard DP edit distance; the table is tiny at utterance scale.
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1], len(ref)


def load_set() -> list[tuple[str, np.ndarray, str]]:
    if not ASR_DIR.exists():
        raise SystemExit(
            "no ASR set found. Generate it first:\n"
            "  powershell -NoProfile -ExecutionPolicy Bypass -File "
            "experiments\\make_asr_set.ps1"
        )
    items = []
    for wav_path in sorted(ASR_DIR.glob("*.wav")):
        txt_path = wav_path.with_suffix(".txt")
        if not txt_path.exists():
            continue
        with wave.open(str(wav_path), "rb") as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        items.append((
            wav_path.stem,
            pcm.astype(np.float32) / 32768.0,
            txt_path.read_text(encoding="utf-8-sig").strip(),
        ))
    if not items:
        raise SystemExit(f"no .wav/.txt pairs in {ASR_DIR}")
    return items


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", nargs="*", help="override the model list")
    ap.add_argument("--csv", default="eval/asr_results.csv",
                    help="per-utterance CSV output path")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the per-utterance progress lines")
    args = ap.parse_args()

    from faster_whisper import WhisperModel

    items = load_set()
    total_audio = sum(a.size for _, a, _ in items) / config.SAMPLE_RATE
    print(f"{len(items)} utterances, {total_audio:.1f}s of audio, "
          f"compute_type={config.WHISPER_COMPUTE_TYPE} on {config.WHISPER_DEVICE}")
    print("(Windows SAPI speech: clean and accent-free -- a relative ranking, "
          "not real-world WER)\n")

    wanted = args.models or [m for m, _ in DEFAULT_MODELS]
    notes = dict(DEFAULT_MODELS)
    rows = []
    detail_rows: list[dict] = []   # one per (model, utterance), written to CSV

    for name in wanted:
        try:
            load0 = time.perf_counter()
            model = WhisperModel(name, device=config.WHISPER_DEVICE,
                                 compute_type=config.WHISPER_COMPUTE_TYPE)
            load_s = time.perf_counter() - load0
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:<18} FAILED to load: {type(exc).__name__}: {exc}")
            continue

        # Untimed warm-up: the first decode pays one-off setup that would
        # otherwise be charged to whichever utterance happened to go first.
        segments, _ = model.transcribe(items[0][1], beam_size=1,
                                       language=config.WHISPER_LANGUAGE,
                                       vad_filter=True,
                                       condition_on_previous_text=False)
        list(segments)

        print(f"\n  {name}  (loaded in {load_s:.1f}s)", flush=True)

        errors = length = 0
        times = []
        # Seeded below zero so the first utterance always wins; seeding at 1.0
        # meant only a total miss could ever replace it, and the slot stayed empty.
        worst = ("", "", -1.0)
        for stem, audio, reference in items:
            t0 = time.perf_counter()
            segments, _ = model.transcribe(audio, beam_size=1,
                                           language=config.WHISPER_LANGUAGE,
                                           vad_filter=True,
                                           condition_on_previous_text=False)
            hypothesis = " ".join(s.text.strip() for s in segments).strip()
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
            e, n = wer(reference, hypothesis)
            errors += e
            length += n
            if n and e / n >= worst[2]:
                worst = (reference, hypothesis, e / n)

            detail_rows.append({
                "model": name, "utterance": stem,
                "reference": reference, "hypothesis": hypothesis,
                "errors": e, "ref_words": n,
                "wer_pct": round(100 * e / n, 1) if n else 0.0,
                "elapsed_s": round(elapsed, 3),
                "duration_s": round(audio.size / config.SAMPLE_RATE, 2),
            })

            if not args.quiet:
                flag = "   " if e == 0 else f"{e:>2}e"
                print(f"    {flag} {stem}  {elapsed:.2f}s  {hypothesis[:52]}",
                      flush=True)

        rows.append({
            "name": name,
            "wer": 100 * errors / length if length else 0.0,
            "mean": statistics.mean(times),
            "rtf": total_audio / sum(times),
            "load": load_s,
            "worst": worst,
        })
        del model  # release before loading the next one

    # Written before the summary so a crash mid-report still leaves the data.
    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        cols = ["model", "utterance", "reference", "hypothesis", "errors",
                "ref_words", "wer_pct", "elapsed_s", "duration_s"]
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        writer.writerows(detail_rows)

    print("\n" + "=" * 78)
    print(f"{'model':<18}{'WER':>7}{'mean/utt':>10}{'RTF':>8}{'load':>8}   note")
    print("-" * 78)
    for r in rows:
        print(f"{r['name']:<18}{r['wer']:>6.1f}%{r['mean']:>9.2f}s"
              f"{r['rtf']:>7.0f}x{r['load']:>7.1f}s   {notes.get(r['name'], '')}")

    print("\nWER = word error rate on normalised text (lower is better).")
    print("RTF = audio seconds transcribed per wall-clock second (higher is better).")
    print("\nNote: much of the residual WER here is numeral formatting -- every model")
    print("writes \"four fifteen\" as \"4.15\" -- which scores as errors but changes")
    print("nothing for the interpreter. Semantic accuracy is better than these figures")
    print("suggest, and the inflation applies equally to every model.")

    if rows:
        best = min(rows, key=lambda r: r["wer"])
        print(f"\nWorst single utterance for {best['name']} (best WER of the set):")
        print(f"  said : {best['worst'][0]}")
        print(f"  heard: {best['worst'][1]}")

    print(f"\nper-utterance rows written to {csv_path}")


if __name__ == "__main__":
    main()
