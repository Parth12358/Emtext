"""In-process metrics for the diagnostics dashboard.

Deliberately memory-only: the brief rules out a database, and for a single-user
tool the interesting window is "what is happening now and over the last few
minutes", not history. Everything here is bounded -- rolling deques with a fixed
maxlen -- so a server left running for days cannot grow without limit.

Three sources are combined:

  - **Pipeline counters** recorded by `main.py` as utterances flow through. These
    are the ones nothing else can tell you: how long Whisper actually took on
    *your* audio, how often the interpreter fell back to offline, what the VAD
    is discarding.
  - **Process/system stats** from psutil -- cheap enough to read on demand.
  - **GPU stats**, which are not cheap. On Windows the per-engine counters cost
    ~2.6s to query, so they are polled on a background thread and served from a
    cache. Never blocks a request; returns None rather than guessing when
    unavailable (non-Windows, permission denied, no counters).

Nothing here raises. A dashboard that takes the server down with it would be
worse than no dashboard.
"""

from __future__ import annotations

import os
import platform
import re
import statistics
import subprocess
import threading
import time
from collections import Counter, deque
from typing import Any

# Rolling windows. 200 utterances is a long conversation; 60 samples of system
# stats at ~1/s is a minute of history for the sparklines.
_WINDOW = 200
_SYS_WINDOW = 60

_started_at = time.time()
_lock = threading.Lock()

# --- pipeline counters ------------------------------------------------------
_counters: Counter[str] = Counter()
_latencies: dict[str, deque[float]] = {
    "whisper": deque(maxlen=_WINDOW),
    "ser": deque(maxlen=_WINDOW),
    "cpu_stage": deque(maxlen=_WINDOW),
    "llm": deque(maxlen=_WINDOW),
    "total": deque(maxlen=_WINDOW),
}
_tones: Counter[str] = Counter()
_emotions: Counter[str] = Counter()
_recent: deque[dict] = deque(maxlen=40)
_connections_active = 0

# --- system sampling --------------------------------------------------------
_sys_history: deque[dict] = deque(maxlen=_SYS_WINDOW)


def connection_opened() -> None:
    global _connections_active
    with _lock:
        _connections_active += 1
        _counters["connections_total"] += 1


def connection_closed() -> None:
    global _connections_active
    with _lock:
        _connections_active = max(0, _connections_active - 1)


def record_utterance(
    *,
    transcript: str,
    tone: str | None,
    read: str | None,
    whisper_s: float,
    ser_s: float,
    cpu_stage_s: float,
    llm_s: float,
    voice: dict | None,
    offline: bool,
) -> None:
    """One completed utterance, start to finish."""
    with _lock:
        _counters["utterances"] += 1
        if offline:
            _counters["llm_offline"] += 1
        _latencies["whisper"].append(whisper_s)
        _latencies["ser"].append(ser_s)
        _latencies["cpu_stage"].append(cpu_stage_s)
        _latencies["llm"].append(llm_s)
        _latencies["total"].append(cpu_stage_s + llm_s)
        if tone:
            _tones[tone] += 1
        if voice and voice.get("emotion"):
            _emotions[voice["emotion"]] += 1
        _recent.appendleft({
            "t": time.time(),
            "transcript": transcript,
            "tone": tone,
            "read": read,
            "voice": voice,
            "whisper_s": round(whisper_s, 3),
            "ser_s": round(ser_s, 3),
            "cpu_stage_s": round(cpu_stage_s, 3),
            "llm_s": round(llm_s, 3),
            "total_s": round(cpu_stage_s + llm_s, 3),
            "offline": offline,
        })


def record_event(name: str, n: int = 1) -> None:
    """Bump a named counter (empty transcripts, VAD discards, errors...)."""
    with _lock:
        _counters[name] += n


def _summary(values: deque[float]) -> dict[str, Any]:
    """mean / median / p95 / last, or nulls when there is nothing yet."""
    if not values:
        return {"n": 0, "mean": None, "median": None, "p95": None, "last": None}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": round(p95, 3),
        "last": round(values[-1], 3),
    }


def pipeline_stats() -> dict:
    with _lock:
        return {
            "uptime_s": round(time.time() - _started_at, 1),
            "connections_active": _connections_active,
            "counters": dict(_counters),
            "latency": {k: _summary(v) for k, v in _latencies.items()},
            "tones": dict(_tones),
            "emotions": dict(_emotions),
            "recent": list(_recent),
        }


# ---------------------------------------------------------------------------
# System + GPU
# ---------------------------------------------------------------------------

