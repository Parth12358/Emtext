"""Mix the voice model's emotion with the interpreter's. Pure logic, zero I/O.

The interpreter is told to START from the voice label and move it only when the
words make it untenable (SYSTEM_PROMPT), so today the headline emotion is simply
the interpreter's answer. `EMOTION_BLEND` makes that a dial, set live from the
dashboard:

    mixed = w * P_interpreter + (1 - w) * P_voice,   headline = argmax(mixed)

  w = 1.0  the interpreter's label, exactly as before (the default; no maths runs)
  w = 0.0  the voice model's label, the interpreter only writes the read
  between  the two distributions are mixed

What to expect from the dial, measured 2026-11-26 on emtext-qwen3-0.6b:v3: the
interpreter's distribution over emotions is near one-hot (0.97 and 0.997 on its
pick in two probes; its probability scores AUROC ~0.51 against correctness on
RAVDESS). So when the two AGREE the dial changes nothing, and when they disagree
it is close to a threshold: the voice keeps its label when
(1 - w) * voice_conf > w * interp_prob, i.e. when
voice_conf > interp_prob * w / (1 - w).
At w = 0.5 a voice label needs more confidence than the interpreter's ~0.97; at
w = 0.3, only ~0.42. The read text is the interpreter's and is not rewritten, so
at low w a read can describe an emotion the headline no longer shows.
"""

from __future__ import annotations


def _onehot(label: str | None) -> dict[str, float]:
    return {label: 1.0} if label else {}


def mix(
    interp_label: str | None,
    interp_dist: dict[str, float] | None,
    voice_label: str | None,
    voice_dist: dict[str, float] | None,
    w: float,
) -> tuple[str | None, float | None]:
    """(headline label, its mixed probability). (interp_label, None) = unchanged.

    Missing inputs degrade to today's behaviour: no voice -> the interpreter's
    label; no interpreter distribution -> its label as a one-hot.
    """
    if w >= 1.0 or not voice_label:
        return interp_label, None
    w = max(0.0, float(w))
    pi = interp_dist or _onehot(interp_label)
    pv = voice_dist or _onehot(voice_label)
    if not pi:
        return voice_label, pv.get(voice_label)
    labels = set(pi) | set(pv)
    mixed = {k: w * pi.get(k, 0.0) + (1.0 - w) * pv.get(k, 0.0) for k in labels}
    # Ties go to the voice label (the stated source), then to the interpreter's.
    best = max(sorted(labels), key=lambda k: (mixed[k], k == voice_label, k == interp_label))
    return best, round(mixed[best], 4)


if __name__ == "__main__":
    V = {"sad": 0.6, "neutral": 0.3, "happy": 0.1}
    I = {"angry": 0.97, "sad": 0.03}

    # 1. w = 1: unchanged, no probability returned
    assert mix("angry", I, "sad", V, 1.0) == ("angry", None)
    # 2. w = 0: the voice label
    assert mix("angry", I, "sad", V, 0.0)[0] == "sad"
    # 3. w = 0.5: 0.5*0.97 = 0.485 for angry vs 0.5*(0.6+0.03) = 0.315 for sad -> angry
    assert mix("angry", I, "sad", V, 0.5)[0] == "angry"
    # 4. w = 0.3: 0.291 angry vs 0.3*0.03 + 0.7*0.6 = 0.429 sad -> sad
    assert mix("angry", I, "sad", V, 0.3)[0] == "sad"
    # 5. agreement: any w gives the shared label
    assert all(mix("sad", {"sad": 0.99}, "sad", V, w)[0] == "sad" for w in (0, .3, .7, 1))
    # 6. no voice -> interpreter, whatever w
    assert mix("happy", None, None, None, 0.0) == ("happy", None)
    # 7. no interpreter distribution -> its label as one-hot
    assert mix("angry", None, "sad", {"sad": 0.9}, 0.4)[0] == "sad"      # 0.54 vs 0.4
    assert mix("angry", None, "sad", {"sad": 0.9}, 0.6)[0] == "angry"    # 0.6 vs 0.36
    print("blend self-test: 7/7 passed")
