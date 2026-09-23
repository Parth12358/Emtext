"""Does the confidence number mean anything? Score it against tone correctness.

    python -m eval.confidence_eval                          # pipeline.csv, default cell
    python -m eval.confidence_eval --csv eval/results/pipeline_conf.csv
    python -m eval.confidence_eval --cell emotion2vec qwen3:14b --fit

Why this exists
---------------
server/confidence.py turns a handful of signals into one score the listener
sees as weight (high / medium / low). A confidence number that does not track
correctness is worse than none: it tells the listener when to trust a read and
is wrong about it. So every signal, and the composite, is measured here against
`tone_correct` on the rows `eval/pipeline_eval.py` wrote, and the thresholds in
config.py are meant to come from this output rather than from taste.

What is measured
----------------
  AUROC        can the signal rank correct reads above wrong ones at all?
               0.5 = coin flip. This is the first thing to look at: a signal
               with AUROC ~0.5 should not be in the score, whatever its ECE.
  ECE          expected calibration error of the composite: how far "0.8"
               is from being right 80% of the time, over 10 equal-width bins.
  reliability  the bin table behind ECE, so a miscalibration is visible.
  levels       accuracy inside each level the server would have shown, and
               the share of reads in each -- the number the listener feels.
  Platt        with --fit, a 1-D logistic regression (numpy, no sklearn) of
               tone_correct on logit(tone_prob), printed as CONFIDENCE_PLATT_A/B
               for config.py, with ECE before and after.

Caveats that stay in the output on purpose: RAVDESS is acted, studio speech
with neutral words, so "correct" here means "tone matched the acted emotion",
and a run of 280 clips gives ECE a standard error around 0.015. Numbers move
with the LLM and the ASR; re-run after changing either.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

from server import confidence, config

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "eval" / "results" / "pipeline.csv"

SIGNALS = ("emotion_prob", "tone_prob", "asr_conf", "ser_confidence", "voiced_s", "confidence_score")


def _f(v) -> float | None:
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def load_rows(path: Path, cell: tuple[str, str] | None, target: str = "emotion_correct") -> list[dict]:
    """Rows with a scoreable `target` column (emotion_correct by default,
    tone_correct for pre-2026-09-22 CSVs via --target tone)."""
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    if cell:
        backend, llm = cell
        rows = [r for r in rows if backend in (r.get("ser_backend") or "") and r.get("llm_model") == llm]
    out = []
    for r in rows:
        if r.get(target) in ("", None) or r.get("llm_offline") == "1":
            continue
        r["_correct"] = r[target]
        out.append(r)
    return out


def auroc(scores: list[float], labels: list[int]) -> float | None:
    """Rank-based AUROC (Mann-Whitney), ties counted half. None if one class."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(pos) * len(neg))


def ece(scores: list[float], labels: list[int], bins: int = 10) -> tuple[float, list[dict]]:
    edges = np.linspace(0, 1, bins + 1)
    total = len(scores)
    err = 0.0
    table = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        idx = [k for k, s in enumerate(scores) if (lo <= s < hi) or (i == bins - 1 and s == hi)]
        if not idx:
            table.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": 0, "conf": None, "acc": None})
            continue
        conf = sum(scores[k] for k in idx) / len(idx)
        acc = sum(labels[k] for k in idx) / len(idx)
        err += abs(conf - acc) * len(idx) / total
        table.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx), "conf": conf, "acc": acc})
    return err, table


