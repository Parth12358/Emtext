"""Score SER backends against RAVDESS -- real acted emotional speech, real labels.

    python -m eval.ser_eval                          # default backend, 160 clips
    python -m eval.ser_eval --limit 0                # the whole set (slow!)
    python -m eval.ser_eval --models MERaLiON/MERaLiON-SER-v1 emotion2vec/emotion2vec_plus_base
    python -m eval.ser_eval --csv out.csv            # where to write per-clip rows

Why this exists
---------------
Every other SER check in this repo uses Windows SAPI speech, which is
deliberately flat and carries no real emotion. That is fine for testing plumbing
and useless for testing accuracy. RAVDESS gives 1440 clips of 24 actors speaking
two fixed sentences in eight emotions, so for the first time we can ask whether
the voice signal is actually *right* rather than merely present.

The fixed sentences are the point. Both are semantically neutral -- "Kids are
talking by the door" and "Dogs are sitting by the door" -- so the words carry no
emotion at all and every bit of signal has to come from delivery. That is
precisely the situation the interpreter's words-vs-voice mismatch rule depends
on, which makes this the most honest test of SER we can run.

Caveats worth holding on to
---------------------------
  - These are *acted* emotions, recorded in a studio by professional actors.
    They are cleaner and more exaggerated than a real conversation over a cheap
    microphone. Expect accuracy here to be an optimistic ceiling.
  - RAVDESS has a "calm" class our 7-label vocabulary does not, and it is
    EXCLUDED by default. Folding it into `neutral` looked like the honest
    mapping but measured badly: models read calm as `sad` (MERaLiON: 163 of 192)
    because both are low-arousal, which dragged the neutral class to 32% recall
    and macro recall from 67.3% to 61.3%. That penalises a model for a class we
    do not have. --include-calm restores the fold.
  - Accuracy is per-class recall averaged (macro), not raw hit rate. Raw hit rate
    flatters a model that just guesses the biggest class; here neutral+calm is
    twice the size of any other class once merged, so it would matter.

Output
------
A CSV with one row per clip (see COLUMNS) for slicing in a spreadsheet, plus a
printed summary: per-emotion recall, a confusion matrix, and -- for backends
that have a dimensional head -- mean valence per true emotion, which is the
single most useful diagnostic in the file. Valence is the axis the sarcasm rule
keys on, so if valence does not separate angry from happy, the mismatch logic
cannot work no matter how good the categorical label is.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import statistics
import subprocess
import sys
import time
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
RAVDESS_DIR = ROOT / "data" / "ravdess"

# RAVDESS encodes everything in the filename:
#   modality-vocalChannel-emotion-intensity-statement-repetition-actor.wav
# e.g. 03-01-06-01-02-01-12.wav = audio-only, speech, fearful, normal,
# statement 2, repetition 1, actor 12.
RAVDESS_EMOTIONS = {
    "01": "neutral",
    "02": "calm",       # no equivalent in our 7 labels -- excluded by default
    "03": "happy",
    "04": "sad",
    "05": "angry",
    "06": "fearful",
    "07": "disgusted",  # RAVDESS calls it "disgust"
    "08": "surprised",
}

# How RAVDESS classes map onto the vocabulary ser.analyze() returns.
TO_OURS = {
    "neutral": "neutral",
    "calm": "neutral",
    "happy": "happy",
    "sad": "sad",
    "angry": "angry",
    "fearful": "fearful",
    "disgusted": "disgusted",
    "surprised": "surprised",
}

STATEMENTS = {
    "01": "Kids are talking by the door",
    "02": "Dogs are sitting by the door",
}
INTENSITY = {"01": "normal", "02": "strong"}

# Rough expected direction of valence per emotion, used only to sanity-check
# that the dimensional head separates pleasant from unpleasant. Deliberately
# coarse: we are asking "does it point the right way", not scoring precision.
EXPECTED_VALENCE = {
    "happy": "high",
    "surprised": "high",
    "neutral": "mid",
    "sad": "low",
    "angry": "low",
    "fearful": "low",
    "disgusted": "low",
}

COLUMNS = [
    "file", "model", "backend",
    "true_emotion", "ravdess_emotion", "pred_emotion", "correct",
    "confidence", "valence", "arousal", "dominance",
    "intensity", "statement", "repetition", "actor", "gender",
    "duration_s", "latency_s", "error",
]


def parse_name(path: Path) -> dict | None:
    """Pull the label fields out of a RAVDESS filename, or None if it isn't one."""
    parts = path.stem.split("-")
    if len(parts) != 7:
        return None
    _modality, _channel, emo, intensity, statement, repetition, actor = parts
    if emo not in RAVDESS_EMOTIONS:
        return None
    actor_n = int(actor)
    return {
        "ravdess_emotion": RAVDESS_EMOTIONS[emo],
        "true_emotion": TO_OURS[RAVDESS_EMOTIONS[emo]],
        "intensity": INTENSITY.get(intensity, intensity),
        "statement": STATEMENTS.get(statement, statement),
        "repetition": repetition,
        "actor": actor_n,
        # RAVDESS convention: odd-numbered actors are male, even are female.
        "gender": "male" if actor_n % 2 else "female",
    }


