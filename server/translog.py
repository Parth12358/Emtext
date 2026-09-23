"""Transcript log: a running record of what the server heard and what it said.

Why this exists
---------------
Every ASR decision in this repo so far rests on eight lines of synthetic SAPI
speech, and `docs/status/TODO.md` says the same thing in three places: record
real utterances first. Nobody was going to sit down and do that by hand. So the
server can keep its own record as it runs -- one JSON line per utterance with
whichever parts are switched on (transcript, ASR model, tone and read, speaker
label, stage timings) plus the utterance's audio as a WAV. Later,
`python -m server.translog --export DIR` turns a slice of that into the
`experiments/asr/` shape (`name.wav` + `name.txt`) that `eval.asr_eval --dir`
scores, with the logged transcript as a draft reference to correct by hand.
That closes the loop: the model that transcribes best on *this* mic in *this*
room wins.

Off by default, part by part
----------------------------
With audio on this retains third-party speech, which the pendant requirements
say should not happen by default. So nothing is logged until someone turns it
on: the dashboard's "Transcript log" card has a master switch and one checkbox
per field (`FIELDS` below), all off, and changes last for the server's lifetime
only -- a restart returns to `config.TRANSCRIPT_LOG_ENABLED` /
`TRANSCRIPT_LOG_FIELDS`, the same rule as the model switches. The record always
carries id, timestamp, duration and the ASR model key; everything else is a
field you opted into.

What it is not
--------------
Not a database (the brief rules one out): a directory is the index, like
`clips.py`. Not the clips feature: clips are moments the listener chose to
keep, uncapped in meaning and capped in count; this is a rolling engineering
log that deletes its oldest audio past `TRANSCRIPT_LOG_MAX_AUDIO`.

Contract (same as clips.py and metrics.py)
------------------------------------------
Blocking file I/O; `main.py` runs it in the executor and does not wait for it.
**Never raises** -- a failed write is a logged warning, never a lost read. Only
this module knows the file layout.
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import shutil
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from . import config

log = logging.getLogger(__name__)

# The parts of an utterance that can be kept, in the order the dashboard shows
# them. Each maps to the entry keys it governs; `audio` governs the WAV.
FIELDS: dict[str, tuple[str, ...]] = {
    "audio": (),
    "transcript": ("transcript",),
    "result": ("tone", "emotion", "read", "confidence"),
    "voice": ("voice",),
    "speaker": ("speaker",),
    "timing": ("whisper_s", "ser_s", "llm_s"),
}

_lock = threading.Lock()
# Number of WAVs on disk, counted once from the directory and then maintained
# in memory, so the rolling cap costs one stat per write rather than a glob.
_audio_count: int | None = None

# Runtime state. Seeded from config at import, mutated only by set_options()
# (the dashboard) -- deliberately NOT persisted, like config.OLLAMA_MODEL.
_enabled: bool = config.TRANSCRIPT_LOG_ENABLED
_fields: set[str] = {
    f.strip() for f in config.TRANSCRIPT_LOG_FIELDS.split(",") if f.strip() in FIELDS
}


def _root() -> Path:
    d = Path(config.TRANSCRIPT_LOG_DIR)
    return d if d.is_absolute() else Path(__file__).resolve().parent.parent / d


def _audio_dir() -> Path:
    return _root() / "audio"


def enabled() -> bool:
    """True when something would actually be written for an utterance."""
    return _enabled and bool(_fields)


def options() -> dict:
    """The current switches, in the shape the dashboard edits."""
    return {"enabled": _enabled, "fields": {f: f in _fields for f in FIELDS}}


def set_options(enabled: bool | None = None, fields: dict[str, bool] | None = None) -> dict:
    """Change the switches for this process's lifetime. Unknown fields are ignored."""
    global _enabled, _fields
    with _lock:
        if enabled is not None:
            _enabled = bool(enabled)
        if fields:
            for name, on in fields.items():
                if name in FIELDS:
                    (_fields.add if on else _fields.discard)(name)
    log.info("transcript log: %s, fields=%s", "on" if _enabled else "off",
             ",".join(sorted(_fields)) or "none")
    return options()


def _count_audio() -> int:
    global _audio_count
    if _audio_count is None:
        d = _audio_dir()
        _audio_count = sum(1 for _ in d.glob("*.wav")) if d.is_dir() else 0
    return _audio_count


