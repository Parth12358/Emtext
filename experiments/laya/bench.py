"""Benchmark Laya (a small, fast structured-QA model) as a tone helper for emtext.

    .venv/Scripts/python experiments/laya/bench.py                # everything
    .venv/Scripts/python experiments/laya/bench.py --quick        # runs 1,3,7, short
    .venv/Scripts/python experiments/laya/bench.py --runs 1,3     # a selection
    .venv/Scripts/python experiments/laya/bench.py --dry-run      # fake worker,
                                                                  # exercises scoring
    .venv/Scripts/python experiments/laya/bench.py --worker path/to/worker.py

Run from the repo root (the directory containing `server/`). Results go to
`experiments/laya/results/`: one CSV per run plus `summary.json` with every
headline number.

Why this exists
---------------
The interpreter's LLM (Ollama, GPU, ~1s per line) is the whole cost of a read.
Laya answers a handful of structured questions in tens of milliseconds on CPU.
The question is not "can Laya replace the LLM" -- it produces no prose -- but
whether it can (a) veto the LLM's worst mistakes (calling plain speech
sarcastic, calling real sarcasm neutral), (b) gate lines that carry no subtext
so they never reach the LLM at all, and (c) do that without stealing CPU from
Whisper and SER, which already share the cores. The seven runs below measure
exactly those three things, each against the same labelled data the rest of
`eval/` uses, so the numbers are comparable with `eval/model_eval.py` and
`eval/pipeline_eval.py`.

The worker process
------------------
Laya lives in its own venv (`.venv-laya/`, torch + transformers versions that
would fight the server's), so it runs as a subprocess speaking JSON lines:

    stdout  {"ready": true, "load_s", "rss_mb", "head_max_len", "max_len"}   once
            (or {"ready": false, "error"} and exit 1)
    stdin   {"id": str, "state": str|dict, "questions": {...}}      per request
    stdout  {"id": str, "answers": {...}, "latency_ms": float, "usage": {...}}
         or {"id": str, "error": str}                                 per request

Cold load is ~17 s (the ready wait is generous); warm latency is ~110 ms plus
~60 ms per extra question, and answers are deterministic across calls.
Criteria descriptions must stay short: an option set over `head_max_len`
(192 tokens) comes back as an error line, not a truncated answer.

`questions` maps a name to either (this is laya's own `Agent.predict` schema,
which `worker.py` passes straight through)
    {"type": "choice", "instructions": str, "criteria": {label: description}}
    {"type": "noul",   "instructions": str}
and `answers[name]` is laya's raw dict: for a choice `{"choice", "probabilities":
{label: p}, "confidence"}`, for a noul `{"noul": p_yes, "confidence"}`.
`parse_answer()` is deliberately tolerant of other spellings (see its
docstring) so the two files can be written independently. If the schema for
`questions` differs, `QUESTION_SETS` is the one place to change.

This file imports from `server/` READ-ONLY: `interpreter.TONES`,
`interpreter._describe_voice` (so the voice hint is byte-identical to what the
LLM sees) and the two SER anchor tables. `server.ser` loads its model at
import, so the anchors are pulled from the source with `ast` instead; the
transcriber is imported lazily and only by the latency run.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import wave
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config  # noqa: E402  (after sys.path fix; read-only)
from server.interpreter import TONES, _describe_voice  # noqa: E402

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CASES_PATH = ROOT / "eval" / "tone_cases.jsonl"
MODEL_CSV = ROOT / "eval" / "results" / "model.csv"
PIPELINE_CSV = ROOT / "eval" / "results" / "pipeline.csv"
SPEECH_WAV = ROOT / "experiments" / "speech.wav"
DEFAULT_WORKER = HERE / "worker.py"
DEFAULT_PYTHON = ROOT / ".venv-laya" / "Scripts" / "python"

THETAS = (0.3, 0.4, 0.5, 0.6, 0.7)
GATE_THETAS = tuple(round(0.1 * i, 1) for i in range(1, 10))


# --------------------------------------------------------------------------
# SER anchors, read from the source rather than imported.
# --------------------------------------------------------------------------

def _load_ser_anchors() -> tuple[dict[str, float], dict[str, float]]:
    """Pull `_AROUSAL_ANCHOR` / `_DOMINANCE_ANCHOR` out of `server/ser.py`.

    Importing `server.ser` runs `_load()` at module level, which downloads and
    loads emotion2vec -- several seconds and a funasr dependency, for two
    dicts of seven floats. Parsing the assignments with `ast` gives the same
    numbers with no side effects and stays in sync with the file.
    """
    src = (ROOT / "server" / "ser.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found: dict[str, dict[str, float]] = {}
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        for name in targets:
            if name in ("_AROUSAL_ANCHOR", "_DOMINANCE_ANCHOR"):
                found[name] = ast.literal_eval(node.value)
    return found["_AROUSAL_ANCHOR"], found["_DOMINANCE_ANCHOR"]


AROUSAL_ANCHOR, DOMINANCE_ANCHOR = _load_ser_anchors()


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------

TONE_CRITERIA = {
    "positive": "genuinely pleased, warm, enthusiastic",
    "negative": "upset, hostile, complaining, resigned",
    "neutral": "plain information or logistics, no emotional charge",
    "sarcastic": "says the opposite of what is meant; mocking or bitter",
    "mixed": "conflicted, masking, or more than one feeling at once",
}
assert set(TONE_CRITERIA) == set(TONES), "tone criteria must cover interpreter.TONES"

QUESTION_SETS: dict[str, dict] = {
    "tone5": {
        "type": "choice",
        "instructions": "What is the emotional tone of the newest line?",
        "criteria": dict(TONE_CRITERIA),
    },
    "polarity3": {
        "type": "choice",
        "instructions": "What is the polarity of the newest line's WORDS only: what the "
                        "words literally say, ignoring how they might be delivered?",
        "criteria": {
            "positive": "the words themselves are pleasant, approving or enthusiastic",
            "negative": "the words themselves are unpleasant, critical or complaining",
            "neutral": "the words are plain information, a question or logistics",
        },
    },
    "sarcastic": {
        "type": "noul",
        "instructions": "Is the newest line sarcastic or ironic?",
    },
    "worth_reading": {
        "type": "noul",
        "instructions": "Does the newest line carry emotional subtext a listener could "
                        "miss, beyond its literal meaning?",
    },
}
ALL_QUESTIONS = tuple(QUESTION_SETS)
PIPELINE_QUESTIONS = ("tone5", "sarcastic", "worth_reading")


def questions(names: Iterable[str]) -> dict:
    return {n: QUESTION_SETS[n] for n in names}


# --------------------------------------------------------------------------
# State builder -- mirrors Interpreter._build_prompt's user section
# --------------------------------------------------------------------------

def build_state(transcript: str, context: list[str] | None, voice: dict | None,
                with_voice: bool) -> str:
    """One string: earlier lines, the newest line, and optionally the voice hint.

    The hint text comes from `interpreter._describe_voice`, so Laya reads the
    same words the LLM does -- a different rendering would make "does the hint
    help" a question about the rendering rather than about the model.
    """
    lines = [f"earlier: {c}" for c in (context or [])]
    lines.append(f"newest: {transcript}")
    if with_voice:
        hint = _describe_voice(voice)
        if hint:
            lines.append(f"voice sounded like: {hint}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Answer parsing
# --------------------------------------------------------------------------

_CHOICE_KEYS = ("choice", "label", "answer", "value", "option", "selected")
_PROB_KEYS = ("prob", "probability", "p", "score")
_MAP_KEYS = ("probabilities", "probs", "distribution", "scores", "options")
_NOUL_KEYS = ("noul", "prob", "probability", "p", "value", "score", "yes")


def _as_float(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if 0.0 <= v <= 1.0 else None


def parse_answer(raw: Any, qtype: str, options: Iterable[str] = ()) -> dict:
    """Normalise one worker answer to `{choice, prob, conf, probs, noul, raw}`.

    The worker's actual shape (laya's raw dict) is
      choice: {"type":"choice","choice":label,"probabilities":{label: p},
               "confidence": c, "action": {...}}
      noul:   {"type":"noul","noul": p_yes,"confidence": max(p,1-p), ...}
    where `confidence` is a CALIBRATED scalar, not the max probability, so the
    two are kept apart: `prob` is the chosen option's entry in the map and
    `conf` is the scalar. The veto rules threshold on `prob` (a probability
    over the tone set, comparable across questions); `conf` is recorded so
    the CSVs can show whether it would have made a better gate.

    The parser stays tolerant of other spellings so the worker can evolve
    without breaking the bench: label under any of `_CHOICE_KEYS` (or a bare
    string); the map under `_MAP_KEYS`; a scalar probability under
    `_PROB_KEYS` as a last resort. If only the map exists the choice is its
    argmax; if only a bare string arrives `prob` is None and the confidence
    metrics skip it. A noul is a number under `_NOUL_KEYS` or a bare number;
    a bool becomes 1.0 / 0.0. `raw` is the untouched dict.

    Never raises: anything unparseable yields all-None fields, which the runs
    score as a miss rather than aborting.
    """
    out: dict[str, Any] = {"choice": None, "prob": None, "conf": None, "probs": None,
                           "noul": None, "raw": raw}
    options = list(options)

    if qtype == "noul":
        val: Any = raw
        if isinstance(raw, dict):
            val = None
            for k in _NOUL_KEYS:
                if k in raw:
                    val = raw[k]
                    break
        if isinstance(val, bool):
            val = 1.0 if val else 0.0
        out["noul"] = _as_float(val)
        if isinstance(raw, dict):
            out["conf"] = _as_float(raw.get("confidence"))
        return out

    # choice
    if isinstance(raw, str):
        out["choice"] = raw.strip().lower()
        return out
    if not isinstance(raw, dict):
        return out

    probs = None
    for k in _MAP_KEYS:
        v = raw.get(k)
        if isinstance(v, dict) and v:
            probs = {str(a).lower(): _as_float(b) for a, b in v.items()}
            probs = {a: b for a, b in probs.items() if b is not None}
            if probs:
                break
            probs = None
    out["probs"] = probs

    choice = None
    for k in _CHOICE_KEYS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            choice = v.strip().lower()
            break
    if choice is None and probs:
        choice = max(probs, key=probs.get)
    out["choice"] = choice

    prob = probs.get(choice) if probs and choice else None
    if prob is None:
        for k in _PROB_KEYS:
            v = raw.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                prob = _as_float(v)
                break
    out["prob"] = prob
    out["conf"] = _as_float(raw.get("confidence"))
    return out


def parse_answers(answers: Any, qs: dict) -> dict[str, dict]:
    """`parse_answer` over every question asked; missing names become empty."""
    answers = answers if isinstance(answers, dict) else {}
    return {
        name: parse_answer(answers.get(name), q["type"], q.get("criteria", ()))
        for name, q in qs.items()
    }


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------

class LayaError(Exception):
    pass


class LayaClient:
    """Drive the Laya worker subprocess over JSON lines.

    `ask()` is synchronous. `ask_many()` writes every request before reading
    any reply, so a worker that batches or overlaps internally can; replies
    are matched by `id`, so order does not matter. A background thread owns
    stdout (the only reader) and parks each reply under its id; stderr is
    drained by another thread into `results/worker.stderr.log` so a chatty
    model load cannot fill the pipe and deadlock the child.
    """

    def __init__(self, worker: Path, python: Path, ready_timeout_s: float = 600.0,
                 timeout_s: float = 120.0) -> None:
        self.timeout_s = timeout_s
        self.ready: dict = {}
        self._replies: dict[str, dict] = {}
        self._cv = threading.Condition()
        self._dead: str | None = None
        self._seq = 0

        env = dict(os.environ)
        env["USE_TF"] = "0"
        env["HF_HUB_DISABLE_SYMLINKS"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        RESULTS.mkdir(parents=True, exist_ok=True)
        self._stderr_file = open(RESULTS / "worker.stderr.log", "ab")
        self.proc = subprocess.Popen(
            [str(python), str(worker)], cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_file,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        with self._cv:
            deadline = time.monotonic() + ready_timeout_s
            while not self.ready and self._dead is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise LayaError("worker never printed a ready line")
                self._cv.wait(remaining)
            if self._dead is not None and not self.ready:
                raise LayaError(f"worker died before ready: {self._dead}")

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue            # not protocol; a stray print from a library
            if not isinstance(msg, dict):
                continue
            with self._cv:
                if "ready" in msg and not self.ready:
                    if msg["ready"]:
                        self.ready = msg
                    else:                       # {"ready": false, "error": ...}
                        self._dead = f"load failed: {msg.get('error')}"
                elif "id" in msg:
                    self._replies[str(msg["id"])] = msg
                self._cv.notify_all()
        with self._cv:
            self._dead = f"stdout closed (exit {self.proc.poll()})"
            self._cv.notify_all()

    def _send(self, req: dict) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise LayaError(f"worker stdin closed: {e}") from e

    def _wait(self, rid: str) -> dict:
        with self._cv:
            deadline = time.monotonic() + self.timeout_s
            while rid not in self._replies:
                if self._dead is not None:
                    return {"id": rid, "error": self._dead}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"id": rid, "error": f"timeout after {self.timeout_s}s"}
                self._cv.wait(remaining)
            return self._replies.pop(rid)

    def ask(self, rid: str, state: str | dict, qs: dict) -> dict:
        """One request, one reply (`{"id","answers","latency_ms"}` or `{"id","error"}`)."""
        self._send({"id": rid, "state": state, "questions": qs})
        return self._wait(rid)

    def ask_many(self, reqs: list[tuple[str, str | dict, dict]]) -> dict[str, dict]:
        """Pipelined: send everything, then collect. Returns replies keyed by id."""
        for rid, state, qs in reqs:
            self._send({"id": rid, "state": state, "questions": qs})
        return {rid: self._wait(rid) for rid, _, _ in reqs}

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        finally:
            self._stderr_file.close()


class FakeClient:
    """Deterministic stand-in for `--dry-run`: exercises every scoring path.

    Answers come from crude keyword rules plus a hash-seeded jitter, so the
    CSVs are non-trivial (some hits, some misses, calibrated-ish noul values)
    and a few requests are deliberately failed to prove the error path. This
    measures NOTHING about Laya; it only proves the bench end to end.
    """

    ready = {"ready": True, "load_s": 0.0, "rss_mb": 0.0, "fake": True}
    _SARC = ("oh great", "wonderful", "perfect", "miracle", "so well", "sure,", "fine.",
             "take your time", "whatever", "great, another", "love to", "unbelievable")
    _NEG = ("didn't", "done with", "raining", "hostile", "not", "no,", "upset", "kids", "dogs")
    _POS = ("happy", "best", "love", "thank", "great", "wonderful", "congrat")

    def __init__(self, fail_every: int = 17) -> None:
        self.fail_every = fail_every
        self.n = 0

    @staticmethod
    def _jit(seed: str, span: float = 0.15) -> float:
        h = int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)
        return (h / 0xFFFFFFFF - 0.5) * 2 * span

    def ask(self, rid: str, state: str | dict, qs: dict) -> dict:
        self.n += 1
        if self.fail_every and self.n % self.fail_every == 0:
            return {"id": rid, "error": "fake failure for the error path"}
        text = state if isinstance(state, str) else json.dumps(state)
        newest = next((l for l in text.splitlines() if l.startswith("newest:")), text).lower()
        hint = next((l for l in text.splitlines() if l.startswith("voice")), "").lower()
        sarc = 0.15 + 0.5 * any(k in newest for k in self._SARC)
        if "angry" in hint or "negative)" in hint or "sad" in hint:
            sarc += 0.25
        if "happy" in hint or "positive)" in hint:
            sarc -= 0.25
        if "low confidence" in hint:
            sarc -= 0.15
        sarc = min(0.98, max(0.02, sarc + self._jit(rid + "s")))
        pos = any(k in newest for k in self._POS)
        neg = any(k in newest for k in self._NEG)
        polarity = "positive" if pos and not neg else ("negative" if neg else "neutral")
        if sarc > 0.6:
            tone = "sarcastic"
        elif "sad" in hint and pos:
            tone = "mixed"
        elif "angry" in hint and "low confidence" not in hint:
            tone = "negative"
        else:
            tone = polarity
        worth = min(0.98, max(0.02, (0.75 if tone != "neutral" else 0.2) + self._jit(rid + "w", 0.2)))
        tconf = min(0.99, max(0.35, 0.7 + self._jit(rid + "t")))
        rest = (1 - tconf) / (len(TONES) - 1)
        answers: dict[str, Any] = {}
        for name, q in qs.items():
            if name == "tone5":
                answers[name] = {"type": "choice", "choice": tone, "confidence": round(tconf * 0.6, 4),
                                 "probabilities": {t: (tconf if t == tone else rest) for t in TONES}}
            elif name == "polarity3":
                answers[name] = {"choice": polarity, "confidence": 0.8}
            elif name == "sarcastic":
                answers[name] = {"noul": sarc}
            elif name == "worth_reading":
                answers[name] = worth          # bare number: the tolerant path
            else:
                answers[name] = {}
        time.sleep(0.001)
        return {"id": rid, "answers": answers, "latency_ms": 1.0 + abs(self._jit(rid, 0.5))}

    def ask_many(self, reqs: list[tuple[str, str | dict, dict]]) -> dict[str, dict]:
        return {rid: self.ask(rid, s, q) for rid, s, q in reqs}

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# Data + scoring helpers (mirroring eval/model_eval.py)
# --------------------------------------------------------------------------

def load_cases() -> list[dict]:
    """Read `eval/tone_cases.jsonl`, skipping the `_comment` documentation lines."""
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


SUBTEXT_CATEGORIES = {"sarcasm", "passive-aggression", "refusal"}


def sarcastic_truth(case: dict) -> bool:
    """Binary truth for the `sarcastic` noul on a tone case."""
    if case["category"] in ("sarcasm", "passive-aggression"):
        return True
    return "sarcastic" in case["expect"] and "positive" not in case["expect"]


def worth_truth(case: dict) -> bool:
    """Binary truth for `worth_reading`: anything but plain/agreeing speech."""
    return case["category"] not in ("literal", "agreement")


def gate_truth(case: dict) -> bool:
    """Lines whose subtext the LLM must not be gated away from (run 6)."""
    if case["category"] in SUBTEXT_CATEGORIES:
        return True
    return case["category"] == "mismatch" and case["id"].endswith("b")


def per_category(rows: list[dict], key: str = "correct") -> dict[str, dict]:
    """`{category: {n, ok, acc}}` over rows carrying `category` and `key`."""
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        agg[r["category"]][0] += 1
        agg[r["category"]][1] += int(bool(r[key]))
    return {c: {"n": n, "ok": ok, "acc": round(ok / n, 3) if n else None}
            for c, (n, ok) in sorted(agg.items())}


def accuracy(rows: list[dict], key: str = "correct") -> float | None:
    return round(sum(int(bool(r[key])) for r in rows) / len(rows), 3) if rows else None


def mismatch_pairs(rows: list[dict], key: str = "correct") -> tuple[int, int]:
    """Pairs (`mm-NNa` / `mm-NNb`) where BOTH halves are correct, majority-voted
    across repeated rows of the same id. Same rule as `eval.model_eval`."""
    by_id: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        if r["category"] == "mismatch":
            by_id[r["id"]].append(bool(r[key]))
    verdict = {cid: sum(v) > len(v) / 2 for cid, v in by_id.items()}
    stems = {cid[:-1] for cid in verdict}
    won = sum(1 for s in stems if verdict.get(f"{s}a") and verdict.get(f"{s}b"))
    return won, len(stems)


def ece(pairs: list[tuple[float, bool]], bins: int = 5) -> dict:
    """Expected calibration error over `bins` equal-width bins on [0, 1].

    Also returns the per-bin table, because with ~34 cases a single ECE number
    hides which end of the scale is miscalibrated.
    """
    if not pairs:
        return {"ece": None, "n": 0, "bins": []}
    table = []
    total = len(pairs)
    e = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        inb = [(p, t) for p, t in pairs if (lo <= p < hi) or (b == bins - 1 and p == 1.0)]
        if not inb:
            table.append({"lo": lo, "hi": hi, "n": 0})
            continue
        conf = sum(p for p, _ in inb) / len(inb)
        acc = sum(t for _, t in inb) / len(inb)
        e += abs(acc - conf) * len(inb) / total
        table.append({"lo": lo, "hi": hi, "n": len(inb), "conf": round(conf, 3), "acc": round(acc, 3)})
    return {"ece": round(e, 4), "n": total, "bins": table}


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})


def fmt_cat(table: dict[str, dict]) -> str:
    return "  ".join(f"{c}={v['ok']}/{v['n']}" for c, v in table.items())


# --------------------------------------------------------------------------
# Asking the case set
# --------------------------------------------------------------------------

CASE_COLUMNS = [
    "id", "category", "transcript", "context", "with_voice", "hint", "expected",
    "tone5", "tone5_prob", "tone5_conf", "tone5_probs", "correct", "polarity3", "polarity3_prob",
    "p_sarcastic", "sarcastic_truth", "p_worth", "worth_truth",
    "latency_ms", "error",
]


def ask_cases(client, cases: list[dict], with_voice: bool, tag: str,
              qnames: Iterable[str] = ALL_QUESTIONS) -> list[dict]:
    """Send every case through the client and return one scored row per case.

    A worker error is recorded in `error` and the row is scored as a miss
    (tone None), never dropped: an unanswered line in production would also
    fall through to the LLM, and the accuracy number should reflect that.
    """
    qs = questions(qnames)
    reqs = [(f"{tag}:{c['id']}", build_state(c["transcript"], c.get("context"), c.get("voice"), with_voice), qs)
            for c in cases]
    replies = client.ask_many(reqs)
    rows = []
    for c, (rid, state, _) in zip(cases, reqs):
        rep = replies.get(rid, {"error": "no reply"})
        ans = parse_answers(rep.get("answers"), qs)
        hint = _describe_voice(c.get("voice")) if with_voice else None
        tone = ans.get("tone5", {}).get("choice")
        rows.append({
            "id": c["id"], "category": c["category"], "transcript": c["transcript"],
            "context": " | ".join(c.get("context") or []), "with_voice": int(with_voice),
            "hint": hint or "", "expected": "|".join(c["expect"]),
            "tone5": tone, "tone5_prob": ans.get("tone5", {}).get("prob"),
            "tone5_conf": ans.get("tone5", {}).get("conf"),
            "tone5_probs": json.dumps(ans.get("tone5", {}).get("probs")) if ans.get("tone5", {}).get("probs") else "",
            "correct": int(tone in c["expect"]),
            "polarity3": ans.get("polarity3", {}).get("choice"),
            "polarity3_prob": ans.get("polarity3", {}).get("prob"),
            "p_sarcastic": ans.get("sarcastic", {}).get("noul"), "sarcastic_truth": int(sarcastic_truth(c)),
            "p_worth": ans.get("worth_reading", {}).get("noul"), "worth_truth": int(worth_truth(c)),
            "latency_ms": rep.get("latency_ms"), "error": rep.get("error", ""),
            "_case": c, "_answers": ans,
        })
    return rows


def score_case_rows(rows: list[dict]) -> dict:
    """Headline numbers for a case run: accuracy, per-category, pairs, ECEs."""
    errs = sum(1 for r in rows if r["error"])
    out = {
        "n": len(rows), "errors": errs,
        "accuracy": accuracy(rows),
        "per_category": per_category(rows),
        "mismatch_pairs": mismatch_pairs(rows),
        "ece_sarcastic": ece([(r["p_sarcastic"], bool(r["sarcastic_truth"])) for r in rows
                              if r["p_sarcastic"] is not None]),
        "ece_worth_reading": ece([(r["p_worth"], bool(r["worth_truth"])) for r in rows
                                  if r["p_worth"] is not None]),
        "latency_ms_p50": _pct([r["latency_ms"] for r in rows if isinstance(r["latency_ms"], (int, float))], 50),
    }
    return out


def print_case_summary(name: str, s: dict) -> None:
    print(f"\n[{name}] n={s['n']} errors={s['errors']} accuracy={s['accuracy']} "
          f"mismatch pairs={s['mismatch_pairs'][0]}/{s['mismatch_pairs'][1]}")
    print("   per category: " + fmt_cat(s["per_category"]))
    print(f"   ECE sarcastic={s['ece_sarcastic']['ece']} (n={s['ece_sarcastic']['n']})  "
          f"ECE worth_reading={s['ece_worth_reading']['ece']} (n={s['ece_worth_reading']['n']})")


# --------------------------------------------------------------------------
# Fusion tables
# --------------------------------------------------------------------------

def _voice_level(voice: dict | None) -> str | None:
    """Collapse a voice dict to 'low' / 'high' / 'mid' / None.

    Valence wins when measured (MERaLiON, hand-labelled cases); otherwise the
    arousal+dominance pair stands in, exactly as the prompt's second rule
    form does for the emotion2vec backend. A reading below
    `SER_MIN_CONFIDENCE` is treated as no voice, matching the prompt's
    "lean on the words" instruction.
    """
    if not voice:
        return None
    conf = voice.get("confidence")
    if isinstance(conf, (int, float)) and conf < config.SER_MIN_CONFIDENCE:
        return None
    val = voice.get("valence")
    if isinstance(val, (int, float)):
        if val < config.VALENCE_LOW:
            return "low"
        if val > config.VALENCE_HIGH:
            return "high"
        return "mid"
    ar, dom = voice.get("arousal"), voice.get("dominance")
    if isinstance(ar, (int, float)) and isinstance(dom, (int, float)):
        if ar < config.AROUSAL_LOW and dom < config.DOMINANCE_LOW:
            return "low"
        if ar > config.AROUSAL_HIGH and dom > config.DOMINANCE_HIGH:
            return "high"
        return "mid"
    return None


# polarity (words) x voice level -> tone. Rows with no voice / mid voice take
# the words literally. The table transcribes the prompt's mismatch rules:
#   positive words + low voice   -> mixed     (sarcasm/masking; "mixed" is the
#                                              hedge, the sarcastic override below
#                                              sharpens it)
#   positive words + high voice  -> positive  (agreement, literal)
#   negative words + high voice  -> mixed     (teasing / banter)
#   negative words + low voice   -> negative  (real anger or resignation: both
#                                              negative, only the prose differs)
#   neutral words  + low voice   -> negative  ("it's fine" that is not fine)
#   neutral words  + high voice  -> neutral   (calm and fine; no over-reading)
FUSION_TABLE: dict[tuple[str, str | None], str] = {
    ("positive", None): "positive", ("positive", "mid"): "positive",
    ("positive", "low"): "mixed", ("positive", "high"): "positive",
    ("negative", None): "negative", ("negative", "mid"): "negative",
    ("negative", "low"): "negative", ("negative", "high"): "mixed",
    ("neutral", None): "neutral", ("neutral", "mid"): "neutral",
    ("neutral", "low"): "negative", ("neutral", "high"): "neutral",
}


def fuse(polarity: str | None, voice: dict | None, p_sarc: float | None = None,
         sarc_theta: float | None = None) -> str | None:
    """Deterministic polarity x voice -> tone, with an optional sarcasm override."""
    if polarity not in ("positive", "negative", "neutral"):
        return None
    tone = FUSION_TABLE[(polarity, _voice_level(voice))]
    if sarc_theta is not None and p_sarc is not None and p_sarc > sarc_theta:
        tone = "sarcastic"
    return tone


# Veto rules for run 4/5 -- Laya second-guesses the LLM only in the two
# directions that matter for this product:
#   (a) the LLM said "sarcastic" but Laya's p(sarcastic) < theta AND its own
#       tone is not sarcastic  -> take Laya's tone. Guards the false-positive
#       trap (plain speech read as sarcasm).
#   (b) the LLM said "neutral" but Laya's tone is one of sarcastic/negative/
#       mixed with probability > theta -> take Laya's tone. Guards the miss
#       (real subtext read as nothing).
VETO_TONES = {"sarcastic", "negative", "mixed"}


def veto(llm_tone: str | None, laya_tone: str | None, laya_prob: float | None,
         p_sarc: float | None, theta: float) -> tuple[str | None, str]:
    """Return (final_tone, rule_fired) where rule_fired is '', 'a' or 'b'."""
    if laya_tone not in TONES:
        return llm_tone, ""
    if llm_tone == "sarcastic" and p_sarc is not None and p_sarc < theta and laya_tone != "sarcastic":
        return laya_tone, "a"
    if llm_tone == "neutral" and laya_tone in VETO_TONES and laya_prob is not None and laya_prob > theta:
        return laya_tone, "b"
    return llm_tone, ""


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

def run_words(client, cases: list[dict], summary: dict) -> list[dict]:
    """Run 1: text + context only, all four questions."""
    rows = ask_cases(client, cases, with_voice=False, tag="r1")
    write_csv(RESULTS / "r1_words.csv", rows, CASE_COLUMNS)
    s = score_case_rows(rows)
    summary["r1_words"] = s
    print_case_summary("r1_words", s)
    return rows


def run_fusion(rows: list[dict], summary: dict) -> None:
    """Run 2: words-only polarity fused with the case's voice through the table."""
    out = []
    for r in rows:
        c = r["_case"]
        voice = c.get("voice")
        plain = fuse(r["polarity3"], voice)
        sarc = {t: fuse(r["polarity3"], voice, r["p_sarcastic"], t) for t in (0.5, 0.7)}
        out.append({
            "id": c["id"], "category": c["category"], "transcript": c["transcript"],
            "expected": "|".join(c["expect"]), "polarity3": r["polarity3"],
            "voice_level": _voice_level(voice) or "", "p_sarcastic": r["p_sarcastic"],
            "fused": plain, "fused_correct": int(plain in c["expect"]),
            "fused_sarc50": sarc[0.5], "fused_sarc50_correct": int(sarc[0.5] in c["expect"]),
            "fused_sarc70": sarc[0.7], "fused_sarc70_correct": int(sarc[0.7] in c["expect"]),
            "error": r["error"],
        })
    write_csv(RESULTS / "r1_fusion.csv", out, list(out[0].keys()) if out else [])
    res = {}
    for key, label in (("fused_correct", "table"), ("fused_sarc50_correct", "table+sarc>0.5"),
                       ("fused_sarc70_correct", "table+sarc>0.7")):
        res[label] = {"accuracy": accuracy(out, key), "per_category": per_category(out, key),
                      "mismatch_pairs": mismatch_pairs(out, key)}
    summary["r1_fusion"] = res
    print("\n[r1_fusion] polarity x voice table (see FUSION_TABLE)")
    for label, s in res.items():
        print(f"   {label:<16} acc={s['accuracy']} pairs={s['mismatch_pairs'][0]}/{s['mismatch_pairs'][1]}  "
              + fmt_cat(s["per_category"]))


