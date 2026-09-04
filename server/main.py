"""FastAPI app + websocket route. Wiring only -- no signal logic lives here.

The one interesting thing in this file is the *concurrency shape*, because it
is what keeps the conversation feeling live:

  - Audio arrives continuously and must never be made to wait. So the websocket
    read loop does the absolute minimum per frame: hand bytes to the segmenter.
  - Transcription (Whisper) is a blocking CPU call. Running it inline would
    freeze the read loop and back up the audio buffer. Instead we push it to a
    thread executor via loop.run_in_executor.
  - Speech emotion recognition (SER) is a second blocking CPU call over the same
    audio, and is independent of transcription. So the two are gathered rather
    than sequenced: one utterance costs about max(whisper, ser), not their sum.
  - Interpretation (the LLM) is slow too. We wrap the whole
    transcribe->interpret->send sequence in a fire-and-forget asyncio task, one
    per utterance. The read loop kicks it off and immediately goes back to
    reading audio, so we can already be listening to (and even transcribing)
    the next sentence while still "thinking" about the last one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, ser
from .interpreter import Interpreter
from .segmenter import Segmenter
from .transcriber import transcribe

log = logging.getLogger(__name__)

app = FastAPI(title="emtext")

_STATIC_DIR = Path(__file__).parent / "static"

# A single shared HTTP client for all Ollama calls (pooled, reused).
_http: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _http
    _http = httpx.AsyncClient()

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


async def _process_utterance(
    ws: WebSocket,
    interpreter: Interpreter,
    uid: int,
    audio,
) -> None:
    """Transcribe then interpret one utterance and stream the results out.

    Runs as its own asyncio task, so multiple utterances can be in flight at
    once. Order between them is best-effort, not guaranteed -- each frame we
    send carries its own `id` so the client can correlate transcript and read.
    """
    loop = asyncio.get_running_loop()

    # Whisper (what was said) and SER (how it sounded) are both blocking CPU
    # work, and both read the same immutable audio buffer without touching each
    # other's state. So we run them as two executor jobs and gather them: the
    # voice analysis overlaps with transcription instead of following it, and
    # the utterance costs about max(whisper, ser) rather than their sum.
    #
    # SER is the optional half. `ser.analyze` returns None when the model is
    # disabled or failed to load, and its own errors are swallowed internally,
    # so this gather cannot fail because of it.
    transcript, voice = await asyncio.gather(
        loop.run_in_executor(None, transcribe, audio),
        loop.run_in_executor(None, ser.analyze, audio),
    )
    if not transcript:
        return  # whisper heard nothing usable; stay quiet

    await _send(ws, {"type": "utterance", "id": uid, "transcript": transcript})
    await _send(ws, {"type": "status", "state": "thinking"})

    result = await interpreter.interpret(transcript, voice)

    message = {
        "type": "read",
        "id": uid,
        "tone": result["tone"],
        "read": result["read"],
    }
    if voice:
        # Optional field, added only when we actually have data: clients that
        # predate SER (and firmware that never implements it) just ignore an
        # unknown key, so the wire protocol stays backward compatible.
        message["voice"] = {
            "emotion": voice.get("emotion"),
            "valence": voice.get("valence"),
            "arousal": voice.get("arousal"),
        }
    await _send(ws, message)


@app.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    await ws.accept()

    # --- handshake: first frame is the auth token (TEXT) --------------------
    try:
        token = await ws.receive_text()
    except WebSocketDisconnect:
        return
    if config.AUTH_TOKEN is not None and token != config.AUTH_TOKEN:
        await ws.close(code=1008)  # policy violation
        return

    await _send(ws, {"type": "ready"})
    await _send(ws, {"type": "status", "state": "listening"})

    # Per-connection state: its own segmenter and its own interpreter (so the
    # rolling context belongs to this conversation only).
    assert _http is not None
    segmenter = Segmenter()
    interpreter = Interpreter(_http)
    uid = 0

    # Hold strong references to fire-and-forget tasks. asyncio only keeps weak
    # references, so without this a task could be garbage-collected mid-flight.
    pending: set[asyncio.Task] = set()

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
    last_vad_sent = 0.0

    async def keepalive() -> None:
        while True:
            await asyncio.sleep(config.WS_KEEPALIVE_CHECK_S)
            if time.monotonic() - last_audio >= config.WS_IDLE_PING_S:
                await _send(ws, {"type": "ping", "t": time.time()})

    keepalive_task = asyncio.create_task(keepalive())

    try:
        while True:
            message = await ws.receive()

            if message.get("type") == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data is None:
                # TEXT frame after the handshake. The protocol expects binary
                # PCM from here on, with one exception: a client may send
                # {"type":"ping"} to measure round-trip latency and to hold an
                # idle connection open. Anything else is ignored, as before.
                text = message.get("text")
                if text:
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("type") == "ping":
                        # Echo the client's timestamp back untouched so it can
                        # compute round-trip time without a synchronised clock.
                        await _send(ws, {"type": "pong", "t": msg.get("t")})
                continue

            last_audio = time.monotonic()

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
                uid += 1
                await _send(ws, {"type": "status", "state": "heard"})
                if vad_telemetry:
                    # Why this utterance closed, and how much of it was actually
                    # voiced. "end_silence" on a short voiced_ms is the signature
                    # of a sentence being split at a soft consonant.
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
                    _process_utterance(ws, interpreter, uid, audio)
                )
                pending.add(task)
                task.add_done_callback(pending.discard)
    except WebSocketDisconnect:
        pass
    finally:
        # Flush a trailing utterance if the client dropped mid-sentence.
        tail = segmenter.flush()
        if tail is not None:
            uid += 1
            await _process_utterance(ws, interpreter, uid, tail)
        keepalive_task.cancel()
        for task in pending:
            task.cancel()


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

    uvicorn.run(app, host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
