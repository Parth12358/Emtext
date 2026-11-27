"""Live-tunable weights for how emotions are decided and shown.

The dashboard's "Emotion weights" card reads and writes these through
`GET/POST /api/tuning`. Every knob is an existing `config` attribute, and the
code that uses it already reads `config.<NAME>` at call time, so setting the
attribute is the whole mechanism -- the same property `POST /api/model` relies
on. Runtime only and never persisted, like the model and transcript-log
switches: a restart returns to the env/config defaults, which reproduce the
measured behaviour exactly (EMOTION_BLEND 1.0, every penalty 0.7).

Only keys in `KNOBS` can be set, each clamped to its range, so the endpoint
cannot be used to write an arbitrary config attribute. `snapshot()` returns the
knobs that differ from their defaults; the transcript log stores it per line so
a session's reads can be compared across settings afterwards.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from . import config


@dataclass(frozen=True)
class Knob:
    key: str        # the config attribute
    group: str
    label: str
    kind: str       # "float" | "int" | "bool"
    lo: float = 0.0
    hi: float = 1.0
    step: float = 0.05
    help: str = ""


KNOBS: tuple[Knob, ...] = (
    # --- emotion choice ---------------------------------------------------
    Knob("EMOTION_BLEND", "emotion", "voice  <->  interpreter", "float", 0, 1, 0.05,
         "1 = the interpreter's label (as before). 0 = the voice model's label. "
         "Between, the two are mixed; since the interpreter is near-certain, this acts "
         "as 'how sure must the voice be to keep its label' (server/blend.py)."),
    # --- voice trust ------------------------------------------------------
    Knob("SER_MIN_CONFIDENCE", "voice", "weak-voice cutoff", "float", 0, 1, 0.05,
         "Below this the interpreter is told the voice is a weak signal, and the "
         "'voice unclear' penalty applies. The prompt's wording ('about 0.4') does not change."),
    # --- confidence -------------------------------------------------------
    Knob("CONF_PEN_SHORT", "confidence", "too short to read the voice", "float", 0, 1, 0.05,
         "Multiplier on the score when under the minimum voiced seconds. 1 = ignore."),
    Knob("CONFIDENCE_MIN_VOICE_S", "confidence", "  minimum voiced seconds", "float", 0, 5, 0.1),
    Knob("CONF_PEN_VOICE", "confidence", "voice unclear", "float", 0, 1, 0.05,
         "Multiplier when the voice model's confidence is under the weak-voice cutoff."),
    Knob("CONF_PEN_WORDS", "confidence", "words may be misheard", "float", 0, 1, 0.05,
         "Multiplier when ASR confidence is under CONFIDENCE_MIN_ASR."),
    Knob("CONF_PEN_TWO_VOICES", "confidence", "two voices in one line", "float", 0, 1, 0.05),
    Knob("CONF_PEN_DISAGREE", "confidence", "signals disagree (tone mixed)", "float", 0, 1, 0.05),
    Knob("CONFIDENCE_HIGH", "confidence", "shown bold at or above", "float", 0, 1, 0.05),
    Knob("CONFIDENCE_LOW", "confidence", "hidden as 'not sure' below", "float", 0, 1, 0.05),
    # --- sentence split ---------------------------------------------------
    Knob("SENTENCE_SPLIT", "split", "split long utterances", "bool"),
    Knob("SPLIT_MIN_S", "split", "minimum piece, seconds", "float", 0, 6, 0.1,
         "Shorter sentences join a neighbour."),
    Knob("SPLIT_MIN_WORDS", "split", "minimum piece, words", "int", 1, 12, 1),
    Knob("SPLIT_MAX_PIECES", "split", "most pieces per utterance", "int", 1, 12, 1),
)

_BY_KEY = {k.key: k for k in KNOBS}
_DEFAULTS = {k.key: getattr(config, k.key) for k in KNOBS}


def _coerce(k: Knob, v):
    if k.kind == "bool":
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)
    x = float(v)
    if x != x:                                   # NaN
        raise ValueError(f"{k.key}: not a number")
    x = min(max(x, k.lo), k.hi)
    return int(round(x)) if k.kind == "int" else round(x, 4)


def state() -> dict:
    return {
        "knobs": [{**asdict(k), "value": getattr(config, k.key), "default": _DEFAULTS[k.key]}
                  for k in KNOBS],
        "persisted": False,
    }


def set_values(updates: dict) -> dict:
    """Apply known keys (clamped); unknown keys raise KeyError, bad values ValueError.

    Validates everything before applying anything, so a bad batch changes nothing.
    """
    staged = {}
    for key, v in (updates or {}).items():
        if key not in _BY_KEY:
            raise KeyError(key)
        staged[key] = _coerce(_BY_KEY[key], v)
    if "CONFIDENCE_LOW" in staged or "CONFIDENCE_HIGH" in staged:
        lo = staged.get("CONFIDENCE_LOW", config.CONFIDENCE_LOW)
        hi = staged.get("CONFIDENCE_HIGH", config.CONFIDENCE_HIGH)
        if lo > hi:
            raise ValueError("CONFIDENCE_LOW must not exceed CONFIDENCE_HIGH")
    for key, v in staged.items():
        setattr(config, key, v)
    return state()


def reset() -> dict:
    for key, v in _DEFAULTS.items():
        setattr(config, key, v)
    return state()


def snapshot() -> dict:
    """Knobs that differ from their defaults ({} when untouched)."""
    return {k: getattr(config, k) for k, d in _DEFAULTS.items() if getattr(config, k) != d}