def run_voice(client, cases: list[dict], summary: dict, r1_rows: list[dict] | None) -> list[dict]:
    """Run 3: text + context + voice hint. Plus paired deltas vs run 1 on the b-halves."""
    rows = ask_cases(client, cases, with_voice=True, tag="r2")
    write_csv(RESULTS / "r2_voice.csv", rows, CASE_COLUMNS)
    s = score_case_rows(rows)
    summary["r2_voice"] = s
    print_case_summary("r2_voice", s)

    if r1_rows:
        r1 = {r["id"]: r for r in r1_rows}
        deltas = []
        for r in rows:
            if r["category"] != "mismatch" or not r["id"].endswith("b"):
                continue
            a = r1.get(r["id"])
            if not a:
                continue
            p0, p1 = a["p_sarcastic"], r["p_sarcastic"]
            deltas.append({
                "id": r["id"], "transcript": r["transcript"], "hint": r["hint"],
                "p_sarc_words": None if p0 is None else round(p0, 3),
                "p_sarc_voice": None if p1 is None else round(p1, 3),
                "delta": round(p1 - p0, 3) if p0 is not None and p1 is not None else None,
                "tone_words": a["tone5"], "tone_voice": r["tone5"],
                "tone_changed": int(a["tone5"] != r["tone5"]),
                "correct_words": a["correct"], "correct_voice": r["correct"],
            })
        write_csv(RESULTS / "r2_voice_pairs.csv", deltas, list(deltas[0].keys()) if deltas else [])
        rose = sum(1 for d in deltas if d["delta"] is not None and d["delta"] > 0)
        changed = sum(d["tone_changed"] for d in deltas)
        summary["r2_voice"]["b_half_deltas"] = {
            "n": len(deltas), "p_sarc_rose": rose, "tone_changed": changed,
            "mean_delta": round(statistics.fmean([d["delta"] for d in deltas if d["delta"] is not None]), 3)
            if any(d["delta"] is not None for d in deltas) else None,
            "pairs": deltas,
        }
        print(f"   b-halves with hint: p(sarc) rose on {rose}/{len(deltas)}, tone changed on {changed}/{len(deltas)}")
        for d in deltas:
            fmt = lambda v: "-" if v is None else f"{v:.2f}"
            print(f"     {d['id']}: p_sarc {fmt(d['p_sarc_words'])} -> {fmt(d['p_sarc_voice'])} "
                  f"(d={d['delta']})  tone {d['tone_words']} -> {d['tone_voice']}")
    return rows


