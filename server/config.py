"""Central configuration for emtext.

Every tunable lives here so there is exactly one place to look when behaviour
needs to change. Each value has a sensible default and can be overridden with an
environment variable of the same name -- this lets you retune the VAD or swap
models at run time without editing code (handy on the ESP32/host split later).

Design note: this module performs no I/O and imports nothing heavy, so it is
cheap for any other module (including the pure `segmenter`) to import.
"""

from __future__ import annotations

import os


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to the default if unset or malformed."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --- Server -----------------------------------------------------------------
PORT: int = _env_int("PORT", 8000)
# If unset, auth is disabled and any first-frame token is accepted. This keeps
# the browser test client and the future ESP32 firmware on one code path.
AUTH_TOKEN: str | None = os.environ.get("AUTH_TOKEN")

# --- Audio format (fixed by the wire protocol) ------------------------------
# The protocol promises raw PCM, 16 kHz, mono, int16 little-endian. These are
# not really "tunable" -- the client and the segmenter must agree -- but they
# live here so the constants are named rather than magic numbers.
SAMPLE_RATE: int = _env_int("SAMPLE_RATE", 16_000)
FRAME_MS: int = _env_int("FRAME_MS", 30)  # analysis frame size for the VAD

# --- Segmenter / VAD knobs --------------------------------------------------
# See segmenter.py for how each of these drives the state machine.
SPEECH_RMS: int = _env_int("SPEECH_RMS", 500)          # int16 RMS to count as speech
END_SILENCE_MS: int = _env_int("END_SILENCE_MS", 650)  # trailing quiet that ends an utterance
MIN_UTTERANCE_MS: int = _env_int("MIN_UTTERANCE_MS", 350)  # shorter -> discarded as a blip
MAX_UTTERANCE_MS: int = _env_int("MAX_UTTERANCE_MS", 15_000)  # hard cap so we never buffer forever
PRE_ROLL_MS: int = _env_int("PRE_ROLL_MS", 240)        # audio kept from just before speech began

# --- Transcriber (faster-whisper) -------------------------------------------
# Whisper is pinned to CPU on purpose: the Intel Arc B580 is reserved for the
# LLM, so the two heavy models never fight over the GPU.
WHISPER_MODEL: str = _env_str("WHISPER_MODEL", "base")
WHISPER_DEVICE: str = _env_str("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE: str = _env_str("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE: str = _env_str("WHISPER_LANGUAGE", "en")

# --- Interpreter (Ollama) ---------------------------------------------------
OLLAMA_URL: str = _env_str("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL: str = _env_str("OLLAMA_MODEL", "gemma3:12b")
CONTEXT_LINES: int = _env_int("CONTEXT_LINES", 12)  # rolling transcript window
OLLAMA_TEMPERATURE: float = _env_float("OLLAMA_TEMPERATURE", 0.2)
OLLAMA_NUM_PREDICT: int = _env_int("OLLAMA_NUM_PREDICT", 80)
OLLAMA_TIMEOUT_S: float = _env_float("OLLAMA_TIMEOUT_S", 30.0)


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var. Accepts 0/1, true/false, yes/no, on/off."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Speech emotion recognition (SER) ---------------------------------------
# Reads *how* a line sounded, so the interpreter can compare voice against
# words -- the mismatch between the two is what exposes sarcasm and masking.
# Like Whisper, this is pinned to CPU so the Arc B580 stays free for the LLM.
# Set SER_ENABLED=0 to skip loading the model entirely (saves ~2 GB of RAM and
# the first-run download); the pipeline then behaves exactly as it did before
# SER existed.
SER_ENABLED: bool = _env_bool("SER_ENABLED", True)
SER_MODEL: str = _env_str("SER_MODEL", "MERaLiON/MERaLiON-SER-v1")
SER_DEVICE: str = _env_str("SER_DEVICE", "cpu")
# Below this softmax probability the categorical label is close to a coin flip,
# so the interpreter is told to treat the voice signal as weak rather than
# letting it override a plain reading of the words.
SER_MIN_CONFIDENCE: float = _env_float("SER_MIN_CONFIDENCE", 0.4)
# Torch intra-op threads for SER. 0 = leave torch's own default (typically one
# per physical core). Raising it to the logical core count measurably speeds up
# a single SER pass, but SER and Whisper run concurrently and Whisper has its
# own CPU threads, so oversubscribing can make the pair slower overall. Tune it
# against your machine rather than assuming more is better.
SER_TORCH_THREADS: int = _env_int("SER_TORCH_THREADS", 0)

# Which SER implementation to use. "auto" picks from SER_MODEL: anything whose
# name contains "emotion2vec" uses the FunASR backend, everything else uses the
# transformers/MERaLiON one. Set explicitly to force a backend.
#   meralion    -- MERaLiON-SER-v1: 0.8B, 7 emotions + valence/arousal/dominance
#   emotion2vec -- emotion2vec_plus_*: ~90M/~300M, categorical only (no VAD dims)
SER_BACKEND: str = _env_str("SER_BACKEND", "auto")
