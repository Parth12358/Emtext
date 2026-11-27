"""A saved override for the interpreter's system prompt, edited from the dashboard.

Why a separate module
---------------------
`interpreter.SYSTEM_PROMPT` is the measured prompt: the evals, the distill
dataset (its sha256 is in every manifest) and the v2/v3 students were all made
against it byte for byte, so it stays a constant and stays the default. This
module holds an OPTIONAL replacement that the live server sends instead.

  * Unlike every other dashboard switch, this one IS persisted, because the
    user asked for "save": the override lives in `PROMPT_OVERRIDE_PATH`
    (under models/, gitignored) with the last few saved versions, and
    `main.py` loads it at startup.
  * Only the server loads it. `eval/` and `distill/` import the interpreter but
    never call `load()`, so a saved experiment can never leak into a measured
    number -- an eval of a custom prompt has to be asked for on purpose.
  * `interpreter.interpret` asks `override()` at call time, so a save applies to
    the next read on every live connection, like the model switch.
  * Reads made under an override carry its short hash in the transcript log
    (`prompt`), so they can be told apart later.

Blocking file I/O; callers off the event loop. Like translog/clips, a bad or
missing file never raises: it just means "use the built-in prompt".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_override: str | None = None
_saved_at: float | None = None
_history: list[dict] = []      # newest first: {"text", "saved_at", "sha"}
_HISTORY_N = 10


def _path() -> Path:
    p = Path(config.PROMPT_OVERRIDE_PATH)
    return p if p.is_absolute() else Path(__file__).resolve().parent.parent / p


def sha(text: str) -> str:
    """Short content hash: what the transcript log stamps, and what the UI shows."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def override() -> str | None:
    """The active custom prompt, or None for the built-in one. Read at call time."""
    return _override


def load() -> None:
    """Read the saved override at startup. Never raises."""
    global _override, _saved_at, _history
    p = _path()
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        text = data.get("text")
        _history = [h for h in data.get("history") or []
                    if isinstance(h, dict) and isinstance(h.get("text"), str)][:_HISTORY_N]
        if isinstance(text, str) and text.strip():
            _override, _saved_at = text, data.get("saved_at")
            log.warning("interpreter: using the CUSTOM system prompt saved from the dashboard "
                        "(%s, %d chars, %s) -- reads differ from the measured baseline",
                        sha(text), len(text), p)
    except (OSError, ValueError) as exc:
        log.warning("interpreter: ignoring unreadable prompt override %s (%s)", p, exc)


def _write() -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"text": _override, "saved_at": _saved_at,
                               "history": _history}, indent=1), encoding="utf-8")
    os.replace(tmp, p)       # atomic: a crash mid-save never leaves half a prompt


def validate(text: str) -> str:
    """Normalise and check a submitted prompt; ValueError says what is wrong."""
    if not isinstance(text, str):
        raise ValueError("prompt must be text")
    text = text.replace("\r\n", "\n").replace("\x00", "")
    if not text.strip():
        raise ValueError("prompt is empty")
    if len(text) > config.PROMPT_MAX_CHARS:
        raise ValueError(f"prompt is {len(text)} chars; the limit is {config.PROMPT_MAX_CHARS}")
    return text


def save(text: str, builtin: str) -> None:
    """Make `text` the active prompt and write it to disk.

    Saving the built-in text itself is the same as `reset()`: no point keeping
    an "override" that changes nothing and makes every log line look custom.
    """
    global _override, _saved_at
    text = validate(text)
    if text == builtin:
        reset()
        return
    with _lock:
        now = time.time()
        _history[:] = [h for h in _history if h.get("text") != text]
        _history.insert(0, {"text": text, "saved_at": now, "sha": sha(text)})
        del _history[_HISTORY_N:]
        _override, _saved_at = text, now
        _write()
    log.info("dashboard: interpreter prompt saved (%s, %d chars)", sha(text), len(text))


def reset() -> None:
    """Back to the built-in prompt. The history is kept, so a version can be restored."""
    global _override, _saved_at
    with _lock:
        _override, _saved_at = None, None
        if _history:
            _write()
        else:
            _path().unlink(missing_ok=True)
    log.info("dashboard: interpreter prompt reset to built-in")


def state(builtin: str) -> dict:
    active = _override or builtin
    return {
        "custom": _override is not None,
        "text": active,
        "sha": sha(active),
        "builtin_sha": sha(builtin),
        "builtin": builtin,
        "saved_at": _saved_at,
        "chars": len(active),
        "max_chars": config.PROMPT_MAX_CHARS,
        "history": [{"sha": h.get("sha") or sha(h["text"]), "saved_at": h.get("saved_at"),
                     "chars": len(h["text"]), "text": h["text"]} for h in _history],
        "path": str(_path()),
        "persisted": True,
    }