def _flag_drops(before: dict, after: dict, cats=("literal", "low-confidence")) -> list[str]:
    return [c for c in cats if c in before and c in after and after[c]["ok"] < before[c]["ok"]]


def run_fusion_llm(r2_rows: list[dict], cases: list[dict], summary: dict) -> float:
    """Run 4: veto the LLM's recorded answers (eval/results/model.csv) with Laya.

    Returns the chosen theta: the one with the best mean post-veto accuracy
    across LLMs among those that cost nothing on `literal` / `low-confidence`
    (the false-positive guards). 0.5 if none qualifies.
    """
    if not MODEL_CSV.exists():
        print(f"\n[r2_fusion_llm] skipped: {MODEL_CSV} missing")
        summary["r2_fusion_llm"] = {"skipped": "model.csv missing"}
        return 0.5
    laya = {r["id"]: r for r in r2_rows}
    expect = {c["id"]: c["expect"] for c in cases}
    with MODEL_CSV.open(encoding="utf-8") as f:
        llm_rows = [r for r in csv.DictReader(f) if r["id"] in laya]
    if not llm_rows:
        print("\n[r2_fusion_llm] skipped: no model.csv rows join to tone cases")
        summary["r2_fusion_llm"] = {"skipped": "no joinable rows"}
        return 0.5

    out_rows = []
    per_model: dict[str, dict] = {}
    models = sorted({r["model"] for r in llm_rows})
    for model in models:
        mrows = [r for r in llm_rows if r["model"] == model]
        base = [{"id": r["id"], "category": r["category"], "correct": int(r["got"] in expect[r["id"]])}
                for r in mrows]
        entry = {"n": len(mrows), "before": {
            "accuracy": accuracy(base), "per_category": per_category(base), "mismatch_pairs": mismatch_pairs(base)},
            "after": {}}
        for theta in THETAS:
            after = []
            fired = {"a": 0, "b": 0}
            for r in mrows:
                lr = laya[r["id"]]
                final, rule = veto(r["got"], lr["tone5"], lr["tone5_prob"], lr["p_sarcastic"], theta)
                if rule:
                    fired[rule] += 1
                ok = int(final in expect[r["id"]])
                after.append({"id": r["id"], "category": r["category"], "correct": ok})
                out_rows.append({
                    "model": model, "run": r["run"], "theta": theta, "id": r["id"], "category": r["category"],
                    "expected": "|".join(expect[r["id"]]), "llm_got": r["got"], "laya_tone5": lr["tone5"],
                    "laya_tone5_prob": lr["tone5_prob"], "laya_p_sarcastic": lr["p_sarcastic"],
                    "rule": rule, "final": final, "llm_correct": int(r["got"] in expect[r["id"]]),
                    "final_correct": ok,
                })
            a = {"accuracy": accuracy(after), "per_category": per_category(after),
                 "mismatch_pairs": mismatch_pairs(after), "fired": fired}
            a["guard_drops"] = _flag_drops(entry["before"]["per_category"], a["per_category"])
            entry["after"][str(theta)] = a
        per_model[model] = entry

    write_csv(RESULTS / "r2_fusion_llm.csv", out_rows, list(out_rows[0].keys()))

    # choose theta: no guard drop on any model, best mean accuracy
    best, best_acc = None, -1.0
    for theta in THETAS:
        if any(per_model[m]["after"][str(theta)]["guard_drops"] for m in models):
            continue
        acc = statistics.fmean(per_model[m]["after"][str(theta)]["accuracy"] or 0 for m in models)
        if acc > best_acc:
            best, best_acc = theta, acc
    chosen = best if best is not None else 0.5
    summary["r2_fusion_llm"] = {"per_model": per_model, "chosen_theta": chosen,
                                "chosen_reason": "best mean accuracy with no literal/low-confidence drop"
                                if best is not None else "fallback: every theta dropped a guard category"}

    print(f"\n[r2_fusion_llm] Laya veto over model.csv, chosen theta={chosen}")
    for m in models:
        e = per_model[m]
        b = e["before"]
        print(f"   {m:<12} before acc={b['accuracy']} pairs={b['mismatch_pairs'][0]}/{b['mismatch_pairs'][1]}  "
              + fmt_cat(b["per_category"]))
        for theta in THETAS:
            a = e["after"][str(theta)]
            flag = f"  DROP:{','.join(a['guard_drops'])}" if a["guard_drops"] else ""
            print(f"      theta={theta} acc={a['accuracy']} pairs={a['mismatch_pairs'][0]}/{a['mismatch_pairs'][1]} "
                  f"fired a={a['fired']['a']} b={a['fired']['b']}  " + fmt_cat(a["per_category"]) + flag)
    return chosen


