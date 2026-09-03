"""Run the whole eval suite and drop a CSV per dimension.

    python -m eval.run_all                  # everything (long: budget ~1-2h)
    python -m eval.run_all --quick          # small samples, minutes not hours
    python -m eval.run_all --only ser asr   # just these stages
    python -m eval.run_all --list           # show the stages and exit

Each stage is a separate module you can also run on its own; this just runs them
in sequence with consistent CSV paths so you end up with one directory of
results to open in a spreadsheet. Output is streamed live rather than captured,
because several stages take tens of minutes and a silent terminal is
indistinguishable from a hang.

Stages, and the question each answers:

  pipeline Does the WHOLE workflow work? Runs Segmenter -> Whisper + SER ->
           Interpreter over RAVDESS across a model matrix. The only stage that
           tests whether the pieces compose rather than each one alone.
  ser      Is the voice signal actually *right*? Scores SER backends against
           RAVDESS (real acted emotional speech, real labels).
  asr      Which Whisper model should WHISPER_MODEL be? WER and CPU cost on
           known-text utterances.
  model    Which Ollama model should read the tone? Scores LLMs on the labeled
           cases in tone_cases.jsonl, including the voice-mismatch pairs.
  bench    Where does the time go? Per-stage latency and Ollama tok/s.
  latency  What does the user actually feel? Stopped-speaking-to-read, over the
           real websocket. Needs the server running.

A stage that fails does not stop the others -- a missing dataset or a downed
Ollama should cost you that stage, not the whole run.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_DIR = ROOT / "eval" / "results"

# (name, module, full args, quick args, needs)
STAGES = [
    (
        "pipeline", "eval.pipeline_eval",
        # --speech-rms 150 is not optional here: at the shipped default of 500
        # the VAD drops ~half of RAVDESS, and the half it drops is the quiet
        # emotions. See TODO.md.
        ["--limit", "280", "--quiet", "--speech-rms", "150"],
        ["--cells", "1", "--limit", "28", "--quiet", "--speech-rms", "150"],
        "RAVDESS in data/ravdess + Ollama with the models pulled",
    ),
    (
        "ser", "eval.ser_eval",
        ["--limit", "0", "--quiet", "--csv", str(CSV_DIR / "ser.csv")],
        ["--limit", "56", "--quiet", "--csv", str(CSV_DIR / "ser.csv")],
        "RAVDESS in data/ravdess",
    ),
    (
        "asr", "eval.asr_eval",
        ["--csv", str(CSV_DIR / "asr.csv")],
        ["--models", "tiny", "base", "--quiet", "--csv", str(CSV_DIR / "asr.csv")],
        "experiments/asr (make_asr_set.ps1)",
    ),
    (
        "model", "eval.model_eval",
        ["--runs", "3", "--show-fails", "--csv", str(CSV_DIR / "model.csv")],
        ["--runs", "1", "--models", "gemma3:12b", "--csv", str(CSV_DIR / "model.csv")],
        "Ollama running with the models pulled",
    ),
    (
        "bench", "eval.bench",
        ["--runs", "3"],
        ["--runs", "1", "--skip-llm"],
        "Ollama (unless --quick)",
    ),
    (
        "latency", "eval.latency",
        ["--runs", "3"],
        ["--runs", "1", "--skip-wire"],
        "the server running on PORT (unless --quick)",
    ),
]


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true",
                    help="small samples: minutes instead of hours")
    ap.add_argument("--only", nargs="*", metavar="STAGE",
                    help="run only these stages")
    ap.add_argument("--list", action="store_true", help="list stages and exit")
    args = ap.parse_args()

    if args.list:
        print("stages:")
        for name, module, _full, _quick, needs in STAGES:
            print(f"  {name:<9} {module:<18} needs: {needs}")
        return

    chosen = [s for s in STAGES if not args.only or s[0] in args.only]
    if not chosen:
        raise SystemExit(f"no stage matched {args.only}; try --list")

    CSV_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(f"emtext eval suite -- {'quick' if args.quick else 'full'} run")
    print("=" * 78)
    print(f"  stages : {', '.join(s[0] for s in chosen)}")
    print(f"  csv    : {CSV_DIR}")
    if not args.quick:
        print("\n  This is the full run and it is slow -- the SER stage alone scores")
        print("  1440 clips. Use --quick for a sanity pass in minutes.")

    outcomes: list[tuple[str, str, float]] = []
    for name, module, full, quick, _needs in chosen:
        argv = quick if args.quick else full
        print("\n" + "#" * 78)
        print(f"# {name}   ({module} {' '.join(argv)})")
        print("#" * 78, flush=True)

        start = time.perf_counter()
        # Streamed, not captured: these stages are long and their own progress
        # output is the only sign the run is alive.
        proc = subprocess.run([sys.executable, "-u", "-m", module, *argv],
                              cwd=str(ROOT))
        elapsed = time.perf_counter() - start
        outcomes.append((name, "ok" if proc.returncode == 0 else
                         f"FAILED (exit {proc.returncode})", elapsed))

    print("\n" + "=" * 78)
    print("SUITE SUMMARY")
    print("=" * 78)
    for name, status, elapsed in outcomes:
        print(f"  {name:<10}{status:<24}{elapsed / 60:>6.1f} min")

    produced = sorted(CSV_DIR.glob("*.csv"))
    if produced:
        print(f"\nCSVs in {CSV_DIR}:")
        for path in produced:
            lines = sum(1 for _ in path.open(encoding="utf-8")) - 1
            print(f"  {path.name:<14}{lines:>6} rows")

    if any(s.startswith("FAILED") for _n, s, _e in outcomes):
        print("\nSome stages failed. Each needs something external -- check the")
        print("'needs' column in `python -m eval.run_all --list`.")


if __name__ == "__main__":
    main()
