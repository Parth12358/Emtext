"""Thin wrapper around Ollama that reads the emotional subtext of a line.

Given a transcript, it asks a local LLM for a compact JSON judgement:

    {"tone": "positive|negative|neutral|sarcastic|mixed", "read": "one line"}

It keeps a short rolling window of recent transcripts so the model can use
conversational context (sarcasm and passive aggression rarely make sense in
isolation) while only ever interpreting the *newest* line.

Failure is expected and handled: if Ollama is down or replies with junk, we
degrade gracefully -- the transcript still reaches the user, just with a
"(interpreter offline)" read -- so a flaky LLM never blocks the conversation.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import deque

import httpx

from . import config

log = logging.getLogger(__name__)

# The system prompt is the whole product, really. Notes on the choices:
#   - Audience framing ("neurodivergent listener") steers toward concrete,
#     practical reads instead of clinical or hedgy ones.
#   - "prefer neutral when unsure" is a guardrail against the worst failure mode
#     here, which is over-reading tone into plain sentences. A listener who is
#     told ordinary speech is loaded learns to distrust the tool.
#   - The "voice sounded like: X" slot is filled from ser.py when speech emotion
#     recognition is available, and omitted entirely when it is not -- so the
#     prompt has to work both ways, and says so explicitly in its last paragraph.
#   - The mismatch rules are the point of the whole SER stage: words and voice
#     disagreeing is what exposes sarcasm and masking.
#   - The 5-word cap is a *listening* constraint, not a style preference. The
#     read lands mid-conversation while the user is still tracking the speaker,
#     so anything long enough to need parsing arrives too late to be useful --
#     and the device glance now caps the read at 5 words too, so a longer read
#     is just truncated on screen. The "caveman" phrasing (content words only,
#     no filler or grammar words) is what keeps 5 words actually meaningful.
#
#     STALE -- the numbers below are for the OLD 10-word wording and no longer
#     describe this prompt (now a 5-word caveman cap); re-run
#     `python -m eval.model_eval` to re-measure.
#     Measured, qwen3:14b, 3 runs of eval/tone_cases.jsonl (90 reads), against
#     the previous "under about 14 words" wording:
#
#       length   mean 9.3 -> 7.3 words, median 9 -> 7, p90 12 -> 9, max 16 -> 11
#       over 10  25.6% -> 3.3% of reads (the three misses overshoot by one word)
#       accuracy 93% ALL, mismatch 2/4, every guard-rail category 100% -- all
#                UNCHANGED, so the shorter reads cost no tone accuracy
#       speed    0.86s -> 0.76s per interpretation (fewer tokens to decode)
#
#     Note it is not enforced in code on purpose: truncating to a word count
#     would cut mid-clause and ship a mangled read, which is worse than an
#     11-word one.
#
# Changing any of this invalidates the numbers in eval/; re-run
# `python -m eval.model_eval` after editing.
SYSTEM_PROMPT = """You help a neurodivergent listener understand the emotional \
subtext of a conversation they are hearing. For the newest line only, reply \
with a single JSON object: {"emotion": one of \
["neutral","happy","sad","angry","fearful","disgusted","surprised"], "tone": one of \
["positive","negative","neutral","sarcastic","mixed"], "read": a short plain \
sentence}. The "emotion" is what the speaker is feeling. The voice model's \
label (in the "voice:" field) is the primary source for it: START from that \
label and keep it unless the words make it untenable or its confidence is \
weak; if you move it, move to the nearest emotion the words support, and say \
why in the read. With no voice field, choose the emotion from the words alone \
and prefer "neutral". \
The "read" must be AT MOST 5 WORDS, practical rather than clinical -- tell \
them what the speaker likely means or wants, not a diagnosis. Use terse, \
telegraphic "caveman" phrasing: keep only content words, drop every filler and \
grammar word -- no articles ("a"/"the"), no "is"/"are"/"probably", no "The \
speaker is" or "They are", no restating the line back. Bare keywords only, e.g. \
"masking hurt", "friendly teasing", "wants reassurance", "genuinely pleased". \
Start with the point.

