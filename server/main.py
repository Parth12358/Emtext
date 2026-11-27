"""FastAPI app + websocket route. Wiring only -- no signal logic lives here.

The one interesting thing in this file is the *concurrency shape*, because it
is what keeps the conversation feeling live:

  - Audio arrives continuously and must never be made to wait. So the websocket
    read loop does the absolute minimum per frame: hand bytes to the segmenter.
  - Transcription (Whisper) is a blocking CPU call. Running it inline would
    freeze the read loop and back up the audio buffer. Instead we push it to a
    thread executor via loop.run_in_executor.
  - Speech emotion recognition (SER) and speaker identification are two more
    blocking CPU calls over the same audio, each independent of the others. So
    the three are gathered rather than sequenced: one utterance costs about
    max(whisper, ser, speaker), not their sum.
  - Speaker id can short-circuit the rest: a line labelled `user` (the
    listener's own voice) is transcribed and remembered as context, but gets
    no read -- explaining the user's own feelings back to them is the one
    output this product must not produce.
  - Interpretation (the LLM) is slow too. We wrap the whole
    transcribe->interpret->send sequence in a fire-and-forget asyncio task, one
    per utterance. The read loop kicks it off and immediately goes back to
    reading audio, so we can already be listening to (and even transcribing)
    the next sentence while still "thinking" about the last one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import secrets
import time
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import (blend, clips, config, confidence, dashboard, metrics, prompt_store, sentences,
               ser, speaker, streaming, translog, transcriber, tuning)
from .interpreter import Interpreter
from .segmenter import Segmenter
from .transcriber import transcribe

log = logging.getLogger(__name__)

# docs/redoc/openapi are OFF. FastAPI mounts them unauthenticated by default,
# and they are registered in this constructor -- *before* the StaticFiles mount
# at "/" -- so the catch-all never shadows them. On a public hostname that hands
# a stranger the whole API surface (every route, both request schemas, and the
# fact that auth is a `token` query param), plus a point-and-click console at
# /docs for firing POST /api/model. /docs also loads Swagger's JS from a CDN,
# which this project otherwise deliberately avoids.
app = FastAPI(title="emtext", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(dashboard.router)

# Security headers on every response. There were none, and the pages carry the
# auth token, so a single injected element would have been a token theft.
#
# CSP notes, because two directives look wrong until you know why:
#   * 'unsafe-inline' is unavoidable. These are deliberately single-file pages
#     with no build step (a project rule), so all script and style is inline.
#     A per-response nonce would be strictly better and stays compatible with
#     "no build step" -- worth doing if these pages grow.
#   * blob: in script-src/worker-src is required by the AudioWorklet: both
#     capture pages construct the processor from a Blob URL.
#   * connect-src allows arbitrary https/wss ON PURPOSE. remote.html exists to
#     point at whatever hostname the quick tunnel produced this restart, so it
#     cannot be locked to 'self' without destroying the page's reason to exist.
#     What it can't do -- and now can't -- is silently take that host from a
#     link (see remote.html) or pull a script, style, or image from anywhere.
# frame-ancestors is the one that matters most day to day: it stops a foreign
# page framing the dashboard and clickjacking its write buttons -- model switch
# and evict, clip delete, and (via enroll.html) forgetting the voice profile.
_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline' blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    # blob: for media only. The dashboard fetches a saved clip with the auth
    # header and plays it from a Blob URL, so the token never has to sit in an
    # <audio src> query string. 'self' alone would block that silently.
    "media-src 'self' blob:; "
    "connect-src 'self' https: wss: ws:; "
    "worker-src 'self' blob:; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Frame-Options": "DENY",              # legacy twin of frame-ancestors
    "X-Content-Type-Options": "nosniff",
    # The token can still reach a URL by hand (?token=), so never send a
    # Referer that could carry it -- or the tunnel hostname -- to another origin.
    "Referrer-Policy": "no-referrer",
    # The app needs the microphone; nothing else does, and no embedder should.
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
}


@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response

_STATIC_DIR = Path(__file__).parent / "static"

# A single shared HTTP client for all Ollama calls (pooled, reused).
_http: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _http
    _http = httpx.AsyncClient()
    # Streaming ASR (live words + sentence endpoints) is optional and loads in
    # the background: a missing model must not delay or stop the server.
    asyncio.get_running_loop().run_in_executor(None, streaming.load)
    # A system prompt saved from the dashboard, if any (tiny file, read once).
    prompt_store.load()
    # The dashboard reuses this pooled client rather than opening its own.
    app.state.http = _http

    # GPU counters cost ~2.8s and a PowerShell launch per query on Windows, so
    # they are sampled on a background thread and served from cache -- a
    # dashboard poll never waits. The poller is NOT started here: dashboard.py
    # starts it on the first /api/stats and it idles again once nobody polls
    # (it used to cost ~2.4 CPU-hours a day with no dashboard open).
    asyncio.create_task(_sample_system_loop())

    # ser.py loads at import, before uvicorn has configured logging, so restate
    # the outcome here where it will actually be visible in the server log.
    # Running without SER is a supported mode, not an error -- say which mode
    # we are in so a missing "voice:" line is never a mystery.
    if ser.available():
        log.info("speech emotion recognition enabled (%s)", config.SER_MODEL)
    elif config.SER_ENABLED:
        log.warning(
            "speech emotion recognition unavailable -- reads will have no voice "
            "data (see the earlier SER warning for the cause)"
        )
    else:
        log.info("speech emotion recognition disabled (SER_ENABLED=0)")

    # Same for speaker id. "loaded but nothing enrolled" is the state a fresh
    # install sits in, and it silently means every line is interpreted --
    # including the user's own -- so name it.
    if speaker.available():
        if speaker.profile.ready():
            log.info("speaker id enabled (%d enrolled utterances)", speaker.profile.count)
        else:
            log.warning(
                "speaker id loaded but only %d/%d utterances enrolled -- the listener's "
                "own lines WILL be interpreted until they enrol at /enroll.html",
                speaker.profile.count, config.SPEAKER_MIN_ENROLL,
            )
    elif config.SPEAKER_ENABLED:
        log.warning("speaker id unavailable -- every line will be interpreted")
    else:
        log.info("speaker id disabled (SPEAKER_ENABLED=0)")

    # Auth is off by default, which is correct for localhost and dangerous the
    # moment the server is reachable from anywhere else. A quick-tunnel hostname
    # is random, but random is not secret: it travels through Cloudflare, sits in
    # your shell history, and anyone who obtains it gets unmetered use of this
    # machine's CPU and GPU. There is no rate limit or connection cap behind it.
    if config.AUTH_TOKEN is None:
        log.warning("=" * 72)
        log.warning("AUTH_TOKEN is NOT set -- this server accepts ANY client.")
        log.warning("Fine on localhost. Do NOT expose it through a tunnel.")
        log.warning("Set one first, e.g.:")
        log.warning("  python -c \"import secrets;print(secrets.token_urlsafe(32))\"")
        log.warning("=" * 72)
    else:
        log.info("auth enabled (AUTH_TOKEN set, %d chars)", len(config.AUTH_TOKEN))

    # Warning-only was not enough. The warning goes to stderr, which the start
    # scripts redirect into a temp file nobody reads, and a named tunnel is a
    # separate long-lived process -- so a `python -m server.main` typed by hand
    # put the real public hostname in front of an open server, silently. Refuse
    # instead. Loopback stays open with no ceremony (that is the dev path), and
    # ALLOW_NO_AUTH=1 is there for anyone who genuinely wants the old behaviour.
    if config.AUTH_TOKEN is None and not config.ALLOW_NO_AUTH:
        if config.HOST not in ("127.0.0.1", "::1", "localhost"):
            raise SystemExit(
                f"refusing to start: AUTH_TOKEN is unset and HOST is {config.HOST}, "
                "which is reachable from outside this machine.\n"
                "  set a token:        python tunnel/token.py\n"
                "  or bind loopback:   HOST=127.0.0.1 (the default)\n"
                "  or override:        ALLOW_NO_AUTH=1"
            )


async def _sample_system_loop() -> None:
    """One CPU/RAM/GPU sample a second, for the dashboard sparklines."""
    while True:
        try:
            metrics.sample_system()
        except Exception:  # noqa: BLE001 -- telemetry must never kill the server
            pass
        await asyncio.sleep(1.0)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _http is not None:
        await _http.aclose()


async def _send(ws: WebSocket, message: dict) -> None:
    """Send a JSON frame, swallowing errors from an already-closed socket.

    Fire-and-forget tasks may still be running when a client vanishes; we don't
    want their final sends to raise and spam the logs.
    """
    try:
        await ws.send_text(json.dumps(message))
    except (WebSocketDisconnect, RuntimeError):
        pass


async def _close_quietly(ws: WebSocket, code: int) -> None:
    """Close a socket, ignoring a peer that has already gone.

    Rejection paths run against clients that are frequently hostile or already
    disconnected; a raise here would turn a refused handshake into a traceback.
    """
    try:
        await ws.close(code=code)
    except (WebSocketDisconnect, RuntimeError):
        pass


def _token_ok(supplied: str) -> bool:
    """Constant-time token check.

    Compared as *bytes*: `secrets.compare_digest` raises TypeError on a str
    containing non-ASCII, and the supplied value is attacker-chosen -- so the
    obvious str form would hand a remote peer an uncaught exception. Encoding
    first cannot fail, because a websocket TEXT frame is already valid Unicode.

    The timing channel this closes is not realistically exploitable through
    Cloudflare (nanoseconds of signal under milliseconds of jitter, against a
    256-bit token). It is here because this file is the reference the ESP32
    firmware will be written from, and it should not model `==` on a secret.
    """
    if config.AUTH_TOKEN is None:
        return True
    return secrets.compare_digest(
        supplied.encode("utf-8"), config.AUTH_TOKEN.encode("utf-8")
    )


def _log_task_error(task: asyncio.Task) -> None:
    """Surface anything a fire-and-forget utterance task raised.

    asyncio only reports a task's exception when its result is retrieved, and
    nothing was retrieving it. So a bug in the utterance path presented as "no
    transcript ever appears" with a completely silent log -- which is exactly
    how a control-flow mistake here went unnoticed once. Log it loudly and count
    it, so the dashboard shows it too.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("utterance processing failed", exc_info=exc)
        metrics.record_event("utterance_errors")