def pipeline_voice(row: dict) -> dict | None:
    """Rebuild a `ser.analyze()`-shaped dict from a pipeline.csv row.

    The emotion2vec cells stored no arousal/dominance (that derivation landed
    after the run), so an empty scale falls back to the LABEL'S ANCHOR from
    `server/ser.py`. That is an approximation: the live value is the
    probability-weighted mean over all 7 classes and sits nearer the middle
    than the hard anchor. Rows carry `voice_source` so the two can be split.
    """
    emo = row.get("ser_emotion") or None
    if not emo:
        return None

    def num(k):
        v = row.get(k)
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None

    voice = {"emotion": emo, "confidence": num("ser_confidence"), "valence": num("ser_valence"),
             "arousal": num("ser_arousal"), "dominance": num("ser_dominance")}
    if voice["arousal"] is None:
        voice["arousal"] = AROUSAL_ANCHOR.get(emo)
    if voice["dominance"] is None:
        voice["dominance"] = DOMINANCE_ANCHOR.get(emo)
    return {k: v for k, v in voice.items() if v is not None}


def run_pipeline(client, summary: dict, theta: float, limit: int | None = None) -> list[dict]:
    """Run 5: Laya over the pipeline eval's real (RAVDESS) transcripts + SER output."""
    if not PIPELINE_CSV.exists():
        print(f"\n[r2_pipeline] skipped: {PIPELINE_CSV} missing")
        summary["r2_pipeline"] = {"skipped": "pipeline.csv missing"}
        return []
    with PIPELINE_CSV.open(encoding="utf-8") as f:
        prow = [r for r in csv.DictReader(f)
                if r.get("vad_dropped") != "1" and (r.get("transcript") or "").strip()]
    if limit:
        prow = prow[:limit]

    # Dedupe: the same file's ASR+SER is replayed across every LLM cell, so the
    # (transcript, hint) key collapses ~3x. Hint is part of the key because the
    # two SER backends produce different hints for the same file.
    qs = questions(PIPELINE_QUESTIONS)
    unique: dict[tuple[str, str], str] = {}
    reqs = []
    for r in prow:
        voice = pipeline_voice(r)
        state = build_state(r["transcript"].strip(), None, voice, with_voice=True)
        key = (r["transcript"].strip(), _describe_voice(voice) or "")
        if key not in unique:
            rid = f"pipe:{len(unique)}"
            unique[key] = rid
            reqs.append((rid, state, qs))
    print(f"\n[r2_pipeline] {len(prow)} rows -> {len(reqs)} unique requests")
    replies = client.ask_many(reqs)
    cache = {key: parse_answers(replies.get(rid, {}).get("answers"), qs) for key, rid in unique.items()}
    errors = {key: replies.get(rid, {}).get("error", "") for key, rid in unique.items()}

    out = []
    for r in prow:
        voice = pipeline_voice(r)
        key = (r["transcript"].strip(), _describe_voice(voice) or "")
        ans = cache[key]
        tone = ans["tone5"]["choice"]
        exp = r.get("tone_expected") or None
        llm_tone = r.get("llm_tone") or None
        final, rule = veto(llm_tone, tone, ans["tone5"]["prob"], ans["sarcastic"]["noul"], theta)
        out.append({
            "cell_id": r["cell_id"], "ser_backend": r["ser_backend"], "llm_model": r["llm_model"],
            "file": r["file"], "true_emotion": r["true_emotion"], "transcript": r["transcript"].strip(),
            "voice_source": "measured" if r.get("ser_arousal") else "label-anchor",
            "hint": key[1], "tone_expected": exp,
            "llm_tone": llm_tone, "llm_correct": int(llm_tone == exp) if llm_tone and exp else None,
            "laya_tone5": tone, "laya_tone5_prob": ans["tone5"]["prob"], "laya_tone5_conf": ans["tone5"]["conf"],
            "laya_correct": int(tone == exp) if exp else None,
            "p_sarcastic": ans["sarcastic"]["noul"], "p_worth": ans["worth_reading"]["noul"],
            "worth_truth": int(exp != "neutral") if exp else None,
            "theta": theta, "rule": rule, "final_tone": final,
            "final_correct": int(final == exp) if final and exp else None,
            "error": errors[key],
        })
    write_csv(RESULTS / "r2_pipeline.csv", out, list(out[0].keys()) if out else [])

    cells: dict[str, dict] = {}
    for cell in sorted({(r["ser_backend"], r["llm_model"]) for r in out}):
        rows = [r for r in out if (r["ser_backend"], r["llm_model"]) == cell and r["tone_expected"]]
        fired = {"a": sum(r["rule"] == "a" for r in rows), "b": sum(r["rule"] == "b" for r in rows)}
        cells["+".join(cell)] = {
            "n": len(rows),
            "llm_accuracy": accuracy(rows, "llm_correct"),
            "laya_accuracy": accuracy(rows, "laya_correct"),
            "fused_accuracy": accuracy(rows, "final_correct"),
            "fired": fired,
            "errors": sum(1 for r in rows if r["error"]),
        }
    summary["r2_pipeline"] = {"theta": theta, "rows": len(out), "unique_requests": len(reqs), "cells": cells}
    print(f"   theta={theta}")
    for name, c in cells.items():
        print(f"   {name:<24} n={c['n']:<4} llm={c['llm_accuracy']} laya={c['laya_accuracy']} "
              f"fused={c['fused_accuracy']} fired a={c['fired']['a']} b={c['fired']['b']} errors={c['errors']}")
    return out