def _evict_oldest(n: int) -> None:
    """Delete the n oldest WAVs. Names start with a timestamp, so sorted == oldest first."""
    global _audio_count
    d = _audio_dir()
    for p in sorted(d.glob("*.wav"))[:n]:
        try:
            p.unlink()
            _audio_count = max(0, (_audio_count or 1) - 1)
        except OSError as exc:
            log.debug("transcript log: could not evict %s (%s)", p.name, exc)


def record(entry: dict[str, Any], audio: np.ndarray | None) -> bool:
    """Append one utterance to today's JSONL and (if switched on) save its WAV.

    `entry` is whatever main.py knows about the utterance; only the keys whose
    field is switched on are kept. `audio` is the float32 16 kHz buffer the
    pipeline ran on. **Blocking** -- executor only. Returns True on success;
    never raises.
    """
    global _audio_count
    if not enabled():
        return False
    try:
        keep = {k for f in _fields for k in FIELDS[f]}
        root = _root()
        root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(now))
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        name = f"{stamp}_{int(entry.get('uid', 0))}_{secrets.token_hex(2)}"

        audio_rel: str | None = None
        with _lock:
            if "audio" in _fields and audio is not None and audio.size:
                adir = _audio_dir()
                adir.mkdir(parents=True, exist_ok=True)
                over = _count_audio() + 1 - config.TRANSCRIPT_LOG_MAX_AUDIO
                if over > 0:
                    _evict_oldest(over)
                pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
                with wave.open(str(adir / f"{name}.wav"), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(config.SAMPLE_RATE)
                    w.writeframes(pcm)
                _audio_count = _count_audio() + 1
                audio_rel = f"audio/{name}.wav"

            row = {
                "id": name,
                "ts": round(now, 3),
                "duration_s": round(float(audio.size) / config.SAMPLE_RATE, 3)
                if audio is not None else None,
                "asr_model": entry.get("asr_model"),
                "audio": audio_rel,
                **{k: v for k, v in entry.items() if k in keep},
                "uid": entry.get("uid"),
            }
            with (root / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 -- a log must never cost a read
        log.warning("transcript log write failed (%s: %s)", type(exc).__name__, exc)
        return False


def stats() -> dict:
    """What the dashboard shows: switches, where, how much."""
    root = _root()
    days = sorted(p.stem for p in root.glob("*.jsonl")) if root.is_dir() else []
    return {
        **options(),
        "active": enabled(),
        "dir": str(root),
        "days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "audio_files": _count_audio(),
        "max_audio": config.TRANSCRIPT_LOG_MAX_AUDIO,
    }


# ---------------------------------------------------------------------------
# Offline helpers: python -m server.translog --stats | --export DIR [--days N]
# ---------------------------------------------------------------------------

def _rows(days: int | None) -> list[dict]:
    root = _root()
    files = sorted(root.glob("*.jsonl")) if root.is_dir() else []
    if days:
        files = files[-days:]
    out: list[dict] = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def export(dest: Path, days: int | None, include_user: bool) -> int:
    """Copy logged utterances into an `experiments/asr/`-shaped folder.

    Each becomes `<id>.wav` + `<id>.txt`, the .txt holding the logged transcript
    as a DRAFT reference. Rows logged without audio or without a transcript are
    skipped: both are needed to score anything.
    """
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in _rows(days):
        if not r.get("audio") or not r.get("transcript"):
            continue
        if not include_user and (r.get("speaker") or {}).get("label") == "user":
            continue
        src = _root() / r["audio"]
        if not src.is_file():
            continue
        shutil.copyfile(src, dest / f"{r['id']}.wav")
        (dest / f"{r['id']}.txt").write_text(r["transcript"].strip() + "\n", encoding="utf-8")
        n += 1
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stats", action="store_true", help="print what is logged")
    ap.add_argument("--export", metavar="DIR", help="write wav+txt pairs for eval.asr_eval --dir")
    ap.add_argument("--days", type=int, default=None, help="only the most recent N days")
    ap.add_argument("--include-user", action="store_true",
                    help="export the listener's own lines too (skipped by default)")
    args = ap.parse_args()
    if args.export:
        n = export(Path(args.export), args.days, args.include_user)
        print(f"exported {n} utterances to {args.export}; correct the .txt files by ear, "
              f"then: python -m eval.asr_eval --dir {args.export}")
    else:
        s = stats()
        rows = _rows(args.days)
        by_model: dict[str, int] = {}
        for r in rows:
            by_model[r.get("asr_model") or "?"] = by_model.get(r.get("asr_model") or "?", 0) + 1
        print(json.dumps({**s, "rows": len(rows), "by_asr_model": by_model}, indent=1))
