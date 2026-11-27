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
# 127.0.0.1 (see docs/guides/tunnel.md), so binding every interface buys the tunnel
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
# A client that stops SENDING (its own energy gate closed) is silent too. The
# pendant sends 500 ms of hangover then nothing, short of END_SILENCE_MS, so its
# utterances never closed until the next speech or the 15 s cap. Once no frame
# has arrived for this long, the gap counts as trailing silence
# (Segmenter.idle). Above normal frame jitter (frames are 20-100 ms); 0 = off.
CLIENT_GAP_MS: int = _env_int("CLIENT_GAP_MS", 400)
MIN_UTTERANCE_MS: int = _env_int("MIN_UTTERANCE_MS", 350)  # shorter -> discarded as a blip
MAX_UTTERANCE_MS: int = _env_int("MAX_UTTERANCE_MS", 15_000)  # hard cap so we never buffer forever
PRE_ROLL_MS: int = _env_int("PRE_ROLL_MS", 240)        # audio kept from just before speech began

# --- Sentence split (server/sentences.py) ------------------------------------
# After ASR, an utterance holding several sentences is cut at sentence ends
# (from the ASR's per-token timestamps) and each piece gets its own SER, read and
# wire id. Why: 140 of the first 256 logged real utterances ran into the 15 s
# MAX_UTTERANCE_MS cap, median 25 words, because the room's background kept the
# energy VAD from ever seeing END_SILENCE_MS of quiet; a read of a paragraph is
# too vague to help. Pieces shorter than SPLIT_MIN_S seconds of speech or
# SPLIT_MIN_WORDS words join their neighbour (SER is unreliable on short audio).
# Only backends with token timestamps split (Parakeet; Whisper never does).
SENTENCE_SPLIT: bool = _env_bool("SENTENCE_SPLIT", True)
SPLIT_MIN_S: float = _env_float("SPLIT_MIN_S", 1.5)
SPLIT_MIN_WORDS: int = _env_int("SPLIT_MIN_WORDS", 3)
SPLIT_MAX_PIECES: int = _env_int("SPLIT_MAX_PIECES", 6)
SPLIT_PAD_S: float = _env_float("SPLIT_PAD_S", 0.25)  # audio kept around each piece's words
# Live split: while an utterance is still OPEN, re-transcribe it every
# LIVE_SPLIT_EVERY_S of new audio and send each finished sentence at once (one
# whose next sentence has already started), instead of all of them in a burst
# when the segmenter finally closes the utterance. Costs one extra ASR pass per
# LIVE_SPLIT_EVERY_S of speech (Parakeet: ~0.1-0.6 s CPU on a <=15 s buffer).
LIVE_SPLIT: bool = _env_bool("LIVE_SPLIT", True)
LIVE_SPLIT_EVERY_S: float = _env_float("LIVE_SPLIT_EVERY_S", 1.5)
# A sentence whose last word the ASR heard this long ago, with nothing since,
# counts as finished even though no next sentence has started. The VAD's own
# END_SILENCE_MS (650) misses such pauses when the room keeps the RMS up.
LIVE_SPLIT_PAUSE_S: float = _env_float("LIVE_SPLIT_PAUSE_S", 0.8)

# --- Streaming ASR (server/streaming.py) -------------------------------------
# A second, streaming recognizer for live words (`partial` frames, opt-in with
# /stream?partial=1) and sentence endpoints. Parakeet stays the text the
# interpreter reads. Nemotron Speech Streaming 0.6B, 160 ms chunks, int8:
# punctuated, RTF 0.31 on 2 threads, ~0.6 GB RAM. "" disables it.
STREAMING_ASR_MODEL: str = _env_str(
    "STREAMING_ASR_MODEL", "sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25")