def system_stats() -> dict:
    """CPU and memory. Cheap enough to read per request."""
    try:
        import psutil
    except ImportError:
        return {"available": False, "reason": "psutil not installed"}

    try:
        proc = psutil.Process()
        vm = psutil.virtual_memory()
        with proc.oneshot():
            rss = proc.memory_info().rss
            # Per-process CPU is scaled by core count, so a fully-busy 6-core box
            # reads 600%. Divide so it means the same thing as the system figure.
            proc_cpu = proc.cpu_percent(None) / (psutil.cpu_count() or 1)
        return {
            "available": True,
            "cpu_percent": psutil.cpu_percent(None),
            "cpu_per_core": psutil.cpu_percent(None, percpu=True),
            "cores_physical": psutil.cpu_count(logical=False),
            "cores_logical": psutil.cpu_count(),
            "ram_used_gb": round(vm.used / 1e9, 2),
            "ram_total_gb": round(vm.total / 1e9, 2),
            "ram_percent": vm.percent,
            "proc_rss_gb": round(rss / 1e9, 2),
            "proc_cpu_percent": round(proc_cpu, 1),
            "threads": proc.num_threads(),
        }
    except Exception as exc:  # noqa: BLE001 -- diagnostics must never 500
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


# --- GPU: polled on a thread, served from cache -----------------------------
# Windows exposes per-engine GPU counters for every vendor, which is the only
# portable way to see an Intel Arc here (nvidia-smi obviously does not apply and
# Intel's xpu-smi is Linux/datacenter). They cost ~2.6s to query, hence the
# cache: a dashboard poll must never wait on this.
_gpu_cache: dict = {"available": None, "reason": "not sampled yet"}
_gpu_thread: threading.Thread | None = None

# Absolute path, not the bare name "powershell". CreateProcess resolves a bare
# name against the application directory and then the CURRENT WORKING DIRECTORY
# before System32 -- and the server's cwd is the repo root. Anything that can
# drop a file there (a malicious dependency's setup.py, an unpacked dataset)
# would get executed as the server user every poll. The command itself was never
# injectable (argv list, constant script); this closes the other half.
_POWERSHELL = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
)

_PS_GPU = r"""
$ErrorActionPreference='SilentlyContinue'
$u = (Get-Counter '\GPU Engine(*)\Utilization Percentage').CounterSamples |
     Where-Object { $_.CookedValue -gt 0 }
$byType = @{}
foreach ($s in $u) {
  if ($s.InstanceName -match 'engtype_(\w+)') {
    $t = $Matches[1]
    $byType[$t] = [double]$byType[$t] + [double]$s.CookedValue
  }
}
$m = (Get-Counter '\GPU Process Memory(*)\Local Usage').CounterSamples |
     Where-Object { $_.CookedValue -gt 0 }
$tot = ($m | Measure-Object CookedValue -Sum).Sum
$parts = @()
foreach ($k in $byType.Keys) { $parts += ('{0}={1:N1}' -f $k, $byType[$k]) }
Write-Output ('ENGINES ' + ($parts -join ' '))
Write-Output ('MEMBYTES {0}' -f [long]$tot)
"""


def _poll_gpu_once() -> dict:
    if platform.system() != "Windows":
        return {"available": False,
                "reason": f"GPU counters are Windows-only (running {platform.system()})"}
    try:
        out = subprocess.run(
            [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", _PS_GPU],
            capture_output=True, text=True, timeout=25,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    engines: dict[str, float] = {}
    mem_bytes = 0
    for line in out.splitlines():
        if line.startswith("ENGINES"):
            for m in re.finditer(r"(\w+)=([\d.]+)", line):
                engines[m.group(1)] = float(m.group(2))
        elif line.startswith("MEMBYTES"):
            try:
                mem_bytes = int(line.split()[1])
            except (IndexError, ValueError):
                pass

    if not engines and not mem_bytes:
        return {"available": False, "reason": "no GPU counters returned"}
    return {
        "available": True,
        # Engines are summed per type across processes, so a value can exceed
        # 100 when several processes use the same engine type. Reported raw
        # rather than clamped, because clamping would hide exactly that.
        "engines": {k: round(v, 1) for k, v in sorted(engines.items())},
        "busiest": max(engines.values()) if engines else 0.0,
        "mem_used_gb": round(mem_bytes / 1e9, 2),
        "sampled_at": time.time(),
    }


def _gpu_loop(interval: float) -> None:
    global _gpu_cache
    while True:
        result = _poll_gpu_once()
        _gpu_cache = result
        # Back off hard if it is not going to work, rather than spawning a
        # PowerShell process every few seconds forever for nothing.
        time.sleep(interval if result.get("available") else max(interval, 300))


def start_gpu_poller(interval: float = 5.0) -> None:
    """Begin background GPU sampling. Safe to call more than once."""
    global _gpu_thread
    if _gpu_thread is not None:
        return
    _gpu_thread = threading.Thread(target=_gpu_loop, args=(interval,), daemon=True)
    _gpu_thread.start()


def gpu_stats() -> dict:
    return dict(_gpu_cache)


def sample_system() -> None:
    """Append one point to the rolling history that drives the sparklines."""
    sys_stats = system_stats()
    gpu = gpu_stats()
    with _lock:
        _sys_history.append({
            "t": time.time(),
            "cpu": sys_stats.get("cpu_percent"),
            "ram": sys_stats.get("ram_percent"),
            "gpu": gpu.get("busiest") if gpu.get("available") else None,
            "gpu_mem_gb": gpu.get("mem_used_gb") if gpu.get("available") else None,
        })


def history() -> list[dict]:
    with _lock:
        return list(_sys_history)
