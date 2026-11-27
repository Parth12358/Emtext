"""Speech-to-text behind one function, with a swappable backend.

`transcribe(audio) -> str` is the whole contract the rest of the server knows.
Behind it sits exactly ONE resident model, chosen from a fixed catalogue and
replaceable at runtime from the dashboard (`POST /api/asr`), the same way the
interpreter LLM is switched -- and, like that switch, never persisted.

Why a catalogue and not a free-form model name
----------------------------------------------
Two model families live here with different runtimes:

  * ``whisper:<name>``  -- faster-whisper (CTranslate2). Autoregressive, pads
    every utterance to a 30 s window, so its cost is *flat* per utterance and a
    large model costs seconds even for a two-word reply.
  * sherpa-onnx families -- Parakeet TDT (NVIDIA), Moonshine (Useful Sensors)
    and SenseVoice (FunAudioLLM). Non-autoregressive or nearly so, cost scales
    with the audio, and they do not invent text on silence the way Whisper can.
    Files are int8 ONNX archives from the sherpa-onnx `asr-models` release.

A key names both the family and the exact files, so the dashboard can only ever
select something this file has vetted, and an attacker-supplied string never
reaches a loader or a log line (the same rule as `dashboard._require_pulled`).

Concurrency and memory
----------------------
`transcribe()` is a blocking CPU call; `main.py` runs it in the executor. The
resident model is a module global read once per call into a local, so an
utterance that started on the old model finishes on it while a swap happens.
Only one model is resident: switching builds the new one first (so a failed
switch leaves the old one in place), then drops the old one. Nothing here is
ever moved to the GPU -- the Arc is the LLM's (see config.py).

Import loads the configured default synchronously, so every eval script that
warms once and then times keeps its meaning. A failure to load the DEFAULT
raises and stops the server, as it always has: a server that transcribes
nothing is worse than one that refuses to start. A failure to load a model
selected LATER is reported in `status()` and the previous model keeps running.
"""

from __future__ import annotations

import gc
import logging
import math
import tarfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)

_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"


@dataclass(frozen=True)
class Spec:
    """One catalogue entry: how to build it, and what to tell the dashboard."""
    key: str
    family: str            # whisper | nemo_transducer | moonshine | sense_voice
    label: str
    note: str
    size_mb: int           # approximate on-disk size, for the dashboard
    model: str = ""        # faster-whisper name (whisper family only)
    archive: str = ""      # sherpa-onnx archive stem == directory name
    files: tuple[str, ...] = ()   # members that must exist under the directory


_PARAKEET_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
_MOONSHINE_FILES = ("preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx",
                    "cached_decode.int8.onnx", "tokens.txt")

CATALOG: dict[str, Spec] = {s.key: s for s in (
    Spec("whisper:base", "whisper", "Whisper base", "the long-standing default; ~10% WER",
         75, model="base"),
    Spec("whisper:distil-small.en", "whisper", "Whisper distil-small.en",
         "small's accuracy for less CPU (0.83 s measured)", 170, model="distil-small.en"),
    Spec("whisper:small.en", "whisper", "Whisper small.en", "~8.5% WER, 0.93 s measured",
         250, model="small.en"),
    Spec("whisper:distil-large-v3", "whisper", "Whisper distil-large-v3",
         "best Whisper on noisy speech; seconds per utterance on CPU", 750,
         model="distil-large-v3"),
    Spec("whisper:large-v3-turbo", "whisper", "Whisper large-v3-turbo",
         "~7.8% WER; seconds per utterance on CPU", 810, model="large-v3-turbo"),
    Spec("parakeet:v2", "nemo_transducer", "Parakeet TDT 0.6B v2 (English)",
         "~6% WER, beats Whisper large; cost scales with audio", 640,
         archive="sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8", files=_PARAKEET_FILES),
    Spec("parakeet:v3", "nemo_transducer", "Parakeet TDT 0.6B v3 (25 languages)",
         "multilingual sibling of v2, ~6.3% English WER", 640,
         archive="sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8", files=_PARAKEET_FILES),
    Spec("moonshine:base", "moonshine", "Moonshine base (English)",
         "built for short utterances; fastest here", 270,
         archive="sherpa-onnx-moonshine-base-en-int8", files=_MOONSHINE_FILES),
    Spec("moonshine:tiny", "moonshine", "Moonshine tiny (English)",
         "smallest and fastest; accuracy near Whisper base", 120,
         archive="sherpa-onnx-moonshine-tiny-en-int8", files=_MOONSHINE_FILES),
    Spec("sensevoice:small", "sense_voice", "SenseVoice small",
         "also tags emotion and laughter/crying events (logged only)", 230,
         archive="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
         files=("model.int8.onnx", "tokens.txt")),
)}