Some lines include a "voice:" field describing how the line actually SOUNDED, \
measured from the audio by a speech emotion model. It may report an emotion \
label with a confidence, plus these scales:
- valence: 0 = the voice sounds negative/unpleasant, 1 = positive/pleasant
- arousal: 0 = calm and subdued, 1 = intense and energised (energy, not mood: \
both rage and delight are high)
- dominance: 0 = yielding, submissive, deflated, 1 = assertive, in control
Valence is often absent; arousal and dominance are the axes the voice carries \
that the words cannot, so use them even when valence is missing.

Compare the voice against the words -- the MISMATCH between them is the most \
useful signal you have:
- Positive or agreeable words with LOW arousal and LOW dominance usually mean \
masking, flat compliance, or a "fine" that is not fine.
- Positive words with HIGH arousal and HIGH dominance are usually genuine \
enthusiasm.
- Negative or harsh words with HIGH arousal and HIGH dominance are usually a \
real complaint or anger.
- Negative words with LOW arousal and LOW dominance usually mean sadness or \
resignation rather than hostility.
- These arousal/dominance rules need BOTH axes to agree. Calm on its own is \
how ordinary statements sound: never read masking or sadness from low arousal \
alone, and when valence is present it decides the direction.
- Positive or complimentary words with a low-valence voice usually mean sarcasm, \
or someone masking that they are upset.
- Negative or harsh words with a high-valence voice usually mean teasing, \
joking, or friendly banter rather than a real complaint.
- This works in BOTH directions, and the second one is easy to get wrong. A line \
whose wording looks like a stock sarcastic phrase ("oh wonderful", "just \
perfect", "you're unbelievable") but which is spoken with a genuinely warm, \
high-valence voice is most likely SINCERE -- real delight or affectionate \
teasing. A confident high-valence voice is evidence AGAINST sarcasm. Do not \
call something sarcastic just because the phrasing pattern matches, when the \
voice clearly disagrees.
- When the words and the voice agree, take the line literally and say so plainly.
- High arousal on its own tells you how strongly the speaker feels, not whether \
it is good or bad; read the direction from valence when present, otherwise \
from dominance together with the words.

Weigh the voice by its confidence. Below about 0.4 the label is close to a guess, \
so lean on the words and treat the voice as weak supporting evidence at most. \
Never let a low-confidence voice reading alone turn a plain sentence into \
sarcasm. The voice field is a hint from an imperfect model, not ground truth.