def _log_transcript(loop, uid, audio, transcript, who, voice, result,
                    whisper_s, ser_s, llm_s, conf=None, emotion=None,
                    part: dict | None = None) -> None:
    """Hand one finished utterance to the transcript log, off the event loop.

    Fire-and-forget on purpose: the log is for improving the ASR later, so it
    must never add to the latency of the read, and `translog.record` never
    raises, so there is nothing to await for. Skipped entirely when disabled.
    """
    if not translog.enabled():
        return
    entry = {
        "uid": uid,
        "asr_model": transcriber.current_key(),
        "transcript": transcript,
        "speaker": _speaker_field(who),
        "voice": voice,
        "tone": result.get("tone") if result else None,
        "emotion": emotion,
        "read": result.get("read") if result else None,
        "confidence": conf,
        "whisper_s": round(whisper_s, 3),
        "ser_s": round(ser_s, 3),
        "llm_s": round(llm_s, 3) if llm_s is not None else None,
    }
    if part:
        # Which sentence of which segmenter utterance this was (sentences.py).
        entry["split"] = part
    custom = prompt_store.override()
    if custom:
        # Read under a dashboard-edited system prompt: its short hash, so it can be
        # matched to the saved version (GET /api/prompt history).
        entry["prompt"] = prompt_store.sha(custom)
    tuned = tuning.snapshot()
    if tuned:
        # Live dashboard weights that differ from the defaults, so reads made
        # under different settings can be told apart afterwards.
        entry["tuning"] = tuned
    loop.run_in_executor(None, translog.record, entry, audio)


def _speaker_field(who: dict | None) -> dict | None:
    """The optional `speaker` object for the wire, or None to omit it.

    Only label and score go out: `cos_user` and `windows` are diagnostics for
    the dashboard, not part of the client contract.
    """
    if not who:
        return None
    return {"label": who.get("label"), "score": who.get("score")}


class _Turns:
    """Emission order for one connection: reads go out in the order the audio came in.

    Utterance tasks are fire-and-forget and overlap, which was fine when one
    utterance meant one read. With the live sentence split, the sentences of
    one stretch of talk come from several tasks (live pieces first, the tail at
    close), and a fast later task must not overtake a slower earlier one: the
    pendant would show sentence 3 before sentence 2, and the interpreter's
    context would be out of order. So every task takes a ticket when it is
    created (on the event loop, in audio order), does its CPU work freely, then
    waits for its turn before sending anything. `done()` runs in a `finally`,
    so a task that emits nothing (empty transcript) or fails still frees its turn.
    """

    def __init__(self) -> None:
        self._next = 0
        self._head = 0
        self._done: set[int] = set()
        self._cond = asyncio.Condition()

    def ticket(self) -> int:
        t = self._next
        self._next += 1
        return t

    async def wait(self, t: int) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: self._head >= t)

    async def done(self, t: int) -> None:
        async with self._cond:
            self._done.add(t)
            while self._head in self._done:
                self._done.discard(self._head)
                self._head += 1
            self._cond.notify_all()


