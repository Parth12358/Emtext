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
from collections import deque

import httpx

from . import config

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
#
# Changing any of this invalidates the numbers in eval/; re-run
# `python -m eval.model_eval` after editing.
SYSTEM_PROMPT = """You help a neurodivergent listener understand the emotional \
subtext of a conversation they are hearing. For the newest line only, reply \
with a single JSON object: {"tone": one of \
["positive","negative","neutral","sarcastic","mixed"], "read": a short plain \
sentence}. The "read" must be one line, under about 14 words, practical rather \
than clinical -- tell them what the speaker likely means or wants, not a \
diagnosis.

Some lines include a "voice:" field describing how the line actually SOUNDED, \
measured from the audio by a speech emotion model. It may report an emotion \
label with a confidence, plus these scales:
- valence: 0 = the voice sounds negative/unpleasant, 1 = positive/pleasant
- arousal: 0 = calm and subdued, 1 = intense and energised (energy, not mood: \
both rage and delight are high)

Compare the voice against the words -- the MISMATCH between them is the most \
useful signal you have:
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
- High arousal mainly tells you how strongly the speaker feels, not whether it \
is good or bad; use valence for that.

Weigh the voice by its confidence. Below about 0.4 the label is close to a guess, \
so lean on the words and treat the voice as weak supporting evidence at most. \
Never let a low-confidence voice reading alone turn a plain sentence into \
sarcasm. The voice field is a hint from an imperfect model, not ground truth.

When there is no "voice:" field you are reading text alone with no vocal tone \
at all, so be especially careful not to over-read sarcasm into plain sentences. \
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

    # Gloss thresholds are deliberately coarse. SER dimensions are not precise
    # enough to justify fine-grained wording, and a middling value genuinely
    # means "unremarkable" rather than anything the listener should act on.
    for name, low, mid, high in (
        ("valence", "negative", "neutral", "positive"),
        ("arousal", "calm", "moderate", "elevated"),
    ):
        value = voice.get(name)
        if isinstance(value, (int, float)):
            gloss = low if value < 0.4 else (high if value > 0.6 else mid)
            parts.append(f"{name} {value:.2f} ({gloss})")

    return ", ".join(parts) if parts else None


TONES = ("positive", "negative", "neutral", "sarcastic", "mixed")

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
        "tone": {"type": "string", "enum": list(TONES)},
        "read": {"type": "string"},
    },
    "required": ["tone", "read"],
}


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
        self._context: deque[str] = deque(maxlen=config.CONTEXT_LINES)

    def _build_prompt(self, newest: str, voice_hint: str | None = None) -> str:
        """Assemble recent context + the newest line into one prompt string.

        `voice_hint` is the acoustic slot, filled from ser.analyze() when speech
        emotion recognition is available and omitted entirely when it is not.
        """
        parts: list[str] = []
        earlier = list(self._context)
        if earlier:
            parts.append("Earlier lines (context only):")
            parts.extend(f"- {line}" for line in earlier)
            parts.append("")
        if voice_hint:
            parts.append(f"voice sounded like: {voice_hint}")
        parts.append(f'Newest line, interpret only this one: "{newest}"')
        return "\n".join(parts)

    async def interpret(self, transcript: str, voice: dict | None = None) -> dict:
        """Return {"tone": ..., "read": ...} for `transcript`.

        `voice` is an optional ser.analyze() result describing how the line
        sounded. When present it is rendered into the prompt's "voice sounded
        like" slot so the model can play words and voice off each other; when
        absent the prompt is exactly what it was before SER existed.

        Note we pass emotion/valence/arousal but not dominance: dominance is
        useful for separating emotion classes inside the model, but it adds
        little the LLM can act on and lengthens the prompt for no gain.

        Always returns a dict; on any failure it returns a safe neutral-ish
        fallback so the caller can send *something* to the user.
        """
        prompt = self._build_prompt(transcript, _describe_voice(voice))

        # Record the line into context AFTER building the prompt, so a line is
        # never fed as its own "earlier context".
        self._context.append(transcript)

        payload = {
            "model": config.OLLAMA_MODEL,
            "system": SYSTEM_PROMPT,
            "prompt": prompt,
            "stream": False,
            "format": RESPONSE_SCHEMA,  # constrain output at decode time
            "keep_alive": -1,       # keep the model resident so we skip reloads
            "options": {
                "temperature": config.OLLAMA_TEMPERATURE,
                "num_predict": config.OLLAMA_NUM_PREDICT,  # the read is short; cap tokens
            },
        }

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
            if tone not in TONES:
                tone = "neutral"
            if not read:
                read = "(no read)"
            return {"tone": tone, "read": read, **_ollama_stats(body)}
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError):
            # Unreachable Ollama, non-2xx, or malformed/garbage JSON all land
            # here. Degrade gracefully rather than dropping the utterance.
            return {"tone": "neutral", "read": "(interpreter offline)",
                    **_ollama_stats(None)}
