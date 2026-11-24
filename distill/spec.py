"""The versioned rules of the distillation set: what to generate and what to expect.

Pure data and logic, zero I/O, self-test in `__main__` (`python -m distill.spec`).

Why a rule table at all, when a teacher labels every row
--------------------------------------------------------
The teacher (qwen3:14b) is the ceiling AND carries known failures: it over-calls
sarcasm on a confidently warm voice (mm-01a / mm-03a) and drops passive
aggression to neutral (pa-03 / pa-04). Distilling it blindly copies those. So
every row also carries the outcome the PRODUCTION PROMPT'S OWN RULES predict for
its kind of line and voice -- the same generous accepted-sets idea as
`eval/tone_cases.jsonl` -- and a row is kept only when the teacher lands inside
it. That is candidate-label distillation (CanDist, design doc §5.1d) with the
prompt as the second voter. A second LLM teacher is recorded per row too, but it
is not the gate by default: Stage 0 measured gemma3:12b at 0% on the
low-confidence guard and qwen3:8b at 0% on passive aggression, so requiring
agreement would gut exactly the categories the student most needs.

Every number below is a starting point, not a measurement. Change one and the
dataset version (`SPEC_VERSION`) must change with it, because the rows are no
longer comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# v2 (2026-10-25): category and per-category variant weights rebalanced toward
# the cells the v1 pilot kept least of (see OVERSAMPLING below). The rules
# (accepted sets) are unchanged from v1.
# v3 (2026-11-24): more no-voice rows in sarcasm, sincere_positive and passive
# aggression (see NO-VOICE below). Scenarios are v2's, adopted; rules unchanged.
SPEC_VERSION = "v3"

EMOTIONS = ("neutral", "happy", "sad", "angry", "fearful", "disgusted", "surprised")
TONES = ("positive", "negative", "neutral", "sarcastic", "mixed")

# Voice variants. Each row is one scenario line under one of these.
#   congruent -- the voice sounds the way the line is meant (confident label)
#   contrast  -- a confident voice that OPPOSES the surface words; the cases the
#                mismatch rules in SYSTEM_PROMPT exist for
#   low_conf  -- a random label under SER_MIN_CONFIDENCE: the guard rail, the
#                words must win
#   none      -- no voice line at all (SER off or failed)
VARIANTS = ("congruent", "contrast", "low_conf", "none")

# Share of rows per variant when a category allows it. low_conf and none are
# guard-rail shapes the design doc asks for "several hundred" of each (§5.3).
VARIANT_WEIGHTS = {"congruent": 0.40, "contrast": 0.25, "low_conf": 0.15, "none": 0.20}

# OVERSAMPLING (v2). The v1 pilot (2026-10-25, 440 rows) kept 57% overall but
# far less where the teacher has a known failure, because build() rejects a
# label outside the prompt's rules rather than teaching it:
#   teasing/congruent           4/24  17%  (both teachers call warm teasing negative)
#   sarcasm/contrast            3/13  23%  (a warm voice on sarcastic words)
#   sincere_positive           20/48  42%  (contrast 5/12, low_conf 3/8, none 4/10)
#   masking                    18/48  38%
# Equal generation would leave these cells a sliver of the kept set. So their
# categories get more calls and, where a category has a spare variant slot,
# the weak variant gets picked more often; the always-kept categories (literal
# 100%, refusal 89%, sincere_negative 82%) give up share. Pilot cells are 10-25
# rows each, so these are coarse corrections, not a fitted model: re-read the
# manifest's per_cell yield after the next run and adjust.
# What oversampling cannot fix: the kept rows in a weak cell are the instances
# the teacher happened to get right, so they lean towards the easy end of the
# cell. If the student is still weak there, the answer is a stronger teacher.

@dataclass(frozen=True)
class Expect:
    """Accepted tones and emotions for one (category, variant).

    `emotions` may contain the placeholders "VOICE" (the row's voice label) and
    "FEELING" (the generator's stated feeling for the line); `resolve()` fills
    them in per row. "VOICE" is how the 2026-09-22 rule "start from the voice
    label and keep it" becomes a training target: Stage 0 measured that only
    the 14B follows it, so the student has to be shown it.
    """
    tones: tuple[str, ...]
    emotions: tuple[str, ...]

    def resolve(self, voice_label: str | None, feeling: str) -> tuple[list[str], list[str]]:
        emos: list[str] = []
        for e in self.emotions:
            v = {"VOICE": voice_label, "FEELING": feeling}.get(e, e)
            if v and v in EMOTIONS and v not in emos:
                emos.append(v)
        return list(self.tones), emos


@dataclass(frozen=True)
class Category:
    """One kind of line the generator writes, and how each voice variant reads it."""
    key: str
    weight: float                         # share of scenarios
    brief: str                            # what the generator is asked to write
    feelings: tuple[str, ...]             # allowed generator feelings (balanced across)
    delivery: tuple[str, ...]             # voice labels for the congruent variant
    contrast: tuple[str, ...] = ()        # voice labels for the contrast variant
    expect: dict[str, Expect] = field(default_factory=dict)  # variant -> Expect
    # Overrides VARIANT_WEIGHTS for this category (see OVERSAMPLING). Only
    # matters when the category has more variants than a scenario gets rows.
    variant_weights: dict[str, float] = field(default_factory=dict)

    def variants(self) -> tuple[str, ...]:
        """The variants this category defines an expectation for."""
        return tuple(v for v in VARIANTS if v in self.expect)

    def variant_weight(self, variant: str) -> float:
        return self.variant_weights.get(variant, VARIANT_WEIGHTS[variant])


# Words-alone emotion: the prompt says "with no voice field, choose the emotion
# from the words alone and prefer neutral", and a low-confidence label is "close
# to a guess", so both variants accept the stated feeling or neutral.
_WORDS = ("FEELING", "neutral")
# Hostility carried by the words alone (sarcasm, passive aggression): which of
# the irritated feelings it is cannot be read off the text.
_IRRITATED = ("angry", "disgusted", "sad", "neutral")
# Variant mix for sincere_positive and sarcasm, whose contrast cell kept least.
# With two of four variants per scenario, contrast lands in 77% of scenarios
# (v1: 52%; simulated); low_conf and none still get several hundred rows at full size.
_CONTRAST_HEAVY = {"congruent": 0.20, "contrast": 0.40, "low_conf": 0.10, "none": 0.30}
# NO-VOICE (v3). The v2 student (docs/reports/small-interpreter-stage1.md) beat the
# 14B wherever a voice hint was present but missed text-only sarcasm and passive
# aggression, and read voiceless praise as neutral. The v2 labels there were right
# (sarcasm/none: 124 of 131 sarcastic); there were just few of them, ~16% of each
# cell's rows. Simulated share of scenarios per variant (two picks of four):
#   v2 _CONTRAST_HEAVY  congruent 63%  contrast 77%  low_conf 24%  none 36%
#   v3 _CONTRAST_HEAVY  congruent 44%  contrast 72%  low_conf 23%  none 61%
#   passive_aggression  v2 (defaults) none 64%  ->  v3 _NONE_HEAVY none 83%
# Contrast stays near 77% because it is what bought 5/6 mismatch pairs.
# `contrast` is unused for passive_aggression (no contrast voice); it is listed
# only because a variant_weights override must name every variant.
_NONE_HEAVY = {"congruent": 0.40, "contrast": 0.0, "low_conf": 0.15, "none": 0.45}

CATEGORIES: tuple[Category, ...] = (
    Category(
        "literal", 0.10,
        "plain information, logistics or a factual statement with no subtext at all "
        "(times, places, plans, status updates, ordinary requests).",
        feelings=("neutral",), delivery=("neutral",),
        expect={
            "congruent": Expect(("neutral",), ("neutral",)),
            # A strong label on a plain sentence is the false-positive trap the
            # low-confidence cases guard; at low confidence it MUST stay neutral.
            "low_conf": Expect(("neutral",), ("neutral",)),
            "none": Expect(("neutral",), ("neutral",)),
        },
    ),
    Category(
        "sincere_positive", 0.12,
        "genuine praise, gratitude, relief, excitement or good news, meant exactly as said.",
        feelings=("happy", "surprised"), delivery=("FEELING",), contrast=("sad",),
        variant_weights=_CONTRAST_HEAVY,
        expect={
            "congruent": Expect(("positive",), ("VOICE", "FEELING")),
            # Warm words, flat sad voice: masking (mm-02b's shape). Should NOT
            # read as plain positive. `neutral` tone is accepted because the
            # teacher reports masking through emotion (the voice's sad) and the
            # read ("masking hurt"), leaving tone neutral -- consistent with tone
            # now being mostly a sarcasm flag (TODO, "The headline is the SER's
            # emotion"). Measured in the 2026-10-25 pilot; positive stays out.
            "contrast": Expect(("mixed", "negative", "sarcastic", "neutral"), ("VOICE",)),
            "low_conf": Expect(("positive",), _WORDS),
            "none": Expect(("positive",), _WORDS),
        },
    ),
    Category(
        "sincere_negative", 0.09,
        "a genuine complaint, frustration, sadness, worry, fear, disgust or bad news, "
        "meant exactly as said.",
        feelings=("sad", "angry", "fearful", "disgusted"), delivery=("FEELING",),
        expect={
            "congruent": Expect(("negative",), ("VOICE", "FEELING")),
            "low_conf": Expect(("negative", "neutral"), _WORDS),
            "none": Expect(("negative",), _WORDS),
        },
    ),
    Category(
        "sarcasm", 0.14,
        "sarcasm: words that say something positive or agreeable while meaning the "
        "opposite, where the context makes the sarcasm clear. Avoid stock phrases.",
        feelings=("angry", "disgusted"), delivery=("angry", "disgusted", "neutral"),
        contrast=("happy",),
        variant_weights=_CONTRAST_HEAVY,
        expect={
            "congruent": Expect(("sarcastic", "negative"), ("VOICE", "FEELING")),
            # SYSTEM_PROMPT: a genuinely warm, confident voice is evidence AGAINST
            # sarcasm -- most likely affectionate teasing. The 14B fails this
            # (mm-01a / mm-03a); rows it gets wrong are rejected, not taught.
            "contrast": Expect(("positive", "mixed"), ("VOICE",)),
            # Words-alone sarcasm says little about WHICH feeling is under it: the
            # pilot's teacher got the tone right and answered sad or surprised.
            # Same set as passive aggression.
            "low_conf": Expect(("sarcastic", "negative"), _IRRITATED),
            "none": Expect(("sarcastic", "negative"), _IRRITATED),
        },
    ),
    Category(
        "lookalike", 0.08,
        "a SINCERE line that uses wording people often say sarcastically (exaggerated "
        "praise, over-the-top thanks, mock disbelief), in a context where it is genuinely "
        "meant: real delight, relief or affection.",
        feelings=("happy", "surprised"), delivery=("happy", "surprised"),
        contrast=("angry", "disgusted"),
        expect={
            # The hard negative the design doc asks a deliberate share for
            # (§5.2): sarcastic-looking, sincere, warm voice.
            "congruent": Expect(("positive", "mixed"), ("VOICE", "FEELING")),
            "contrast": Expect(("sarcastic", "negative"), ("VOICE",)),
            # No none / low_conf: without a voice the line is honestly
            # ambiguous, so there is no expectation to teach.
        },
    ),
    Category(
        "masking", 0.11,
        "masking: the speaker says they are fine, okay or happy for someone, while the "
        "context shows they are actually hurt, disappointed or upset.",
        feelings=("sad",), delivery=("sad",), contrast=("happy",),
        expect={
            # Agreeable words + low arousal + low dominance = masking (prompt rule).
            # `neutral` tone accepted for the reason given at sincere_positive's
            # contrast; the emotion must still be the voice's sad.
            "congruent": Expect(("mixed", "negative", "sarcastic", "neutral"), ("VOICE",)),
            # Same words, bright assertive voice: genuinely fine (mm-05a).
            "contrast": Expect(("positive", "neutral"), ("VOICE", "neutral")),
            "low_conf": Expect(("neutral", "positive", "mixed", "negative"), ("sad", "neutral")),
            "none": Expect(("neutral", "positive", "mixed", "negative"), ("sad", "neutral")),
        },
    ),
    Category(
        "passive_aggression", 0.10,
        "passive aggression: hostility or resentment wrapped in polite or agreeable "
        "words. Vary the form: a backhanded compliment, a pointed remark about someone "
        "else's luck, an over-polite reminder, a martyred offer, a cool 'noted', a "
        "loaded question. Do not start every line with thanks.",
        feelings=("angry", "disgusted"), delivery=("neutral", "angry", "disgusted"),
        variant_weights=_NONE_HEAVY,
        expect={
            # Stage 0: every small model and qwen3:8b scored 0% here. The
            # category the student most needs examples of.
            "congruent": Expect(("negative", "sarcastic", "mixed"), ("VOICE", "FEELING")),
            "low_conf": Expect(("negative", "sarcastic", "mixed"), _IRRITATED),
            "none": Expect(("negative", "sarcastic", "mixed"), _IRRITATED),
        },
    ),
    Category(
        "mixed", 0.07,
        "genuinely mixed or ambivalent feelings in one line (good news with a cost, "
        "bittersweet, happy for someone but sad for themselves).",
        feelings=("happy", "sad", "fearful", "surprised"), delivery=("FEELING", "neutral"),
        expect={
            "congruent": Expect(("mixed", "positive", "negative"), ("VOICE", "FEELING")),
            "low_conf": Expect(("mixed", "positive", "negative"), _WORDS),
            "none": Expect(("mixed", "positive", "negative"), _WORDS),
        },
    ),
    Category(
        "refusal", 0.05,
        "a polite refusal, soft no or non-commitment: the speaker declines or stalls "
        "without saying no outright.",
        feelings=("neutral", "sad", "fearful"), delivery=("neutral", "FEELING"),
        expect={
            "congruent": Expect(("negative", "neutral", "mixed"), ("VOICE", "FEELING", "neutral")),
            "low_conf": Expect(("negative", "neutral", "mixed"), _WORDS),
            "none": Expect(("negative", "neutral", "mixed"), _WORDS),
        },
    ),
    Category(
        "teasing", 0.14,
        "affectionate teasing or banter: words that look harsh or insulting, said "
        "between people who clearly like each other.",
        feelings=("happy",), delivery=("happy", "surprised"), contrast=("angry",),
        expect={
            # Negative words + high-valence / warm voice = teasing, not a complaint.
            "congruent": Expect(("positive", "mixed"), ("VOICE", "FEELING")),
            # Same words, angry high-dominance voice = a real complaint (mm-03b).
            "contrast": Expect(("negative",), ("VOICE",)),
        },
    ),
)

BY_KEY = {c.key: c for c in CATEGORIES}


def category_for_call(call: int) -> str:
    """The category of generator call `call`, by smooth weighted round-robin.

    Drawing a category at random per call was measured too noisy: a 40-call pilot
    (2026-10-25) got 0 teasing and 6 sincere-positive scenarios against 48
    refusals. Round-robin tracks the weights at any size, and a call keeps its
    category when `--calls` grows, so resuming never relabels finished work.
    """
    current = {c.key: 0.0 for c in CATEGORIES}
    pick = CATEGORIES[0].key
    for _ in range(call + 1):
        for c in CATEGORIES:
            current[c.key] += c.weight
        pick = max(current, key=current.get)
        current[pick] -= 1.0
    return pick

# What the generator is told to vary, so scenarios do not converge on the
# generator's favourite office small talk. Sampled per call.
SETTINGS = (
    "an open-plan office", "a video call with remote colleagues", "a family dinner",
    "a group chat read aloud", "a school classroom", "a university seminar",
    "a doctor's waiting room", "a supermarket checkout", "a shared flat kitchen",
    "a car on a long drive", "a football match in the stands", "a birthday party",
    "a phone call with a parent", "a retail shift", "a hospital ward",
    "a band rehearsal", "a cafe queue", "a gym", "an online multiplayer game lobby",
    "a wedding planning session", "a landlord visit", "a job interview",
    "a train delay on the platform", "a parent-teacher meeting", "a restaurant kitchen",
    "a moving day", "a camping trip", "a support group", "a dentist appointment",
    "a neighbour's doorstep", "a holiday airport queue", "a construction site",
)
RELATIONSHIPS = (
    "close friends", "a couple", "siblings", "a parent and adult child",
    "a manager and an employee", "two coworkers", "flatmates", "strangers",
    "a customer and a shop worker", "a teacher and a student", "teammates",
    "neighbours", "old school friends", "in-laws", "a doctor and a patient",
    "a coach and a player", "new acquaintances", "an ex-couple",
)

# Phrases the eval suites already use. The generator is told to avoid them, and
# pipeline.build() rejects any line too close to an eval transcript anyway
# (design doc §5.5: "generated scenarios will reinvent 'oh wonderful, just
# perfect' on their own").
# Found 2026-10-25: the first pilot's briefs QUOTED some of these as examples
# and the generator copied them ("Thanks for finally noticing the fridge"), so
# the briefs above now describe each form without eval wording, and build()
# rejects any line containing one of these.
AVOID_PHRASES = (
    "another meeting", "could have been an email", "it's fine", "take your time",
    "anywhere to be", "whatever you think is best", "must be nice", "thanks for finally",
    "getting back to me", "oh wonderful", "just perfect", "unbelievable",
    "so happy for you", "really don't mind", "showed up on time", "worked out so well",
    "do it myself", "by the door",
)

# Rows whose newest line carries a speaker tag: rendered by _build_prompt only
# when some line has one, so most rows stay byte-identical to the unlabelled
# prompt that ships before a voice is enrolled.
LABELLED_SHARE = 0.25
# Of labelled rows, the newest line marked "mixed" (may contain both voices).
MIXED_WHO_SHARE = 0.05

# Read quality, checked in build(): SYSTEM_PROMPT's own limit.
READ_MAX_WORDS = 5
# Any single read string may cover at most this share of kept rows (and at least
# READ_CAP_MIN occurrences are always allowed), so the student cannot collapse
# onto one stock answer the way RAVDESS reads collapsed onto "noticing kids".
READ_CAP_SHARE = 0.003
READ_CAP_MIN = 3


def expectation(category: str, variant: str, voice_label: str | None,
                feeling: str) -> tuple[list[str], list[str]] | None:
    """Accepted (tones, emotions) for one row, or None if the variant is undefined."""
    cat = BY_KEY.get(category)
    if not cat or variant not in cat.expect:
        return None
    return cat.expect[variant].resolve(voice_label, feeling)


def _self_test() -> None:
    total = sum(c.weight for c in CATEGORIES)
    assert abs(total - 1.0) < 1e-6, f"category weights sum to {total}"
    assert abs(sum(VARIANT_WEIGHTS.values()) - 1.0) < 1e-6
    for c in CATEGORIES:
        assert c.feelings and all(f in EMOTIONS for f in c.feelings), c.key
        if c.variant_weights:
            assert set(c.variant_weights) == set(VARIANTS), c.key
            assert abs(sum(c.variant_weights.values()) - 1.0) < 1e-6, c.key
        for d in c.delivery + c.contrast:
            assert d == "FEELING" or d in EMOTIONS, (c.key, d)
        assert ("contrast" in c.expect) == bool(c.contrast), f"{c.key}: contrast voice vs expect"
        for v, e in c.expect.items():
            assert v in VARIANTS, (c.key, v)
            assert e.tones and all(t in TONES for t in e.tones), (c.key, v)
            for f in c.feelings:
                for label in (c.delivery + c.contrast) or ("neutral",):
                    tones, emos = e.resolve(f if label == "FEELING" else label, f)
                    assert tones and emos, (c.key, v, f, label)
    # The guard rails, spelled out: plain lines stay neutral under a weak voice,
    # and "keep the voice label" is what a confident congruent voice expects.
    assert expectation("literal", "low_conf", "angry", "neutral") == (["neutral"], ["neutral"])
    assert expectation("sarcasm", "congruent", "angry", "disgusted")[1] == ["angry", "disgusted"]
    assert expectation("lookalike", "none", None, "happy") is None
    # Round-robin tracks the weights: over 1000 calls every category lands
    # within one call of its share.
    from collections import Counter
    got = Counter(category_for_call(i) for i in range(1000))
    for c in CATEGORIES:
        assert abs(got[c.key] - 1000 * c.weight) <= 1, (c.key, got[c.key])
    print(f"distill.spec {SPEC_VERSION}: {len(CATEGORIES)} categories, self-test passed")


if __name__ == "__main__":
    _self_test()
