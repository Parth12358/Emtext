"""Laya worker: one resident English checkpoint, driven over stdin/stdout JSON lines.

Why a separate process: ``laya`` needs transformers 5.x and the main venv pins
``transformers<5`` (MERaLiON's remote code breaks under 5). So laya lives in its own
``.venv-laya/`` and anything in the main venv talks to it through this worker instead
of importing it. Run it with the isolated interpreter::

    .venv-laya/Scripts/python experiments/laya/worker.py

Environment: ``HF_HUB_DISABLE_SYMLINKS=1`` (Windows HF cache without symlink
privileges) and ``USE_TF=0`` (the model card warns of a TensorFlow/abseil deadlock)
are both set at the top of this module before anything heavy is imported, so the
caller does not have to. CPU only, by design -- the GPU is the LLM's.

Protocol (all lines are UTF-8 JSON, one object per line, flushed immediately):

1. On start the worker loads the checkpoint and prints ONE line::

       {"ready": true, "load_s": <float>, "rss_mb": <float>,
        "head_max_len": <int>, "max_len": <int>}

   If the load fails it prints ``{"ready": false, "error": "<str>"}`` and exits 1.

2. Then it reads stdin line by line. Each request line is::

       {"id": <str>, "state": <str | dict | list>, "questions": {<qid>: <qdef>, ...}}

   ``state`` and ``questions`` are passed straight to ``Agent.predict`` -- a question
   definition is laya's own shape, e.g.
   ``{"type": "choice", "instructions": "...", "criteria": {"opt": "desc", ...}}``,
   ``{"type": "score", "instructions": "...", "criteria": ["lvl0", "lvl1", ...]}`` or
   ``{"type": "noul", "instructions": "..."}``.

3. For each request exactly ONE reply line is written, in order::

       {"id": <str>, "answers": <result["answers"]>, "latency_ms": <float>,
        "usage": <result["usage"]>}

   ``answers`` is laya's raw dict keyed by question id. Per type:

   - choice: ``{"type": "choice", "choice": <opt>, "probabilities": {<opt>: p, ...},
     "confidence": float, "action": {"act_probability": float}}``
   - score:  ``{"type": "score", "score": float, "legend": {"0": ..}, "probabilities":
     {"0": p, ...}, "confidence": float, "action": {...}}``
   - noul:   ``{"type": "noul", "noul": p_yes, "confidence": max(p, 1-p),
     "action": {...}}``

   On any per-request failure (bad JSON, missing keys, laya raising, e.g. options
   exceeding ``head_max_len``) the reply is ``{"id": <str or null>, "error": <str>}``
   and the worker keeps serving. Blank lines are ignored. EOF on stdin ends the worker.

Nothing but protocol lines goes to stdout; diagnostics go to stderr.
"""
from __future__ import annotations

import os

# Must precede every transformers / huggingface_hub import.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from typing import Any, Dict, Optional  # noqa: E402

MODEL_ID = os.environ.get("LAYA_MODEL", "convaiinnovations/laya")  # repo root == English


def _rss_mb() -> float:
    """Resident set size of this process in MiB; psutil if present, else a Windows/Linux fallback."""
    try:
        import psutil  # type: ignore

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            k32 = ctypes.windll.kernel32
            # GetCurrentProcess returns the pseudo-handle -1; without explicit argtypes
            # ctypes tries to pass it as a C int and overflows on 64-bit.
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            fn = ctypes.windll.psapi.GetProcessMemoryInfo
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
            fn.restype = wintypes.BOOL
            if fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return pmc.WorkingSetSize / (1024 * 1024)
        else:
            import resource

            kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return kb / 1024.0  # Linux reports KiB
    except Exception:
        pass
    return -1.0


def _emit(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _load():
    import torch

    torch.set_num_threads(max(1, os.cpu_count() or 1))
    from laya import Agent

    # Explicit device="cpu": Agent() would otherwise probe CUDA; never use the GPU here.
    return Agent(MODEL_ID, device="cpu")


def main() -> int:
    t0 = time.perf_counter()
    try:
        agent = _load()
    except Exception as e:  # noqa: BLE001
        _emit({"ready": False, "error": "%s: %s" % (type(e).__name__, e)})
        return 1
    _emit({
        "ready": True,
        "load_s": round(time.perf_counter() - t0, 3),
        "rss_mb": round(_rss_mb(), 1),
        "head_max_len": int(agent.cfg.get("head_max_len", 192)),
        "max_len": int(agent.cfg.get("max_len", 512)),
    })

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid: Optional[str] = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            state = req["state"]
            questions = req["questions"]
            t1 = time.perf_counter()
            result = agent.predict(state, questions)
            _emit({
                "id": rid,
                "answers": result["answers"],
                "latency_ms": round((time.perf_counter() - t1) * 1000.0, 2),
                "usage": result.get("usage"),
            })
        except Exception as e:  # noqa: BLE001 -- one bad request must not kill the worker
            _emit({"id": rid, "error": "%s: %s" % (type(e).__name__, e)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