def run_gate(sources: dict[str, list[tuple[float | None, bool]]], summary: dict) -> None:
    """Run 6: if lines with p(worth_reading) < theta skipped the LLM, what is lost?

    `sources` maps a name to (p_worth, truly_subtext) pairs. `skip_frac` is
    the compute saved; `lost_frac` is the fraction of subtext-carrying lines
    that would never get a read -- the product cost. Both curves per source.
    """
    curve = []
    res: dict[str, list] = {}
    for name, pairs in sources.items():
        pairs = [(p, t) for p, t in pairs if p is not None]
        n_sub = sum(t for _, t in pairs)
        res[name] = []
        for theta in GATE_THETAS:
            skipped = [(p, t) for p, t in pairs if p < theta]
            lost = sum(t for _, t in skipped)
            point = {"source": name, "theta": theta, "n": len(pairs), "n_subtext": n_sub,
                     "skip_frac": round(len(skipped) / len(pairs), 3) if pairs else None,
                     "lost_frac": round(lost / n_sub, 3) if n_sub else None}
            curve.append(point)
            res[name].append(point)
    write_csv(RESULTS / "r3_gate.csv", curve, ["source", "theta", "n", "n_subtext", "skip_frac", "lost_frac"])
    summary["r3_gate"] = res
    print("\n[r3_gate] skip the LLM when p(worth_reading) < theta")
    for name, pts in res.items():
        print(f"   {name}: " + "  ".join(f"t{p['theta']}:skip={p['skip_frac']}/lost={p['lost_frac']}" for p in pts))


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(q / 100 * (len(s) - 1)))))
    return round(s[k], 2)


