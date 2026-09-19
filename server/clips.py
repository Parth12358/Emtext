"""Clips: keep a moment the listener flagged, for review later.

Three small pieces, none of which know about websockets or FastAPI:

  - `Recent` -- a per-connection ring of the last few utterances, keyed by the
    same per-connection `id` the `utterance` / `read` frames carry. The device
    only ever *names* a moment (`{"type":"save","id":n}`); the server already
    holds everything for it. This ring is what makes that lookup possible a
    few seconds after the read went out.
  - `save()` -- writes one entry to disk as a WAV plus a JSON sidecar. Blocking
    file I/O; `main.py` runs it in the executor.
  - `list_clips()` / `clip_path()` / `delete_clip()` -- the review surface the
    dashboard routes are built on. Only this module knows the file layout.

**Compliance.** The brief forbids retaining third-party audio *by default*. The
ring is not retention in that sense: it holds the same audio the in-flight
pipeline already has, for a few seconds longer, bounded in count and age, and
it dies with the connection. Nothing reaches disk unless the user explicitly
saves, and the device shows a visible confirmation when they do.

**Bounded, never raises.** Like `metrics.py`: memory-only rolling state with
fixed caps, and every public function returns a value rather than throwing --
a failed save must become `ok: false` on the wire, not a traceback.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from . import config

log = logging.getLogger(__name__)

# The id doubles as the filename stem, so its shape is the whole defence
# against path traversal through `GET /api/clips/{id}/audio`. Validate the
# string before touching the filesystem -- never the other way round.
_CLIP_ID = re.compile(r"^\d{8}T\d{6}_\d+_[0-9a-f]{4}$")


# ---------------------------------------------------------------------------
# In-memory retention
# ---------------------------------------------------------------------------

class Recent:
    """Bounded ring of recent utterances for one connection, keyed by uid.

    Bounded twice: at most `CLIP_RETENTION_N` entries, and nothing older than
    `CLIP_RETENTION_S`. Audio is converted to int16 bytes on insert -- half the
    size of the float32 the segmenter emits, and already the format the WAV
    writer wants.

    Not thread-safe on purpose: it is only ever touched from the event loop
    (the read loop adds, the utterance task updates, the save handler reads).
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[int, dict[str, Any]] = OrderedDict()

    def add(self, uid: int, audio: np.ndarray) -> None:
        """Register a freshly segmented utterance (float32 in [-1, 1])."""
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        self._entries[uid] = {
            "uid": uid,
            "t": time.time(),
            "pcm": pcm,
            "transcript": None,
            "tone": None,
            "read": None,
            "voice": None,
            "speaker": None,
        }
        self._prune()

    def update(self, uid: int, **fields: Any) -> None:
        """Fill in transcript / tone / read / voice as the pipeline produces them.

        A no-op if the entry has already aged out -- the pipeline can be slower
        than the retention window on a bad day, and that must not raise.
        """
        entry = self._entries.get(uid)
        if entry is not None:
            entry.update(fields)

    def get(self, uid: int) -> dict[str, Any] | None:
        self._prune()
        return self._entries.get(uid)

    def get_range(self, lo: int, hi: int) -> list[dict[str, Any]]:
        """Every retained entry with `lo <= uid <= hi`, in id order.

        Walks the ring (at most CLIP_RETENTION_N entries) rather than the
        range: a client-supplied `to` of a billion must cost nothing. Ids not
        in the ring are simply absent from the result, per clips.md.
        """
        self._prune()
        return sorted(
            (e for uid, e in self._entries.items() if lo <= uid <= hi),
            key=lambda e: e["uid"],
        )

    def _prune(self) -> None:
        cutoff = time.time() - config.CLIP_RETENTION_S
        while self._entries:
            oldest_uid, oldest = next(iter(self._entries.items()))
            if len(self._entries) > config.CLIP_RETENTION_N or oldest["t"] < cutoff:
                del self._entries[oldest_uid]
            else:
                break


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _clips_dir() -> Path:
    return Path(config.CLIPS_DIR)


def _count_clips() -> int:
    d = _clips_dir()
    if not d.is_dir():
        return 0
    return sum(1 for p in d.glob("*.json") if _CLIP_ID.match(p.stem))


def _speaker_meta(who: Any) -> dict[str, Any] | None:
    """Label + score only, as plain JSON types.

    `speaker.identify` returns diagnostics too (`cos_user`, `windows`), some
    of them numpy scalars that `json.dumps` refuses. The sidecar needs just
    what the wire carries.
    """
    if not isinstance(who, dict) or not who.get("label"):
        return None
    score = who.get("score")
    try:
        score = round(float(score), 3) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return {"label": str(who["label"]), "score": score}


def _bundle_speaker(labels: list[str]) -> str | None:
    """One label for the whole clip: unanimous, or "mixed" if the turn changed."""
    if not labels:
        return None
    return labels[0] if all(l == labels[0] for l in labels) else "mixed"