def load_clips(limit: int, seed: int, exclude_calm: bool) -> list[tuple[Path, dict]]:
    """Collect clips, balanced across emotions so no class dominates the score."""
    if not RAVDESS_DIR.exists():
        raise SystemExit(
            f"RAVDESS not found at {RAVDESS_DIR}.\n"
            "Download and extract it first:\n"
            "  curl -L -o ravdess.zip \\\n"
            "    https://www.kaggle.com/api/v1/datasets/download/"
            "uwrfkaggler/ravdess-emotional-speech-audio\n"
            f"  then extract the Actor_* folders into {RAVDESS_DIR}"
        )

    items: list[tuple[Path, dict]] = []
    for path in sorted(RAVDESS_DIR.rglob("*.wav")):
        meta = parse_name(path)
        if meta is None:
            continue
        if exclude_calm and meta["ravdess_emotion"] == "calm":
            continue
        items.append((path, meta))

    if not items:
        raise SystemExit(f"no RAVDESS-named .wav files under {RAVDESS_DIR}")
    if limit <= 0 or limit >= len(items):
        return items

    # Balanced sample: take round-robin from each class rather than a flat
    # random draw, so a small --limit still covers every emotion evenly.
    by_class: dict[str, list] = defaultdict(list)
    for item in items:
        by_class[item[1]["true_emotion"]].append(item)
    rng = random.Random(seed)
    for bucket in by_class.values():
        rng.shuffle(bucket)

    chosen: list = []
    classes = sorted(by_class)
    i = 0
    while len(chosen) < limit:
        bucket = by_class[classes[i % len(classes)]]
        if bucket:
            chosen.append(bucket.pop())
        elif all(not b for b in by_class.values()):
            break
        i += 1
    return chosen


