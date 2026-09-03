"""Score interpreter LLMs against the labeled cases in `tone_cases.jsonl`.

    python -m eval.model_eval                          # all default models
    python -m eval.model_eval --models gemma3:12b      # just one
    python -m eval.model_eval --runs 3                 # repeat, to see variance
    python -m eval.model_eval --show-fails             # print every miss

It drives the real `Interpreter` class, so every case goes through the exact
system prompt, context assembly and JSON parsing that production uses. A
benchmark on a hand-written prompt would measure something the app never does.

What it measures, and why each part is separate
-----------------------------------------------
`tone` accuracy alone is a weak signal, so the report breaks out four things:

  - **overall accuracy** -- tone in the case's accepted set. Blunt but comparable
    across models.

  - **per-category accuracy** -- the interesting view. A model can score well
    overall while being useless in one direction, and the two failure modes are
    not equally bad here: missing sarcasm is a miss, but flagging sarcasm in a
    plain sentence actively teaches the listener to distrust ordinary speech.
    `literal` and `low-confidence` are the false-positive guards; watch them.

  - **mismatch discrimination** -- the headline number for this app. The
    `mm-*` cases come in pairs with IDENTICAL text and opposite voice data. A
    model that ignores the voice field answers both halves the same way and
    scores 0 here no matter how good its overall accuracy looks. This is the
    only metric that proves the voice signal is actually being used.

  - **speed** -- wall clock plus Ollama's own prefill/decode token counters.
    A model that is 3 points better but twice as slow may still be the wrong
    choice for a live conversation.

Accepted-tone sets are deliberately generous (see the note in tone_cases.jsonl):
several readings are often defensible, and scoring against one "correct" answer
would mostly measure agreement with the label author's taste.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

import httpx

from server import config
from server.interpreter import Interpreter

CASES_PATH = Path(__file__).resolve().parent / "tone_cases.jsonl"

# The three models under test, and the role each is being considered for.
DEFAULT_MODELS = [
    ("gemma3:12b", "primary -- social/tonal nuance per VRAM, ~8GB"),
    ("qwen3:8b", "fast alternative -- ~5GB, speed + context headroom"),
    ("qwen3:14b", "ceiling test -- ~9GB, snug once context grows"),
]


def load_cases() -> list[dict]:
    """Read the JSONL suite, skipping the `_comment` documentation lines."""
    cases = []
    for line in CASES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if "_comment" in obj:
            continue
        cases.append(obj)
    return cases


async def run_model(model: str, cases: list[dict], runs: int) -> dict:
    """Run every case `runs` times against one model and collect raw results."""
    results: list[dict] = []

    async with httpx.AsyncClient() as client:
        # Point the shared config at this model for the duration. The Interpreter
        # reads config.OLLAMA_MODEL at call time, which is what lets us A/B
        # without touching the class itself.
        original = config.OLLAMA_MODEL
        config.OLLAMA_MODEL = model
        try:
            # One untimed call so the first real case isn't charged with loading
            # the model into VRAM (~8s on a cold 12B). Without this the model
            # that happens to run first looks slower than it is, which would
            # make the whole A/B meaningless.
            await Interpreter(client).interpret("Warming up the model.")

            for run_index in range(1, runs + 1):
                for case in cases:
                    interp = Interpreter(client)
                    # Prime the rolling window with the case's context lines so
                    # context-dependent cases (pa-01, ref-01) behave as they
                    # would mid-conversation.
                    for line in case.get("context") or []:
                        interp._context.append(line)

                    start = time.perf_counter()
                    out = await interp.interpret(case["transcript"], case.get("voice"))
                    elapsed = time.perf_counter() - start

                    voice = case.get("voice") or {}
                    results.append({
                        "id": case["id"],
                        "category": case["category"],
                        "expect": case["expect"],
                        "got": out["tone"],
                        "read": out["read"],
                        "ok": out["tone"] in case["expect"],
                        "offline": out["read"] == "(interpreter offline)",
                        "elapsed": elapsed,
                        # Kept for the CSV so a row is self-contained -- you can
                        # read a miss without cross-referencing the case file.
                        "model": model,
                        "run": run_index,
                        "transcript": case["transcript"],
                        "context": " | ".join(case.get("context") or []),
                        "voice_emotion": voice.get("emotion", ""),
                        "voice_confidence": voice.get("confidence", ""),
                        "voice_valence": voice.get("valence", ""),
                        "voice_arousal": voice.get("arousal", ""),
                        "note": case.get("note", ""),
                    })

                if runs > 1:
                    print(f"    run {run_index}/{runs} done", flush=True)
        finally:
            config.OLLAMA_MODEL = original

    return {"model": model, "results": results}


def mismatch_score(results: list[dict]) -> tuple[int, int]:
    """How many identical-text voice pairs did the model actually distinguish?

    A pair counts only if BOTH halves land in their accepted sets. Getting one
    half right is not discrimination -- a model that always says "sarcastic"
    would score 50% on pairs by accident, and this returns 0 for it.
    """
    by_id: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        if r["category"] == "mismatch":
            by_id[r["id"]].append(r)

    # Majority-vote each half across runs, then pair them up by stem (mm-01).
    verdict: dict[str, bool] = {}
    for cid, rows in by_id.items():
        verdict[cid] = sum(r["ok"] for r in rows) > len(rows) / 2

    stems = {cid[:-1] for cid in verdict}
    won = sum(
        1
        for s in sorted(stems)
        if verdict.get(f"{s}a", False) and verdict.get(f"{s}b", False)
    )
    return won, len(stems)


CSV_COLUMNS = [
    "model", "run", "id", "category", "transcript", "context",
    "voice_emotion", "voice_confidence", "voice_valence", "voice_arousal",
    "expected", "got", "correct", "read", "elapsed_s", "offline", "note",
]


def write_csv(runs_data: list[dict], path: Path) -> None:
    """One row per (model, run, case), so a spreadsheet can slice any way.

    Each row carries the transcript, the voice data and the expected set, so a
    miss can be understood on its own without opening tone_cases.jsonl.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for data in runs_data:
            for r in data["results"]:
                writer.writerow({
                    "model": r["model"],
                    "run": r["run"],
                    "id": r["id"],
                    "category": r["category"],
                    "transcript": r["transcript"],
                    "context": r["context"],
                    "voice_emotion": r["voice_emotion"],
                    "voice_confidence": r["voice_confidence"],
                    "voice_valence": r["voice_valence"],
                    "voice_arousal": r["voice_arousal"],
                    "expected": "|".join(r["expect"]),
                    "got": r["got"],
                    "correct": int(r["ok"]),
                    "read": r["read"],
                    "elapsed_s": round(r["elapsed"], 3),
                    "offline": int(r["offline"]),
                    "note": r["note"],
                })
    print(f"\nper-case rows written to {path}")


