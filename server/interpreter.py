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
#   - "prefer neutral when unsure" is a guardrail: transcripts carry NO vocal
#     tone, so the model must not over-read sarcasm into plain sentences.
#   - The optional "voice sounded like: X" line is wired in now but always
#     blank -- when the acoustic-emotion model lands it will fill that slot and
#     the prompt shape won't have to change.
SYSTEM_PROMPT = """You help a neurodivergent listener understand the emotional \
subtext of a conversation they are hearing. For the newest line only, reply \
with a single JSON object: {"tone": one of \
["positive","negative","neutral","sarcastic","mixed"], "read": a short plain \
sentence}. The "read" must be one line, under about 14 words, practical rather \
than clinical -- tell them what the speaker likely means or wants, not a \
diagnosis. You are reading text transcripts with no vocal tone, so when you are \
unsure, prefer "neutral" rather than guessing at sarcasm. Use the earlier lines \
only as context; interpret just the newest line."""


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

        `voice_hint` is the forward-looking acoustic slot; it stays None today.
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

    async def interpret(self, transcript: str, voice_hint: str | None = None) -> dict:
        """Return {"tone": ..., "read": ...} for `transcript`.

        Always returns a dict; on any failure it returns a safe neutral-ish
        fallback so the caller can send *something* to the user.
        """
        prompt = self._build_prompt(transcript, voice_hint)

        # Record the line into context AFTER building the prompt, so a line is
        # never fed as its own "earlier context".
        self._context.append(transcript)

        payload = {
            "model": config.OLLAMA_MODEL,
            "system": SYSTEM_PROMPT,
            "prompt": prompt,
            "stream": False,
            "format": "json",       # ask Ollama to constrain output to JSON
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
            if tone not in {"positive", "negative", "neutral", "sarcastic", "mixed"}:
                tone = "neutral"
            if not read:
                read = "(no read)"
            return {"tone": tone, "read": read}
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError):
            # Unreachable Ollama, non-2xx, or malformed/garbage JSON all land
            # here. Degrade gracefully rather than dropping the utterance.
            return {"tone": "neutral", "read": "(interpreter offline)"}
