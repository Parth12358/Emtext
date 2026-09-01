"""FastAPI app + websocket route. Wiring only -- no signal logic lives here.

The one interesting thing in this file is the *concurrency shape*, because it
is what keeps the conversation feeling live:

  - Audio arrives continuously and must never be made to wait. So the websocket
    read loop does the absolute minimum per frame: hand bytes to the segmenter.
  - Transcription (Whisper) is a blocking CPU call. Running it inline would
    freeze the read loop and back up the audio buffer. Instead we push it to a
    thread executor via loop.run_in_executor.
  - Interpretation (the LLM) is slow too. We wrap the whole
    transcribe->interpret->send sequence in a fire-and-forget asyncio task, one
    per utterance. The read loop kicks it off and immediately goes back to
    reading audio, so we can already be listening to (and even transcribing)
    the next sentence while still "thinking" about the last one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .interpreter import Interpreter
from .segmenter import Segmenter
from .transcriber import transcribe

app = FastAPI(title="emtext")

_STATIC_DIR = Path(__file__).parent / "static"

# A single shared HTTP client for all Ollama calls (pooled, reused).
_http: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _http
    _http = httpx.AsyncClient()


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

    # Whisper is blocking CPU work -> run it in the default thread pool so this
    # coroutine yields the event loop while it decodes.
    transcript = await loop.run_in_executor(None, transcribe, audio)
    if not transcript:
        return  # whisper heard nothing usable; stay quiet

    await _send(ws, {"type": "utterance", "id": uid, "transcript": transcript})
    await _send(ws, {"type": "status", "state": "thinking"})

    result = await interpreter.interpret(transcript)
    await _send(ws, {
        "type": "read",
        "id": uid,
        "tone": result["tone"],
        "read": result["read"],
    })


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

    try:
        while True:
            message = await ws.receive()

            if message.get("type") == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data is None:
                # A stray TEXT frame after the handshake -- ignore it. The
                # protocol only expects binary PCM from here on.
                continue

            # Hot path: just segment. This returns instantly for the common
            # case (mid-utterance) and occasionally hands back finished audio.
            for audio in segmenter.feed(data):
                uid += 1
                await _send(ws, {"type": "status", "state": "heard"})
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
        for task in pending:
            task.cancel()


# Serve the browser test client at "/". Mounting static last means the API
# routes above take precedence over the file server.
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=_STATIC_DIR), name="static")


def main() -> None:
    """Entry point for `python -m server.main`."""
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