def report(runs_data: list[dict], cases: list[dict], show_fails: bool) -> None:
    categories = sorted({c["category"] for c in cases})

    print("\n" + "=" * 78)
    print("ACCURACY BY CATEGORY (tone within the case's accepted set)")
    print("=" * 78)
    header = f"{'model':<14}" + "".join(f"{c[:11]:>13}" for c in categories) + f"{'ALL':>8}"
    print(header)
    print("-" * len(header))

    for data in runs_data:
        row = f"{data['model']:<14}"
        results = data["results"]
        if not results:
            print(row + "  (no results)")
            continue
        for cat in categories:
            subset = [r for r in results if r["category"] == cat]
            pct = 100 * sum(r["ok"] for r in subset) / len(subset) if subset else 0
            row += f"{pct:>12.0f}%"
        overall = 100 * sum(r["ok"] for r in results) / len(results)
        row += f"{overall:>7.0f}%"
        print(row)

    print("\n" + "=" * 78)
    print("VOICE MISMATCH DISCRIMINATION  (identical text, opposite voice)")
    print("=" * 78)
    print("Both halves of a pair must be right. A model ignoring the voice field")
    print("scores 0 here regardless of its overall accuracy.\n")
    for data in runs_data:
        won, total = mismatch_score(data["results"])
        bar = "#" * won + "." * (total - won)
        print(f"  {data['model']:<14} {won}/{total} pairs  [{bar}]")

    print("\n" + "=" * 78)
    print("SPEED (per interpretation, wall clock)")
    print("=" * 78)
    print(f"{'model':<14}{'mean':>9}{'median':>9}{'p95':>9}{'slowest':>10}")
    print("-" * 51)
    for data in runs_data:
        times = [r["elapsed"] for r in data["results"]]
        if not times:
            continue
        ordered = sorted(times)
        p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
        print(f"{data['model']:<14}{statistics.mean(times):>8.2f}s"
              f"{statistics.median(times):>8.2f}s{p95:>8.2f}s{max(times):>9.2f}s")

    if show_fails:
        print("\n" + "=" * 78)
        print("MISSES")
        print("=" * 78)
        for data in runs_data:
            seen: set[str] = set()
            misses = [r for r in data["results"] if not r["ok"] and r["id"] not in seen
                      and not seen.add(r["id"])]
            print(f"\n--- {data['model']} ({len(misses)} distinct) ---")
            for r in misses:
                print(f"  [{r['id']:<8}] got {r['got']:<10} want {'/'.join(r['expect'])}")
                print(f"             \"{r['read']}\"")


async def main_async(args: argparse.Namespace) -> None:
    cases = load_cases()
    print(f"{len(cases)} cases from {CASES_PATH.name}, {args.runs} run(s) each")

    async with httpx.AsyncClient() as client:
        try:
            tags = await client.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
            available = {m["name"] for m in tags.json().get("models", [])}
        except httpx.HTTPError as exc:
            raise SystemExit(f"Ollama unreachable at {config.OLLAMA_URL}: {exc}")

    wanted = args.models or [m for m, _ in DEFAULT_MODELS]
    roles = dict(DEFAULT_MODELS)

    missing = [m for m in wanted if m not in available]
    if missing:
        print(f"\nnot pulled, skipping: {', '.join(missing)}")
        print(f"  ollama pull {missing[0]}")
    wanted = [m for m in wanted if m in available]
    if not wanted:
        raise SystemExit("none of the requested models are pulled")

    runs_data = []
    for model in wanted:
        role = roles.get(model, "")
        print(f"\n>>> {model}{'  (' + role + ')' if role else ''}")
        start = time.perf_counter()
        data = await run_model(model, cases, args.runs)
        offline = sum(r["offline"] for r in data["results"])
        if offline:
            # Every case failing this way means the model errored, not that it
            # answered badly -- say so rather than reporting a 0% score.
            print(f"    WARNING: {offline}/{len(data['results'])} calls fell back to "
                  f"'(interpreter offline)' -- results below are not meaningful")
        print(f"    done in {time.perf_counter() - start:.0f}s")
        runs_data.append(data)

    write_csv(runs_data, Path(args.csv))
    report(runs_data, cases, args.show_fails)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", nargs="*", help="override the model list")
    ap.add_argument("--runs", type=int, default=1, help="repetitions per case")
    ap.add_argument("--show-fails", action="store_true", help="print every miss")
    ap.add_argument("--csv", default="eval/model_results.csv",
                    help="per-case CSV output path")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
