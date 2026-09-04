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


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var. Accepts 0/1, true/false, yes/no, on/off."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_token(name: str) -> str | None:
    """Read the auth token, collapsing every 'empty' spelling to None.

    `AUTH_TOKEN=""` used to be the worst of both worlds: the `is not None` check
    reported auth as ENABLED -- in the startup log and on the dashboard -- while
    the empty string was itself the valid credential, so a client authenticated
    by sending nothing at all. Whitespace-only had the same shape. Both now mean
    "unset", which is reported honestly and caught by the startup guard in
    main.py.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip() or None


# --- Server -----------------------------------------------------------------
PORT: int = _env_int("PORT", 8000)
# Bind address. Loopback by default: cloudflared reaches the server over
# 127.0.0.1 (see tunnel/README.md), so binding every interface buys the tunnel
# nothing while putting the server on whatever LAN the machine is attached to.
# On a public network -- a cafe, a campus, a conference -- that is a second
# entrance that bypasses Cloudflare and anything you put in front of it.
# Set HOST=0.0.0.0 deliberately when you actually want LAN access.
HOST: str = _env_str("HOST", "127.0.0.1")

# If None, auth is disabled and any first-frame token is accepted. Fine on
# loopback, unacceptable on any other bind -- hence ALLOW_NO_AUTH below.
AUTH_TOKEN: str | None = _env_token("AUTH_TOKEN")

# Refuse to start unauthenticated on a non-loopback bind. Running wide open is
# still possible, but it must now be asked for rather than being what you get by
# forgetting. This matters because a *named* tunnel is a separate long-lived
# process: it outlives the start script that sets AUTH_TOKEN, so a stray
# `python -m server.main` was enough to put the real public hostname in front of
# an unauthenticated server.
ALLOW_NO_AUTH: bool = _env_bool("ALLOW_NO_AUTH", False)

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
# qwen3:14b, chosen on the RAVDESS pipeline eval (280 clips per model), where the
# spoken text is always neutral so every bit of signal is in the voice field:
#   qwen3:14b   81% tone accuracy, 48% voice sensitivity, 0.89s
#   gemma3:12b  43%, 6%, 1.13s   -- answers "neutral" 68% of the time
#   qwen3:8b    33%, 19%, 0.55s  -- answers "neutral" 83% of the time
# The two losers largely ignore the voice hint when it is a bare categorical label
# (which is all emotion2vec supplies). Note gemma scores 93% on eval/tone_cases,
# where the hint carries NUMERIC valence -- it uses valence well and discounts a
# label. If a backend with usable valence lands, re-run the comparison.
OLLAMA_MODEL: str = _env_str("OLLAMA_MODEL", "qwen3:14b")
CONTEXT_LINES: int = _env_int("CONTEXT_LINES", 12)  # rolling transcript window
OLLAMA_TEMPERATURE: float = _env_float("OLLAMA_TEMPERATURE", 0.2)
OLLAMA_NUM_PREDICT: int = _env_int("OLLAMA_NUM_PREDICT", 80)
OLLAMA_TIMEOUT_S: float = _env_float("OLLAMA_TIMEOUT_S", 30.0)


# --- Speech emotion recognition (SER) ---------------------------------------
# Reads *how* a line sounded, so the interpreter can compare voice against
# words -- the mismatch between the two is what exposes sarcasm and masking.
# Like Whisper, this is pinned to CPU so the Arc B580 stays free for the LLM.
# Set SER_ENABLED=0 to skip loading the model entirely (saves ~2 GB of RAM and
# the first-run download); the pipeline then behaves exactly as it did before
# SER existed.
SER_ENABLED: bool = _env_bool("SER_ENABLED", True)
# emotion2vec by default, measured against MERaLiON on all 1440 RAVDESS clips:
#   accuracy  86%   vs 61.3% macro recall
#   cost      ~0.17s vs 2.72s per utterance
# It is both more accurate and ~16x cheaper here, and MERaLiON's cost is FLAT --
# it pads every clip to 30s, so a 1s utterance costs the same 2.7s as a 14s one.
# That put SER below real time (RTF 0.3x at 1s), meaning sustained speech built a
# backlog. emotion2vec runs ~10x real time and removes that ceiling.
#
# The trade: emotion2vec is categorical only, so valence/arousal come back None
# and the interpreter reasons from the emotion label alone. MERaLiON remains
# fully supported -- set SER_MODEL=MERaLiON/MERaLiON-SER-v1 to switch back --
# but its valence measured a +0.085 pleasant/unpleasant separation on this data,
# which is too compressed to be useful anyway.
SER_MODEL: str = _env_str("SER_MODEL", "emotion2vec/emotion2vec_plus_base")
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

# Pin the model to an exact upstream commit. Empty means "use the vetted
# revision for this model if we know one, else track the hub".
#
# This matters most for the MERaLiON backend, which loads with
# `trust_remote_code=True` -- i.e. it downloads and EXECUTES Python from the
# hub at import time. Unpinned, that is "whatever the repo's main branch says
# today", so a maintainer push or a hub account compromise changes the code
# running on this machine at the next restart, silently. ser.py therefore ships
# the commit that was actually reviewed and benchmarked, and a human has to
# bump it.
#
# The emotion2vec/ModelScope default backend cannot be pinned the same way:
# upstream publishes only a moving `master` branch for it -- no tags, no commit
# refs -- so there is nothing immutable to point at. Its protection is that the
# weights are already cached locally and it does not execute downloaded code.
SER_REVISION: str = _env_str("SER_REVISION", "")

# Gloss thresholds for the valence/arousal words in the interpreter prompt.
# These are a property of the BACKEND, not of the concept: a model whose valence
# only ever spans 0.12-0.41 (MERaLiON, measured over 1440 RAVDESS clips) will
# never cross a fixed 0.6 "positive" line, so every utterance gets described as
# negative and the mismatch rule silently dies. Profile a backend before trusting
# it:  python -m eval.ser_eval --profile-valence
# The 0.4/0.6 defaults suit a well-calibrated 0-1 head; narrow them for a
# compressed one. Only used when the backend supplies dimensions at all --
# emotion2vec returns None and these are never consulted.
VALENCE_LOW: float = _env_float("VALENCE_LOW", 0.4)
VALENCE_HIGH: float = _env_float("VALENCE_HIGH", 0.6)
AROUSAL_LOW: float = _env_float("AROUSAL_LOW", 0.4)
AROUSAL_HIGH: float = _env_float("AROUSAL_HIGH", 0.6)

# --- Websocket keepalive ----------------------------------------------------
# Cloudflare closes a proxied websocket after ~100s with no traffic in either
# direction (Free/Pro). uvicorn already sends protocol-level pings every 20s,
# but that is a server setting; a different proxy, or the future ESP32 stack,
# may not honour or emit them. So the app sends its own visible {"type":"ping"}
# frame when no audio has arrived for a while -- which also gives a diagnostic
# page something concrete to measure round-trip latency against.
WS_IDLE_PING_S: float = _env_float("WS_IDLE_PING_S", 30.0)
# How often the keepalive task wakes to check. Cheap; keep well under the idle
# threshold so the ping actually lands near WS_IDLE_PING_S rather than late.
WS_KEEPALIVE_CHECK_S: float = _env_float("WS_KEEPALIVE_CHECK_S", 5.0)

# How often to emit VAD telemetry frames on a /stream?vad=1 connection. ~10/s is
# enough to watch a level meter move without flooding the log in remote.html.
VAD_TELEMETRY_INTERVAL_S: float = _env_float("VAD_TELEMETRY_INTERVAL_S", 0.1)

# --- Exposure limits --------------------------------------------------------
# None of these constrain a conforming client: the wire protocol sends ~20-100ms
# of PCM per frame and speaks at most one utterance per second. They exist
# because every one of them is unbounded by default, and this server is reachable
# from the internet through a tunnel.

# How long a freshly-accepted socket may go without sending its auth token.
# Without a bound, a peer that connects and stays silent is held forever: the
# websocket layer answers protocol pings on its behalf, so it costs the attacker
# nothing and never times out. A real client sends the token immediately.
WS_AUTH_TIMEOUT_S: float = _env_float("WS_AUTH_TIMEOUT_S", 5.0)

# Largest single websocket message accepted. uvicorn's default is 16 MiB, which
# is ~5000x any legitimate audio frame; one such message segments into ~500
# utterances and spawns ~1000 pipeline jobs in a single read. 64 KiB is two
# seconds of audio -- generous for a frame, useless as an amplifier.
WS_MAX_MESSAGE_BYTES: int = _env_int("WS_MAX_MESSAGE_BYTES", 65_536)

# Ceiling on concurrent connections (uvicorn's limit_concurrency, covering HTTP
# and websockets). Unset by default, i.e. unlimited.
MAX_CONNECTIONS: int = _env_int("MAX_CONNECTIONS", 32)

# Utterances that may be in the pipeline at once, per connection. Whisper and
# SER both serialise globally (~3 utterances/sec for the whole process), so
# without a bound a client can queue work far faster than it drains and the
# backlog is retained audio. Over the cap the utterance is dropped and counted,
# never queued -- dropping a word beats an unbounded queue that ends in an OOM.
# 3 is well clear of conversational speech, which produces about one per second.
MAX_INFLIGHT_UTTERANCES: int = _env_int("MAX_INFLIGHT_UTTERANCES", 3)

# Close a connection that has sent no audio for this long. The keepalive pings
# were holding idle sockets open indefinitely, which is exactly what an attacker
# wants and what an abandoned browser tab does by accident. 0 disables the close
# and restores ping-forever. Generously above any pause in real conversation.
WS_IDLE_CLOSE_S: float = _env_float("WS_IDLE_CLOSE_S", 900.0)

# Time budget for the trailing utterance flushed after a client disconnects.
# Worth attempting -- a real client that drops mid-sentence still wants the read
# -- but nobody is waiting on the other end, so it must not pin the pipeline for
# a full Ollama timeout.
WS_FLUSH_TIMEOUT_S: float = _env_float("WS_FLUSH_TIMEOUT_S", 8.0)

# Minimum gap between interpreter model switches / evictions. Each one moves
# 8-9 GB in or out of VRAM; back-to-back calls thrash the GPU and can leave a
# model partly on CPU, which reads as "the LLM got slow" rather than as an
# attack. Cheap to send, expensive to serve -- so rate-limit it.
MODEL_SWITCH_COOLDOWN_S: float = _env_float("MODEL_SWITCH_COOLDOWN_S", 10.0)