def _timed(fn, *args):
    """(result, seconds). Each stage times ITSELF rather than being timed around
    .result(): timing the gather would fold queue wait into whichever finished
    second, and make SER look free simply because Whisper was awaited first."""
    start = time.perf_counter()
    return fn(*args), time.perf_counter() - start


async def _emit_pieces(ws, interpreter, recent, session, pieces, ids, audio, tokens,
                       whole_who, spk_s, whisper_s, cpu_t0, speech_rms, parent,
                       per_piece_speaker: bool, gate=None) -> None:
    """Send sentence pieces (server/sentences.py) as ordinary utterances, in order.

    Each piece gets its own SER, all submitted at once (they queue on SER's lock
    in order), so piece 1's read is not held up by piece 4's audio. Speaker id
    is run per piece when `per_piece_speaker` (the live split, where nothing was
    identified yet, and a whole utterance that came back `mixed`); otherwise
    every piece inherits `whole_who`, so the partner centroid is not fed the
    same audio twice. `ids` are allocated by the caller, on the event loop, so
    they stay in audio order. `gate` is awaited after the CPU jobs are queued
    and before anything is sent (the connection's `_Turns`).
    """
    loop = asyncio.get_running_loop()
    jobs = []
    for pc in pieces:
        seg = audio[pc.start:pc.end]
        sj = loop.run_in_executor(None, _timed, ser.analyze, seg)
        kj = (loop.run_in_executor(None, _timed, speaker.identify, seg, session)
              if per_piece_speaker else None)
        jobs.append((pc, seg, sj, kj))
    if gate is not None:
        await gate()
    for i, (pc, seg, sj, kj) in enumerate(jobs):
        p_voice, p_ser_s = await sj
        p_who, p_spk_s = (await kj) if kj is not None else (whole_who, spk_s)
        if p_who is None or p_who.get("label") in (None, "unknown"):
            p_who = whole_who if whole_who is not None else p_who
        await _emit(ws, interpreter, recent, ids[i], seg, pc.text, pc.asr_conf(tokens),
                    p_voice, p_who, whisper_s, p_ser_s, p_spk_s or 0.0,
                    time.perf_counter() - cpu_t0,
                    part={"parent": parent, "part": i + 1, "of": len(pieces)},
                    speech_rms=speech_rms)


async def _process_utterance(
    ws: WebSocket,
    interpreter: Interpreter,
    recent: clips.Recent,
    session: speaker.Session,
    uid: int,
    audio,
    new_id=None,
    speech_rms: float | None = None,
    turns: _Turns | None = None,
    ticket: int | None = None,
) -> None:
    """Transcribe then interpret one utterance and stream the results out.

    Runs as its own asyncio task, so multiple utterances can be in flight at
    once. Order between them is best-effort, not guaranteed -- each frame we
    send carries its own `id` so the client can correlate transcript and read.

    A multi-sentence utterance is cut into sentence-sized pieces after ASR
    (`server/sentences.py`), and each piece goes out as its own utterance with
    its own id from `new_id()` (the first piece keeps `uid`), its own SER and
    its own read. No wire change: a client sees ordinary utterance/read pairs.
    """
    try:
        await _process_utterance_body(ws, interpreter, recent, session, uid, audio,
                                      new_id, speech_rms, turns, ticket)
    finally:
        if turns is not None and ticket is not None:
            await turns.done(ticket)


async def _process_utterance_body(ws, interpreter, recent, session, uid, audio,
                                  new_id, speech_rms, turns, ticket) -> None:
    loop = asyncio.get_running_loop()
    # A callable, not a coroutine: a path that returns early must not leave an
    # un-awaited coroutine behind.
    gate = ((lambda: turns.wait(ticket)) if turns is not None and ticket is not None else None)

    # Whisper (what was said), SER (how it sounded) and speaker id (whose voice
    # it was) are all blocking CPU work over the same immutable audio buffer,
    # none touching another's state. So they run as three executor jobs and are
    # gathered: the utterance costs about max(whisper, ser, speaker) rather
    # than their sum, and speaker id (~20 ms) is effectively free.
    #
    # SER and speaker id are the optional parts. Both return None when their
    # model is disabled, failed to load, or (for speaker id) nobody has
    # enrolled yet, and both swallow their own errors, so this gather cannot
    # fail because of them.
    # Each stage times itself (see `_timed`).
    cpu_t0 = time.perf_counter()
    ((transcript, asr_conf, tokens), whisper_s), (voice, ser_s), (who, spk_s) = await asyncio.gather(
        loop.run_in_executor(None, _timed, transcriber.transcribe_timed, audio),
        loop.run_in_executor(None, _timed, ser.analyze, audio),
        loop.run_in_executor(None, _timed, speaker.identify, audio, session),
    )
    cpu_stage_s = time.perf_counter() - cpu_t0
    if not transcript:
        # Whisper heard nothing usable. Invisible everywhere else, and a useful
        # number: a rising count means the VAD is passing through non-speech.
        metrics.record_event("empty_transcript")
        return

    pieces = []
    if config.SENTENCE_SPLIT and tokens and new_id is not None:
        pieces = sentences.split(
            tokens, int(audio.size), config.SAMPLE_RATE,
            min_s=config.SPLIT_MIN_S, min_words=config.SPLIT_MIN_WORDS,
            max_pieces=config.SPLIT_MAX_PIECES, pad_s=config.SPLIT_PAD_S,
        )
    if len(pieces) < 2:
        if gate is not None:
            await gate()
        await _emit(ws, interpreter, recent, uid, audio, transcript, asr_conf,
                    voice, who, whisper_s, ser_s, spk_s, cpu_stage_s,
                    speech_rms=speech_rms)
        return

    # Several sentences. The whole-utterance SER above is discarded: one label
    # averaged over a paragraph is exactly what this split exists to replace.
    # Speaker id is re-run per piece only when the whole utterance came back
    # `mixed`, the glued-turns case where a per-sentence label is the point.
    metrics.record_event("utterance_split")
    ids = [uid] + [new_id() for _ in pieces[1:]]
    for pid, pc in zip(ids, pieces):
        recent.add(pid, audio[pc.start:pc.end])   # piece 1 replaces the whole utterance's audio
    await _emit_pieces(ws, interpreter, recent, session, pieces, ids, audio, tokens,
                       who, spk_s, whisper_s, cpu_t0, speech_rms, parent=uid,
                       per_piece_speaker=bool(who) and who.get("label") == "mixed",
                       gate=gate)