def spec_for(key: str) -> Spec:
    """Resolve a key. `whisper:<anything>` is accepted so WHISPER_MODEL keeps working."""
    if key in CATALOG:
        return CATALOG[key]
    if key.startswith("whisper:") and len(key) > 8:
        name = key[8:]
        return Spec(key, "whisper", f"Whisper {name}", "not in the catalogue", 0, model=name)
    raise KeyError(f"unknown ASR model key {key!r}")


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class _Whisper:
    """faster-whisper, exactly as this file always ran it."""

    family = "whisper"

    def __init__(self, spec: Spec) -> None:
        from faster_whisper import WhisperModel

        self.spec = spec
        self._model = WhisperModel(spec.model, device=config.WHISPER_DEVICE,
                                   compute_type=config.WHISPER_COMPUTE_TYPE)

    def transcribe(self, audio: np.ndarray) -> str:
        return self.transcribe_scored(audio)[0]

    def transcribe_scored(self, audio: np.ndarray) -> tuple[str, float | None]:
        """(text, confidence). Confidence is exp(mean avg_logprob) over segments.

        faster-whisper flags a segment as unreliable below avg_logprob -1.0
        (that is its own temperature-fallback threshold), which is exp(-1) =
        0.37 on this scale; a segment the model thinks is silence
        (no_speech_prob > 0.6) forces 0, because whatever text it produced is
        hallucinated. None when there were no segments.
        """
        # Whisper settings, and why:
        #   beam_size=1                  greedy is fastest; utterances are short
        #   vad_filter=True              trims non-speech the energy VAD let through
        #   condition_on_previous_text=False
        #                                each utterance decoded independently, so
        #                                one bad transcript cannot poison the next
        segments, _info = self._model.transcribe(
            audio,
            beam_size=1,
            language=config.WHISPER_LANGUAGE,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        # `segments` is a lazy generator; listing it forces the decode to run.
        segs = list(segments)
        text = " ".join(seg.text.strip() for seg in segs).strip()
        if not segs:
            return text, None
        if any(getattr(s, "no_speech_prob", 0.0) > 0.6 for s in segs):
            return text, 0.0
        lps = [float(getattr(s, "avg_logprob", 0.0)) for s in segs]
        return text, round(min(1.0, math.exp(sum(lps) / len(lps))), 3)


def _pin_sherpa_onnxruntime() -> None:
    """Load sherpa-onnx's bundled onnxruntime.dll by full path, before the import.

    Windows 11 ships its own onnxruntime.dll (1.17.1) in System32. sherpa-onnx's
    extension asks for `onnxruntime.dll` by name, and the loader resolves that to
    the System32 copy rather than the 1.29 sitting beside it in sherpa_onnx/lib/,
    so every sherpa key dies with "requested API version [28] is not available"
    (found 2026-10-24; it took parakeet:v3, the default, and with it the server).
    Once a DLL of that name is mapped, later by-name requests reuse it, so loading
    the bundled one first by path is enough. Windows-only, and silent when the
    file is missing: a non-Windows or differently packaged wheel loads as before.
    """
    import sys

    if sys.platform != "win32":
        return
    import ctypes
    import importlib.util

    spec = importlib.util.find_spec("sherpa_onnx")
    if not spec or not spec.origin:
        return
    dll = Path(spec.origin).parent / "lib" / "onnxruntime.dll"
    if dll.exists():
        ctypes.WinDLL(str(dll))


class _Sherpa:
    """Parakeet / Moonshine / SenseVoice through sherpa-onnx's OfflineRecognizer."""

    def __init__(self, spec: Spec, model_dir: Path) -> None:
        # Imported here, not at module top: a missing wheel must only break
        # these keys, never the import of this module (and therefore the server).
        _pin_sherpa_onnxruntime()
        import sherpa_onnx

        self.spec = spec
        self.family = spec.family
        p = lambda name: str(model_dir / name)  # noqa: E731
        threads = max(1, config.ASR_THREADS)
        if spec.family == "nemo_transducer":
            self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=p("encoder.int8.onnx"), decoder=p("decoder.int8.onnx"),
                joiner=p("joiner.int8.onnx"), tokens=p("tokens.txt"),
                num_threads=threads, model_type="nemo_transducer", provider="cpu",
            )
        elif spec.family == "moonshine":
            self._rec = sherpa_onnx.OfflineRecognizer.from_moonshine(
                preprocessor=p("preprocess.onnx"), encoder=p("encode.int8.onnx"),
                uncached_decoder=p("uncached_decode.int8.onnx"),
                cached_decoder=p("cached_decode.int8.onnx"), tokens=p("tokens.txt"),
                num_threads=threads, provider="cpu",
            )
        elif spec.family == "sense_voice":
            # SenseVoice auto-detects language among zh/en/ja/ko/yue; pin it
            # when the app is configured for English so a mumbled line is not
            # transcribed as Cantonese. use_itn adds punctuation and numerals.
            lang = "en" if config.WHISPER_LANGUAGE == "en" else "auto"
            self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=p("model.int8.onnx"), tokens=p("tokens.txt"),
                num_threads=threads, language=lang, use_itn=True, provider="cpu",
            )
        else:
            raise ValueError(f"unsupported family {spec.family}")
        # OfflineRecognizer is not documented as thread-safe and main.py can
        # have several utterances in flight; one lock, as in ser.py.
        self._lock = threading.Lock()

    def transcribe(self, audio: np.ndarray) -> str:
        return self.transcribe_scored(audio)[0]

    def transcribe_scored(self, audio: np.ndarray) -> tuple[str, float | None]:
        text, conf, _tokens = self.transcribe_timed(audio)
        return text, conf

    def transcribe_timed(self, audio: np.ndarray):
        """(text, confidence, tokens). Confidence is exp(min per-token log-prob).

        `tokens` is a list of `sentences.Token` (text, start time, log-prob) when
        the decoder reports per-token timestamps, which is what
        `server/sentences.py` cuts on; None when it does not.

        sherpa-onnx's NeMo transducer decoder (Parakeet) fills
        `result.ys_log_probs` with the log-softmax value of each chosen token.
        A greedy transducer's posteriors are overconfident by construction (the
        token was chosen because it was the max), so the MINIMUM over tokens is
        the informative statistic: one badly uncertain token is what makes a
        transcript wrong. Moonshine and SenseVoice leave the list empty and get
        None.
        """
        samples = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).reshape(-1))
        with self._lock:
            stream = self._rec.create_stream()
            stream.accept_waveform(config.SAMPLE_RATE, samples)
            self._rec.decode_stream(stream)
            result = stream.result
        text = (result.text or "").strip()
        conf: float | None = None
        lps = list(getattr(result, "ys_log_probs", None) or [])
        if lps:
            conf = round(min(1.0, math.exp(float(min(lps)))), 3)
        if self.family == "sense_voice":
            # SenseVoice's side channels. Logged only: the interpreter's
            # mismatch rule needs an acoustic-only SER channel, and this one is
            # entangled with the transcript (docs/design/RESEARCH.md §4.3). Recorded in
            # docs/status/TODO.md as a candidate cross-check for laughter/crying events.
            extra = {k: getattr(result, k, "") for k in ("lang", "emotion", "event")}
            if any(extra.values()):
                log.debug("sensevoice tags: %s", extra)
        tokens = None
        toks = list(getattr(result, "tokens", None) or [])
        times = list(getattr(result, "timestamps", None) or [])
        if toks and len(toks) == len(times):
            from .sentences import Token

            lp = lps if len(lps) == len(toks) else [None] * len(toks)
            tokens = [Token(str(a), float(b), None if c is None else float(c))
                      for a, b, c in zip(toks, times, lp)]
        return text, conf, tokens


