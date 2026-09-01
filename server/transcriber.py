"""Thin wrapper around faster-whisper.

Responsibilities kept deliberately small: load the model exactly once, and turn
a float32 utterance into a plain string. Everything policy-ish (model name,
device) comes from config so this file rarely needs to change.

The model is loaded at import time. Import happens once at server startup, so
the (multi-second) load cost is paid before any client connects rather than on
the first utterance. The GPU is off-limits here on purpose -- see config.
"""

from __future__ import annotations

import numpy as np
from faster_whisper import WhisperModel

from . import config

# Loaded once, at import. int8 on CPU is the sweet spot for the "base" model:
# small memory footprint, fast enough to keep up with conversational speech
# while the Arc GPU stays free for the LLM.
_model = WhisperModel(
    config.WHISPER_MODEL,
    device=config.WHISPER_DEVICE,
    compute_type=config.WHISPER_COMPUTE_TYPE,
)


def transcribe(audio: np.ndarray) -> str:
    """Transcribe one utterance (float32 PCM in [-1, 1]) to text.

    This is a *blocking* CPU call -- callers must run it off the event loop (see
    main.py's executor usage) so audio ingest never stalls.

    Whisper settings, and why:
      - beam_size=1                  greedy decoding is fastest; utterances are
                                     short so the accuracy hit is negligible.
      - vad_filter=True              trims leading/trailing non-speech that our
                                     energy segmenter let through.
      - condition_on_previous_text=False
                                     each utterance is decoded independently, so
                                     one bad transcript can't poison the next.
    """
    segments, _info = _model.transcribe(
        audio,
        beam_size=1,
        language=config.WHISPER_LANGUAGE,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    # `segments` is a lazy generator; joining forces the actual decode to run.
    text = " ".join(seg.text.strip() for seg in segments)
    return text.strip()