async def _emit(
    ws: WebSocket,
    interpreter: Interpreter,
    recent: clips.Recent,
    uid: int,
    audio,
    transcript: str,
    asr_conf,
    voice,
    who,
    whisper_s: float,
    ser_s: float,
    spk_s: float,
    cpu_stage_s: float,
    part: dict | None = None,
    speech_rms: float | None = None,
) -> None:
    """Send one utterance (and, unless it is the user's, its read).

    Shared by a whole utterance and by each sentence piece of a split one.
    """
    loop = asyncio.get_running_loop()
    # Seconds of actual speech in the buffer: the voice model is unreliable under
    # ~1.5 s of it, and the confidence score says so rather than pretending
    # otherwise. Counted against the threshold the segmenter was actually using
    # when it closed this utterance (`speech_rms`), not the fixed SPEECH_RMS
    # ceiling: in a quiet room speech sits at RMS ~200-900, and against 500 a
    # 15 s conversational clip counted as 2.2 s voiced (docs/status/TODO.md).
    voiced_s = (speaker.voiced_fraction(audio, speech_rms)
                * (float(audio.size) / config.SAMPLE_RATE))

    label = who.get("label") if who else None
    spk_field = _speaker_field(who)
    recent.update(uid, transcript=transcript, speaker=who)
    utt_msg: dict = {"type": "utterance", "id": uid, "transcript": transcript}
    if spk_field:
        # Optional, additive: a client that predates speaker id ignores it.
        utt_msg["speaker"] = spk_field
    await _send(ws, utt_msg)

    if label == "user":
        # The listener's own line. It is transcribed because it is what the
        # other person is about to reply to -- but it gets NO read, and its
        # voice result is dropped rather than sent: explaining the user's own
        # feelings back to them is the one output this product must not
        # produce, and docs/design/RESEARCH.md §1.5 says the acoustic channel is exactly
        # where a model misreads an autistic speaker. No `read` frame follows,
        # so the client must not wait for one on a `user` utterance.
        interpreter.remember(transcript, who="user")
        metrics.record_speaker(label, spk_s)
        metrics.record_event("user_line")
        _log_transcript(loop, uid, audio, transcript, who, None, None,
                        whisper_s, ser_s, None, part=part)
        return

    metrics.record_speaker(label, spk_s)
    await _send(ws, {"type": "status", "state": "thinking"})

    llm_t0 = time.perf_counter()
    result = await interpreter.interpret(transcript, voice, who=label)
    llm_s = time.perf_counter() - llm_t0
    if result.get("context_near_full"):
        # interpreter.py already logged it; count it so the dashboard shows it.
        metrics.record_event("context_near_full")

    # Voice <-> interpreter mix for the headline emotion (server/blend.py), set
    # live from the dashboard. At the default 1.0 this returns the interpreter's
    # label untouched and nothing below changes. Offline reads already carry the
    # voice label, so they are left alone.
    if not result.get("offline") and config.EMOTION_BLEND < 1.0:
        v_label = result.get("emotion_voice")
        label_m, prob_m = blend.mix(result.get("emotion"), result.get("emotion_dist"),
                                    v_label, (voice or {}).get("dist"), config.EMOTION_BLEND)
        if label_m and label_m != result.get("emotion"):
            result = {**result, "emotion": label_m,
                      "emotion_adjusted": bool(v_label) and label_m != v_label,
                      "emotion_prob": prob_m}

    # How sure is this read? Pure combination of the signals above; never
    # changes what the LLM saw, only how the read is weighted on the way out.
    # The headline is the voice model's emotion, so an unadjusted read's base
    # is the voice model's own confidence; only an adjusted one uses the
    # interpreter's probability (confidence.base_prob has the measurements).
    base, ser_pen = confidence.base_prob(result, voice)
    conf = confidence.score(
        tone=result.get("tone"),
        tone_prob=base,
        offline=bool(result.get("offline")),
        asr_conf=asr_conf,
        ser_conf=ser_pen,
        voiced_s=voiced_s,
        speaker_label=label,
    )

    # The wire's `emotion` object: the label the clients headline, the voice
    # model's own label it started from, and whether the interpreter moved it.
    emotion = {
        "label": result.get("emotion") or "neutral",
        "voice": result.get("emotion_voice"),
        "adjusted": bool(result.get("emotion_adjusted")),
    }

    metrics.record_utterance(
        transcript=transcript,
        tone=result.get("tone"),
        emotion=emotion["label"],
        read=result.get("read"),
        whisper_s=whisper_s,
        ser_s=ser_s,
        cpu_stage_s=cpu_stage_s,
        llm_s=llm_s,
        voice=voice,
        offline=result.get("read") == "(interpreter offline)",
        speaker=label,
        confidence=conf,
    )

    # Keep the retention ring current so a `save` that names this id gets the
    # read as well as the audio. Cheap, and a no-op if the entry aged out.
    recent.update(uid, tone=result.get("tone"), emotion=emotion, read=result.get("read"),
                  voice=voice, confidence=conf)
    _log_transcript(loop, uid, audio, transcript, who, voice, result,
                    whisper_s, ser_s, llm_s, conf, emotion, part=part)

    message = {
        "type": "read",
        "id": uid,
        "tone": result["tone"],
        "read": result["read"],
        # Additive: the headline emotion in the voice model's vocabulary,
        # {"label", "voice", "adjusted"}. `voice` is the SER label it started
        # from (null with SER off); `adjusted` says the interpreter moved it.
        # Always present on this server; older clients ignore it and keep
        # colouring by `tone`.
        "emotion": emotion,
        # Additive, like `voice` and `speaker`: how sure the server is, as a
        # 0-1 score, a level the clients render (high as before, medium
        # de-emphasised, low suppressed behind a "not sure" marker), and the
        # plain-language reasons. Older clients ignore the key.
        "confidence": conf,
    }
    if spk_field:
        message["speaker"] = spk_field
    if voice:
        # Optional field, added only when we actually have data: clients that
        # predate SER (and firmware that never implements it) just ignore an
        # unknown key, so the wire protocol stays backward compatible.
        message["voice"] = {
            "emotion": voice.get("emotion"),
            "confidence": voice.get("confidence"),
            "valence": voice.get("valence"),
            "arousal": voice.get("arousal"),
            # Added with the derived-dimensions work: on the default backend
            # arousal and dominance are the voice signal. Additive, like the rest.
            "dominance": voice.get("dominance"),
        }
    await _send(ws, message)