def _lat_stats(xs: list[float]) -> dict:
    return {"n": len(xs), "p50_ms": _pct(xs, 50), "p95_ms": _pct(xs, 95),
            "max_ms": round(max(xs), 2) if xs else None}


def _time_calls(client, qs: dict, n: int, state: str) -> tuple[list[float], list[float]]:
    """Sequential calls (no pipelining -- this measures one line's wait).

    Returns (wall_ms, worker_reported_ms): the gap between them is the pipe +
    JSON overhead, worth knowing on its own.
    """
    wall, inner = [], []
    for i in range(n):
        t0 = time.perf_counter()
        rep = client.ask(f"lat:{qs and '+'.join(qs)}:{i}", state, qs)
        wall.append((time.perf_counter() - t0) * 1000)
        if isinstance(rep.get("latency_ms"), (int, float)):
            inner.append(float(rep["latency_ms"]))
    return wall, inner


def run_latency(client, summary: dict, n: int, dry: bool) -> None:
    """Run 7: warm latency alone, then with Whisper contending for the CPU.

    Whisper and SER own the CPU in production; a helper that looks fast on an
    idle machine but doubles under a concurrent transcribe is not fast. The
    contended pass runs `server.transcriber.transcribe` on `experiments/
    speech.wav` in a loop on a thread for the duration of the timing.
    """
    state = build_state("Oh wonderful. That is just perfect.", [],
                        {"emotion": "angry", "confidence": 0.77, "arousal": 0.68, "dominance": 0.7}, True)
    q1, q3 = questions(("tone5",)), questions(PIPELINE_QUESTIONS)
    for _ in range(3):                                  # warm
        client.ask("lat:warm", state, q3)
    res: dict[str, Any] = {"n": n}
    w, i = _time_calls(client, q1, n, state)
    res["tone5_alone"] = {"wall": _lat_stats(w), "worker": _lat_stats(i)}
    w, i = _time_calls(client, q3, n, state)
    res["three_alone"] = {"wall": _lat_stats(w), "worker": _lat_stats(i)}

    if dry:
        res["three_with_whisper"] = {"skipped": "dry run: transcriber not loaded"}
    else:
        try:
            from server import transcriber   # lazy: loads config.ASR_MODEL at import
            with wave.open(str(SPEECH_WAV)) as wf:
                pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            audio = pcm.astype(np.float32) / 32768.0
            stop = threading.Event()
            counts = {"n": 0}

            def loop():
                transcriber.transcribe(audio)          # warm before timing starts
                while not stop.is_set():
                    transcriber.transcribe(audio)
                    counts["n"] += 1

            th = threading.Thread(target=loop, daemon=True)
            th.start()
            time.sleep(2.0)                             # let the first (warm) call land
            w, i = _time_calls(client, q3, n, state)
            stop.set()
            th.join(timeout=60)
            res["three_with_whisper"] = {"wall": _lat_stats(w), "worker": _lat_stats(i),
                                         "whisper_calls_during": counts["n"],
                                         "asr_model": transcriber.status().get("key") if hasattr(transcriber, "status") else None}
        except Exception as e:                          # missing model files, etc.
            res["three_with_whisper"] = {"error": f"{type(e).__name__}: {e}"}
    summary["latency"] = res
    print(f"\n[latency] n={n} sequential warm calls (wall clock, ms)")
    for k in ("tone5_alone", "three_alone", "three_with_whisper"):
        v = res[k]
        if "wall" in v:
            print(f"   {k:<20} p50={v['wall']['p50_ms']} p95={v['wall']['p95_ms']} max={v['wall']['max_ms']}"
                  f"   (worker-reported p50={v['worker']['p50_ms']})"
                  + (f"  whisper calls={v['whisper_calls_during']}" if "whisper_calls_during" in v else ""))
        else:
            print(f"   {k:<20} {v}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

RUN_NAMES = {1: "r1_words", 2: "r1_fusion", 3: "r2_voice", 4: "r2_fusion_llm",
             5: "r2_pipeline", 6: "r3_gate", 7: "latency"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--quick", action="store_true", help="runs 1,3,7 with a short latency pass")
    ap.add_argument("--runs", default=None, help="comma list of run numbers, e.g. 1,3 (default all)")
    ap.add_argument("--worker", default=str(DEFAULT_WORKER), help="path to worker.py")
    ap.add_argument("--python", default=str(DEFAULT_PYTHON), help="interpreter for the worker")
    ap.add_argument("--dry-run", action="store_true", help="fake client with fixed answers; no worker")
    ap.add_argument("--latency-n", type=int, default=None, help="calls per latency pass (200; quick 20)")
    ap.add_argument("--pipeline-limit", type=int, default=None, help="cap pipeline rows (debugging)")
    args = ap.parse_args()

    if args.runs:
        selected = {int(x) for x in args.runs.split(",") if x.strip()}
    elif args.quick:
        selected = {1, 3, 7}
    else:
        selected = set(RUN_NAMES)
    # dependencies: 2 needs 1; 4 and 6 need 3 (6 also wants 1 and 5 if present)
    if 2 in selected:
        selected.add(1)
    if 4 in selected:
        selected.add(3)
    if 6 in selected and not selected & {1, 3, 5}:
        selected |= {1, 3}
    lat_n = args.latency_n or (20 if args.quick else 200)

    RESULTS.mkdir(parents=True, exist_ok=True)
    print(f"runs: {', '.join(RUN_NAMES[k] for k in sorted(selected))}"
          + ("   [DRY RUN -- fake answers, measures nothing]" if args.dry_run else ""))

    t0 = time.time()
    if args.dry_run:
        client: Any = FakeClient()
    else:
        client = LayaClient(Path(args.worker), Path(args.python))
    summary: dict[str, Any] = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "dry_run": args.dry_run,
        "worker_ready": client.ready, "runs": sorted(RUN_NAMES[k] for k in selected),
    }
    cases = load_cases()
    r1_rows = r2_rows = None
    pipe_rows: list[dict] = []
    chosen_theta = 0.5
    try:
        if 1 in selected:
            r1_rows = run_words(client, cases, summary)
        if 2 in selected and r1_rows:
            run_fusion(r1_rows, summary)
        if 3 in selected:
            r2_rows = run_voice(client, cases, summary, r1_rows)
        if 4 in selected and r2_rows:
            chosen_theta = run_fusion_llm(r2_rows, cases, summary)
        if 5 in selected:
            pipe_rows = run_pipeline(client, summary, chosen_theta, args.pipeline_limit)
        if 6 in selected:
            sources = {}
            if r1_rows:
                sources["tone_cases_words"] = [(r["p_worth"], gate_truth(r["_case"])) for r in r1_rows]
            if r2_rows:
                sources["tone_cases_voice"] = [(r["p_worth"], gate_truth(r["_case"])) for r in r2_rows]
            if pipe_rows:
                sources["pipeline"] = [(r["p_worth"], bool(r["worth_truth"])) for r in pipe_rows
                                       if r["worth_truth"] is not None]
            run_gate(sources, summary)
        if 7 in selected:
            run_latency(client, summary, lat_n, args.dry_run)
    finally:
        client.close()

    summary["elapsed_s"] = round(time.time() - t0, 1)
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print("\n==== summary ====")
    for key in ("r1_words", "r2_voice"):
        if key in summary and "accuracy" in summary[key]:
            s = summary[key]
            print(f"{key:<16} acc={s['accuracy']} pairs={s['mismatch_pairs'][0]}/{s['mismatch_pairs'][1]} "
                  f"ECE sarc={s['ece_sarcastic']['ece']} worth={s['ece_worth_reading']['ece']} errors={s['errors']}")
    if "r1_fusion" in summary:
        print("r1_fusion        " + "  ".join(f"{k}={v['accuracy']}" for k, v in summary["r1_fusion"].items()))
    if "r2_fusion_llm" in summary and "per_model" in summary["r2_fusion_llm"]:
        f = summary["r2_fusion_llm"]
        t = str(f["chosen_theta"])
        print(f"r2_fusion_llm    theta={t}  " + "  ".join(
            f"{m}:{e['before']['accuracy']}->{e['after'][t]['accuracy']}" for m, e in f["per_model"].items()))
    if "r2_pipeline" in summary and "cells" in summary["r2_pipeline"]:
        print("r2_pipeline      " + "  ".join(
            f"{k}:llm {c['llm_accuracy']}/laya {c['laya_accuracy']}/fused {c['fused_accuracy']}"
            for k, c in summary["r2_pipeline"]["cells"].items()))
    if "latency" in summary:
        l = summary["latency"]
        print("latency          " + "  ".join(
            f"{k}:p50={v['wall']['p50_ms']}ms p95={v['wall']['p95_ms']}ms" for k, v in l.items()
            if isinstance(v, dict) and "wall" in v))
    print(f"wrote {RESULTS / 'summary.json'}  ({summary['elapsed_s']}s)")


if __name__ == "__main__":
    main()
