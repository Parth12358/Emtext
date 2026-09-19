"""Speaker identification: is this line the listener's own voice, or someone else's?

Why this stage exists
---------------------
emtext hears a microphone, not a person. The user's own voice, their
conversational partner's, and anything else in the room arrive as one stream.
Without this module the interpreter is regularly handed the user's own words and
asked what "the speaker" meant by them -- it explains the user's feelings back to
them, which is the single worst output this product can produce (and, per
RESEARCH.md §1.5, the acoustic channel is exactly where bias against an autistic
speaker lives, so the user's voice should not be scored at all).

So the question here is narrow. Not "who is everyone in the room" (that is
diarization, which is expensive and needs to count speakers) but "is this the
one person I have a voiceprint for". One enrolled profile, one decision per
utterance, three answers: ``user`` / ``other`` / ``mixed`` -- plus ``unknown``
when there is no profile yet, which is today's behaviour.

How the decision is made (all numbers from TODO.md, "No diarization")
--------------------------------------------------------------------
CAM++ (via funasr, already a dependency for emotion2vec SER) turns any stretch
of speech into a 192-d embedding whose cosine similarity is speaker identity.
~20 ms per utterance on CPU, so it hides inside the existing whisper+SER gather.

The trap is that the segmenter *glues turns together*: END_SILENCE_MS is 650 ms
and a normal reply lands 0-200 ms after the question, so "are you coming?" /
"yeah" is one utterance. A single label per utterance is therefore not enough,
and cutting the utterance is worse -- blind splitting falsely halves 24-84% of
single-speaker utterances, because one person changing emotion mid-sentence
looks exactly like a second person. What *does* separate cleanly is the
fraction of 1 s windows that match the enrolled user:

    user only ~0.75  |  contains a turn change ~0.43  |  partner only ~0.08

Thresholding that fraction (SPEAKER_USER_FRAC_LO / _HI) gives the 3-way label.
The error that matters most -- a partner's line dropped as the user's -- is
1.4%; the common error (28% of the user's own lines land in ``mixed``) just
means that line is interpreted anyway, as it is today.

Two things move the accuracy far more than the model does:

  * **Enrol across moods.** Same-speaker cosine against a calm-only profile is
    0.76 on calm speech and 0.52 on fearful. A profile built only from calm
    speech rejects the user precisely when they are upset -- the state this app
    exists for. The enrol page's prompts span the range for this reason.
  * **Model the partner online.** Once an utterance is labelled ``other``, its
    windows feed a per-connection partner centroid, and later windows are
    scored nearest-centroid instead of against a fixed threshold. Worth ~7
    points (87.5% -> 94.3%), needs no enrolment from anyone but the user, and
    dies with the connection because it is a property of the conversation.

Contract (same as ser.py)
-------------------------
Loaded once at import, CPU only (the GPU is the LLM's). ``identify()`` is a
blocking call -- run it in the executor. It **never raises**: any failure to
load or to infer degrades to "no speaker information", and the rest of the
codebase knows only ``available()``, ``identify()``, ``Profile`` and ``Session``.

What is stored: ``Profile`` persists the user's enrolment *embeddings* (a few
hundred floats) to ``SPEAKER_PROFILE_PATH`` -- never audio. That is still
biometric data, so only the user's own profile is ever written to disk, and
``Profile.clear()`` / ``DELETE /api/speaker`` forget it completely.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)

LABELS = ("user", "other", "mixed", "unknown")

# CAM++ needs a little audio to say anything. Below this the fbank frontend can
# emit zero frames and the embedding is garbage.
_MIN_SAMPLES = int(0.4 * config.SAMPLE_RATE)

_model = None
_lock = threading.Lock()   # funasr models are not documented thread-safe


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def _load() -> None:
    """Load CAM++ once, converting any failure into 'speaker id disabled'."""
    global _model

    if not config.SPEAKER_ENABLED:
        log.info("speaker id disabled by config (SPEAKER_ENABLED=0)")
        return

    try:
        # Reuse ser.py's loader patch: it makes FunASR skip a redundant
        # deepcopy of the checkpoint. Harmless here (the file is 27 MB) but it
        # keeps one behaviour for the two FunASR models in the process.
        try:
            from . import ser as _ser
            _ser._patch_funasr_deepcopy()
        except Exception:  # noqa: BLE001 -- purely an optimisation
            pass

        from funasr import AutoModel

        # Explicit device (funasr defaults to cuda and only falls back) and NO
        # trust_remote_code -- same posture as the emotion2vec backend in ser.py.
        _model = AutoModel(model=config.SPEAKER_MODEL, device=config.SPEAKER_DEVICE,
                           disable_update=True)
        log.info("speaker id model loaded: %s on %s", config.SPEAKER_MODEL,
                 config.SPEAKER_DEVICE)
    except Exception as exc:  # noqa: BLE001 -- never prevent startup
        _model = None
        log.warning("speaker id unavailable (%s: %s) -- every line will be interpreted",
                    type(exc).__name__, exc)


_load()


def available() -> bool:
    """True if the model loaded. Says nothing about whether a profile exists."""
    return _model is not None


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def embed(audio: np.ndarray) -> np.ndarray | None:
    """One L2-normalised 192-d embedding for a float32 16 kHz buffer, or None.

    Never raises. Returns None when the model is unavailable, the audio is too
    short, or inference fails.
    """
    if _model is None or audio is None or audio.size < _MIN_SAMPLES:
        return None
    try:
        samples = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).reshape(-1))
        with _lock:
            out = _model.generate(samples, fs=config.SAMPLE_RATE, disable_pbar=True)
        if not out:
            return None
        emb = out[0].get("spk_embedding")
        if emb is None:
            return None
        vec = np.asarray(emb.detach().cpu().numpy() if hasattr(emb, "detach") else emb,
                         dtype=np.float32).reshape(-1)
        return _unit(vec)
    except Exception as exc:  # noqa: BLE001
        log.warning("speaker embedding failed (%s: %s)", type(exc).__name__, exc)
        return None


def voiced_fraction(audio: np.ndarray, speech_rms: float | None = None) -> float:
    """Share of FRAME_MS frames whose RMS clears the VAD threshold (float scale)."""
    frame = int(config.SAMPLE_RATE * config.FRAME_MS / 1000)
    n = int(audio.size) // frame
    if n == 0:
        return 0.0
    thr = (config.SPEECH_RMS if speech_rms is None else speech_rms) / 32768.0
    f = audio[: n * frame].reshape(n, frame).astype(np.float32)
    rms = np.sqrt(np.mean(f * f, axis=1))
    return float(np.mean(rms >= thr))


def embed_windows(
    audio: np.ndarray,
    win_s: float | None = None,
    hop_s: float | None = None,
    speech_rms: float | None = None,
    min_voiced: float = 0.5,
) -> list[np.ndarray]:
    """Embeddings of overlapping windows across the utterance.

    This is what makes a glued two-speaker utterance visible: each window is
    scored on its own, and the utterance label comes from the *fraction* that
    match the user rather than from one embedding of the whole thing.

    Windows that are mostly silence are skipped. The segmenter leaves PRE_ROLL_MS
    at the front and END_SILENCE_MS at the back of every utterance, and a window
    sitting on that silence embeds as noise -- measured cosines of 0.0 and -0.09
    against the speaker's own centroid, on RAVDESS, where the same speaker's
    voiced windows score ~0.5. Left in, those windows would vote "not the user"
    on every utterance. The gate uses the same RMS threshold as the VAD, so
    "silent" means the same thing in both places. If nothing survives the gate
    (a very short utterance), fall back to one embedding of the whole thing.
    """
    if audio is None or audio.size < _MIN_SAMPLES:
        return []
    win = int((win_s or config.SPEAKER_WINDOW_S) * config.SAMPLE_RATE)
    hop = int((hop_s or config.SPEAKER_HOP_S) * config.SAMPLE_RATE)
    n = int(audio.size)
    out: list[np.ndarray] = []
    if n > win:
        start = 0
        while start < n:
            end = min(start + win, n)
            if end - start < _MIN_SAMPLES:
                break
            chunk = audio[start:end]
            if voiced_fraction(chunk, speech_rms) >= min_voiced:
                e = embed(chunk)
                if e is not None:
                    out.append(e)
            if end >= n:
                break
            start += hop
    if not out:
        e = embed(audio)
        if e is not None:
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# The user's profile (persisted) and the conversation partner (per connection)
# ---------------------------------------------------------------------------

class Profile:
    """The listener's enrolled voiceprint: a centroid over enrolment embeddings.

    Persisted as JSON at SPEAKER_PROFILE_PATH -- embeddings only, so the file is
    a few KB and contains nothing a person could listen to. Loaded once at
    startup; `add()` and `clear()` write through immediately, so a restart
    never loses an enrolment. Not thread-safe by design: it is only mutated
    from dashboard routes, which run on the event loop.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or config.SPEAKER_PROFILE_PATH)
        self.embeddings: list[np.ndarray] = []
        self.centroid: np.ndarray | None = None
        self.updated_at: float | None = None
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        """Read the profile if present. A corrupt file means 'no profile', not a crash."""
        self.embeddings, self.centroid, self.updated_at = [], None, None
        try:
            if not self.path.is_file():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            embs = [np.asarray(e, dtype=np.float32) for e in data.get("embeddings", [])]
            self.embeddings = [_unit(e) for e in embs if e.ndim == 1 and e.size > 0]
            self.updated_at = data.get("updated_at")
            self._recompute()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read speaker profile %s (%s); treating as absent",
                        self.path, exc)
            self.embeddings, self.centroid = [], None

    def save(self) -> bool:
        """Write through. Returns False (and logs) rather than raising."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.updated_at = time.time()
            payload = {
                "version": 1,
                "model": config.SPEAKER_MODEL,
                "updated_at": self.updated_at,
                "embeddings": [e.round(6).tolist() for e in self.embeddings],
            }
            self.path.write_text(json.dumps(payload), encoding="utf-8")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not write speaker profile %s (%s)", self.path, exc)
            return False

    # -- mutation -----------------------------------------------------------

    def add(self, embeddings: list[np.ndarray]) -> int:
        """Add enrolment embeddings (one per enrolled utterance) and persist."""
        added = 0
        for e in embeddings:
            if e is None:
                continue
            self.embeddings.append(_unit(np.asarray(e, dtype=np.float32).reshape(-1)))
            added += 1
        if added:
            self._recompute()
            self.save()
        return added

    def clear(self) -> None:
        """Forget the voiceprint entirely, including the file on disk."""
        self.embeddings, self.centroid, self.updated_at = [], None, None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete speaker profile %s (%s)", self.path, exc)

    def _recompute(self) -> None:
        if self.embeddings:
            self.centroid = _unit(np.mean(np.stack(self.embeddings), axis=0))
        else:
            self.centroid = None

    # -- queries ------------------------------------------------------------

    @property
    def count(self) -> int:
        return len(self.embeddings)

    def ready(self) -> bool:
        """Enough enrolment to trust a match (SPEAKER_MIN_ENROLL)."""
        return self.centroid is not None and self.count >= config.SPEAKER_MIN_ENROLL

    def similarity(self, emb: np.ndarray) -> float:
        if self.centroid is None:
            return -1.0
        return float(np.dot(self.centroid, emb))


# One profile per server: this is a single-user device. Reading the (tiny)
# JSON file is done even when the model is disabled, so the dashboard can still
# report what is enrolled.
profile = Profile()


class Session:
    """The other voice in *this* conversation, learned online. One per connection.

    A running mean of the window embeddings from utterances labelled ``other``.
    Once it exists, windows are scored nearest-centroid (user vs partner)
    instead of against a bare threshold -- the ~7-point win from TODO.md. It is
    a property of the conversation, not of the user, so it is never persisted
    and resets with the connection.
    """

    def __init__(self) -> None:
        self._sum: np.ndarray | None = None
        self._n = 0
        self.counts: dict[str, int] = {k: 0 for k in LABELS}

    @property
    def partner(self) -> np.ndarray | None:
        return _unit(self._sum) if self._sum is not None and self._n else None

    def update_partner(self, windows: list[np.ndarray]) -> None:
        for w in windows:
            self._sum = w.copy() if self._sum is None else self._sum + w
            self._n += 1


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def classify_windows(
    windows: list[np.ndarray],
    user: np.ndarray,
    partner: np.ndarray | None,
    threshold: float | None = None,
) -> tuple[str, float, float]:
    """Pure decision rule over precomputed embeddings -> (label, user_frac, mean_cos).

    Separated from `identify()` so eval/spk_eval.py can score the rule on
    RAVDESS without going through the model twice.
    """
    thr = config.SPEAKER_MATCH_THRESHOLD if threshold is None else threshold
    if not windows:
        return "unknown", 0.0, 0.0
    hits = 0
    cos_sum = 0.0
    for w in windows:
        cu = float(np.dot(user, w))
        cos_sum += cu
        is_user = cu >= thr
        if partner is not None:
            # Nearest centroid once we know what the partner sounds like: a
            # window closer to them is theirs even if it clears the threshold.
            is_user = is_user and cu > float(np.dot(partner, w))
        hits += int(is_user)
    frac = hits / len(windows)
    if frac >= config.SPEAKER_USER_FRAC_HI:
        label = "user"
    elif frac <= config.SPEAKER_USER_FRAC_LO:
        label = "other"
    else:
        label = "mixed"
    return label, frac, cos_sum / len(windows)


def identify(audio: np.ndarray, session: Session | None = None) -> dict | None:
    """Label one utterance as user / other / mixed, or None when it cannot.

    Returns {"label": str, "score": user_fraction, "cos_user": mean cosine,
    "windows": n}. None means "no information" -- model missing, no profile
    enrolled yet, audio too short, or inference failed -- and the caller must
    treat that as today's behaviour (interpret the line). Never raises.
    """
    if _model is None or not profile.ready():
        return None
    if audio is None or audio.size < _MIN_SAMPLES:
        return None
    try:
        windows = embed_windows(audio)
        if not windows:
            return None
        partner = session.partner if session is not None else None
        label, frac, mean_cos = classify_windows(windows, profile.centroid, partner)
        if session is not None:
            session.counts[label] = session.counts.get(label, 0) + 1
            if label == "other":
                session.update_partner(windows)
        return {
            "label": label,
            "score": round(frac, 3),
            "cos_user": round(mean_cos, 3),
            "windows": len(windows),
        }
    except Exception as exc:  # noqa: BLE001 -- one bad utterance must not kill the task
        log.warning("speaker id failed (%s: %s)", type(exc).__name__, exc)
        return None


if __name__ == "__main__":
    # Smoke test:  python -m server.speaker a.wav [b.wav ...]
    # Prints each file's self-similarity across windows and the cosine between
    # files. Two clips of one person should sit above ~0.6; two people below
    # ~0.45. Anything else means the fbank backend or the model is off.
    import sys
    import wave

    logging.basicConfig(level=logging.INFO)

    def _read(path: str) -> np.ndarray:
        with wave.open(path, "rb") as w:
            rate, ch = w.getframerate(), w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        x = pcm.astype(np.float32) / 32768.0
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        if rate != config.SAMPLE_RATE:
            from math import gcd

            from scipy.signal import resample_poly
            g = gcd(rate, config.SAMPLE_RATE)
            x = resample_poly(x, config.SAMPLE_RATE // g, rate // g).astype(np.float32)
        return np.ascontiguousarray(x)

    print("available:", available(), "| profile:", profile.count, "enrolled at", profile.path)
    if len(sys.argv) < 2:
        print("usage: python -m server.speaker a.wav [b.wav ...]")
        sys.exit(0)

    embs = []
    for p in sys.argv[1:]:
        x = _read(p)
        t0 = time.perf_counter()
        e = embed(x)
        dt = time.perf_counter() - t0
        wins = embed_windows(x)
        self_sim = (np.mean([float(np.dot(e, w)) for w in wins]) if e is not None and wins
                    else float("nan"))
        print(f"{p}: {x.size / config.SAMPLE_RATE:.2f}s  embed {dt * 1000:.0f} ms  "
              f"windows {len(wins)}  window-vs-whole cos {self_sim:.3f}")
        embs.append(e)
    for i in range(len(embs)):
        for j in range(i + 1, len(embs)):
            if embs[i] is not None and embs[j] is not None:
                print(f"cos({sys.argv[1 + i]}, {sys.argv[1 + j]}) = "
                      f"{float(np.dot(embs[i], embs[j])):.3f}")