# ---------------------------------------------------------------------------
# Model files
# ---------------------------------------------------------------------------

def _models_dir() -> Path:
    d = Path(config.ASR_MODELS_DIR)
    return d if d.is_absolute() else Path(__file__).resolve().parent.parent / d


def model_dir(spec: Spec) -> Path:
    return _models_dir() / spec.archive


def downloaded(spec: Spec) -> bool:
    """Cheap stat check: every required member file is present."""
    if spec.family == "whisper":
        return True   # faster-whisper manages its own HF cache
    d = model_dir(spec)
    return all((d / f).is_file() for f in spec.files)


def ensure_downloaded(key: str, progress=None) -> Path:
    """Fetch and unpack a sherpa-onnx archive if its files are missing.

    Streams the tar.bz2 to a `.part` file next to the target so a failed or
    interrupted download never leaves a directory that *looks* complete, then
    extracts with the `data` filter (Python 3.12+), which refuses absolute
    paths and `..` members -- the archive comes from a GitHub release, but a
    release asset is still a download and gets the same treatment as any other.
    `progress(done_bytes, total_bytes)` is called during the transfer.
    """
    spec = spec_for(key)
    if spec.family == "whisper":
        return Path()
    target = model_dir(spec)
    if downloaded(spec):
        return target
    root = _models_dir()
    root.mkdir(parents=True, exist_ok=True)
    url = _RELEASE + spec.archive + ".tar.bz2"
    part = root / (spec.archive + ".tar.bz2.part")
    log.info("ASR: downloading %s (~%d MB) from %s", spec.archive, spec.size_mb, url)
    req = urllib.request.Request(url, headers={"User-Agent": "emtext"})
    done = 0
    with urllib.request.urlopen(req, timeout=config.ASR_DOWNLOAD_TIMEOUT_S) as resp, \
            part.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    with tarfile.open(part, "r:bz2") as tar:
        tar.extractall(root, filter="data")
    part.unlink(missing_ok=True)
    if not downloaded(spec):
        raise FileNotFoundError(f"{spec.archive}: archive unpacked but expected files "
                                f"are missing under {target}")
    log.info("ASR: %s ready (%d MB)", spec.archive, done >> 20)
    return target