STREAMING_ASR_THREADS: int = _env_int("STREAMING_ASR_THREADS", 2)
# Quiet after decoded text that counts as a sentence end (the recognizer's rule 2).
# Fixed at load; restart to change it.
STREAMING_ENDPOINT_S: float = _env_float("STREAMING_ENDPOINT_S", 0.6)
# On an endpoint, hand out the open utterance's sentences at once (the live split).
STREAM_ENDPOINT_CUT: bool = _env_bool("STREAM_ENDPOINT_CUT", True)
# Partial frames are sent at most this often (they are also sent on every endpoint).
PARTIAL_MIN_INTERVAL_S: float = _env_float("PARTIAL_MIN_INTERVAL_S", 0.15)

# --- Adaptive threshold ------------------------------------------------------
# SPEECH_RMS alone was the wrong shape for this product. It is an ABSOLUTE
# level, and RAVDESS measured a ~130x spread in peak RMS across emotions
# (angry 13456, sad 103-168): at 500, 5 of 10 sample clips produced no segment
# at all, and they were the sad and neutral ones -- the speech this app exists
# to read. Lowering it to 150 took that to 1/10, but a fixed low value false-
# triggers on room noise. So the threshold now tracks the room:
#
#     threshold = clamp(noise_floor * NOISE_MARGIN, SPEECH_RMS_MIN, SPEECH_RMS)
#
# SPEECH_RMS keeps its meaning as the CEILING (a loud room behaves exactly as
# before), SPEECH_RMS_MIN is the floor (digital silence settles here, which is
# the measured RAVDESS workaround), and in between the segmenter asks for a
# margin above whatever the microphone hears when nobody is talking. The floor
# is measured only on frames the machine classes as non-speech, and it rises
# slowly but falls fast, so a sentence cannot lift it under itself.
# VAD_ADAPTIVE=0 restores the constant-threshold behaviour byte for byte; the
# evals pass adaptive=False whenever an explicit --speech-rms is given.
VAD_ADAPTIVE: bool = _env_bool("VAD_ADAPTIVE", True)
SPEECH_RMS_MIN: int = _env_int("SPEECH_RMS_MIN", 150)      # never ask for less than this
NOISE_MARGIN: float = _env_float("NOISE_MARGIN", 3.0)      # x the floor (~+9.5 dB)
NOISE_FLOOR_RISE: float = _env_float("NOISE_FLOOR_RISE", 0.02)   # EMA alpha when the room gets louder
NOISE_FLOOR_FALL: float = _env_float("NOISE_FLOOR_FALL", 0.30)   # EMA alpha when it gets quieter
# Start value for the floor. SPEECH_RMS / NOISE_MARGIN makes the very first
# frames behave exactly like the old constant threshold until the room has
# actually been measured. 0 = "assume silence", which is wrong for a live mic.
NOISE_FLOOR_INIT: float = _env_float("NOISE_FLOOR_INIT", SPEECH_RMS / NOISE_MARGIN)