def read_wav_16k(path: Path) -> np.ndarray:
    """Load a RAVDESS wav (48 kHz) as mono float32 at 16 kHz.

    RAVDESS ships 48 kHz, which is an exact 3x of our 16 kHz, so a polyphase
    decimate is both cheap and clean -- no fractional resampling artefacts.
    """
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: expected 16-bit PCM, got {width * 8}-bit")
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        from scipy.signal import resample_poly

        from math import gcd
        g = gcd(int(rate), 16000)
        audio = resample_poly(audio, 16000 // g, int(rate) // g).astype(np.float32)
    return np.ascontiguousarray(audio, dtype=np.float32)


# ---------------------------------------------------------------------------
# Running one model. ser.py loads its model once at import by design, so each
# model gets its own subprocess rather than us reaching past that interface.
# ---------------------------------------------------------------------------

_CHILD = r'''
import json, sys, time
import numpy as np
sys.path.insert(0, sys.argv[1])
from eval.ser_eval import read_wav_16k
from pathlib import Path
from server import ser

print(json.dumps({"event": "loaded", "ok": ser.available(),
                  "backend": getattr(ser, "_backend", None)}), flush=True)
if not ser.available():
    raise SystemExit(0)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    path = Path(line)
    try:
        audio = read_wav_16k(path)
        t0 = time.perf_counter()
        result = ser.analyze(audio)
        elapsed = time.perf_counter() - t0
        print(json.dumps({"event": "row", "file": path.name, "r": result,
                          "t": elapsed, "dur": len(audio) / 16000.0}), flush=True)
    except Exception as exc:
        print(json.dumps({"event": "row", "file": path.name, "r": None,
                          "t": 0.0, "dur": 0.0,
                          "err": f"{type(exc).__name__}: {exc}"}), flush=True)
'''


def run_model(model: str, clips: list, writer, verbose: bool) -> dict | None:
    """Score every clip with one SER model, streaming rows to the CSV as they land.

    Rows are written incrementally rather than at the end: a full-set run takes
    the better part of an hour, and a crash at clip 1300 should not throw away
    the first 1299 results.
    """
    env = {
        **os.environ,
        "SER_MODEL": model,
        "HF_HUB_DISABLE_SYMLINKS": "1",
        "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }

    print(f"\n{'=' * 78}\n{model}\n{'=' * 78}")
    print("  loading model (first run downloads weights; this can take minutes)...",
          flush=True)

    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD, str(ROOT)],
        cwd=str(ROOT), env=env, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # these libraries are extremely chatty on load
        bufsize=1,
    )

    import json as _json

    handshake = None
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("{"):
            try:
                handshake = _json.loads(line)
                break
            except ValueError:
                continue
    if not handshake or not handshake.get("ok"):
        print("  UNAVAILABLE -- model failed to load (see server/ser.py warning)")
        proc.kill()
        return None

    backend = handshake.get("backend") or "?"
    print(f"  loaded ({backend} backend). scoring {len(clips)} clips...\n", flush=True)

    rows: list[dict] = []
    started = time.perf_counter()

    # Feed paths on a thread so we can read results as they stream back; writing
    # all paths up front would deadlock once the OS pipe buffer fills.
    import threading

    def feed() -> None:
        try:
            for path, _meta in clips:
                proc.stdin.write(f"{path}\n")
                proc.stdin.flush()
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    threading.Thread(target=feed, daemon=True).start()

    meta_by_name = {p.name: m for p, m in clips}
    done = 0
    for line in proc.stdout:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = _json.loads(line)
        except ValueError:
            continue
        if msg.get("event") != "row":
            continue

        done += 1
        name = msg["file"]
        meta = meta_by_name.get(name, {})
        result = msg.get("r") or {}
        pred = result.get("emotion")
        true = meta.get("true_emotion")
        correct = bool(pred and true and pred == true)

        row = {
            "file": name,
            "model": model,
            "backend": backend,
            "true_emotion": true,
            "ravdess_emotion": meta.get("ravdess_emotion"),
            "pred_emotion": pred,
            "correct": int(correct),
            "confidence": result.get("confidence"),
            "valence": result.get("valence"),
            "arousal": result.get("arousal"),
            "dominance": result.get("dominance"),
            "intensity": meta.get("intensity"),
            "statement": meta.get("statement"),
            "repetition": meta.get("repetition"),
            "actor": meta.get("actor"),
            "gender": meta.get("gender"),
            "duration_s": round(msg.get("dur", 0.0), 2),
            "latency_s": round(msg.get("t", 0.0), 3),
            "error": msg.get("err", ""),
        }
        rows.append(row)
        writer.writerow(row)

        if verbose:
            mark = "OK " if correct else "MISS"
            val = result.get("valence")
            val_s = f" val {val:.2f}" if isinstance(val, (int, float)) else ""
            elapsed = time.perf_counter() - started
            eta = (elapsed / done) * (len(clips) - done)
            print(
                f"  [{done:>4}/{len(clips)}] {mark} {name:<34} "
                f"true={str(true):<10} pred={str(pred):<10}"
                f"{val_s} {msg.get('t', 0):.2f}s  eta {eta / 60:.1f}m",
                flush=True,
            )
        elif done % 25 == 0:
            elapsed = time.perf_counter() - started
            eta = (elapsed / done) * (len(clips) - done)
            acc = 100 * sum(r["correct"] for r in rows) / len(rows)
            print(f"  {done}/{len(clips)}  running acc {acc:.0f}%  "
                  f"eta {eta / 60:.1f}m", flush=True)

    proc.wait(timeout=60)
    return {"model": model, "backend": backend, "rows": rows}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarise(result: dict) -> None:
    rows = [r for r in result["rows"] if r["true_emotion"] and r["pred_emotion"]]
    if not rows:
        print("  no scorable rows")
        return

    classes = sorted({r["true_emotion"] for r in rows})
    preds = sorted({r["pred_emotion"] for r in rows} | set(classes))

    print(f"\n  --- {result['model']} ({len(rows)} clips) ---")

    # Per-class recall, then the macro average. Macro, not raw accuracy: the
    # merged neutral+calm class is twice the size of the others, so raw accuracy
    # would reward a model that leans neutral.
    print(f"\n  {'emotion':<12}{'n':>5}{'recall':>9}   most-confused-with")
    print("  " + "-" * 58)
    recalls = []
    for cls in classes:
        subset = [r for r in rows if r["true_emotion"] == cls]
        hits = sum(r["correct"] for r in subset)
        recall = hits / len(subset) if subset else 0.0
        recalls.append(recall)
        wrong: dict[str, int] = defaultdict(int)
        for r in subset:
            if not r["correct"]:
                wrong[r["pred_emotion"]] += 1
        top = sorted(wrong.items(), key=lambda kv: -kv[1])[:2]
        conf = ", ".join(f"{k} x{v}" for k, v in top) if top else "-"
        print(f"  {cls:<12}{len(subset):>5}{100 * recall:>8.0f}%   {conf}")

    macro = 100 * statistics.mean(recalls) if recalls else 0.0
    raw = 100 * sum(r["correct"] for r in rows) / len(rows)
    print(f"\n  macro-average recall : {macro:.1f}%   "
          f"(raw accuracy {raw:.1f}%, chance ~{100 / len(classes):.0f}%)")

    lat = [r["latency_s"] for r in rows if r["latency_s"]]
    if lat:
        print(f"  latency per clip     : {statistics.mean(lat):.2f}s mean, "
              f"{statistics.median(lat):.2f}s median")

    # Confusion matrix -- small enough to read at a glance and far more
    # informative than a single score when deciding if a model is usable.
    print(f"\n  confusion (rows = true, cols = predicted)")
    head = "".join(f"{p[:6]:>8}" for p in preds)
    print(f"  {'':<12}{head}")
    for cls in classes:
        subset = [r for r in rows if r["true_emotion"] == cls]
        counts = defaultdict(int)
        for r in subset:
            counts[r["pred_emotion"]] += 1
        line = "".join(f"{counts.get(p, 0):>8}" for p in preds)
        print(f"  {cls:<12}{line}")

    # The dimensional check. This matters more than the labels for our use case:
    # the interpreter's sarcasm rule compares valence against the words, so
    # valence failing to separate angry from happy would break mismatch
    # detection even with a perfect categorical head.
    with_val = [r for r in rows if isinstance(r["valence"], (int, float))]
    if not with_val:
        print("\n  (no dimensional head on this backend -- valence/arousal unavailable,")
        print("   so the interpreter has only the categorical label to work with)")
        return

    print(f"\n  mean valence / arousal by true emotion (expected direction in parens)")
    print(f"  {'emotion':<12}{'valence':>9}{'arousal':>9}   expected valence")
    print("  " + "-" * 52)
    for cls in classes:
        subset = [r for r in with_val if r["true_emotion"] == cls]
        if not subset:
            continue
        mv = statistics.mean(r["valence"] for r in subset)
        ma = statistics.mean(r["arousal"] for r in subset
                             if isinstance(r["arousal"], (int, float)))
        print(f"  {cls:<12}{mv:>9.3f}{ma:>9.3f}   {EXPECTED_VALENCE.get(cls, '?')}")

    pos = [r["valence"] for r in with_val
           if EXPECTED_VALENCE.get(r["true_emotion"]) == "high"]
    neg = [r["valence"] for r in with_val
           if EXPECTED_VALENCE.get(r["true_emotion"]) == "low"]
    if pos and neg:
        gap = statistics.mean(pos) - statistics.mean(neg)
        print(f"\n  valence separation (pleasant - unpleasant): {gap:+.3f}")
        if gap < 0.05:
            print("  WARNING: valence barely separates pleasant from unpleasant here.")
            print("  The mismatch rule keys on this axis, so sarcasm detection will be")
            print("  weak regardless of how good the emotion labels look above.")


