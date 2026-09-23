"""How sure is this read? One number, one level, and the reasons, per utterance.

Why this exists
---------------
Every read used to arrive with the same weight, whether the pipeline was sure
or guessing. The listener has to decide how much to act on "masking annoyance",
and nothing told them when the evidence was thin. docs/design/RESEARCH.md asks
for this twice (§4.8: calibrate, then abstain; §4.10: the voice model is in its
worst regime under ~1.5 s) and the pendant requirements demand it ("low-
confidence interpretations shall be rendered with visibly reduced emphasis").

What goes in
------------
Signals the pipeline already had and was throwing away, plus one it now asks for:

  tone_prob     the interpreter's probability for the tone label it chose, i.e.
                the first-token probability from Ollama's logprobs, Platt-scaled
                with parameters fitted by `eval.confidence_eval`. This is the
                BASE score. Verbalised confidence ("I am 80% sure") was rejected
                on the literature: 7-14B models cluster at 80-100% regardless of
                accuracy (ECE 0.25-0.30 for Qwen3 8B/14B). Token probability is
                the cheapest signal that discriminates at all, and scaling it on
                a couple of hundred labelled cases is what makes the number mean
                what it says.
  asr_conf      how sure the transcriber was (exp of its log-probs, backend-
                specific; None when the backend has none).
  ser_conf      the SER softmax mass on its label (uncalibrated; see §4.8).
  voiced_s      seconds of voiced audio in the utterance.
  speaker_label the speaker-id label; "mixed" means two voices in one line.
  tone          the chosen tone; "mixed" is the interpreter saying its inputs
                disagree.
  offline       the interpreter fell back to "(interpreter offline)".

How they combine
----------------
Multiplicative penalties on the base score, each carrying a plain-language
reason written for the listener rather than the developer. Multiplicative
rather than additive so two weak signals compound and one strong signal cannot
mask them, and so the score can never leave [0, 1]. The thresholds live in
config.py and are meant to be SET BY `python -m eval.confidence_eval`, which
measures how each signal actually tracks correctness on this pipeline; hand-
tuning them is how a confidence number stops meaning anything.

What the level means downstream (RESEARCH.md §6.5, "suppress rather than
hedge"): `high` renders as before; `medium` renders with reduced emphasis; `low`
is suppressed by the client behind a "not sure" marker, and shown faint on the
pendant. The server always sends the read -- suppression is the client's call,
so logs and evals keep the full record.

This module is pure logic with zero I/O so it can be unit-tested with the
`__main__` block below and imported by the evals without dragging in a model.
"""

from __future__ import annotations

import math

from . import config

LEVELS = ("high", "medium", "low")

# Reasons are user-facing: the browser shows them as the "not sure" chip's
# tooltip and the transcript log keeps them. Keep them short and concrete.
R_OFFLINE = "interpreter offline"
R_NO_PROB = "interpreter gave no probability"
R_SHORT = "too short to read the voice"
R_VOICE = "voice unclear"
R_WORDS = "words may be misheard"
R_TWO_VOICES = "two voices in one line"
R_DISAGREE = "signals disagree"


def platt(p: float, a: float | None = None, b: float | None = None) -> float:
    """Platt scaling: sigmoid(a * logit(p) + b). (a, b) = (1, 0) is the identity.

    Fitted offline by eval.confidence_eval on the interpreter's first-token
    probabilities against tone correctness; the identity is the safe default
    until that has been run.
    """
    a = config.CONFIDENCE_PLATT_A if a is None else a
    b = config.CONFIDENCE_PLATT_B if b is None else b
    p = min(max(p, 1e-6), 1 - 1e-6)
    z = a * math.log(p / (1 - p)) + b
    return 1.0 / (1.0 + math.exp(-z))


def level(score: float) -> str:
    if score >= config.CONFIDENCE_HIGH:
        return "high"
    if score < config.CONFIDENCE_LOW:
        return "low"
    return "medium"


def score(
    *,
    tone: str | None,
    tone_prob: float | None,
    offline: bool = False,
    asr_conf: float | None = None,
    ser_conf: float | None = None,
    voiced_s: float | None = None,
    speaker_label: str | None = None,
) -> dict:
    """Combine the signals into {"score", "level", "reasons"}. Never raises.

    Every input is optional: a missing signal neither helps nor hurts, except
    a missing `tone_prob`, which drops the base to 0.5 and says so -- a read
    whose own model would not say how sure it was is not "high" by default.
    """
    if offline:
        return {"score": 0.0, "level": "low", "reasons": [R_OFFLINE]}

    reasons: list[str] = []
    if isinstance(tone_prob, (int, float)) and 0.0 <= tone_prob <= 1.0:
        s = platt(float(tone_prob))
    else:
        s = 0.5
        reasons.append(R_NO_PROB)

    pen = config.CONFIDENCE_PENALTY

    def penalise(reason: str) -> None:
        nonlocal s
        s *= pen
        reasons.append(reason)

    # Order matters only for the reasons list: most actionable first.
    if isinstance(voiced_s, (int, float)) and voiced_s < config.CONFIDENCE_MIN_VOICE_S:
        penalise(R_SHORT)
    if isinstance(ser_conf, (int, float)) and ser_conf < config.SER_MIN_CONFIDENCE:
        penalise(R_VOICE)
    if isinstance(asr_conf, (int, float)) and asr_conf < config.CONFIDENCE_MIN_ASR:
        penalise(R_WORDS)
    if speaker_label == "mixed":
        penalise(R_TWO_VOICES)
    if tone == "mixed":
        penalise(R_DISAGREE)

    s = min(1.0, max(0.0, s))
    return {"score": round(s, 3), "level": level(s), "reasons": reasons}


if __name__ == "__main__":
    # Self-test: run `python -m server.confidence`. Pure logic, no models.
    # Uses the config defaults, so it also documents what they mean.
    hi = score(tone="negative", tone_prob=0.95, asr_conf=0.9, ser_conf=0.8, voiced_s=3.0)
    assert hi["level"] == "high" and not hi["reasons"], hi
    print("case 1 OK: clean, sure read ->", hi)

    off = score(tone="neutral", tone_prob=None, offline=True)
    assert off == {"score": 0.0, "level": "low", "reasons": [R_OFFLINE]}, off
    print("case 2 OK: interpreter offline ->", off)

    # Two penalties on a 0.8 base: 0.8 * 0.7 * 0.7 = 0.39, just under CONFIDENCE_LOW.
    weak = score(tone="sarcastic", tone_prob=0.8, ser_conf=0.2, voiced_s=0.8)
    assert weak["level"] == "low" and weak["reasons"] == [R_SHORT, R_VOICE], weak
    print("case 3 OK: short clip, weak voice ->", weak)

    noprob = score(tone="neutral", tone_prob=None)
    assert noprob["score"] == 0.5 and noprob["reasons"] == [R_NO_PROB], noprob
    print("case 4 OK: no probability -> base 0.5 ->", noprob)

    pile = score(tone="mixed", tone_prob=1.0, asr_conf=0.1, ser_conf=0.1, voiced_s=0.1,
                 speaker_label="mixed")
    assert 0.0 <= pile["score"] <= 1.0 and len(pile["reasons"]) == 5, pile
    print("case 5 OK: every penalty stacks, still in [0,1] ->", pile)

    assert abs(platt(0.7, 1.0, 0.0) - 0.7) < 1e-9
    print("case 6 OK: identity Platt")
    print("confidence self-test passed")