def fit_platt(probs: list[float], labels: list[int], iters: int = 500, lr: float = 0.1) -> tuple[float, float]:
    """Logistic regression of label on logit(p): sigmoid(a*logit(p)+b). Gradient descent."""
    x = np.array([math.log(min(max(p, 1e-6), 1 - 1e-6) / (1 - min(max(p, 1e-6), 1 - 1e-6))) for p in probs])
    y = np.array(labels, dtype=float)
    a, b = 1.0, 0.0
    for _ in range(iters):
        z = a * x + b
        pred = 1 / (1 + np.exp(-z))
        ga = float(np.mean((pred - y) * x))
        gb = float(np.mean(pred - y))
        a -= lr * ga
        b -= lr * gb
    return round(a, 3), round(b, 3)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--cell", nargs=2, metavar=("SER_BACKEND", "LLM_MODEL"),
                    default=("emotion2vec", "qwen3:14b"))
    ap.add_argument("--all-cells", action="store_true", help="ignore --cell; pool every row")
    ap.add_argument("--fit", action="store_true", help="fit Platt scaling for the base probability and print config values")
    ap.add_argument("--target", choices=("emotion", "tone"), default="emotion",
                    help="score against emotion_correct (default) or tone_correct (older CSVs)")
    args = ap.parse_args()

    target = f"{args.target}_correct"
    rows = load_rows(args.csv, None if args.all_cells else tuple(args.cell), target)
    if not rows:
        raise SystemExit(f"no scoreable rows in {args.csv} for cell {args.cell} ({target})")
    labels = [int(r["_correct"]) for r in rows]
    print(f"{len(rows)} rows from {args.csv.name}"
          f"{'' if args.all_cells else ' (' + ' x '.join(args.cell) + ')'}; "
          f"{args.target} accuracy {100 * sum(labels) / len(labels):.1f}%")
    print(f"correct = {args.target} matched the acted RAVDESS emotion; acted studio speech, neutral words\n")

    # --- per-signal discrimination ---
    print(f"{'signal':<18}{'n':>6}{'AUROC':>8}   note")
    print("-" * 60)
    have: dict[str, tuple[list[float], list[int]]] = {}
    for sig in SIGNALS:
        pairs = [(_f(r.get(sig)), y) for r, y in zip(rows, labels)]
        pairs = [(s, y) for s, y in pairs if s is not None]
        if not pairs:
            print(f"{sig:<18}{0:>6}{'n/a':>8}   column missing or empty -- re-run pipeline_eval")
            continue
        s, y = [p[0] for p in pairs], [p[1] for p in pairs]
        have[sig] = (s, y)
        a = auroc(s, y)
        note = ("~coin flip; leave it out of the score" if a is not None and abs(a - 0.5) < 0.05
                else "inverted: higher = more wrong?!" if a is not None and a < 0.45 else "")
        print(f"{sig:<18}{len(s):>6}{(f'{a:.3f}' if a is not None else 'n/a'):>8}   {note}")

    # --- composite calibration ---
    if "confidence_score" in have:
        s, y = have["confidence_score"]
        e, table = ece(s, y)
        print(f"\ncomposite confidence_score: ECE {e:.3f} (n={len(s)})")
        print(f"  {'bin':<10}{'n':>5}{'mean conf':>11}{'accuracy':>10}")
        for t in table:
            if t["n"]:
                print(f"  {t['bin']:<10}{t['n']:>5}{t['conf']:>11.2f}{t['acc']:>10.2f}")
        # Levels are recomputed from the score with the CURRENT config cut-offs,
        # so this table answers "what would the listener see with the thresholds
        # I am about to ship", not what the run happened to use.
        lv = defaultdict(list)
        for r, yy in zip(rows, labels):
            sc = _f(r.get("confidence_score"))
            lv[confidence.level(sc) if sc is not None else "?"].append(yy)
        print(f"  by level the listener would see (HIGH>={config.CONFIDENCE_HIGH}, "
              f"LOW<{config.CONFIDENCE_LOW}):")
        for name in ("high", "medium", "low"):
            if lv.get(name):
                print(f"    {name:<7} {len(lv[name]):>4} reads ({100 * len(lv[name]) / len(rows):4.0f}%)"
                      f"  accuracy {100 * sum(lv[name]) / len(lv[name]):5.1f}%")

    # --- Platt fit for the base signal (emotion_prob since 2026-09-22) ---
    base = "emotion_prob" if "emotion_prob" in have else "tone_prob"
    if args.fit and base in have:
        s, y = have[base]
        e0, _ = ece(s, y)
        a, b = fit_platt(s, y)
        scaled = [confidence.platt(p, a, b) for p in s]
        e1, _ = ece(scaled, y)
        print(f"\nPlatt fit on {base}: a={a} b={b}   ECE {e0:.3f} -> {e1:.3f}"
              + ("  (applies only to ADJUSTED reads; an unadjusted read's base is ser_confidence)"
                 if base == "emotion_prob" else ""))
        if e1 < e0 - 0.01:
            print(f"  worth using: CONFIDENCE_PLATT_A={a}  CONFIDENCE_PLATT_B={b}")
        else:
            print("  no real gain over the identity (A=1, B=0); leave the config alone")

    # --- threshold sweep on the composite: what each cut-off would show the listener ---
    if "confidence_score" in have:
        s, y = have["confidence_score"]
        print("\nthreshold sweep on confidence_score (pick CONFIDENCE_HIGH where 'above' is "
              "reliable and CONFIDENCE_LOW where 'below' is mostly wrong):")
        print(f"  {'cut':>5}{'above n':>9}{'acc':>7}{'below n':>10}{'acc':>7}")
        for t in (0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 0.9):
            above = [yy for ss, yy in zip(s, y) if ss >= t]
            below = [yy for ss, yy in zip(s, y) if ss < t]
            fa = f"{100 * sum(above) / len(above):5.1f}%" if above else "   --"
            fb = f"{100 * sum(below) / len(below):5.1f}%" if below else "   --"
            print(f"  {t:>5.2f}{len(above):>9}{fa:>7}{len(below):>10}{fb:>7}")
        print(f"  (current: LOW={config.CONFIDENCE_LOW} HIGH={config.CONFIDENCE_HIGH} "
              f"PENALTY={config.CONFIDENCE_PENALTY} A={config.CONFIDENCE_PLATT_A} B={config.CONFIDENCE_PLATT_B})")


if __name__ == "__main__":
    main()
