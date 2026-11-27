"""Streaming speech recognition: live words while someone talks, and where their sentences end.

Why a second ASR
----------------
The main transcriber (Parakeet TDT, `transcriber.py`) is OFFLINE: it decodes a
finished buffer. That is what the interpreter reads and what the evals score,
and it stays so. But a listener in a live conversation needs two things an
offline model cannot give:

  * the words as they are said (a `partial` frame), not seconds later;
  * the moment a sentence ENDS, so its read can go out at once. The energy VAD
    misses pauses in a noisy room, the pendant stops sending when it goes quiet,
    and the live split in main.py only looks every LIVE_SPLIT_EVERY_S.

A streaming transducer decodes 160 ms at a time and has an endpoint detector
(trailing silence after decoded text). main.py feeds it every audio frame of a
connection, sends its text as `partial`, and on an endpoint asks the live split
to hand out everything said so far at once (Parakeet re-transcribes those
pieces, so the text the interpreter reads is unchanged in kind).

Model choice, measured 2026-11-26 on three logged real clips (12 threads CPU,
2 decode threads): Nemotron Speech Streaming 0.6B (160 ms chunks, int8) writes
punctuated, capitalised text close to Parakeet's ("If my honesty comes off as
weird to you, maybe the problem is...") at RTF 0.31; NeMo FastConformer 80 ms is
cheaper (RTF 0.08) but lowercase, unpunctuated and visibly worse ("in my honesty
comes up as we had to you..."). Endpoints fired at the clips' real pauses.

Contract, like ser.py and speaker.py: CPU only (the GPU is the LLM's), loaded
once, **never raises**. With the model missing or `STREAMING_ASR_MODEL` empty,
`available()` is False and main.py runs exactly as before.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)

_rec = None
_lock = threading.Lock()   # the recognizer is shared; decodes are serialised
_error: str | None = None


def _model_dir() -> Path:
    d = Path(config.ASR_MODELS_DIR)
    d = d if d.is_absolute() else Path(__file__).resolve().parent.parent / d
    return d / config.STREAMING_ASR_MODEL


def load() -> None:
    """Build the shared recognizer. Blocking (~1.5 s); main.py runs it in the executor."""
    global _rec, _error
    if not config.STREAMING_ASR_MODEL:
        _error = "disabled (STREAMING_ASR_MODEL empty)"
        log.info("streaming ASR %s", _error)
        return
    try:
        from .transcriber import _pin_sherpa_onnxruntime   # same DLL clash as the offline models

        _pin_sherpa_onnxruntime()
        import sherpa_onnx

        d = _model_dir()
        files = {k: d / f"{k}.int8.onnx" for k in ("encoder", "decoder", "joiner")}
        missing = [str(p) for p in [*files.values(), d / "tokens.txt"] if not p.is_file()]
        if missing:
            _error = f"model files missing: {missing[0]}"
            log.warning("streaming ASR unavailable: %s -- download %s.tar.bz2 from the "
                        "sherpa-onnx asr-models release into %s", _error,
                        config.STREAMING_ASR_MODEL, d.parent)
            return
        _rec = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(d / "tokens.txt"),
            encoder=str(files["encoder"]), decoder=str(files["decoder"]),
            joiner=str(files["joiner"]),
            num_threads=config.STREAMING_ASR_THREADS,
            sample_rate=config.SAMPLE_RATE, feature_dim=80,
            enable_endpoint_detection=True,
            # rule1: no text at all for this long -> reset (keeps the stream short)
            # rule2: this much quiet AFTER some text -> the sentence ended
            # rule3: force an endpoint on a very long run-on
            rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=config.STREAMING_ENDPOINT_S,
            rule3_min_utterance_length=20.0,
            decoding_method="greedy_search",
            model_type="nemo_transducer",
        )
        _error = None
        log.info("streaming ASR loaded: %s (%d threads, endpoint after %.2fs quiet)",
                 config.STREAMING_ASR_MODEL, config.STREAMING_ASR_THREADS,
                 config.STREAMING_ENDPOINT_S)
    except Exception as exc:  # noqa: BLE001 -- optional feature, never fatal
        _rec = None
        _error = f"{type(exc).__name__}: {exc}"
        log.warning("streaming ASR unavailable (%s) -- continuing without live words", _error)


def available() -> bool:
    return _rec is not None


def status() -> dict:
    return {"model": config.STREAMING_ASR_MODEL or None, "ready": available(), "error": _error}


@dataclass
class Update:
    text: str                        # everything decoded since the last endpoint
    tokens: list[tuple[str, int]]    # (token, absolute sample index) for the same span
    endpoint: bool                   # a sentence just ended (the stream was reset)


class Live:
    """One connection's stream. Not thread-safe: main.py feeds it from one worker task.

    Positions are ABSOLUTE sample counts over everything fed on this
    connection, so main.py can line tokens up with what the segmenter has
    already handed out.
    """

    def __init__(self) -> None:
        self._stream = _rec.create_stream() if _rec is not None else None
        self._start = 0      # absolute sample index where the current stream began
        self._fed = 0

    def feed(self, samples: np.ndarray) -> Update | None:
        """Decode `samples` (float32, 16 kHz). Blocking; run in the executor. Never raises."""
        if self._stream is None or samples.size == 0:
            return None
        try:
            with _lock:
                self._stream.accept_waveform(config.SAMPLE_RATE, samples)
                while _rec.is_ready(self._stream):
                    _rec.decode_stream(self._stream)
                res = json.loads(_rec.get_result_as_json_string(self._stream))
                endpoint = bool(_rec.is_endpoint(self._stream))
                if endpoint:
                    _rec.reset(self._stream)
            self._fed += int(samples.size)
            sr = config.SAMPLE_RATE
            toks = [(str(t), self._start + int(float(ts) * sr))
                    for t, ts in zip(res.get("tokens") or [], res.get("timestamps") or [])]
            upd = Update(text=(res.get("text") or "").strip(), tokens=toks, endpoint=endpoint)
            if endpoint:
                self._start = self._fed
            return upd
        except Exception as exc:  # noqa: BLE001 -- a lost partial, never a lost connection
            log.warning("streaming ASR decode failed (%s: %s)", type(exc).__name__, exc)
            return None


def text_after(tokens: list[tuple[str, int]], abs_sample: int) -> str:
    """The decoded text from tokens at or after `abs_sample`: what has not been sent yet."""
    return "".join(t for t, at in tokens if at >= abs_sample).strip()