def save(entries: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Write a run of retained utterances to disk as ONE clip.

    Returns `(clip_id, error)`; exactly one is None. `entries` come from
    `Recent.get_range` and are already in id order -- their audio is
    concatenated in that order, and the sidecar keeps both the per-utterance
    detail and a joined transcript/read for the listing. A single utterance
    is just a run of one.

    **Blocking** -- call from an executor. Never raises: every failure becomes
    an error string for the `saved` frame.
    """
    if not config.CLIPS_ENABLED:
        return None, "clips disabled"
    parts = [e for e in entries if e.get("pcm")]
    if not parts:
        return None, "no audio"

    try:
        d = _clips_dir()
        d.mkdir(parents=True, exist_ok=True)
        if _count_clips() >= config.CLIPS_MAX_FILES:
            return None, "clip store full"

        pcm = b"".join(e["pcm"] for e in parts)
        first, last = parts[0], parts[-1]
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        # Two saves in the same second from two connections would otherwise
        # collide on `<stamp>_<uid>`; four hex chars is enough to make that
        # a non-event without inflating the filename.
        clip_id = f"{stamp}_{int(first['uid'])}_{secrets.token_hex(2)}"
        wav_path = d / f"{clip_id}.wav"
        json_path = d / f"{clip_id}.json"

        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(config.SAMPLE_RATE)
            w.writeframes(pcm)

        utterances = [{
            "uid": e["uid"],
            "heard_at": e.get("t"),
            "duration_s": round(len(e["pcm"]) / 2 / config.SAMPLE_RATE, 3),
            "transcript": e.get("transcript"),
            "tone": e.get("tone"),
            "read": e.get("read"),
            "voice": e.get("voice"),
            # Speaker-id result (user / other / mixed) if it was known. A saved
            # "user" clip is the listener's own voice, which is worth seeing
            # when reviewing -- and before deciding to keep it.
            "speaker": _speaker_meta(e.get("speaker")),
        } for e in parts]
        tones = [u["tone"] for u in utterances if u["tone"]]
        meta = {
            "id": clip_id,
            # `uid` stays as the first id so older sidecars and the dashboard
            # keep one shape; `from`/`to`/`count` describe the span.
            "uid": first["uid"],
            "from": first["uid"],
            "to": last["uid"],
            "count": len(parts),
            "saved_at": time.time(),
            "heard_at": first.get("t"),
            "duration_s": round(len(pcm) / 2 / config.SAMPLE_RATE, 3),
            # Joined views for the listing; the per-utterance truth is below.
            "transcript": " ".join(u["transcript"] for u in utterances if u["transcript"]) or None,
            "read": " / ".join(u["read"] for u in utterances if u["read"]) or None,
            "tone": tones[-1] if tones else None,      # the most recent read's tone
            "voice": last.get("voice"),
            "speaker": _bundle_speaker(
                [u["speaker"]["label"] for u in utterances if u["speaker"]]),
            "utterances": utterances,
        }
        # Sidecar last, so a crash mid-write leaves an orphan wav (ignored by
        # the listing) rather than a listed clip with no audio.
        json_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        log.info("clip saved: %s (%.1fs, %r)", clip_id, meta["duration_s"],
                 (meta["transcript"] or "")[:60])
        return clip_id, None
    except Exception as exc:  # noqa: BLE001 -- must become ok:false, never a raise
        log.warning("clip save failed: %s: %s", type(exc).__name__, exc)
        return None, "write failed"


# ---------------------------------------------------------------------------
# Review surface
# ---------------------------------------------------------------------------

def clip_path(clip_id: str, ext: str) -> Path | None:
    """Resolve an id to a file, or None if the id is malformed or missing.

    The regex check comes first and is the only thing standing between a URL
    path segment and the filesystem, so it stays strict.
    """
    if not isinstance(clip_id, str) or not _CLIP_ID.match(clip_id):
        return None
    p = _clips_dir() / f"{clip_id}.{ext}"
    return p if p.is_file() else None


def list_clips() -> list[dict[str, Any]]:
    """Every saved clip, newest first. Unreadable sidecars are skipped, not fatal."""
    d = _clips_dir()
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in d.glob("*.json"):
        if not _CLIP_ID.match(p.stem):
            continue
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        meta["id"] = p.stem                      # the filename is authoritative
        meta["has_audio"] = (d / f"{p.stem}.wav").is_file()
        out.append(meta)
    out.sort(key=lambda m: m.get("saved_at") or 0, reverse=True)
    return out


def delete_clip(clip_id: str) -> bool:
    """Remove a clip's wav and sidecar. False if the id is bad or nothing existed."""
    if not isinstance(clip_id, str) or not _CLIP_ID.match(clip_id):
        return False
    removed = False
    for ext in ("wav", "json"):
        p = _clips_dir() / f"{clip_id}.{ext}"
        try:
            p.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete %s: %s", p, exc)
    return removed
