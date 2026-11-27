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
    Knob("LIVE_SPLIT", "split", "send sentences while still talking", "bool",
         help="Off = a long utterance's sentences are sent together when it closes."),
    Knob("LIVE_SPLIT_EVERY_S", "split", "  check every, seconds of speech", "float", 0.5, 5, 0.25,
         "How often the open utterance is re-transcribed to look for a finished sentence. "
         "Lower = sooner reads, more CPU."),
    Knob("LIVE_SPLIT_PAUSE_S", "split", "  pause that ends a sentence, s", "float", 0.3, 3, 0.1,
         "A finished sentence followed by this much quiet (as heard by the ASR) is sent "
         "without waiting for the next one. Lower = sooner, but risks cutting a mid-sentence pause."),
    Knob("STREAM_ENDPOINT_CUT", "split", "send a sentence when streaming ASR hears it end", "bool",
         help="The streaming recognizer's endpoint (a pause after words) hands the sentence out "
              "at once instead of waiting for the next live-split look."),
    Knob("CLIENT_GAP_MS", "split", "client went quiet = pause after, ms", "int", 0, 2000, 50,
         "A client that stops sending (the pendant's own gate) counts as silent once no audio "
         "has arrived for this long. 0 = off (utterances then wait for more speech or the cap)."),
    Knob("SPLIT_MIN_S", "split", "minimum piece, seconds", "float", 0, 6, 0.1,
         "Shorter sentences join a neighbour."),
    Knob("SPLIT_MIN_WORDS", "split", "minimum piece, words", "int", 1, 12, 1),
    Knob("SPLIT_MAX_PIECES", "split", "most pieces per utterance", "int", 1, 12, 1),
)

_BY_KEY = {k.key: k for k in KNOBS}
_DEFAULTS = {k.key: getattr(config, k.key) for k in KNOBS}

# --- presets ------------------------------------------------------------------
# Named bundles for the dashboard. An "emotion" preset sets EVERY emotion, voice
# and confidence knob, so it means the same thing whatever was moved before; a
# "timing" preset sets every live-split/gap knob. Sentence-split sizes are left
# alone by both. Numbers come from replaying the eval CSVs on 2026-11-26
# (docs/status/TODO.md, "Emotion presets"):
#   * blend, on tone_cases x3 with emtext-qwen3-0.6b:v3 (interpreter ~0.97 sure):
#     emotion 94.1% at w >= 0.25, 91.2% at 0.2, 88.2% at <= 0.15. Every move the
#     student made was right, mostly weak-voice lines (conf 0.22-0.27) read as neutral.
#   * confidence, on 280 RAVDESS clips (studio audio -- a ceiling): AUROC of each
#     input is ser_confidence 0.78, voiced_s 0.62, asr_conf 0.52 (a coin flip),
#     so the voice penalty is the one worth weight and the words penalty is not.
_EMOTION_KEYS = ("EMOTION_BLEND", "SER_MIN_CONFIDENCE", "CONF_PEN_SHORT", "CONFIDENCE_MIN_VOICE_S",
                 "CONF_PEN_VOICE", "CONF_PEN_WORDS", "CONF_PEN_TWO_VOICES", "CONF_PEN_DISAGREE",
                 "CONFIDENCE_HIGH", "CONFIDENCE_LOW")
_TIMING_KEYS = ("LIVE_SPLIT", "LIVE_SPLIT_EVERY_S", "LIVE_SPLIT_PAUSE_S", "CLIENT_GAP_MS",
                "STREAM_ENDPOINT_CUT")