def build(key: str, progress=None):
    """Build a backend for `key` WITHOUT making it the resident one.

    Used by `select()` and by eval/asr_eval.py, which compares several models
    in one process and must not disturb whatever the server would load.
    """
    spec = spec_for(key)
    if spec.family == "whisper":
        return _Whisper(spec)
    return _Sherpa(spec, ensure_downloaded(key, progress))


# ---------------------------------------------------------------------------
# The resident model
# ---------------------------------------------------------------------------

_current = None
_current_key: str = ""
_select_lock = threading.Lock()
_status: dict = {"state": "loading", "key": "", "progress": None, "error": None,
                 "since": time.time()}


def _set_status(state: str, key: str, progress=None, error: str | None = None) -> None:
    _status.update({"state": state, "key": key, "progress": progress,
                    "error": error, "since": time.time()})


def status() -> dict:
    """Load/download state for the dashboard. Progress is 0-1 or None."""
    return dict(_status, selected=_current_key)


def current_key() -> str:
    return _current_key


def current_family() -> str | None:
    return getattr(_current, "family", None)


def catalog() -> list[dict]:
    """Every selectable model, with the two flags the dashboard needs."""
    out = []
    for s in CATALOG.values():
        out.append({
            "key": s.key, "label": s.label, "family": s.family, "note": s.note,
            "size_mb": s.size_mb, "downloaded": downloaded(s),
            "selected": s.key == _current_key,
        })
    if _current_key and _current_key not in CATALOG:      # a whisper:<custom> default
        s = spec_for(_current_key)
        out.insert(0, {"key": s.key, "label": s.label, "family": s.family,
                       "note": s.note, "size_mb": s.size_mb, "downloaded": True,
                       "selected": True})
    return out