# --- Transcriber (faster-whisper) -------------------------------------------
# Whisper is pinned to CPU on purpose: the Intel Arc B580 is reserved for the
# LLM, so the two heavy models never fight over the GPU.
WHISPER_MODEL: str = _env_str("WHISPER_MODEL", "base")
WHISPER_DEVICE: str = _env_str("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE: str = _env_str("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE: str = _env_str("WHISPER_LANGUAGE", "en")

# --- Transcriber backend catalogue (server/transcriber.py) -------------------
# Which speech-to-text model is resident. A catalogue KEY, not a bare model
# name, because there are now two families behind one `transcribe()`:
#
#   whisper:<name>     faster-whisper (CTranslate2). Any name faster-whisper
#                      accepts: base, small.en, distil-small.en, distil-large-v3,
#                      large-v3-turbo... Cost is FLAT per utterance (30 s padding).
#   parakeet:v2 / v3   NVIDIA Parakeet TDT 0.6B via sherpa-onnx, int8. ~6% WER on
#                      the Open ASR leaderboard vs ~10% for whisper base; cost
#                      scales with utterance length. ~650 MB download, ~1.2 GB RSS.
#   moonshine:tiny/base  Useful Sensors Moonshine via sherpa-onnx. Built for
#                      short utterances; the fastest option here.
#   sensevoice:small   FunAudioLLM SenseVoice via sherpa-onnx. Also emits
#                      language / emotion / audio-event tags (logged, not used).
#
# Switchable live from the dashboard (POST /api/asr), NOT persisted: a restart
# returns here. Exactly one model is resident at a time -- the large ones are
# 1-1.5 GB each and this process is already ~1.3-2 GB.
#
# Default: parakeet:v3 (multilingual, 25 languages). On the SAPI set it scored
# 6.0% WER at 0.10 s per utterance -- level with whisper:base's 6.0% at 0.26 s,
# a point behind the English-only parakeet:v2's 4.8% (the one extra error is
# "415" vs "4.15", numeral formatting) -- and it does not invent text on
# silence. Chosen over v2 for the language coverage at the same cost. Its int8
# archive (~640 MB) is fetched on first start if models/asr/ does not hold it.
# The caveat in docs/status/TODO.md still stands: no real-room recording has
# been scored yet, so re-run `python -m eval.asr_eval` once one exists.
#
# Back-compat: if ASR_MODEL is unset but WHISPER_MODEL is set explicitly, the
# key is derived as whisper:<WHISPER_MODEL>, so existing start scripts keep
# their Whisper. Only a fully unset environment lands on Parakeet.
ASR_MODEL: str = _env_str("ASR_MODEL", "") or (
    f"whisper:{WHISPER_MODEL}" if os.environ.get("WHISPER_MODEL") else "parakeet:v3"
)
# Where sherpa-onnx model archives are unpacked (gitignored). One directory per
# catalogue entry, named after the upstream archive.
ASR_MODELS_DIR: str = _env_str("ASR_MODELS_DIR", "models/asr")
# Intra-op threads for the sherpa-onnx backends. Whisper and SER share this CPU
# and run concurrently with the transcriber, so this is deliberately not "all
# cores"; measure before raising it.
ASR_THREADS: int = _env_int("ASR_THREADS", 4)
# Ceiling on one model download (a Parakeet archive is ~650 MB).
ASR_DOWNLOAD_TIMEOUT_S: float = _env_float("ASR_DOWNLOAD_TIMEOUT_S", 900.0)

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
#
# Since 2026-11-24 the default is the distilled student (docs/reports/small-interpreter-
# stage1.md): Qwen3-0.6B fine-tuned on distill v3, built with
# `ollama create emtext-qwen3-0.6b:v3 -f distill/Modelfile.student` (the stock qwen3
# template does NOT match its training format). Same RAVDESS eval, emotion2vec:
#   emtext-qwen3-0.6b:v3     89% shown emotion, 90% tone, +79% voice sensitivity, 0.15s
#   emtext-qwen3-0.6b:v3-ds  89%, 93%, +91%, 0.15s (DeepSeek-only labels; a tie with v3,
#                            which is 3 points ahead on text-only emotion, the headline)
#   emtext-qwen3-0.6b:v2     89%, 81%, +49%, 0.16s
#   qwen3:14b                85%, 65%, +30%, 0.85s
# It is weaker than the 14B on text-only sarcasm and passive aggression (no voice
# hint); OLLAMA_MODEL=qwen3:14b brings the old default back.
OLLAMA_MODEL: str = _env_str("OLLAMA_MODEL", "emtext-qwen3-0.6b:v3")
# One line per known interpreter model, shown beside it in the dashboard's model list
# (GET /api/models) so the student versions, which share a size and a family, can be
# told apart. Display only: any pulled model can still be selected. The numbers are
# RAVDESS end to end (tone / voice sensitivity) and the median read, from
# docs/reports/small-interpreter-stage1.md.
MODEL_NOTES: dict[str, str] = {
    "emtext-qwen3-0.6b:v3": "student v3 (default): more words-only training rows; "
                            "RAVDESS tone 90%, voice +79%; text emotion 94%; 0.15 s",
    "emtext-qwen3-0.6b:v2": "student v2: RAVDESS tone 81%, voice +49%; 0.16 s",
    "emtext-qwen3-0.6b:v3-ds": "student v3, DeepSeek labels only: RAVDESS tone 93%, "
                               "voice +91%; text emotion 91%; 0.15 s",
    "qwen3:14b": "previous default: best on words-only sarcasm; RAVDESS tone 65%; 0.7-0.9 s",
    "qwen3:0.6b": "stock base model, not fine-tuned (Stage 0 floor)",
}
CONTEXT_LINES: int = _env_int("CONTEXT_LINES", 12)  # rolling transcript window
OLLAMA_TEMPERATURE: float = _env_float("OLLAMA_TEMPERATURE", 0.2)
OLLAMA_NUM_PREDICT: int = _env_int("OLLAMA_NUM_PREDICT", 80)
OLLAMA_TIMEOUT_S: float = _env_float("OLLAMA_TIMEOUT_S", 30.0)

# Load-time options. Ollama reloads the runner whenever a request arrives with
# load options that differ from the loaded model's, so these are sent by EVERY
# caller through interpreter.load_options() -- never spelled out per call site.
#
# use_mmap off: on Windows the mmap loader leaves the whole GGUF resident in the
# runner's working set after the weights have gone to the GPU. Measured on
# qwen3:14b (docs/reports/server-resources.md): 8.6 GB of host RAM and a 10-12 s
# load with mmap, 0.6 GB and 2.7 s without. 1 restores Ollama's default.
OLLAMA_USE_MMAP: bool = _env_bool("OLLAMA_USE_MMAP", False)
# Context window. 0 = do not send it, Ollama picks (4096 on a 12 GB card). The
# runner uses --context-shift --keep 4, so an overflowing prompt silently loses
# the START OF SYSTEM_PROMPT -- see CONTEXT_WARN_FRACTION before lowering it.
OLLAMA_NUM_CTX: int = _env_int("OLLAMA_NUM_CTX", 0)
# Warn (log + dashboard counter "context_near_full") when prompt tokens plus
# OLLAMA_NUM_PREDICT exceed this share of the context. Prompts measured at
# ~1,000-1,560 tokens against 4096, so this never fires today; it is the guard
# that makes shrinking OLLAMA_NUM_CTX safe to try.
CONTEXT_WARN_FRACTION: float = _env_float("CONTEXT_WARN_FRACTION", 0.85)


# --- Speech emotion recognition (SER) ---------------------------------------
# Reads *how* a line sounded, so the interpreter can compare voice against
# words -- the mismatch between the two is what exposes sarcasm and masking.
# Like Whisper, this is pinned to CPU so the Arc B580 stays free for the LLM.
# Set SER_ENABLED=0 to skip loading the model entirely; the pipeline then
# behaves exactly as it did before SER existed. It is the single biggest thing
# in the process -- measured at +1,068 MB of a 1,269 MB server once the slimmed
# checkpoint below is in use (it was +2,496 MB of 3,051 MB before that), because
# the guard returns before `import torch`, so funasr, transformers and modelscope
# are never imported either.
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

# Local directory holding a SLIMMED copy of the emotion2vec checkpoint. When it
# exists, ser.py loads from here instead of the ModelScope cache; when it does
# not, nothing changes and the hub id above is used. Build it with
# `python -m eval.slim_ser_ckpt`.
#
# Worth ~1.4 GB of resident memory, which is not obvious from the file size.
# ModelScope ships the full pretraining state: 355 MB of weights plus 711 MB of
# Adam optimizer state that inference never touches. FunASR then `torch.load`s
# the file AND `copy.deepcopy`s it before extracting the one key it wants, so
# the 711 MB is materialised twice. Measured: the SER load alone accounts for
# +2,496 MB of a ~3.0 GB process, against a live model of 358 MB. The peak is
# also permanent -- gc frees nothing measurable because the allocator does not
# return the pages -- so allocating less is the only lever.
#
# Set to "" to force the hub path.
SER_MODEL_DIR: str = _env_str("SER_MODEL_DIR", "models/emotion2vec_plus_base_slim")
# Below this softmax probability the categorical label is close to a coin flip,
# so the interpreter is told to treat the voice signal as weak rather than
# letting it override a plain reading of the words. NOTE: SYSTEM_PROMPT spells
# this out as "about 0.4" in words; changing the env does not change the
# prompt text, and editing the prompt invalidates the model_eval baseline.
# confidence.py uses the same number for its "voice unclear" penalty.
SER_MIN_CONFIDENCE: float = _env_float("SER_MIN_CONFIDENCE", 0.4)
# Torch intra-op threads for SER. 0 = leave torch's own default (typically one
# per physical core). Raising it to the logical core count measurably speeds up
# a single SER pass, but SER, Whisper and speaker id run concurrently and
# Whisper has its own CPU threads, so oversubscribing can make the set slower
# overall. Tune it
# against your machine rather than assuming more is better.
SER_TORCH_THREADS: int = _env_int("SER_TORCH_THREADS", 0)
# "int8" = dynamic int8 quantisation of every nn.Linear in the emotion2vec model
# after load (torch.ao.quantization.quantize_dynamic). Opt-in, emotion2vec only.
# Measured on 144 RAVDESS clips (docs/reports/server-resources.md, F5): half the
# SER time, labels 97% unchanged, but confidence moved by up to 0.42 on one clip,
# and confidence feeds the prompt, SER_MIN_CONFIDENCE and confidence.py. So this
# is a model change. Full evals on 2026-11-26 rejected it: end to end 11 ms
# faster (SER runs beside Parakeet), shown emotion 88.9% -> 84.3%, confidence
# AUROC 0.78 -> 0.71. Kept only so the next quantisation is scored the same way.
SER_QUANT: str = _env_str("SER_QUANT", "").strip().lower()

# Which SER implementation to use. "auto" picks from SER_MODEL: anything whose
# name contains "emotion2vec" uses the FunASR backend, everything else uses the
# transformers/MERaLiON one. Set explicitly to force a backend.
#   meralion    -- MERaLiON-SER-v1: 0.8B, 7 emotions + valence/arousal/dominance
#   emotion2vec -- emotion2vec_plus_*: ~90M/~300M params, categorical only (no
#                  VAD dims). Note that is the PARAMETER count: the checkpoint
#                  ModelScope ships is 1,066 MB, because two thirds of it is
#                  Adam optimizer state. See SER_MODEL_DIR above.
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
# Dominance is now passed to the interpreter too, because on the default
# backend arousal + dominance ARE the voice signal: emotion2vec has no
# dimensional head, so ser.py derives both as the probability-weighted mean of
# per-emotion anchors over its 7-class distribution (valence is deliberately
# left to the LLM, which reads it from the words -- see docs/status/TODO.md, "valence may
# be the wrong target"). Derived values are compressed toward the middle by the
# softmax spread, so these thresholds are a first guess: profile them with
# `python -m eval.ser_eval --profile-valence` (it prints all three dimensions
# by true emotion) and set them from the actual range before trusting them.
DOMINANCE_LOW: float = _env_float("DOMINANCE_LOW", 0.4)
DOMINANCE_HIGH: float = _env_float("DOMINANCE_HIGH", 0.6)

# --- Speaker identification (server/speaker.py) -----------------------------
# Answers one question per utterance: is this the LISTENER's own voice, someone
# else's, or both? Without it the interpreter is regularly handed the user's
# own words and asked what "the speaker" meant by them -- explaining the
# user's feelings back to them, which is the one output this product must not
# produce. With it, the user's lines are transcribed (they are the context the
# partner is replying to) but never interpreted.
#
# Numbers below are from docs/status/TODO.md ("No diarization"), measured on RAVDESS (24
# actors, same two sentences, so any separation is speaker identity, not words).
# CAM++ via funasr: ~20 ms per utterance on CPU, no measurable RSS increase on
# top of SER, 192-d embeddings. Set SPEAKER_ENABLED=0 to skip loading it; the
# pipeline then behaves exactly as before (every utterance interpreted).
SPEAKER_ENABLED: bool = _env_bool("SPEAKER_ENABLED", True)
# funasr's alias for iic/speech_campplus_sv_zh-cn_16k-common (~27 MB). Trained
# on Mandarin, measured at 94% on English pairs; ModelScope's English checkpoint
# (speech_campplus_sv_en_voxceleb_16k) is NOT registered in funasr's AutoModel,
# so the numbers here are a floor.
SPEAKER_MODEL: str = _env_str("SPEAKER_MODEL", "cam++")
SPEAKER_DEVICE: str = _env_str("SPEAKER_DEVICE", "cpu")
# Where the user's enrolled voiceprint lives: EMBEDDINGS only, never audio.
# models/ is gitignored. Delete the file (or DELETE /api/speaker) to forget it.
SPEAKER_PROFILE_PATH: str = _env_str("SPEAKER_PROFILE_PATH", "models/speaker_user.json")

# A custom interpreter system prompt saved from the dashboard (server/prompt_store.py),
# with its last few versions. models/ is gitignored. Delete the file, or press "Use
# built-in" on the dashboard, to go back to the measured SYSTEM_PROMPT. Only the
# server loads it; eval/ and distill/ always use the built-in prompt.
PROMPT_OVERRIDE_PATH: str = _env_str("PROMPT_OVERRIDE_PATH", "models/interpreter_prompt.json")
# The built-in prompt is ~2,600 chars. The cap keeps the prompt plus 12 context lines
# well inside the 4096-token context (an overflow silently cuts the system prompt).
PROMPT_MAX_CHARS: int = _env_int("PROMPT_MAX_CHARS", 8000)
# Cosine above which a 1 s window counts as the enrolled user. Same-speaker
# cosine against a calm-only profile ranges 0.76 (calm) down to 0.52 (fearful);
# cross-speaker is well below 0.45 on average.
SPEAKER_MATCH_THRESHOLD: float = _env_float("SPEAKER_MATCH_THRESHOLD", 0.45)
# The utterance label comes from the FRACTION of its windows that match the
# user, not from cutting it: user-only utterances score ~0.75, partner-only
# ~0.08, and an utterance containing a turn change ~0.43. Below LO -> "other",
# above HI -> "user", between -> "mixed" (interpreted anyway, as today).
# Measured 3-way accuracy 79%, and the error that matters -- a partner line
# silently dropped as the user's -- is 1.4%.
SPEAKER_USER_FRAC_LO: float = _env_float("SPEAKER_USER_FRAC_LO", 0.15)
SPEAKER_USER_FRAC_HI: float = _env_float("SPEAKER_USER_FRAC_HI", 0.75)
SPEAKER_WINDOW_S: float = _env_float("SPEAKER_WINDOW_S", 1.0)
SPEAKER_HOP_S: float = _env_float("SPEAKER_HOP_S", 0.5)
# Below this many enrolled utterances the profile is too thin to trust and
# every line is labelled "unknown" (= today's behaviour). Enrolment should span
# moods: a calm-only profile rejects the user precisely when they are upset.
SPEAKER_MIN_ENROLL: int = _env_int("SPEAKER_MIN_ENROLL", 3)
# Cap on one POST /api/enroll body. 4 MiB is ~2 minutes of 16 kHz int16 PCM,
# far more than the guided page records; an exposure limit like
# WS_MAX_MESSAGE_BYTES, not a feature.
ENROLL_MAX_BYTES: int = _env_int("ENROLL_MAX_BYTES", 4 * 1024 * 1024)

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

# Utterances that may be in the pipeline at once, per connection. Whisper, SER
# and speaker id all serialise globally (~3 utterances/sec for the whole
# process), so
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

# --- Clips (explicit, user-initiated retention) ------------------------------
# The brief rules out retaining third-party audio BY DEFAULT. A clip is the
# sanctioned exception: the listener holds the pendant's button, the server
# writes the utterances heard during the hold to disk as one clip, and the
# device shows a visible "saved" confirmation. See README.md (wire protocol) for the contract.
CLIPS_ENABLED: bool = _env_bool("CLIPS_ENABLED", True)
# Where clips land: a wav + json sidecar per clip, relative to the cwd (the
# project root). No database, per the brief -- the directory IS the index.
CLIPS_DIR: str = _env_str("CLIPS_DIR", "clips")
# How much recent audio each connection keeps in memory so that a `save`
# arriving a few seconds after the `read` still finds it. The read lands
# ~1-5s after the utterance and the user reacts a moment later, so a handful
# is plenty. Both bounds apply: N utterances AND S seconds, whichever is
# tighter. Audio is held as int16, so the worst case per connection is
# min(N x MAX_UTTERANCE_MS, S) x 32 KB/s ~= 1.4 MB at the defaults, and the ring is
# dropped with the connection.
# Raised from 8 to 24 with the sentence split: one 15 s utterance can now
# become up to SPLIT_MAX_PIECES ids, so 8 entries could hold under 20 s of
# speech. The age bound (CLIP_RETENTION_S) is the real limit, and the pieces of
# one utterance together hold no more audio than the utterance did.
CLIP_RETENTION_N: int = _env_int("CLIP_RETENTION_N", 24)
CLIP_RETENTION_S: float = _env_float("CLIP_RETENTION_S", 45.0)
# Ceiling on stored clips. Load-bearing, not decoration: without it a `save`
# per utterance from a hostile client through the tunnel writes audio to disk
# indefinitely. Over the cap a save replies ok:false ("clip store full") rather
# than silently evicting moments the user deliberately kept -- deleting is the
# user's call, from the dashboard.
CLIPS_MAX_FILES: int = _env_int("CLIPS_MAX_FILES", 200)

# --- Confidence (server/confidence.py) ---------------------------------------
# Every read carries {"score","level","reasons"} on the wire. The base score is
# the interpreter's first-token probability for its tone label (Ollama
# logprobs), Platt-scaled; each weak input multiplies it by CONFIDENCE_PENALTY
# and adds a reason. The numbers here are meant to be SET BY
# `python -m eval.confidence_eval`, which measures how each signal tracks
# correctness on this pipeline -- hand-tuned thresholds are how a confidence
# number stops meaning anything. Until that has been run: identity Platt and
# conservative cut-offs.
OLLAMA_LOGPROBS: bool = _env_bool("OLLAMA_LOGPROBS", True)   # ask Ollama for token probabilities
# Measured 2026-09-22 (eval/results/pipeline_conf.csv, emotion2vec x qwen3:14b,
# 280 RAVDESS clips, parakeet:v3): tone_prob alone has AUROC 0.81 against tone
# correctness and the composite 0.84; the Platt fit (a=0.35, b=-0.94) moved ECE
# from 0.187 to 0.186, i.e. nothing, so the identity stays. Wrong reads sit at
# tone_prob 0.6-0.9; correct ones at ~1.0. With the cut-offs below the listener
# sees: high 63 reads at 97% accuracy, medium 175 at 83%, low 41 at 17%.
# Caveat: 68% of those clips are under 1.5 s voiced, so nearly every RAVDESS
# read carries the "too short" penalty; live speech is longer and lands higher.
CONFIDENCE_PLATT_A: float = _env_float("CONFIDENCE_PLATT_A", 1.0)
CONFIDENCE_PLATT_B: float = _env_float("CONFIDENCE_PLATT_B", 0.0)
CONFIDENCE_HIGH: float = _env_float("CONFIDENCE_HIGH", 0.9)   # >= this renders as before
CONFIDENCE_LOW: float = _env_float("CONFIDENCE_LOW", 0.6)     # < this is suppressed ("not sure")
CONFIDENCE_PENALTY: float = _env_float("CONFIDENCE_PENALTY", 0.7)  # per weak input
# One multiplier per weak input, each defaulting to CONFIDENCE_PENALTY so the
# measured behaviour is unchanged. 1.0 ignores that input. Live-tunable from the
# dashboard's "Emotion weights" card (server/tuning.py), never persisted.
CONF_PEN_SHORT: float = _env_float("CONF_PEN_SHORT", CONFIDENCE_PENALTY)        # too short
CONF_PEN_VOICE: float = _env_float("CONF_PEN_VOICE", CONFIDENCE_PENALTY)        # voice unclear
CONF_PEN_WORDS: float = _env_float("CONF_PEN_WORDS", CONFIDENCE_PENALTY)        # misheard
CONF_PEN_TWO_VOICES: float = _env_float("CONF_PEN_TWO_VOICES", CONFIDENCE_PENALTY)
CONF_PEN_DISAGREE: float = _env_float("CONF_PEN_DISAGREE", CONFIDENCE_PENALTY)  # tone mixed
# Voice <-> interpreter mix for the headline emotion (server/blend.py). 1.0 = the
# interpreter's label, exactly the measured behaviour; 0.0 = the voice model's.
EMOTION_BLEND: float = _env_float("EMOTION_BLEND", 1.0)
# Voice models sit in their worst regime under ~1.5 s of speech (RESEARCH.md
# §4.10: a 5-6 point UAR penalty at 2 s), so a read on a very short line leans
# on words alone and says so.
CONFIDENCE_MIN_VOICE_S: float = _env_float("CONFIDENCE_MIN_VOICE_S", 1.5)
# Transcriber confidence below which the words themselves are in doubt. Backend
# specific (exp of Whisper's avg_logprob, or of Parakeet's minimum token
# log-prob). 0.35 is exp(-1), Whisper's own "this segment failed" line. On
# RAVDESS it did not track tone correctness (AUROC 0.45 -- the transcripts
# there are near-perfect), so it is kept only as a flag for genuinely doubtful
# transcripts, not as a tone signal.
CONFIDENCE_MIN_ASR: float = _env_float("CONFIDENCE_MIN_ASR", 0.35)

# --- Transcript log (server/translog.py) -------------------------------------
# A running record of every utterance the server transcribed -- text, ASR
# model, tone/read, speaker label, timings -- plus the utterance's audio, so
# ASR models can later be re-scored on the speech this listener actually hears
# (see docs/status/TODO.md: every ASR decision so far rests on synthetic SAPI
# speech). `python -m server.translog --export DIR` turns the log into an
# `experiments/asr/`-shaped set for `eval.asr_eval`, with the transcripts as
# draft references to correct by hand.
#
# Retention note: with audio on, this keeps third-party speech, which the
# pendant brief otherwise avoids (clips are the explicit exception). So it is
# OFF by default, field by field, and switched on from the dashboard's
# "Transcript log" card for a session -- runtime only, never persisted, the
# same rule as the model switches. The env vars below set the startup state
# for anyone who wants it on from boot; the audio store is a rolling cap
# either way, so it cannot grow without bound.
TRANSCRIPT_LOG_ENABLED: bool = _env_bool("TRANSCRIPT_LOG_ENABLED", False)
# Which parts of an utterance to keep, as a comma list. The record always
# carries id, timestamp, duration and the ASR model key; everything else is
# opt-in:
#   audio       the utterance WAV (the part that makes ASR re-scoring possible)
#   transcript  the text the ASR produced
#   result      the interpreter's tone and read
#   voice       the SER dict (emotion, confidence, arousal, dominance)
#   speaker     the speaker-id label and score
#   timing      per-stage seconds (whisper_s, ser_s, llm_s)
TRANSCRIPT_LOG_FIELDS: str = _env_str("TRANSCRIPT_LOG_FIELDS", "")
# One JSONL file per day plus an audio/ folder, relative to the project root.
TRANSCRIPT_LOG_DIR: str = _env_str("TRANSCRIPT_LOG_DIR", "logs/transcripts")
# Rolling cap on stored utterance WAVs; the oldest are deleted past it. A 3 s
# utterance is ~100 KB, so 5000 files is roughly 500 MB and a few weeks of
# conversation. The JSONL text rows are not capped -- they are tiny.
TRANSCRIPT_LOG_MAX_AUDIO: int = _env_int("TRANSCRIPT_LOG_MAX_AUDIO", 5000)