PRESETS: tuple[dict, ...] = (
    {"id": "measured", "group": "emotion", "label": "Measured default",
     "note": "As shipped: the interpreter decides, every penalty 0.7. RAVDESS: 18 bold (100%), "
             "32 'not sure' (69% right). Test cases: emotion 94%.",
     "values": {k: _DEFAULTS[k] for k in _EMOTION_KEYS}},
    {"id": "voice-led", "group": "emotion", "label": "Voice-led",
     "note": "emotion2vec leads; the interpreter overrides only a voice under ~42% sure. Words "
             "penalty off (ASR confidence is a coin flip), voice penalty stronger. RAVDESS: 68 bold "
             "(97%), 12 'not sure' (42% right, i.e. mostly the wrong ones). Test cases: 94%, no loss.",
     "values": {"EMOTION_BLEND": 0.3, "SER_MIN_CONFIDENCE": 0.4, "CONF_PEN_SHORT": 0.85,
                "CONFIDENCE_MIN_VOICE_S": 1.0, "CONF_PEN_VOICE": 0.6, "CONF_PEN_WORDS": 1.0,
                "CONF_PEN_TWO_VOICES": 0.7, "CONF_PEN_DISAGREE": 0.8,
                "CONFIDENCE_HIGH": 0.9, "CONFIDENCE_LOW": 0.55}},
    {"id": "voice-first", "group": "emotion", "label": "Voice-first",
     "note": "The interpreter overrides only a voice under ~24% sure; short lines no longer "
             "penalised. RAVDESS: 251 bold (91%), 12 'not sure' (42%). Test cases: 91% (-3: "
             "weak-voice lines the words show are neutral keep the voice's guess).",
     "values": {"EMOTION_BLEND": 0.2, "SER_MIN_CONFIDENCE": 0.35, "CONF_PEN_SHORT": 1.0,
                "CONFIDENCE_MIN_VOICE_S": 1.0, "CONF_PEN_VOICE": 0.5, "CONF_PEN_WORDS": 1.0,
                "CONF_PEN_TWO_VOICES": 0.7, "CONF_PEN_DISAGREE": 0.85,
                "CONFIDENCE_HIGH": 0.85, "CONFIDENCE_LOW": 0.65}},
    {"id": "voice-only", "group": "emotion", "label": "Voice only (experiment)",
     "note": "The headline is always emotion2vec's label; the interpreter only writes the read, "
             "which can then disagree with the headline. Test cases: 88% (-6). For comparing, "
             "not for daily use.",
     "values": {"EMOTION_BLEND": 0.0, "SER_MIN_CONFIDENCE": 0.35, "CONF_PEN_SHORT": 1.0,
                "CONFIDENCE_MIN_VOICE_S": 1.0, "CONF_PEN_VOICE": 0.5, "CONF_PEN_WORDS": 1.0,
                "CONF_PEN_TWO_VOICES": 0.7, "CONF_PEN_DISAGREE": 1.0,
                "CONFIDENCE_HIGH": 0.85, "CONFIDENCE_LOW": 0.65}},
    {"id": "careful", "group": "emotion", "label": "Careful display",
     "note": "Interpreter decides as shipped, but fewer reads are bold and more are marked 'not "
             "sure'. RAVDESS: 65 bold (97%), 37 'not sure' (68% right).",
     "values": {**{k: _DEFAULTS[k] for k in _EMOTION_KEYS},
                "CONFIDENCE_MIN_VOICE_S": 1.0, "CONFIDENCE_HIGH": 0.95, "CONFIDENCE_LOW": 0.65}},
    {"id": "timing-default", "group": "timing", "label": "Default timing",
     "note": "Live split every 1.5 s, a 0.8 s pause ends a sentence, a client gap of 0.4 s counts as quiet.",
     "values": {k: _DEFAULTS[k] for k in _TIMING_KEYS}},
    {"id": "snappy", "group": "timing", "label": "Snappy",
     "note": "Sooner reads, more CPU, more risk of cutting a sentence at a mid-sentence pause.",
     "values": {"LIVE_SPLIT": True, "LIVE_SPLIT_EVERY_S": 1.0, "LIVE_SPLIT_PAUSE_S": 0.6,
                "CLIENT_GAP_MS": 300, "STREAM_ENDPOINT_CUT": True}},
    {"id": "patient", "group": "timing", "label": "Patient",
     "note": "Waits longer before calling a sentence done: fewer fragments for slow or hesitant talkers.",
     "values": {"LIVE_SPLIT": True, "LIVE_SPLIT_EVERY_S": 2.0, "LIVE_SPLIT_PAUSE_S": 1.2,
                "CLIENT_GAP_MS": 600, "STREAM_ENDPOINT_CUT": False}},
    {"id": "no-live", "group": "timing", "label": "Whole utterances",
     "note": "Live split off: sentences of a long utterance arrive together when it closes.",
     "values": {"LIVE_SPLIT": False, "LIVE_SPLIT_EVERY_S": _DEFAULTS["LIVE_SPLIT_EVERY_S"],
                "LIVE_SPLIT_PAUSE_S": _DEFAULTS["LIVE_SPLIT_PAUSE_S"],
                "CLIENT_GAP_MS": _DEFAULTS["CLIENT_GAP_MS"], "STREAM_ENDPOINT_CUT": False}},
)
_PRESETS = {p["id"]: p for p in PRESETS}


def _active(group: str) -> str | None:
    """The preset in `group` that matches the current values exactly, if any."""
    for p in PRESETS:
        if p["group"] == group and all(getattr(config, k) == v for k, v in p["values"].items()):
            return p["id"]
    return None


def apply_preset(pid: str) -> dict:
    """Set every knob a preset names (validated as one batch). KeyError if unknown."""
    return set_values(dict(_PRESETS[pid]["values"]))


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
        "presets": [{k: p[k] for k in ("id", "group", "label", "note", "values")} for p in PRESETS],
        "active": {"emotion": _active("emotion"), "timing": _active("timing")},
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