def _wire_int(v) -> int | None:
    """An int off the wire, or None. `bool` is an int in Python; not here."""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


async def _save_clip(ws: WebSocket, entries: list[dict], cid: int) -> None:
    """Persist a run of retained utterances as one clip and confirm to the device.

    `clips.save` is blocking file I/O and never raises, so the only job here is
    to keep it off the event loop and turn its result into a `saved` frame.
    `cid` is the range's `to`, echoed as the reply's `id`.
    """
    loop = asyncio.get_running_loop()
    clip_id, error = await loop.run_in_executor(None, clips.save, entries)
    if error is None:
        metrics.record_event("clip_saved")
        await _send(ws, {"type": "saved", "id": cid, "ok": True, "clip": clip_id})
    else:
        metrics.record_event("clip_failed")
        await _send(ws, {"type": "saved", "id": cid, "ok": False, "error": error})


@app.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    await ws.accept()

    # --- handshake: first frame is the auth token (TEXT) --------------------
    # Bounded on three axes, because everything before the token check is work
    # done on behalf of an unauthenticated stranger:
    #   * time    -- without a timeout a peer that connects and says nothing is
    #                held forever (the websocket layer answers protocol pings for
    #                it, so it never looks idle and costs the attacker nothing).
    #   * type    -- starlette's receive_text() does message["text"], so a BINARY
    #                first frame raised an uncaught KeyError: a traceback per
    #                attempt, into a log file that does not rotate.
    #   * length  -- a token is 43 chars; anything near the frame cap is an
    #                attempt to make us allocate before we have checked anything.
    try:
        token = await asyncio.wait_for(
            ws.receive_text(), timeout=config.WS_AUTH_TIMEOUT_S
        )
    except (WebSocketDisconnect, asyncio.TimeoutError, KeyError):
        await _close_quietly(ws, code=1008)
        return
    if len(token) > 512 or not _token_ok(token):
        # Count and log it: a rejected handshake used to be completely silent,
        # so there was no signal that anyone was probing and nothing to act on.
        metrics.record_event("auth_failed")
        client = ws.client.host if ws.client else "?"
        log.warning("rejected /stream handshake from %s", client)
        await _close_quietly(ws, code=1008)  # policy violation
        return

    # Identify the connection for the dashboard. All of this is best-effort and
    # display-only -- nothing downstream branches on it.
    #
    # CF-Connecting-IP first: behind the tunnel every connection arrives from
    # cloudflared on loopback, so ws.client.host is always 127.0.0.1 and tells
    # you nothing about which device is actually streaming.
    _peer = ws.headers.get("cf-connecting-ip") or (ws.client.host if ws.client else None)
    _agent = (ws.headers.get("user-agent") or "")[:120] or None
    client_id = metrics.connection_opened(
        peer=_peer,
        agent=_agent,
        vad=ws.query_params.get("vad") in ("1", "true", "yes"),
    )

    await _send(ws, {"type": "ready"})
    await _send(ws, {"type": "status", "state": "listening"})

    # Per-connection state: its own segmenter and its own interpreter (so the
    # rolling context belongs to this conversation only).
    assert _http is not None
    segmenter = Segmenter()
    interpreter = Interpreter(_http)
    # Who the OTHER voice in this conversation is, learned online from lines
    # that were not the user. A property of the conversation, so per connection
    # and never persisted (speaker.py).
    session = speaker.Session()
    # Recent utterances by id, so a `save` frame can name one (README.md, wire protocol). Per
    # connection because ids are per connection: a save after a reconnect
    # refers to an id this ring has never seen, and correctly fails.
    recent = clips.Recent()
    uid = 0

    def new_id() -> int:
        """Next id on this connection. Utterance tasks call it too: a split
        utterance takes one id per sentence piece (sentences.py), so ids stay
        unique and increasing even though some are handed out after ASR."""
        nonlocal uid
        uid += 1
        return uid

    # Hold strong references to fire-and-forget tasks. asyncio only keeps weak
    # references, so without this a task could be garbage-collected mid-flight.
    pending: set[asyncio.Task] = set()

    # Reads leave in audio order (see _Turns), whichever task produced them.
    turns = _Turns()

    # Live sentence split. While an utterance is still open, re-transcribe it
    # every LIVE_SPLIT_EVERY_S of new audio; every sentence that is finished
    # (a later one has started) goes out at once as its own utterance and is
    # consumed from the front of the segmenter's buffer. Without this a stretch
    # of talk is read only when the segmenter closes it -- in a room whose
    # background keeps the VAD from hearing a pause, at the 15 s cap -- and then
    # all its sentences flash out together. Its tasks live in their own set, not
    # `pending`, so they never count against MAX_INFLIGHT_UTTERANCES and can
    # never cause a closing utterance to be dropped. At most one check runs at a
    # time, so the extra CPU is bounded at one ASR pass per LIVE_SPLIT_EVERY_S.
    live_tasks: set[asyncio.Task] = set()

    # Streaming ASR (server/streaming.py): every audio frame also goes to a
    # per-connection streaming recognizer, for two things. (1) Live words: a
    # `partial` frame with what is being said right now, opt-in per connection
    # (`/stream?partial=1`) so the pendant, which predates it, is not sent
    # several frames a second it would only discard. (2) Endpoints: when the
    # recognizer hears a sentence end, the live split hands out everything said
    # so far at once instead of waiting for its next look. Positions are counted
    # in samples received on this connection (`abs_received`), which is the
    # timeline both the recognizer and the segmenter see.
    partials = ws.query_params.get("partial") in ("1", "true", "yes")
    live_asr = streaming.Live() if streaming.available() else None
    asr_q: asyncio.Queue[bytes] = asyncio.Queue()
    abs_received = 0       # samples received on this connection
    emitted_until = 0      # samples already handed out as utterances / pieces
    last_tokens: list[tuple[str, int]] = []
    last_partial = None
    last_partial_t = 0.0
    force_split = False    # an endpoint arrived while a live split was busy
    force_until = 0        # absolute sample where that endpoint's sentence ended

    async def send_partial(force_send: bool = False) -> None:
        nonlocal last_partial, last_partial_t
        if not partials or live_asr is None:
            return
        text = streaming.text_after(last_tokens, emitted_until)
        now = time.monotonic()
        if text == last_partial or (not force_send and now - last_partial_t < config.PARTIAL_MIN_INTERVAL_S):
            return
        last_partial, last_partial_t = text, now
        await _send(ws, {"type": "partial", "text": text})

    async def stream_worker() -> None:
        """Feed queued audio to the streaming recognizer, one decode at a time."""
        nonlocal last_tokens, force_split, force_until
        loop = asyncio.get_running_loop()
        carry = b""
        while True:
            parts = [await asr_q.get()]
            while not asr_q.empty():          # catch up in one decode if we fell behind
                parts.append(asr_q.get_nowait())
            buf = carry + b"".join(parts)
            carry = buf[len(buf) & ~1:]       # keep an odd trailing byte for next time
            buf = buf[:len(buf) & ~1]
            if not buf:
                continue
            samples = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
            upd = await loop.run_in_executor(None, live_asr.feed, samples)
            if upd is None:
                continue
            last_tokens = upd.tokens
            if upd.endpoint and upd.text:
                metrics.record_event("stream_endpoint")
                if config.STREAM_ENDPOINT_CUT and segmenter.buffered_ms and upd.tokens:
                    # Cut where the recognizer heard the sentence end (its last
                    # token, plus a little for that word's tail), NOT at the end
                    # of whatever audio has arrived by now: the next sentence may
                    # already have started, and cutting into its first word made
                    # Parakeet drop the whole phrase that followed (measured).
                    force_until = upd.tokens[-1][1] + int(0.35 * config.SAMPLE_RATE)
                    force_split = True
                    maybe_live_split()
            await send_partial(force_send=upd.endpoint)
            if upd.endpoint:
                last_tokens = []              # the stream was reset; start fresh

    def maybe_live_split() -> None:
        """Start a live split if one is due (its cadence) or forced (an endpoint)."""
        nonlocal live_busy, live_seq, live_mark_ms, force_split
        if not (config.LIVE_SPLIT and config.SENTENCE_SPLIT) or live_busy:
            return
        if segmenter.utterance_seq != live_seq:
            live_seq, live_mark_ms = segmenter.utterance_seq, 0
        grown = segmenter.buffered_ms - live_mark_ms
        due = segmenter.buffered_ms and grown >= config.LIVE_SPLIT_EVERY_S * 1000
        if segmenter.buffered_ms and (due or force_split):
            open_audio = segmenter.peek()
            if open_audio is not None:
                live_busy = True
                forced, force_split = force_split, False
                # After a consume the buffer is shorter, so the next look is
                # measured from what is left.
                live_mark_ms = segmenter.buffered_ms
                _keep(asyncio.create_task(
                    live_split(segmenter.utterance_seq, open_audio, segmenter.speech_rms,
                               force=forced, end_abs=abs_received,
                               force_until=force_until if forced else 0)))
        elif grown < 0:
            live_mark_ms = segmenter.buffered_ms
        if not segmenter.buffered_ms:
            force_split = False
    live_busy = False
    live_seq = -1          # segmenter.utterance_seq the mark below belongs to
    live_mark_ms = 0       # buffered_ms at the last check of that utterance

    def _keep(task: asyncio.Task) -> None:
        live_tasks.add(task)
        task.add_done_callback(live_tasks.discard)
        task.add_done_callback(_log_task_error)

    async def live_split(seq: int, audio, speech_rms: float, force: bool = False,
                         end_abs: int = 0, force_until: int = 0) -> None:
        nonlocal live_busy, emitted_until
        start_abs = end_abs - int(audio.size)   # where the snapshot began
        try:
            if force:
                # Only the audio up to the endpoint is finished; anything after
                # it belongs to the next sentence and stays in the buffer.
                n = force_until - start_abs
                if n <= 0:
                    return
                audio = audio[:min(int(audio.size), n)]
            loop = asyncio.get_running_loop()
            cpu_t0 = time.perf_counter()
            (_text, _conf, tokens), asr_s = await loop.run_in_executor(
                None, _timed, transcriber.transcribe_timed, audio)
            # The utterance may have closed (and been handed to
            # _process_utterance whole) while ASR ran: then this result is stale
            # and must not be applied, or its sentences would go out twice.
            if not tokens or segmenter.utterance_seq != seq or segmenter.peek() is None:
                return
            kw = dict(min_s=config.SPLIT_MIN_S, min_words=config.SPLIT_MIN_WORDS,
                      max_pieces=config.SPLIT_MAX_PIECES, pad_s=config.SPLIT_PAD_S)
            sr = config.SAMPLE_RATE
            # Which sentences are finished? Two signals, both from the ASR:
            #   * a later sentence has started -> every piece but the last is done;
            #   * the last token ends a sentence and nothing has been said for
            #     LIVE_SPLIT_PAUSE_S since -> the last one is done too. This is
            #     the energy VAD's END_SILENCE rule judged by the ASR instead,
            #     which hears a pause the VAD misses when background noise keeps
            #     the RMS above threshold.
            last_tok = tokens[-1]
            # `force`: the streaming recognizer saw the sentence end (endpoint).
            paused = force or (last_tok.text.strip().endswith((".", "?", "!"))
                      and audio.size / sr - last_tok.t >= config.LIVE_SPLIT_PAUSE_S)
            if paused:
                done = sentences.split(tokens, int(audio.size), sr, **kw)
                if not done:
                    return
                # The last piece would otherwise run to the end of the buffer,
                # handing SER the pause as well.
                end = min(done[-1].end, int((last_tok.t + 0.2 + config.SPLIT_PAD_S) * sr))
                done[-1] = dataclasses.replace(done[-1], end=max(end, done[-1].start + 1))
                cut = done[-1].end
            else:
                pieces = sentences.split(tokens, int(audio.size), sr, hold_last=True, **kw)
                if len(pieces) < 2:
                    return
                done, cut = pieces[:-1], pieces[-1].start
            # Hand them out and drop them from the open utterance --
            # synchronously, on the event loop, so no frame can arrive between
            # the staleness check above and the consume.
            before_ms = segmenter.buffered_ms
            dropped = segmenter.consume(cut)
            # DEBUG only: it carries transcript text, which the INFO log never does.
            log.debug("live split: force=%s paused=%s snapshot=%.2fs cut=%.2fs dropped=%.2fs "
                      "buffer %dms -> %dms; pieces=%s", force, paused, audio.size / sr, cut / sr,
                      dropped / sr, before_ms, segmenter.buffered_ms, [pc.text for pc in done])
            # Everything up to the cut has now been handed out; the live
            # transcript should show only what comes after it.
            emitted_until = max(emitted_until, start_abs + int(cut))
            await send_partial(force_send=True)
            ids = [new_id() for _ in done]
            for pid, pc in zip(ids, done):
                recent.add(pid, audio[pc.start:pc.end])
            ticket = turns.ticket()
            metrics.record_event("live_split", len(done))

            async def emit() -> None:
                try:
                    await _emit_pieces(ws, interpreter, recent, session, done, ids, audio,
                                       tokens, None, 0.0, asr_s, cpu_t0, speech_rms,
                                       parent=ids[0], per_piece_speaker=True,
                                       gate=lambda: turns.wait(ticket))
                finally:
                    await turns.done(ticket)

            _keep(asyncio.create_task(emit()))
        finally:
            live_busy = False
            if force_split:
                maybe_live_split()

    # Idle keepalive. Cloudflare closes a proxied websocket after ~100s with no
    # traffic in either direction, and a user who opens the page but does not
    # press Start sends nothing at all. uvicorn's own protocol pings (every 20s)
    # normally cover this, but they are a server setting a proxy or a future
    # ESP32 stack may not honour -- so send an application-level frame too,
    # which is visible to the client and to any diagnostic page.
    #
    # This lives in its own task rather than as a timeout on ws.receive(): the
    # receive loop is the audio hot path and must not be restructured around
    # something that only matters when nothing is happening.
    last_audio = time.monotonic()

    # Opt-in per connection: /stream?vad=1. Diagnostics are for the diagnostic
    # page, not for every client and certainly not for the ESP32.
    vad_telemetry = ws.query_params.get("vad") in ("1", "true", "yes")
    discarded_seen = 0
    last_vad_sent = 0.0

    async def keepalive() -> None:
        while True:
            await asyncio.sleep(config.WS_KEEPALIVE_CHECK_S)
            idle = time.monotonic() - last_audio
            # Ping to hold the connection open through Cloudflare...
            if idle >= config.WS_IDLE_PING_S:
                await _send(ws, {"type": "ping", "t": time.time()})
            # ...but eventually let it go. Pinging forever meant an idle socket
            # was never reclaimed: a post-auth client could hold connections at
            # zero cost, and the server helpfully kept them alive. A real
            # listener sends audio; silence for this long is an abandoned tab.
            if config.WS_IDLE_CLOSE_S > 0 and idle >= config.WS_IDLE_CLOSE_S:
                log.info("closing idle /stream connection (%.0fs without audio)", idle)
                await _close_quietly(ws, code=1000)
                return

    keepalive_task = asyncio.create_task(keepalive())

    async def on_closed(audio) -> None:
        """One utterance the segmenter just closed: hand it to the pipeline."""
        nonlocal uid, emitted_until
        emitted_until = abs_received
        # Backpressure. Whisper and SER both serialise process-wide, so
        # the pipeline drains ~3 utterances/sec no matter how many
        # arrive; without a bound a client can queue work far faster
        # than that and the backlog IS retained audio. Conversational
        # speech produces about one utterance a second, so a real
        # client never reaches this. Drop rather than queue: losing a
        # word beats an unbounded queue that ends as an OOM.
        if len(pending) >= config.MAX_INFLIGHT_UTTERANCES:
            metrics.record_event("utterance_dropped")
            return
        uid = new_id()
        metrics.client_seen(client_id, utterance=True)
        recent.add(uid, audio)
        await _send(ws, {"type": "status", "state": "heard"})
        if vad_telemetry:
            # Why this utterance closed, and how much of it was actually
            # voiced. "end_silence" on a short voiced_ms is the signature
            # of a sentence being split at a soft consonant; "client_gap"
            # means the client stopped sending (its own gate closed).
            await _send(ws, {
                "type": "vad_close",
                "id": uid,
                "reason": segmenter.last_close_reason,
                "voiced_ms": segmenter.last_voiced_ms,
                "utterance_ms": segmenter.last_utterance_ms,
                "peak_rms": round(segmenter.peak_rms, 1),
            })
        segmenter.peak_rms = 0.0   # peak is per-utterance, so reset it
        task = asyncio.create_task(
            _process_utterance(ws, interpreter, recent, session, uid, audio, new_id,
                                segmenter.speech_rms, turns, turns.ticket())
        )
        pending.add(task)
        task.add_done_callback(pending.discard)
        task.add_done_callback(_log_task_error)
        await send_partial(force_send=True)

    async def gap_watch() -> None:
        """Close an open utterance when the CLIENT stops sending (see Segmenter.idle).

        The pendant gates its own audio and goes quiet 500 ms after the last
        voiced chunk, which is less than END_SILENCE_MS, so without this its
        utterances closed only when the next speech arrived or at the 15 s cap.
        A gap counts only once it exceeds CLIENT_GAP_MS, so ordinary network
        jitter between frames (bursts over Wi-Fi/TLS) is never mistaken for a
        pause. Continuous clients (the browser) never produce such a gap.
        """
        seen = last_audio
        credited = 0.0          # ms of the current gap already given to the segmenter
        while True:
            await asyncio.sleep(0.1)
            if last_audio != seen:
                seen, credited = last_audio, 0.0
            if config.CLIENT_GAP_MS <= 0 or not segmenter.buffered_ms:
                continue
            gap_ms = (time.monotonic() - last_audio) * 1000.0
            if gap_ms < config.CLIENT_GAP_MS:
                continue
            audio = segmenter.idle(gap_ms - credited)
            credited = gap_ms
            if audio is not None:
                metrics.record_event("client_gap_close")
                await on_closed(audio)

    gap_task = asyncio.create_task(gap_watch())
    asr_task = asyncio.create_task(stream_worker()) if live_asr is not None else None

    try:
        while True:
            message = await ws.receive()

            if message.get("type") == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data is None:
                # TEXT frame after the handshake. The protocol expects binary
                # PCM from here on, with two exceptions: {"type":"ping"} to
                # measure round-trip latency and hold an idle connection open,
                # and {"type":"save",...} to keep a clip. Anything else is
                # ignored, as before.
                text = message.get("text")
                if text:
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    kind = msg.get("type")
                    if kind == "ping":
                        # Echo the client's timestamp back untouched so it can
                        # compute round-trip time without a synchronised clock.
                        await _send(ws, {"type": "pong", "t": msg.get("t")})
                    elif kind == "save":
                        # {"type":"save","from":a,"to":b} -- the pendant's
                        # hold-to-record (README.md, wire protocol): every utterance heard
                        # while the button was held, bundled into ONE clip.
                        # The older {"id":n} form is accepted as from == to,
                        # which is what the browser client's "save" link is.
                        # Reply with `saved` either way; a failure is
                        # ok:false, never a closed socket. The disk write goes
                        # to the executor as its own task so this loop gets
                        # straight back to audio.
                        if "from" in msg or "to" in msg:
                            lo, hi = _wire_int(msg.get("from")), _wire_int(msg.get("to"))
                        else:
                            lo = hi = _wire_int(msg.get("id"))
                        if lo is None or hi is None or hi < lo:
                            metrics.record_event("clip_failed")
                            await _send(ws, {"type": "saved", "id": hi,
                                             "ok": False, "error": "unknown id"})
                            continue
                        entries = recent.get_range(lo, hi)
                        if not entries:
                            # Nothing from that span is still retained: a
                            # stale id after a reconnect, or a hold that ended
                            # before the pipeline produced anything.
                            metrics.record_event("clip_failed")
                            await _send(ws, {"type": "saved", "id": hi,
                                             "ok": False, "error": "no audio"})
                        else:
                            task = asyncio.create_task(_save_clip(ws, entries, hi))
                            pending.add(task)
                            task.add_done_callback(pending.discard)
                            task.add_done_callback(_log_task_error)
                    # Any other type is ignored: additive protocol, unknown
                    # frames are never fatal.
                continue

            last_audio = time.monotonic()
            abs_received += len(data) // 2
            if live_asr is not None:
                asr_q.put_nowait(data)
            # Lock-free by design -- see metrics.client_seen. One dict write per
            # frame, so the read loop is not made to wait on telemetry.
            metrics.client_seen(client_id, audio_bytes=len(data))

            # --- VAD telemetry -------------------------------------------
            # Throttled to ~10/s: enough to watch a level meter move, cheap
            # enough to ignore. Opt-in via ?vad=1 so the normal client and the
            # ESP32 never pay for it. Purely additive -- a client that does not
            # ask never sees these frames, and one that does not understand
            # them ignores an unknown `type`, per the protocol rule.
            if vad_telemetry:
                now = time.monotonic()
                if now - last_vad_sent >= config.VAD_TELEMETRY_INTERVAL_S:
                    last_vad_sent = now
                    await _send(ws, {
                        "type": "vad",
                        "rms": round(segmenter.last_rms, 1),
                        "peak_rms": round(segmenter.peak_rms, 1),
                        "threshold": segmenter.speech_rms,
                        # The room, as the adaptive VAD currently hears it.
                        "noise_floor": round(segmenter.noise_floor, 1),
                        "in_speech": segmenter._in_speech,
                        "trailing_silence_ms": segmenter._trailing_silence_ms,
                        "voiced_ms": segmenter._voiced_ms,
                        "frames": segmenter.frames_seen,
                        "voiced_frames": segmenter.voiced_frames_seen,
                        "discarded": segmenter.discarded,
                        "last_close_reason": segmenter.last_close_reason,
                        "end_silence_ms": segmenter.end_silence_ms,
                        "min_utterance_ms": segmenter.min_utterance_ms,
                    })

            # Hot path: just segment. This returns instantly for the common
            # case (mid-utterance) and occasionally hands back finished audio.
            for audio in segmenter.feed(data):
                await on_closed(audio)

            # Live sentence split: is the open utterance long enough since the
            # last look to be worth another ASR pass?
            maybe_live_split()

            # After the loop, not inside it: a discard produces no utterance, so
            # it is counted here where it cannot be confused with one.
            if segmenter.discarded != discarded_seen:
                metrics.record_event("vad_discarded",
                                     segmenter.discarded - discarded_seen)
                discarded_seen = segmenter.discarded
    except WebSocketDisconnect:
        pass
    finally:
        # Release the accounting and the background work FIRST. This used to run
        # after the flush below, which meant a disconnected client still held a
        # keepalive task, its pending set, and a connections_active slot for as
        # long as the flush took -- up to a 30s Ollama call. Repeating that on a
        # loop was a cheap way to accumulate zombie pipelines.
        metrics.connection_closed(client_id)
        keepalive_task.cancel()
        gap_task.cancel()
        if asr_task is not None:
            asr_task.cancel()
        for task in pending | live_tasks:
            task.cancel()

        # Flush a trailing utterance if the client dropped mid-sentence. Worth
        # doing -- a real client that drops mid-sentence still wants the read --
        # but time-bounded, because nobody is waiting on the other end if the
        # socket is already gone.
        tail = segmenter.flush()
        if tail is not None:
            uid = new_id()
            try:
                recent.add(uid, tail)
                # No turns here: every other task was just cancelled, and waiting
                # on their tickets would only burn the timeout.
                await asyncio.wait_for(
                    _process_utterance(ws, interpreter, recent, session, uid, tail, new_id,
                                       segmenter.speech_rms),
                    timeout=config.WS_FLUSH_TIMEOUT_S,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                log.info("dropped trailing utterance after disconnect (timeout)")


@app.get("/health")
async def health() -> dict:
    """Liveness probe, and the first thing to check through a tunnel.

    Deliberately trivial and unauthenticated: it answers "is the local server up
    and is the tunnel routing to it" without revealing anything. A 502 from
    Cloudflare means the tunnel is up but this is not; a JSON body means the
    whole path works and any further problem is in the websocket layer.
    """
    return {"status": "ok"}


# Serve the browser test client at "/". Mounting static last means the API
# routes above take precedence over the file server.
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=_STATIC_DIR), name="static")


def main() -> None:
    """Entry point for `python -m server.main`."""
    import uvicorn

    # uvicorn configures its own loggers but leaves the root logger at WARNING,
    # which would hide our own INFO lines -- including the one saying whether
    # SER actually loaded. Set it here so that startup state is visible.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:     %(name)s: %(message)s",
    )

    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        # Every one of these is unbounded or permissive by default, and this
        # server is reachable from the internet. None of them constrains a
        # conforming client -- see the "Exposure limits" block in config.py.
        ws_max_size=config.WS_MAX_MESSAGE_BYTES,
        # permessage-deflate compresses PCM ~21:1, and the payload is chosen by
        # the sender. That turned ~1.6 KB on the wire into one full pipeline
        # job. The real client streams raw PCM continuously, so compression
        # buys it little and buys an attacker a great deal.
        ws_per_message_deflate=False,
        limit_concurrency=config.MAX_CONNECTIONS,
        backlog=64,
        # The access log records the query string, and the token travels in it
        # (?token=... on every dashboard poll, ~2000 lines/hour). The start
        # scripts redirect this to a file that is only cleared on next launch,
        # so the credential outlived the process in cleartext.
        access_log=False,
    )


if __name__ == "__main__":
    main()