def profile_valence(result: dict) -> None:
    """Print a backend's real valence range and the gloss thresholds it needs.

    `_describe_voice` in the interpreter turns these numbers into words
    ("valence 0.21 (negative)") using thresholds from config, which assume a
    calibrated 0-1 head. At least one real model does not have one: MERaLiON's
    valence spans 0.12-0.41, so against the old fixed 0.4/0.6 every utterance in
    1440 -- happy ones included -- was described to the LLM as "negative".

    That is invisible in an accuracy score and fatal to the mismatch rule, so run
    this before trusting a new backend. Suggested thresholds are the terciles of
    the observed distribution: coarse, but they at least guarantee all three
    glosses are reachable.
    """
    from server import config

    rows = [r for r in result["rows"] if isinstance(r.get("valence"), (int, float))]
    if not rows:
        print(f"\n  {result['model']}: no dimensional head -- valence/arousal are")
        print("  None, so the interpreter reasons from the emotion label alone.")
        print("  Nothing to calibrate, but the mismatch rule loses its main axis.")
        return

    print("\n" + "=" * 78)
    print(f"VALENCE PROFILE -- {result['model']}")
    print("=" * 78)

    values = sorted(r["valence"] for r in rows)
    p33 = values[int(0.33 * (len(values) - 1))]
    p67 = values[int(0.67 * (len(values) - 1))]

    print(f"  observed range    : {values[0]:.3f} .. {values[-1]:.3f}  (n={len(values)})")
    print(f"  mean              : {statistics.mean(values):.3f}")
    print(f"  current thresholds: <{config.VALENCE_LOW} negative, "
          f">{config.VALENCE_HIGH} positive")

    below = sum(v < config.VALENCE_LOW for v in values)
    above = sum(v > config.VALENCE_HIGH for v in values)
    mid = len(values) - below - above
    print(f"  glosses produced  : negative {100 * below / len(values):.0f}%, "
          f"neutral {100 * mid / len(values):.0f}%, "
          f"positive {100 * above / len(values):.0f}%")

    pos = [r["valence"] for r in rows
           if EXPECTED_VALENCE.get(r["true_emotion"]) == "high"]
    neg = [r["valence"] for r in rows
           if EXPECTED_VALENCE.get(r["true_emotion"]) == "low"]
    gap = statistics.mean(pos) - statistics.mean(neg) if pos and neg else 0.0
    print(f"  pleasant - unpleasant separation: {gap:+.3f}")

    print()
    if len(values) in (below, above, mid):
        print("  BROKEN: every clip lands in ONE gloss band, so the prompt says the")
        print("  same thing about every utterance and carries no information.")
    elif gap < 0.05:
        print("  WEAK: valence barely separates pleasant from unpleasant. Even with")
        print("  good thresholds the mismatch rule has little to work with.")
    else:
        print("  Usable: spans the bands and separates pleasant from unpleasant.")

    print("\n  suggested thresholds for this backend (terciles):")
    print(f"    VALENCE_LOW={p33:.2f}  VALENCE_HIGH={p67:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="*", help="SER model ids (default: SER_MODEL)")
    ap.add_argument("--limit", type=int, default=160,
                    help="clips to score, balanced across emotions; 0 = all 1440")
    ap.add_argument("--seed", type=int, default=0, help="sampling seed")
    ap.add_argument("--csv", default="eval/ser_results.csv", help="CSV output path")
    # Excluded by DEFAULT. Measured on all 1440 clips: RAVDESS `calm` scores 11%
    # recall because the model reads it as `sad` (163/192) -- defensible, since
    # both are low-arousal and it has no calm class. True `neutral` scores 74%.
    # Folding them together dragged the neutral class to 32% and macro recall
    # from 67.3% to 61.3%, i.e. it penalised the model for a class our taxonomy
    # does not have and manufactured a "neutral is broken" signal that was not
    # real. --include-calm restores the old behaviour.
    ap.add_argument("--include-calm", action="store_true",
                    help="fold RAVDESS 'calm' into neutral (default: excluded, "
                         "because no model has a calm class and it reads as sad)")
    ap.add_argument("--quiet", action="store_true",
                    help="progress every 25 clips instead of a line per clip")
    ap.add_argument("--profile-valence", action="store_true",
                    help="print the backend's real valence range and the gloss "
                         "thresholds it needs -- run this before trusting a new "
                         "backend's dimensional head")
    args = ap.parse_args()

    from server import config

    models = args.models or [config.SER_MODEL]
    clips = load_clips(args.limit, args.seed, exclude_calm=not args.include_calm)

    dist = defaultdict(int)
    for _p, m in clips:
        dist[m["true_emotion"]] += 1

    print("=" * 78)
    print("SER accuracy on RAVDESS (acted emotional speech, 24 actors)")
    print("=" * 78)
    print(f"  clips   : {len(clips)}"
          + (" (full set)" if args.limit <= 0 else f" of 1440, balanced, seed {args.seed}"))
    print(f"  classes : " + ", ".join(f"{k} {v}" for k, v in sorted(dist.items())))
    print(f"  calm    : {'folded into neutral' if args.include_calm else 'excluded (default)'}")
    print(f"  models  : {', '.join(models)}")
    print(f"  csv     : {args.csv}")
    print("\n  Both RAVDESS sentences are semantically neutral, so all the signal")
    print("  here comes from delivery -- which is exactly what SER should be reading.")
    print("  These are acted, studio-recorded emotions: treat the numbers as an")
    print("  optimistic ceiling, not what a cheap mic in a real room will give you.")

    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for model in models:
            result = run_model(model, clips, writer, verbose=not args.quiet)
            if result:
                results.append(result)

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for result in results:
        summarise(result)

    if len(results) > 1:
        print("\n" + "=" * 78)
        print("HEAD TO HEAD")
        print("=" * 78)
        print(f"  {'model':<40}{'macro':>8}{'raw':>8}{'mean lat':>10}")
        print("  " + "-" * 64)
        for result in results:
            rows = [r for r in result["rows"] if r["true_emotion"] and r["pred_emotion"]]
            if not rows:
                continue
            classes = sorted({r["true_emotion"] for r in rows})
            recalls = []
            for cls in classes:
                subset = [r for r in rows if r["true_emotion"] == cls]
                recalls.append(sum(r["correct"] for r in subset) / len(subset))
            macro = 100 * statistics.mean(recalls)
            raw = 100 * sum(r["correct"] for r in rows) / len(rows)
            lat = statistics.mean(r["latency_s"] for r in rows)
            print(f"  {result['model']:<40}{macro:>7.1f}%{raw:>7.1f}%{lat:>9.2f}s")

    if args.profile_valence:
        for result in results:
            profile_valence(result)

    print(f"\nper-clip rows written to {csv_path}")
    print("Columns: " + ", ".join(COLUMNS))


if __name__ == "__main__":
    main()
