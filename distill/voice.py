"""Sample SER voice hints the way production's default backend emits them.

Pure logic (seeded `random.Random`, no I/O), self-test in `__main__`
(`python -m distill.voice`).

Why not hand-type voice dicts like `eval/tone_cases.jsonl` does
--------------------------------------------------------------
The mismatch pairs there carry valence gaps of 0.47-0.73, while real SER
separates pleasant from unpleasant by ~0.14, and the shipped emotion2vec backend
emits NO valence at all (TODO, "The mismatch pairs test an input production
never emits"). A student trained on idealised hints learns a cue that never
occurs. So a hint here is built the way `ser._analyze_emotion2vec` builds one:
a 7-class probability distribution with the chosen label on top, arousal and
dominance as `ser._expected()` over `ser._AROUSAL_ANCHOR` / `_DOMINANCE_ANCHOR`
(imported, not copied, so an anchor change reaches the data), valence None,
`dims_source: "derived"`. Every hint is therefore one the real backend could
have produced.

What is still assumed: the CONFIDENCE distribution. emotion2vec's confidence on
this mic in a real room has never been profiled (RAVDESS is studio and acted),
so the ranges below are a design choice, recorded in the manifest.
"""

from __future__ import annotations

import os
import random
import sys

# server.ser loads a ~1 GB model at import unless SER is disabled. This package
# only needs its anchors and _expected(), so switch SER off -- but only if
# server.config has not been imported yet, or the flag would arrive too late.
if "server.config" not in sys.modules:
    os.environ["SER_ENABLED"] = "0"

from server import ser  # noqa: E402

EMOTIONS = ("neutral", "happy", "sad", "angry", "fearful", "disgusted", "surprised")

# Confident voice (congruent / contrast). Above SER_MIN_CONFIDENCE (0.4) by a
# margin, so the prompt treats it as evidence rather than a guess.
CONFIDENT = (0.50, 0.95)
# Low-confidence voice: under SER_MIN_CONFIDENCE, where _describe_voice adds
# "-- low confidence, weak signal" and the prompt says to lean on the words.
LOW_CONF = (0.15, 0.38)
# Dirichlet concentration for how the remaining mass spreads over the other six
# labels. Below 1 = lumpy (one runner-up), which is how a torn classifier looks.
SPREAD_ALPHA = 0.6


def _distribution(rng: random.Random, label: str, confidence: float) -> dict[str, float]:
    """A 7-class distribution whose argmax is `label` at exactly `confidence`."""
    others = [e for e in EMOTIONS if e != label]
    rest = 1.0 - confidence
    for _ in range(50):
        g = [rng.gammavariate(SPREAD_ALPHA, 1.0) for _ in others]
        s = sum(g)
        share = [rest * x / s for x in g]
        if max(share) < confidence:
            break
    else:
        # Uniform spread always keeps `label` on top for confidence >= 1/7.
        share = [rest / len(others)] * len(others)
    dist = dict(zip(others, share))
    dist[label] = confidence
    return dist


def sample(rng: random.Random, label: str, low_confidence: bool = False) -> dict:
    """One voice dict shaped exactly like ser.analyze() on the emotion2vec backend."""
    if label not in EMOTIONS:
        raise ValueError(f"unknown emotion {label!r}")
    lo, hi = LOW_CONF if low_confidence else CONFIDENT
    confidence = round(rng.uniform(lo, hi), 3)
    dist = _distribution(rng, label, confidence)
    return {
        "emotion": label,
        "confidence": confidence,
        "valence": None,
        "arousal": ser._expected(dist, ser._AROUSAL_ANCHOR),
        "dominance": ser._expected(dist, ser._DOMINANCE_ANCHOR),
        "dims_source": "derived",
    }


def _self_test() -> None:
    from server.interpreter import _describe_voice

    rng = random.Random(7)
    for label in EMOTIONS:
        for low in (False, True):
            for _ in range(200):
                v = sample(rng, label, low)
                assert v["emotion"] == label and v["valence"] is None
                assert 0.0 <= v["arousal"] <= 1.0 and 0.0 <= v["dominance"] <= 1.0
                lo, hi = LOW_CONF if low else CONFIDENT
                assert lo <= v["confidence"] <= hi
    # What the LLM actually reads: a weak hint is flagged in words, a confident
    # angry one lands on the high side of both scales.
    weak = _describe_voice(sample(random.Random(1), "angry", low_confidence=True))
    assert "low confidence, weak signal" in weak, weak
    strong = [sample(random.Random(i), "angry") for i in range(300)]
    assert sum(v["arousal"] for v in strong) / 300 > 0.6
    sad = [sample(random.Random(i), "sad") for i in range(300)]
    assert sum(v["dominance"] for v in sad) / 300 < 0.45
    print("example:", _describe_voice(sample(random.Random(3), "sad")))
    print("distill.voice self-test passed")


if __name__ == "__main__":
    _self_test()
