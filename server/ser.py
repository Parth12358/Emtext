"""Speech emotion recognition (SER): how a line *sounded*, not what it said.

This is the acoustic half of the interpreter. `transcriber.py` answers "what
words were spoken"; this module answers "what did the voice carrying them sound
like", and the interpreter reads the two together.

Why that pairing is the whole point
-----------------------------------
Text alone cannot distinguish "oh, great" said with delight from "oh, great"
said flatly through gritted teeth -- the transcript is identical. The voice can.
So the signal we actually care about is not the emotion label on its own but the
*mismatch* between the words and the voice:

    positive words + negative-sounding voice  ->  sarcasm, or masking
    negative words + positive-sounding voice  ->  teasing, joking, banter
    words and voice agreeing                  ->  take it literally

That comparison happens in `interpreter.py`; this module's only job is to
produce an honest description of the voice for it to compare against.

What the numbers mean
---------------------
The model has two heads, and they answer different questions:

  - a *categorical* head (softmax over 7 classes) -> one discrete label plus the
    probability of that label, which is our `confidence`.

  - a *dimensional* head (sigmoid, 3 values in [0,1]) -> the VAD model of affect
    from psychology, which is often more useful than the label because it is
    continuous and degrades gracefully:

      valence    0 = negative/unpleasant  ..  1 = positive/pleasant
                 This is the sarcasm-relevant axis: it is what you compare
                 against the sentiment of the words.
      arousal    0 = calm/subdued         ..  1 = intense/activated
                 Energy, not pleasantness. Rage and delight are both high.
      dominance  0 = submissive/yielding  ..  1 = assertive/in-control
                 Separates e.g. angry (high) from fearful (low), which arousal
                 alone cannot.

Treat every value as a *hint*. SER is noisy, especially on short utterances, on
accents unlike the training data, and over a cheap microphone. Low `confidence`
means the label is close to a coin flip and should not override plain reading of
the words -- the interpreter prompt is written to respect that.

Operational notes
-----------------
Loaded once at import, on CPU by deliberate choice: the Arc B580 is reserved for
the LLM. Inference is a *blocking* call, so callers must run it off the event
loop (see main.py, which runs it in the executor concurrently with
transcription).

Cost, measured rather than assumed (12-core CPU, torch defaults): about 3.3 s
per utterance, against roughly 0.3 s for Whisper `base` on the same audio. That
is a *flat* cost -- a 1 s utterance and a 14 s utterance both take ~3.3 s --
because the Whisper feature extractor pads every input to a fixed 30 s window
(80 x 3000 mel frames) and the encoder always processes the whole thing. So SER,
not transcription, sets how soon a read can appear.

It does not stall the pipeline: each utterance is its own asyncio task, audio
keeps being read and segmented throughout, and running SER concurrently with
Whisper means the pair costs max(...) rather than the sum. But it does add real
latency before the read arrives. If that matters more than the voice data, set
SER_ENABLED=0.

A failure to load is never fatal. If the model is missing, the deps are absent,
or the weights fail to download, `_model` stays None, `available()` reports
False, and `analyze()` returns None -- the server then runs exactly as it did
before SER existed.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from . import config

log = logging.getLogger(__name__)

# Class order is fixed by the model's own output layer -- index i of `logits`
# means EMOTIONS[i]. This is taken verbatim from the MERaLiON-SER-v1 model card;
# note it is NOT alphabetical and NOT the order you might guess (Fearful and
# Disgusted come before Surprised). Reordering this silently mislabels results.
#
# This is only the fallback: `_load()` prefers the `id2label` map in the loaded
# checkpoint's own config, so a future revision that reorders its head relabels
# itself correctly instead of quietly reporting the wrong emotion.
EMOTIONS: tuple[str, ...] = (
    "neutral",
    "happy",
    "sad",
    "angry",
    "fearful",
    "disgusted",
    "surprised",
)

# The dimensional head emits exactly these, in this order.
_DIMS = ("valence", "arousal", "dominance")

# Utterances shorter than this are not worth scoring: the model needs a little
# audio to be meaningful, and the segmenter's MIN_UTTERANCE_MS already filters
# blips. Guards against a degenerate near-empty buffer reaching the model.
_MIN_SAMPLES = 1600  # 100 ms at 16 kHz

# emotion2vec (the lighter fallback backend) predicts 9 classes rather than 7,
# and returns them as bilingual strings like "生气/angry". We normalise to the
# same 7-label vocabulary the rest of the app speaks, so a backend swap never
# reaches the interpreter prompt or the wire protocol.
#
# "other" and "unknown" fold into neutral deliberately: they mean "the model
# could not commit", and neutral is the safe reading -- inventing an emotion
# from a non-answer is exactly the false positive this app must avoid.
_EMOTION2VEC_MAP = {
    "angry": "angry",
    "disgusted": "disgusted",
    "fearful": "fearful",
    "happy": "happy",
    "neutral": "neutral",
    "sad": "sad",
    "surprised": "surprised",
    "other": "neutral",
    "unknown": "neutral",
}

_model = None
_processor = None
_torch = None
_backend: str | None = None  # "meralion" | "emotion2vec" | None when unavailable
# Populated from the checkpoint at load time; falls back to EMOTIONS above.
_labels: tuple[str, ...] = EMOTIONS


def _pick_backend() -> str:
    """Resolve SER_BACKEND, defaulting to inference from the model name."""
    choice = (config.SER_BACKEND or "auto").strip().lower()
    if choice in ("meralion", "emotion2vec"):
        return choice
    return "emotion2vec" if "emotion2vec" in config.SER_MODEL.lower() else "meralion"

# The HF model is not documented as thread-safe, and main.py can have several
# utterances in flight at once (one asyncio task each, all sharing the executor
# thread pool). One lock around inference keeps that safe; utterance-length
# inference is short, so the serialisation costs us little.
_lock = threading.Lock()


def _load_meralion() -> None:
    """Primary backend: MERaLiON-SER-v1 via transformers.

    Gives the full picture -- 7 emotions AND the valence/arousal/dominance
    dimensions -- which is why it is the default despite being the heavy option.
    """
    global _model, _processor, _labels

    from transformers import AutoModelForAudioClassification, AutoProcessor

    # `trust_remote_code` is required: the two-head architecture lives in
    # custom modelling code in the repo, not in transformers itself.
    _processor = AutoProcessor.from_pretrained(config.SER_MODEL)
    _model = AutoModelForAudioClassification.from_pretrained(
        config.SER_MODEL,
        trust_remote_code=True,
    )
    _model.eval()
    _model.to(config.SER_DEVICE)

    # Prefer the checkpoint's own label map over our hardcoded tuple, so a
    # revision that reorders the head cannot silently mislabel results.
    id2label = getattr(_model.config, "id2label", None)
    if isinstance(id2label, dict) and id2label:
        ordered = [id2label[k] for k in sorted(id2label, key=lambda x: int(x))]
        _labels = tuple(str(v).lower() for v in ordered)
    else:
        _labels = EMOTIONS


def _load_emotion2vec() -> None:
    """Fallback backend: emotion2vec_plus_* via FunASR.

    Roughly an order of magnitude smaller and faster than MERaLiON, at a real
    cost: it is categorical only. There is no dimensional head, so valence,
    arousal and dominance come back as None and the interpreter falls back to
    reasoning from the emotion label alone. Since valence is the axis the
    sarcasm rule actually keys on, expect weaker mismatch detection here --
    this is a speed/quality trade, not a free win.
    """
    global _model, _labels

    from funasr import AutoModel

    # The same weights are published under two ids: "emotion2vec/..." on Hugging
    # Face and "iic/..." on ModelScope, which is where FunASR fetches from.
    # Accept the Hugging Face spelling (it is what the model card shows) and
    # translate, so SER_MODEL doesn't have to know which hub is underneath.
    name = config.SER_MODEL
    if name.lower().startswith("emotion2vec/"):
        name = "iic/" + name.split("/", 1)[1]

    # `device` must be passed explicitly. FunASR's AutoModel defaults to
    # device="cuda" and only falls back to CPU when CUDA happens to be
    # unavailable -- so on a machine with a CUDA/XPU torch build this backend
    # would silently take the GPU that is reserved for the LLM. Being explicit
    # means the hardware split holds by instruction rather than by accident.
    _model = AutoModel(model=name, device=config.SER_DEVICE, disable_update=True)
    # FunASR reports labels per result rather than exposing a fixed ordered
    # vocabulary, so record what we normalise TO rather than inventing an order.
    _labels = tuple(sorted(set(_EMOTION2VEC_MAP.values())))


def _load() -> None:
    """Load the model once, converting any failure into 'SER disabled'.

    Deliberately broad `except`: a SER failure must never prevent the server
    from starting, and the failure modes here are open-ended (missing torch, no
    network for the first download, incompatible transformers version, a
    trust_remote_code module that fails to import, out-of-memory).
    """
    global _model, _processor, _torch, _labels, _backend

    if not config.SER_ENABLED:
        log.info("SER disabled by config (SER_ENABLED=0)")
        return

    want = _pick_backend()
    try:
        import torch

        if config.SER_TORCH_THREADS > 0:
            torch.set_num_threads(config.SER_TORCH_THREADS)
        _torch = torch

        if want == "emotion2vec":
            _load_emotion2vec()
        else:
            _load_meralion()
        _backend = want

        log.info(
            "SER model loaded: %s (%s backend) on %s (labels: %s)",
            config.SER_MODEL,
            _backend,
            config.SER_DEVICE,
            ", ".join(_labels),
        )
    except Exception as exc:  # noqa: BLE001 -- see docstring
        _model = _processor = _torch = None
        _backend = None
        log.warning(
            "SER unavailable (%s: %s) -- continuing without voice analysis",
            type(exc).__name__,
            exc,
        )


_load()


def available() -> bool:
    """True if analyze() can actually produce a result."""
    return _model is not None


def _normalise_emotion2vec_label(raw: str) -> str:
    """Turn a FunASR label into our 7-label vocabulary.

    emotion2vec returns bilingual labels like "生气/angry" (and sometimes just
    "angry"), so we take the last "/"-separated part and map it. Anything
    unrecognised -- including the model's own "other"/"unknown" non-answers --
    becomes neutral, which is the safe reading when the model hasn't committed.
    """
    tail = str(raw).split("/")[-1].strip().lower()
    return _EMOTION2VEC_MAP.get(tail, "neutral")


def _analyze_emotion2vec(samples: np.ndarray) -> dict | None:
    """FunASR path. Categorical only -- no dimensional head, so no VAD values."""
    with _lock:
        out = _model.generate(
            samples,
            fs=config.SAMPLE_RATE,
            granularity="utterance",
            extract_embedding=False,
            disable_pbar=True,
        )
    if not out:
        return None

    first = out[0]
    labels = first.get("labels") or []
    scores = first.get("scores") or []
    if not labels or not scores or len(labels) != len(scores):
        return None

    # Several raw classes collapse onto one of ours ("other" and "unknown" both
    # become neutral), so sum probabilities per normalised label before picking
    # the winner. Taking the raw argmax first could hand back a class whose
    # merged total is not actually the largest.
    merged: dict[str, float] = {}
    for label, score in zip(labels, scores):
        merged[_normalise_emotion2vec_label(label)] = (
            merged.get(_normalise_emotion2vec_label(label), 0.0) + float(score)
        )
    emotion = max(merged, key=merged.get)

    return {
        "emotion": emotion,
        "confidence": round(min(1.0, max(0.0, merged[emotion])), 3),
        # No dimensional head on this model. Explicit None (rather than a
        # made-up 0.5) is what lets the interpreter and the web client omit the
        # fields entirely instead of presenting a fabricated neutral reading.
        "valence": None,
        "arousal": None,
        "dominance": None,
    }


def analyze(audio: np.ndarray) -> dict | None:
    """Describe how one utterance *sounded*.

    Args:
        audio: mono float32 PCM in [-1, 1] at config.SAMPLE_RATE (16 kHz) --
            the same buffer the transcriber receives.

    Returns:
        {"emotion": str, "confidence": float, "valence": float,
         "arousal": float, "dominance": float}, or None if SER is unavailable,
        the audio is too short, or inference failed. Returning None rather than
        raising is intentional: the caller treats missing voice data as "no
        extra information", which is always a safe state to be in.
    """
    if _model is None or _torch is None or _backend is None:
        return None
    if audio is None or audio.size < _MIN_SAMPLES:
        return None

    try:
        # The processor expects a plain float32 numpy array; make sure that is
        # what it gets regardless of what the segmenter handed us.
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)

        if _backend == "emotion2vec":
            return _analyze_emotion2vec(samples)

        inputs = _processor(
            samples,
            sampling_rate=config.SAMPLE_RATE,
            return_tensors="pt",
            return_attention_mask=True,
        )
        # The custom forward() accepts only these two keys; passing anything
        # else the processor happens to emit raises a TypeError.
        inputs = {
            k: v.to(config.SER_DEVICE)
            for k, v in inputs.items()
            if k in ("input_features", "attention_mask")
        }

        with _lock, _torch.inference_mode():
            out = _model(**inputs)
            logits = out["logits"]
            dims = out["dims"]

            # Softmax over the categorical head: we want the winning class AND
            # how sure it is, because the interpreter downweights weak signals.
            probs = _torch.softmax(logits.float(), dim=-1)[0]
            idx = int(_torch.argmax(probs).item())
            confidence = float(probs[idx].item())
            values = [float(v) for v in dims.float().reshape(-1)[: len(_DIMS)]]

        emotion = _labels[idx] if idx < len(_labels) else "neutral"

        result: dict = {"emotion": emotion, "confidence": round(confidence, 3)}
        for name, value in zip(_DIMS, values):
            # The head is documented as sigmoid-activated, so values should
            # already be in [0, 1]; clamp anyway so a checkpoint that emits raw
            # logits cannot leak out-of-range numbers into the prompt and the
            # wire protocol.
            result[name] = round(min(1.0, max(0.0, value)), 3)
        for name in _DIMS:
            result.setdefault(name, None)  # short/odd output -> explicit None
        return result
    except Exception as exc:  # noqa: BLE001
        # One bad utterance must not kill the stream or the task that called us.
        log.warning("SER inference failed (%s: %s)", type(exc).__name__, exc)
        return None


if __name__ == "__main__":
    # Smoke test: score a WAV (or a synthetic tone) and print the dict.
    #   python -m server.ser [path.wav]
    import sys
    import wave

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) > 1:
        with wave.open(sys.argv[1], "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            pcm = pcm.astype(np.float32) / 32768.0
            if channels > 1:
                pcm = pcm.reshape(-1, channels).mean(axis=1)
        print(f"{sys.argv[1]}: {pcm.size / rate:.2f}s @ {rate} Hz")
        if rate != config.SAMPLE_RATE:
            print(f"warning: expected {config.SAMPLE_RATE} Hz; result will be off")
    else:
        t = np.linspace(0, 2.0, 32000, dtype=np.float32)
        pcm = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        print("synthetic 220 Hz tone, 2.0s")

    print("available:", available())
    print("result:", analyze(pcm))