def select(key: str) -> dict:
    """Make `key` the resident model. Blocking; run it in the executor.

    Builds the new model first and only then drops the old one, so a failed
    download or load leaves the server exactly as it was. Never raises: the
    outcome is in the returned status dict (and in `status()` afterwards).
    """
    global _current, _current_key
    try:
        spec = spec_for(key)
    except KeyError as exc:
        _set_status("failed", key, error=str(exc))
        return status()
    if not _select_lock.acquire(blocking=False):
        return dict(status(), busy=True)
    try:
        if key == _current_key and _current is not None:
            _set_status("ready", key)
            return status()

        def progress(done: int, total: int) -> None:
            _set_status("downloading", key, progress=(done / total) if total else None)

        if not downloaded(spec):
            _set_status("downloading", key, progress=0.0)
        else:
            _set_status("loading", key)
        t0 = time.perf_counter()
        backend = build(key, progress)
        _set_status("loading", key)
        # One warm decode on silence: the first call pays one-off setup that
        # would otherwise land on a real utterance's latency.
        backend.transcribe(np.zeros(config.SAMPLE_RATE, dtype=np.float32))
        old, old_key = _current, _current_key
        _current, _current_key = backend, key
        del old
        gc.collect()
        _set_status("ready", key)
        log.info("ASR: %s -> %s (%.1fs)", old_key or "none", key, time.perf_counter() - t0)
    except Exception as exc:  # noqa: BLE001 -- keep the old model, report the failure
        log.warning("ASR: could not switch to %s (%s: %s); keeping %s",
                    key, type(exc).__name__, exc, _current_key or "none")
        _set_status("failed", key, error=f"{type(exc).__name__}: {exc}")
    finally:
        _select_lock.release()
    return status()


def transcribe(audio: np.ndarray) -> str:
    """Transcribe one utterance (float32 PCM in [-1, 1], 16 kHz) to text.

    Blocking CPU call -- callers run it off the event loop (main.py's executor)
    so audio ingest never stalls. Returns "" when no model is resident, which
    main.py already treats as "Whisper heard nothing".
    """
    return transcribe_scored(audio)[0]


def transcribe_scored(audio: np.ndarray) -> tuple[str, float | None]:
    """Like transcribe(), plus the backend's confidence in [0, 1] or None.

    The confidence scale is backend-specific (see each backend's docstring),
    which is why it feeds server/confidence.py through a threshold that
    eval.confidence_eval profiles rather than being shown raw.
    """
    backend = _current          # one read: a swap mid-call cannot affect us
    if backend is None:
        return "", None
    return backend.transcribe_scored(audio)


def transcribe_timed(audio: np.ndarray):
    """Like transcribe_scored(), plus per-token timings or None.

    Whisper returns None for the tokens (word timestamps would cost a second
    decode pass), so with a Whisper backend utterances are never split.
    """
    backend = _current
    if backend is None:
        return "", None, None
    fn = getattr(backend, "transcribe_timed", None)
    if fn is None:
        text, conf = backend.transcribe_scored(audio)
        return text, conf, None
    return fn(audio)


def _load_default() -> None:
    """Load config.ASR_MODEL at import. Raises on failure, as before."""
    global _current, _current_key
    key = config.ASR_MODEL
    spec = spec_for(key)
    if spec.family != "whisper" and not downloaded(spec):
        log.warning("ASR: default %s is not downloaded yet; fetching ~%d MB now",
                    key, spec.size_mb)
    _set_status("loading", key)
    _current = build(key)
    _current_key = key
    _set_status("ready", key)


_load_default()


if __name__ == "__main__":
    # python -m server.transcriber --list
    # python -m server.transcriber --fetch parakeet:v2 | all
    # python -m server.transcriber <key> file.wav [file.wav ...]
    import sys
    import wave

    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:]
    if not args or args[0] == "--list":
        for m in catalog():
            flags = ("*" if m["selected"] else " ") + ("d" if m["downloaded"] else " ")
            print(f"{flags} {m['key']:<26} {m['family']:<16} ~{m['size_mb']:>4} MB  {m['label']}")
        print("\n* = resident, d = downloaded")
        sys.exit(0)
    if args[0] == "--fetch":
        keys = list(CATALOG) if len(args) < 2 or args[1] == "all" else args[1:]
        for k in keys:
            if spec_for(k).family != "whisper":
                ensure_downloaded(k, lambda d, t: print(f"\r  {k}: {d >> 20} MB", end=""))
                print()
        sys.exit(0)

    key, paths = args[0], args[1:]
    backend = build(key)
    for path in paths:
        with wave.open(path, "rb") as w:
            rate, ch = w.getframerate(), w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        x = pcm.astype(np.float32) / 32768.0
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        if rate != config.SAMPLE_RATE:
            print(f"warning: {path} is {rate} Hz, expected {config.SAMPLE_RATE}")
        backend.transcribe(x[: config.SAMPLE_RATE])   # warm
        t0 = time.perf_counter()
        text, conf = backend.transcribe_scored(x)
        print(f"{key}  {time.perf_counter() - t0:.3f}s  {x.size / rate:.2f}s audio  "
              f"conf {conf}  -> {text!r}")