When there is no "voice:" field, or it carries only an emotion label with no \
scales, you are reading mostly text with little or no vocal tone, so be \
especially careful not to over-read sarcasm into plain sentences. \
Whenever you are unsure, prefer "neutral" rather than guessing. Use the earlier \
lines only as context; interpret just the newest line."""


def _describe_voice(voice: dict | None) -> str | None:
    """Render a SER result as the one-line 'voice:' hint for the prompt.

    Turns raw numbers into number + plain-word gloss ("valence 0.21 (negative)")
    because the LLM reasons far more reliably about the words than about bare
    floats, while the number keeps the precision for borderline cases.

    Returns None when there is nothing worth saying, so the caller simply omits
    the field and the prompt degrades to its text-only form.
    """
    if not voice:
        return None

    parts: list[str] = []

    emotion = voice.get("emotion")
    confidence = voice.get("confidence")
    if emotion:
        if isinstance(confidence, (int, float)):
            parts.append(f"{emotion} (conf {confidence:.2f})")
            if confidence < config.SER_MIN_CONFIDENCE:
                # Say it in words too: the prompt explains the threshold, but
                # stating it inline is far harder for the model to overlook.
                parts[-1] += " -- low confidence, weak signal"
        else:
            parts.append(str(emotion))

    # Gloss thresholds come from config, NOT hardcoded, because they are a
    # property of the BACKEND rather than of the concept.
    #
    # This was a real bug, not a hypothetical. MERaLiON's valence spans 0.12-0.41
    # across all 1440 RAVDESS clips (mean 0.254, pleasant-vs-unpleasant
    # separation +0.085). Against the old fixed 0.4/0.6 thresholds, not one clip
    # in 1440 ever reached "positive" -- so every single utterance, happy ones
    # included, was described to the model as "valence 0.2x (negative)". The
    # model was not ignoring the voice; we were lying to it. Measure a backend
    # with `python -m eval.ser_eval --profile-valence` and set the thresholds to
    # match its actual range before trusting it.
    scales = 0
    for name, low, mid, high, lo_t, hi_t in (
        ("valence", "negative", "neutral", "positive",
         config.VALENCE_LOW, config.VALENCE_HIGH),
        ("arousal", "calm", "moderate", "elevated",
         config.AROUSAL_LOW, config.AROUSAL_HIGH),
        ("dominance", "yielding", "moderate", "assertive",
         config.DOMINANCE_LOW, config.DOMINANCE_HIGH),
    ):
        value = voice.get(name)
        if isinstance(value, (int, float)):
            gloss = low if value < lo_t else (high if value > hi_t else mid)
            parts.append(f"{name} {value:.2f} ({gloss})")
            scales += 1

    # A label with no scales at all is a weak hint, and the prompt's caution
    # paragraph keys on exactly this. Found 2026-09-05: the old emotion2vec
    # output ("angry (conf 0.77)", nothing else) was present enough to skip
    # the "no voice field" warning yet carried none of the numbers the rules
    # use, so the model was handed rules it could not apply and denied the
    # fallback. Say it inline, where it cannot be overlooked.
    if parts and scales == 0:
        parts.append("label only, no scales measured")

    return ", ".join(parts) if parts else None


TONES = ("positive", "negative", "neutral", "sarcastic", "mixed")

# The headline emotion, in the SER model's own vocabulary (server/ser.py
# EMOTIONS). Since 2026-09-22 the voice label is the PRIMARY source and the
# interpreter only adjusts it: the listener found the five-way tone verdict
# less useful than the seven emotions the voice model already produces, so
# the LLM is asked to start from the voice label and move it only when the
# words make it untenable. `tone` stays for what an emotion cannot carry
# (sarcasm, a mixed line) and for older clients.
EMOTIONS = ("neutral", "happy", "sad", "angry", "fearful", "disgusted", "surprised")
_EMOTION_SYNONYMS = {
    "anger": "angry", "mad": "angry", "annoyed": "angry", "frustrated": "angry",
    "sadness": "sad", "upset": "sad", "disappointed": "sad",
    "fear": "fearful", "afraid": "fearful", "anxious": "fearful", "scared": "fearful",
    "disgust": "disgusted",
    "surprise": "surprised", "shocked": "surprised",
    "happiness": "happy", "joy": "happy", "pleased": "happy", "excited": "happy",
    "calm": "neutral", "flat": "neutral",
}

# Off-enum answers that have an obvious home. RESPONSE_SCHEMA constrains `tone`
# to TONES, but only on Ollama builds that honour schemas; a model that
# volunteers "ironic" used to be recorded as "neutral" -- the maximally wrong
# bucket for a line that IS sarcastic. Map to the adjacent tone instead, and
# let only genuinely unknown strings fall through to neutral. Irony is folded
# into sarcasm on purpose (a separate tone would invalidate the eval baseline
# and needs its own labelling effort; see docs/status/TODO.md).
_TONE_SYNONYMS = {
    "ironic": "sarcastic", "irony": "sarcastic", "sardonic": "sarcastic",
    "sarcasm": "sarcastic", "ambivalent": "mixed", "conflicted": "mixed",
}

# How speaker-id labels appear in the context list. "mixed" (an utterance that
# holds a turn change) is deliberately left untagged: tagging it "speaker:"
# would assert something the model cannot rely on, and untagged is how every
# line looked before speaker id existed. "unknown" likewise.
SPEAKER_TAGS = {"user": "listener:", "other": "speaker:"}

# Ollama accepts a JSON *schema* here, not just the string "json", and the
# difference matters. Bare {"format": "json"} only requires that the output be
# valid JSON -- and `{}` is valid JSON, so a model is free to emit two tokens
# and stop. qwen3:14b does exactly that, every time, which looks like a model
# that answers "neutral" to everything rather than one that isn't complying.
#
# Naming the required fields and constraining `tone` to the enum makes the
# contract enforceable at decode time: the model cannot return an empty object
# or invent a sixth tone. Verified to fix qwen3:14b and to leave gemma3:12b and
# qwen3:8b unchanged. The defensive parsing below stays anyway -- older Ollama
# builds ignore schemas they don't understand.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "emotion": {"type": "string", "enum": list(EMOTIONS)},
        "tone": {"type": "string", "enum": list(TONES)},
        "read": {"type": "string"},
    },
    "required": ["emotion", "tone", "read"],
}


def load_options() -> dict:
    """Ollama options that are fixed when a model LOADS, for every caller.

    Ollama compares these against the loaded runner and reloads on any
    difference, so a dashboard warm-up and the interpreter sending different
    values would reload the model every time they alternate -- which reads as
    "the model got slow". Every request that can load or unload a model
    (interpreter, dashboard warm/evict, eval warm-ups) merges this in.
    """
    opts: dict = {"use_mmap": config.OLLAMA_USE_MMAP}
    if config.OLLAMA_NUM_CTX > 0:
        opts["num_ctx"] = config.OLLAMA_NUM_CTX
    return opts


# What Ollama uses when num_ctx is not sent: its VRAM-based default, logged as
# "vram-based default context" at startup (4096 on the 12 GB Arc).
_OLLAMA_DEFAULT_CTX = 4096


def context_near_full(prompt_tokens: int | None) -> bool:
    """True when this prompt plus the output cap is close to the context size.

    The runner shifts context on overflow keeping only 4 tokens, i.e. it would
    silently cut SYSTEM_PROMPT. This makes the approach visible long before.
    """
    if not isinstance(prompt_tokens, int):
        return False
    ctx = config.OLLAMA_NUM_CTX or _OLLAMA_DEFAULT_CTX
    return prompt_tokens + config.OLLAMA_NUM_PREDICT > config.CONTEXT_WARN_FRACTION * ctx


def _ollama_stats(body: dict | None) -> dict:
    """Pull Ollama's own timing/token counters out of a /api/generate response.

    Ollama reports exactly how many tokens it prefilled and generated and how
    long each phase took. Those are the honest numbers for "how fast is this
    model" -- wall clock also contains our own HTTP and JSON overhead -- so the
    evals want them, and re-deriving them from wall time would be a guess.

    Returned as extra keys on interpret()'s dict rather than a separate return
    value: `main.py` reads only "tone" and "read", so additional keys are
    invisible to production and nothing downstream has to change. Every field is
    None when the call failed, so a caller can always index them.

    Durations are nanoseconds on the wire; converted to ms here because nobody
    reasons in nanoseconds.
    """
    if not body:
        return {
            "prompt_eval_count": None, "eval_count": None,
            "prompt_eval_ms": None, "eval_ms": None,
            "load_ms": None, "total_ms": None, "decode_tps": None,
        }

    def ms(key: str) -> float | None:
        value = body.get(key)
        return round(value / 1e6, 1) if isinstance(value, (int, float)) else None

    eval_count = body.get("eval_count")
    eval_ns = body.get("eval_duration")
    # Guard the division: a cached or degenerate response can report 0 here.
    decode_tps = (
        round(eval_count / (eval_ns / 1e9), 1)
        if isinstance(eval_count, (int, float)) and eval_count
        and isinstance(eval_ns, (int, float)) and eval_ns
        else None
    )

    return {
        "prompt_eval_count": body.get("prompt_eval_count"),
        "eval_count": eval_count,
        "prompt_eval_ms": ms("prompt_eval_duration"),
        "eval_ms": ms("eval_duration"),
        "load_ms": ms("load_duration"),
        "total_ms": ms("total_duration"),
        "decode_tps": decode_tps,
    }


def _tone_prob(body: dict | None, key: str = "tone") -> float | None:
    """The model's probability for the tone (or emotion) it chose, from Ollama's logprobs.

    `key` names the JSON field: "tone" or "emotion". Both values are enum
    strings whose first token is distinctive, so the same offset walk works.

    Measured on qwen3:14b (Ollama 0.34, JSON-schema `format`, 2026-09-22):
    the logprobs are PRE-grammar-mask -- the forced opening `{"` scores about
    -25 while `<think>` sits at 0, because the schema, not the model, chose it.
    That is exactly what we want at the tone position: the token that starts the
    tone value gets a real distribution over the enum's first tokens (e.g.
    `sar` -0.07 against `negative` -3.6, `neutral` -3.9, `mixed` -3.9), so its
    probability says how torn the model was between labels. A structural token
    would say nothing.

    Finds that token by character offset: the response is a JSON string whose
    tokens concatenate to it, so walk the tokens until the offset of the tone
    value's first character. Returns None whenever the shape is not as
    expected (older Ollama, no logprobs, unexpected key order) -- never raises.
    """
    try:
        if not body or not isinstance(body.get("logprobs"), list):
            return None
        text = body.get("response") or ""
        m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', text)
        if not m:
            return None
        target = m.end()                 # offset of the value's first character
        pos = 0
        for tok in body["logprobs"]:
            piece = tok.get("token") or ""
            if pos + len(piece) > target:  # this token contains the offset
                if pos != target:
                    return None            # value starts mid-token; not comparable
                lp = tok.get("logprob")
                return round(math.exp(float(lp)), 4) if isinstance(lp, (int, float)) else None
            pos += len(piece)
        return None
    except Exception:  # noqa: BLE001 -- a missing number, never a failed read
        return None


# Belt-and-braces: the shipped prompt delimits with quotes, not tags, so this
# matches nothing today. It is here so that if the tag variant is ever revisited
# (see the docstring below), a spoken "</utterance>" cannot close the fence.
_UNSAFE_RE = re.compile(r"</?\s*(?:utterance|context)\s*>", re.I)


def _fence(line: str) -> str:
    """Flatten one transcript line so it cannot escape its delimiter.

    The transcript is whatever someone said into the mic -- the one part of the
    prompt an outsider controls. Two things let a spoken line stop being data
    and start looking like prompt structure:

      * newlines, which forge new sections ("Newest line: ...", "System: ...")
      * the fence tags themselves, which close the span early and leave the rest
        of the sentence sitting outside it, addressed to the model

    Collapsing whitespace removes both. This is deliberately a *mechanical*
    defence with no prompt text change: for any line without newlines the prompt
    is byte-identical to before, so it cost nothing on the eval (93% ALL, 2/4
    mismatch, every guard-rail category 100% -- same as baseline, 3 runs).

    Two stronger variants were tried and REJECTED on measurement, both 3 runs
    against qwen3:14b via `python -m eval.model_eval`:

      * Fencing the line in <utterance></utterance> tags instead of quotes:
        90% ALL, sarcasm 100% -> 75%. Losing the wording "Newest line, interpret
        only this one" reliably cost sar-04.
      * Adding a paragraph to SYSTEM_PROMPT stating that the transcript is data
        rather than instructions: 91% ALL, sarcasm 83%, and it made sar-04
        flap between runs.

    Also rejected: stripping quotes so the line cannot escape its delimiter.
    The character class catches apostrophes too, turning "It's" into "Its" --
    it mangles ordinary speech, and scored worst of all (89%, mismatch 1/4).

    So the model is not told to distrust the transcript; it is simply never
    handed anything shaped like a new instruction line. That leaves an attacker
    one flat line inside quotes, which is a far weaker position than being able
    to forge a "System:" line -- and it keeps the prompt, which is the product,
    exactly as it was measured.
    """
    return _UNSAFE_RE.sub("", " ".join(str(line).split()))


class Interpreter:
    """Stateful per-conversation interpreter holding the rolling context.

    One instance per websocket connection: the context window is that
    conversation's memory and should not bleed across clients.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        # Reuse one AsyncClient (connection pooling / keep-alive) rather than
        # spinning up a connection per request.
        self._client = client
        # Bounded deque = the rolling window: appends past `maxlen` silently
        # drop the oldest line, which is exactly the behaviour we want.
        #
        # Each entry is (who, line). `who` is one of the SPEAKER_TAGS keys or
        # None when speaker id had nothing to say -- and with every entry None
        # the prompt is byte-identical to the pre-speaker-id prompt, which is
        # what keeps eval/model_eval's numbers valid.
        self._context: deque[tuple[str | None, str]] = deque(maxlen=config.CONTEXT_LINES)

    def remember(self, transcript: str, who: str | None = None) -> None:
        """Add a line to the rolling context WITHOUT interpreting it.

        This is how the listener's own lines get in: they are never read (the
        whole point of speaker id is not to explain the user's feelings back to
        them), but they are exactly what the other person is replying to, so
        the model needs them as context.
        """
        self._context.append((who, transcript))

    def _build_prompt(self, newest: str, voice_hint: str | None = None,
                      who: str | None = None) -> str:
        """Assemble recent context + the newest line into one prompt string.

        `voice_hint` is the acoustic slot, filled from ser.analyze() when speech
        emotion recognition is available and omitted entirely when it is not.

        `who` is the speaker-id label of the newest line. Speaker labels are
        rendered here, not in SYSTEM_PROMPT, and only when at least one line
        actually carries one: without a profile enrolled the prompt must stay
        byte-for-byte what was measured.
        """
        parts: list[str] = []
        earlier = list(self._context)
        labelled = who in SPEAKER_TAGS or any(w in SPEAKER_TAGS for w, _ in earlier)
        if labelled:
            parts.append(
                'Lines marked "listener:" were said by the person you are helping; '
                'they are context only. Interpret only the other speaker.'
            )
            if who == "mixed":
                parts.append("The newest line may contain both voices.")
            parts.append("")
        if earlier:
            parts.append("Earlier lines (context only):")
            for w, line in earlier:
                tag = SPEAKER_TAGS.get(w) if labelled else None
                parts.append(f"- {tag} {_fence(line)}" if tag else f"- {_fence(line)}")
            parts.append("")
        if voice_hint:
            parts.append(f"voice sounded like: {voice_hint}")
        parts.append(f'Newest line, interpret only this one: "{_fence(newest)}"')
        return "\n".join(parts)

    async def interpret(self, transcript: str, voice: dict | None = None,
                        who: str | None = None) -> dict:
        """Return {"emotion": ..., "tone": ..., "read": ...} for `transcript`.

        `emotion` is the headline: one of EMOTIONS, the voice label as
        adjusted by the model. `emotion_adjusted` says whether it differs from
        the voice label, and `emotion_prob` is the model's probability for it.
        When the call fails, `emotion` is the voice label untouched (or neutral
        with no voice), so the SER stays the source of truth with the LLM down.

        `voice` is an optional ser.analyze() result describing how the line
        sounded. When present it is rendered into the prompt's "voice sounded
        like" slot so the model can play words and voice off each other; when
        absent the prompt is exactly what it was before SER existed.

        `who` is the optional speaker-id label ("other" / "mixed"); "user" lines
        never reach this method -- main.py routes them to remember() instead.

        All three dimensions are passed when present. Dominance earned its
        place once the default backend stopped supplying valence: arousal +
        dominance are the two genuinely paralinguistic axes, and dominance is
        what separates anger (high) from fear or resignation (low) at the same
        arousal.

        Always returns a dict; on any failure it returns a safe neutral-ish
        fallback so the caller can send *something* to the user.
        """
        prompt = self._build_prompt(transcript, _describe_voice(voice), who)
        voice_label = (voice or {}).get("emotion")
        voice_label = voice_label if voice_label in EMOTIONS else None

        # Record the line into context AFTER building the prompt, so a line is
        # never fed as its own "earlier context".
        self._context.append((who, transcript))

        payload = {
            "model": config.OLLAMA_MODEL,
            "system": SYSTEM_PROMPT,
            "prompt": prompt,
            "stream": False,
            "format": RESPONSE_SCHEMA,  # constrain output at decode time
            # qwen3 is a thinking model. Since the Ollama 0.34 upgrade an unset
            # `think` means ON: it spent all OLLAMA_NUM_PREDICT tokens in the
            # `thinking` field and returned an empty response, so EVERY read
            # came back "(interpreter offline)". The measured baselines were all
            # taken without thinking; say so explicitly. Ignored by non-thinking models.
            "think": False,
            "keep_alive": -1,       # keep the model resident so we skip reloads
            "options": {
                "temperature": config.OLLAMA_TEMPERATURE,
                "num_predict": config.OLLAMA_NUM_PREDICT,  # the read is short; cap tokens
                **load_options(),  # must match every other caller, see load_options()
            },
        }
        if config.OLLAMA_LOGPROBS:
            # Token probabilities for the confidence score (server/confidence.py).
            # Ignored by Ollama builds before 0.12.11; _tone_prob() then returns
            # None and the score falls back to its "no probability" base.
            payload["logprobs"] = True
            payload["top_logprobs"] = 5

        try:
            resp = await self._client.post(
                f"{config.OLLAMA_URL}/api/generate",
                json=payload,
                timeout=config.OLLAMA_TIMEOUT_S,
            )
            resp.raise_for_status()
            # Ollama wraps the model output in {"response": "...json string..."}.
            body = resp.json()
            parsed = json.loads(body["response"])
            tone = str(parsed.get("tone", "neutral")).lower().strip()
            read = str(parsed.get("read", "")).strip()
            tone = _TONE_SYNONYMS.get(tone, tone)
            if tone not in TONES:
                tone = "neutral"
            if not read:
                read = "(no read)"
            emotion = str(parsed.get("emotion", "")).lower().strip()
            emotion = _EMOTION_SYNONYMS.get(emotion, emotion)
            if emotion not in EMOTIONS:
                # An off-enum or missing emotion is not a verdict: fall back to
                # the voice label, which is the source anyway.
                emotion = voice_label or "neutral"
            stats = _ollama_stats(body)
            near_full = context_near_full(stats["prompt_eval_count"])
            if near_full:
                log.warning("prompt is %s tokens, near the %s-token context; an overflow "
                            "would silently cut SYSTEM_PROMPT",
                            stats["prompt_eval_count"],
                            config.OLLAMA_NUM_CTX or _OLLAMA_DEFAULT_CTX)
            return {"emotion": emotion, "tone": tone, "read": read, "offline": False,
                    "emotion_voice": voice_label,
                    "emotion_adjusted": bool(voice_label) and emotion != voice_label,
                    "emotion_prob": _tone_prob(body, "emotion"),
                    "tone_prob": _tone_prob(body), "context_near_full": near_full,
                    **stats}
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError):
            # Unreachable Ollama, non-2xx, or malformed/garbage JSON all land
            # here. Degrade gracefully rather than dropping the utterance: the
            # voice label still ships as the emotion, unadjusted.
            return {"emotion": voice_label or "neutral", "tone": "neutral",
                    "read": "(interpreter offline)", "offline": True,
                    "emotion_voice": voice_label, "emotion_adjusted": False,
                    "emotion_prob": None, "tone_prob": None, "context_near_full": False,
                    **_ollama_stats(None)}
