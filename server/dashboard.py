"""Diagnostics and control API behind the dashboard, clips and enrol pages.

Kept out of `main.py` so that file stays what it says it is -- websocket wiring.
Everything here is read-only observation plus a handful of deliberate write
actions: select or evict an Ollama model, delete a saved clip, and enrol or
forget the listener's voice profile. None of it touches the live `/stream`
audio path -- `POST /api/enroll` does run the segmenter and speaker embedder,
but on an uploaded file in the executor, and the only shared state it changes
is the module-level profile that `speaker.identify` reads.

**Auth.** These routes are token-gated, matching `/stream`: when `AUTH_TOKEN` is
set it must be supplied, and when it is unset everything is open. That is the
same contract the websocket already has, so there is one rule to remember rather
than two. It matters more here than there, though -- `/stream` only lets a
stranger burn CPU, while `POST /api/model` lets them change which model the
server runs. The server warns loudly at startup when the token is unset, and
`main.py` now refuses to start that way on a non-loopback bind.

The token may arrive as an `X-Auth-Token` header (what the dashboard sends, so
the credential stays out of URLs, access logs and browser history) or as
`?token=`, which remains supported for pasting a link by hand.
"""

from __future__ import annotations

import asyncio
import io
import logging
import secrets
import time
import wave
from pathlib import Path

import httpx
import numpy as np
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import clips, config, metrics, ser, speaker, transcriber, translog, tuning
from .interpreter import load_options
from .segmenter import Segmenter

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["diagnostics"])


def _require_token(token: str | None, header_token: str | None) -> None:
    """Match /stream's rule exactly: enforced only when AUTH_TOKEN is set.

    Either credential is sufficient -- the header is preferred (the dashboard
    sends it that way so the token stays out of URLs, access logs and history),
    and `?token=` remains for pasting a link by hand.
    """
    if config.AUTH_TOKEN is None:
        return
    if not (_matches(header_token) or _matches(token)):
        raise HTTPException(status_code=401, detail="bad or missing token")


def _matches(supplied: str | None) -> bool:
    """Constant-time compare against AUTH_TOKEN. See main.py's `_token_ok`."""
    if supplied is None or config.AUTH_TOKEN is None:
        return False
    return secrets.compare_digest(
        supplied.encode("utf-8"), config.AUTH_TOKEN.encode("utf-8")
    )


# Serialise and rate-limit the two routes that move models in and out of VRAM.
# Module-level, so the limit is per-server rather than per-request: two clients
# alternating between models would otherwise thrash 8-9 GB back and forth with
# ~200 bytes per request, and concurrent calls would race on config.OLLAMA_MODEL.
_vram_lock = asyncio.Lock()
_last_vram_op = 0.0


async def _vram_gate() -> None:
    """Refuse a model load/evict that arrives too soon after the last one."""
    global _last_vram_op
    since = time.monotonic() - _last_vram_op
    if since < config.MODEL_SWITCH_COOLDOWN_S:
        raise HTTPException(
            status_code=429,
            detail=f"model changed {since:.1f}s ago; wait "
                   f"{config.MODEL_SWITCH_COOLDOWN_S - since:.1f}s",
        )
    _last_vram_op = time.monotonic()


async def _require_pulled(client: httpx.AsyncClient, model: str) -> None:
    """Reject any model name that is not already pulled.

    Both write routes take a model name from the request body and hand it to
    Ollama, which has no auth of its own. Restricting them to names Ollama
    already reports keeps an arbitrary attacker-supplied string out of that
    call, and out of the log line that follows it -- an unvalidated name can
    carry newlines and forge log entries.
    """
    tags = await _ollama(client, "/api/tags")
    if "_error" in tags:
        raise HTTPException(status_code=503, detail=f"Ollama unreachable: {tags['_error']}")
    available = {m.get("name") for m in tags.get("models", []) or []}
    if model not in available:
        raise HTTPException(
            status_code=400,
            detail=f"{model} is not pulled. Available: {sorted(available)}",
        )


async def _ollama(client: httpx.AsyncClient, path: str, **kw) -> dict:
    """Call Ollama, converting unreachability into a dict rather than a 500.

    Ollama being down is a *state the dashboard exists to show*, not an error
    that should blank the page.
    """
    try:
        if kw.get("json") is not None:
            resp = await client.post(f"{config.OLLAMA_URL}{path}", timeout=30, **kw)
        else:
            resp = await client.get(f"{config.OLLAMA_URL}{path}", timeout=10, **kw)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


@router.get("/stats")
async def stats(request: Request,
                token: str | None = Query(None),
                x_auth_token: str | None = Header(None)) -> dict:
    """Everything the dashboard polls: pipeline, system, GPU, models, config."""
    _require_token(token, x_auth_token)
    # Someone is watching: keep the (expensive) GPU poller running. Returns at
    # once; this response serves whatever sample is cached.
    metrics.gpu_wanted()

    client: httpx.AsyncClient = request.app.state.http
    ps = await _ollama(client, "/api/ps")

    loaded = []
    for m in ps.get("models", []) or []:
        size, vram = m.get("size", 0), m.get("size_vram", 0)
        loaded.append({
            "name": m.get("name"),
            "size_gb": round(size / 1e9, 2),
            "vram_gb": round(vram / 1e9, 2),
            # The distinction that matters: a model only partly in VRAM is
            # running partly on CPU, which looks like "this model is slow"
            # rather than "this model did not fit".
            "fully_on_gpu": bool(vram and vram >= size),
            "expires_at": m.get("expires_at"),
        })

    return {
        "now": time.time(),
        "pipeline": metrics.pipeline_stats(),
        "system": metrics.system_stats(),
        "gpu": metrics.gpu_stats(),
        "history": metrics.history(),
        "ollama": {
            "url": config.OLLAMA_URL,
            "reachable": "_error" not in ps,
            "error": ps.get("_error"),
            "loaded": loaded,
        },
        "config": {
            "llm_model": config.OLLAMA_MODEL,
            # ASR is a catalogue key now (server/transcriber.py). whisper_* stay
            # for the whisper family; asr_* are what the dashboard reads.
            "asr_model": transcriber.current_key(),
            "asr_family": transcriber.current_family(),
            "asr_state": transcriber.status().get("state"),
            "whisper_model": config.WHISPER_MODEL,
            "whisper_device": config.WHISPER_DEVICE,
            "whisper_compute": config.WHISPER_COMPUTE_TYPE,
            "ser_model": config.SER_MODEL if ser.available() else None,
            "ser_backend": getattr(ser, "_backend", None),
            "ser_available": ser.available(),
            "speaker_available": speaker.available(),
            "speaker_enrolled": speaker.profile.count,
            "speaker_min_enroll": config.SPEAKER_MIN_ENROLL,
            "speaker_ready": speaker.available() and speaker.profile.ready(),
            # The running transcript log (server/translog.py): what is being
            # retained and how much, so nobody is surprised by it later.
            "transcript_log": translog.stats(),
            "speech_rms": config.SPEECH_RMS,
            "end_silence_ms": config.END_SILENCE_MS,
            "min_utterance_ms": config.MIN_UTTERANCE_MS,
            "auth_enabled": config.AUTH_TOKEN is not None,
            "num_predict": config.OLLAMA_NUM_PREDICT,
            "temperature": config.OLLAMA_TEMPERATURE,
        },
    }


@router.get("/models")
async def models(request: Request,
                 token: str | None = Query(None),
                 x_auth_token: str | None = Header(None)) -> dict:
    """Everything Ollama has pulled, plus which is currently selected."""
    _require_token(token, x_auth_token)
    client: httpx.AsyncClient = request.app.state.http
    tags = await _ollama(client, "/api/tags")
    if "_error" in tags:
        return {"reachable": False, "error": tags["_error"], "models": [],
                "selected": config.OLLAMA_MODEL}
    out = []
    for m in tags.get("models", []) or []:
        details = m.get("details") or {}
        out.append({
            "name": m.get("name"),
            "size_gb": round((m.get("size") or 0) / 1e9, 2),
            "family": details.get("family"),
            "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            "note": config.MODEL_NOTES.get(m.get("name")),
        })
    out.sort(key=lambda m: m["size_gb"])
    return {"reachable": True, "models": out, "selected": config.OLLAMA_MODEL}


class SelectRequest(BaseModel):
    model: str
    warm: bool = True


@router.post("/model")
async def select_model(body: SelectRequest, request: Request,
                       token: str | None = Query(None),
                       x_auth_token: str | None = Header(None)) -> dict:
    """Switch the interpreter's model at runtime.

    This works because `Interpreter.interpret()` reads `config.OLLAMA_MODEL` at
    call time rather than caching it -- the same property `eval/model_eval.py`
    relies on to A/B models. So the change takes effect on the next utterance,
    with no restart and without disturbing any live websocket.

    It is deliberately NOT persisted: a restart returns to the configured
    default. A dashboard toggle that silently rewrote config would be a
    surprising thing to discover later.
    """
    _require_token(token, x_auth_token)
    client: httpx.AsyncClient = request.app.state.http

    async with _vram_lock:
        await _vram_gate()
        await _require_pulled(client, body.model)

    previous = config.OLLAMA_MODEL
    config.OLLAMA_MODEL = body.model
    log.info("dashboard: interpreter model %s -> %s", previous, body.model)

    warmed = None
    if body.warm and previous != body.model:
        # Evict the old one first. The Arc B580 has 12GB and two 8-9GB models do
        # not fit; without this Ollama may keep part of the new model on CPU,
        # which reads as "this model is slow" rather than "it did not fit".
        await _ollama(client, "/api/generate",
                      json={"model": previous, "keep_alive": 0, "prompt": "",
                            "stream": False, "options": load_options()})
        t0 = time.perf_counter()
        warm = await _ollama(client, "/api/generate",
                             json={"model": body.model, "prompt": "hi",
                                   "stream": False, "keep_alive": -1,
                                   # same load options as the interpreter, or
                                   # its first read reloads what we just warmed
                                   "options": load_options()})
        warmed = round(time.perf_counter() - t0, 2)
        if "_error" in warm:
            log.warning("dashboard: warm-up of %s failed: %s", body.model, warm["_error"])

    return {"selected": config.OLLAMA_MODEL, "previous": previous,
            "warmed_s": warmed, "persisted": False}


class EvictRequest(BaseModel):
    model: str


@router.post("/evict")
async def evict(body: EvictRequest, request: Request,
                token: str | None = Query(None),
                x_auth_token: str | None = Header(None)) -> dict:
    """Unload a model from VRAM.

    `interpreter.py` pins models with `keep_alive: -1`, so nothing releases them
    on its own -- a finished session leaves 8-9GB occupied indefinitely. This is
    the button that gives the GPU back without restarting Ollama.
    """
    _require_token(token, x_auth_token)
    client: httpx.AsyncClient = request.app.state.http
    # Same allowlist as POST /api/model. This route used to pass the body
    # straight through, so it accepted any string -- which made it both an
    # existence oracle for arbitrary model names and a log-forging primitive.
    async with _vram_lock:
        await _vram_gate()
        await _require_pulled(client, body.model)
    result = await _ollama(client, "/api/generate",
                           json={"model": body.model, "keep_alive": 0,
                                 "prompt": "", "stream": False,
                                 "options": load_options()})
    if "_error" in result:
        raise HTTPException(status_code=503, detail=result["_error"])
    log.info("dashboard: evicted %s from VRAM", body.model)
    return {"evicted": body.model}


# ---------------------------------------------------------------------------
# Clips -- the review surface for moments the listener saved (README.md, wire protocol)
# ---------------------------------------------------------------------------
# Same token rule as everything else. The audio route matters most: it serves
# retained third-party speech, so it must never be more open than /stream is.
# `clips.py` validates the id's shape before touching the filesystem, which is
# what keeps a URL path segment from becoming a path traversal.

@router.get("/clips")
async def list_clips(token: str | None = Query(None),
                     x_auth_token: str | None = Header(None)) -> dict:
    """Every saved clip, newest first."""
    _require_token(token, x_auth_token)
    # A directory scan plus one small JSON read per clip. Off the loop anyway:
    # the audio path shares this process and must not wait on a slow disk.
    loop = asyncio.get_running_loop()
    items = await loop.run_in_executor(None, clips.list_clips)
    return {
        "clips": items,
        "dir": str(Path(config.CLIPS_DIR).resolve()),
        "max": config.CLIPS_MAX_FILES,
        "enabled": config.CLIPS_ENABLED,
    }


@router.get("/clips/{clip_id}/audio")
async def clip_audio(clip_id: str,
                     token: str | None = Query(None),
                     x_auth_token: str | None = Header(None)) -> FileResponse:
    """The clip's WAV. 404 for a malformed id as well as a missing file."""
    _require_token(token, x_auth_token)
    path = clips.clip_path(clip_id, "wav")
    if path is None:
        raise HTTPException(status_code=404, detail="no such clip")
    return FileResponse(path, media_type="audio/wav", filename=f"{clip_id}.wav")


@router.delete("/clips/{clip_id}")
async def delete_clip(clip_id: str,
                      token: str | None = Query(None),
                      x_auth_token: str | None = Header(None)) -> dict:
    """Remove one clip (wav + sidecar). The user's call, from the dashboard."""
    _require_token(token, x_auth_token)
    loop = asyncio.get_running_loop()
    removed = await loop.run_in_executor(None, clips.delete_clip, clip_id)
    if not removed:
        raise HTTPException(status_code=404, detail="no such clip")
    log.info("dashboard: deleted clip %s", clip_id)
    return {"deleted": clip_id}


# ---------------------------------------------------------------------------
# Speaker profile -- the listener's own voiceprint (server/speaker.py)
# ---------------------------------------------------------------------------
# Same token rule as everything else. The profile is biometric data, so the
# surface is deliberately small: see it, add to it, delete it. Nothing here
# ever returns the embeddings themselves, and nothing stores audio.

def _speaker_state() -> dict:
    return {
        "available": speaker.available(),
        "enabled": config.SPEAKER_ENABLED,
        "model": config.SPEAKER_MODEL,
        "enrolled": speaker.profile.count,
        "min_enroll": config.SPEAKER_MIN_ENROLL,
        "ready": speaker.available() and speaker.profile.ready(),
        "updated_at": speaker.profile.updated_at,
        "path": str(speaker.profile.path),
    }


@router.get("/speaker")
async def speaker_state(token: str | None = Query(None),
                        x_auth_token: str | None = Header(None)) -> dict:
    """Is speaker id running, and how much of the listener's voice is enrolled."""
    _require_token(token, x_auth_token)
    return _speaker_state()


def _pcm_from_upload(body: bytes, content_type: str) -> np.ndarray:
    """Decode the enrol upload to float32 16 kHz mono, or raise ValueError.

    Accepts a WAV (any of the common flavours the browser produces from raw
    PCM) or raw int16 LE 16 kHz mono when the client says so. Anything else
    is a 400: this route is not a transcoder.
    """
    if content_type.startswith("audio/pcm") or content_type == "application/octet-stream":
        pcm = np.frombuffer(body[: len(body) - (len(body) % 2)], dtype="<i2")
        return pcm.astype(np.float32) / 32768.0
    try:
        with wave.open(io.BytesIO(body), "rb") as w:
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"not a PCM WAV: {exc}") from None
    if width != 2:
        raise ValueError("expected 16-bit PCM")
    if rate != config.SAMPLE_RATE:
        raise ValueError(f"expected {config.SAMPLE_RATE} Hz, got {rate}")
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(audio, dtype=np.float32)


def _enrol_blocking(audio: np.ndarray) -> tuple[int, int, int]:
    """Segment -> embed -> add. Returns (utterances found, embedded, total enrolled).

    Reuses the real Segmenter so an enrolment utterance is shaped exactly like
    a live one (same pre-roll, same trailing silence, same minimum length);
    one embedding per utterance, as the profile is a mean over utterances.
    """
    seg = Segmenter()
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    utterances = seg.feed(pcm)
    tail = seg.flush()
    if tail is not None:
        utterances.append(tail)
    embs = [e for e in (speaker.embed(u) for u in utterances) if e is not None]
    added = speaker.profile.add(embs)
    return len(utterances), added, speaker.profile.count


@router.post("/enroll")
async def enroll(request: Request,
                 token: str | None = Query(None),
                 x_auth_token: str | None = Header(None)) -> dict:
    """Add a recording of the LISTENER's voice to their profile.

    Body: a 16 kHz mono 16-bit WAV (what enroll.html sends), or raw int16 PCM
    with Content-Type audio/pcm. The recording is segmented into utterances
    with the same Segmenter the stream uses, each utterance becomes one
    embedding, and the audio is discarded -- only embeddings are kept.

    Bounded by ENROLL_MAX_BYTES before the body is read: like every other
    limit in config.py it exists because this server can be reached through a
    tunnel, not because the enrol page would ever approach it.
    """
    _require_token(token, x_auth_token)
    if not speaker.available():
        raise HTTPException(status_code=503, detail="speaker id is not available")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.ENROLL_MAX_BYTES:
        raise HTTPException(status_code=413, detail="recording too large")
    body = await request.body()
    if len(body) > config.ENROLL_MAX_BYTES:
        raise HTTPException(status_code=413, detail="recording too large")
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    try:
        audio = _pcm_from_upload(body, (request.headers.get("content-type") or "").lower())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    loop = asyncio.get_running_loop()
    found, added, total = await loop.run_in_executor(None, _enrol_blocking, audio)
    log.info("speaker enrol: %d utterances found, %d added, %d total", found, added, total)
    if found == 0:
        # The most likely cause is the same one docs/status/TODO.md documents for the VAD:
        # a mic quieter than SPEECH_RMS assumes. Say so rather than "0 added".
        raise HTTPException(
            status_code=422,
            detail=f"no speech found above SPEECH_RMS={config.SPEECH_RMS}; "
                   "speak closer to the mic or lower the threshold",
        )
    return {"found": found, "added": added, **_speaker_state()}


@router.delete("/speaker")
async def forget_speaker(token: str | None = Query(None),
                         x_auth_token: str | None = Header(None)) -> dict:
    """Forget the listener's voiceprint completely (memory and disk)."""
    _require_token(token, x_auth_token)
    speaker.profile.clear()
    log.info("speaker profile cleared")
    return _speaker_state()


# ---------------------------------------------------------------------------
# Transcription model -- the ASR twin of POST /api/model
# ---------------------------------------------------------------------------
# Same shape as the interpreter switch: a fixed allowlist (the catalogue in
# transcriber.py -- never an attacker-supplied path), the same cooldown, not
# persisted. One difference: a switch can mean a ~650 MB download, so the POST
# starts the work and returns at once; the card polls GET /api/asr for state.

_asr_lock = asyncio.Lock()
_asr_task: asyncio.Future | None = None
_last_asr_op = 0.0


class AsrSelectRequest(BaseModel):
    model: str


@router.get("/asr")
async def asr_state(token: str | None = Query(None),
                    x_auth_token: str | None = Header(None)) -> dict:
    """The catalogue, which entry is resident, and any download/load in flight."""
    _require_token(token, x_auth_token)
    return {"models": transcriber.catalog(), "selected": transcriber.current_key(),
            "status": transcriber.status(), "persisted": False}


@router.post("/asr")
async def select_asr(body: AsrSelectRequest,
                     token: str | None = Query(None),
                     x_auth_token: str | None = Header(None)) -> dict:
    """Switch the resident speech-to-text model at runtime (not persisted).

    Returns immediately with the status; the actual download + load runs in the
    executor as a background task, because a Parakeet archive is ~650 MB and a
    request that hangs for minutes would be killed by every proxy in the path.
    409 while a previous switch is still running; 429 inside the cooldown.
    """
    global _asr_task, _last_asr_op
    _require_token(token, x_auth_token)
    key = body.model
    if key not in transcriber.CATALOG and not (key.startswith("whisper:") and len(key) > 8):
        raise HTTPException(status_code=400, detail="unknown ASR model key")
    async with _asr_lock:
        if _asr_task is not None and not _asr_task.done():
            raise HTTPException(status_code=409, detail="a model switch is already running")
        since = time.monotonic() - _last_asr_op
        if since < config.MODEL_SWITCH_COOLDOWN_S:
            raise HTTPException(
                status_code=429,
                detail=f"ASR model changed {since:.1f}s ago; wait "
                       f"{config.MODEL_SWITCH_COOLDOWN_S - since:.1f}s",
            )
        _last_asr_op = time.monotonic()
        loop = asyncio.get_running_loop()
        log.info("dashboard: ASR model %s -> %s requested", transcriber.current_key(), key)
        # run_in_executor already returns an awaitable Future; wrapping it in
        # create_task() raises TypeError (a Future is not a coroutine).
        _asr_task = loop.run_in_executor(None, transcriber.select, key)

        def _done(task: asyncio.Future) -> None:
            if not task.cancelled() and task.exception() is not None:
                log.error("ASR switch task failed", exc_info=task.exception())
        _asr_task.add_done_callback(_done)
    # Give the executor a moment so an already-downloaded model reports
    # "loading" rather than the stale previous state.
    await asyncio.sleep(0.05)
    return {"requested": key, "status": transcriber.status(), "persisted": False}


# ---------------------------------------------------------------------------
# Transcript log switches (server/translog.py)
# ---------------------------------------------------------------------------
# Runtime only, not persisted -- the same rule as the model switches -- and off
# by default because with audio on it retains other people's speech. The
# dashboard card is the intended way to turn it on for a session.

class TranslogRequest(BaseModel):
    enabled: bool | None = None
    fields: dict[str, bool] | None = None


@router.get("/translog")
async def translog_state(token: str | None = Query(None),
                         x_auth_token: str | None = Header(None)) -> dict:
    """Current switches plus what is on disk."""
    _require_token(token, x_auth_token)
    return {**translog.stats(), "available_fields": list(translog.FIELDS), "persisted": False}


@router.post("/translog")
async def translog_set(body: TranslogRequest,
                       token: str | None = Query(None),
                       x_auth_token: str | None = Header(None)) -> dict:
    """Switch the log, or individual fields, on or off for this process."""
    _require_token(token, x_auth_token)
    translog.set_options(body.enabled, body.fields)
    log.info("dashboard: transcript log -> %s", translog.options())
    return {**translog.stats(), "available_fields": list(translog.FIELDS), "persisted": False}


# ---------------------------------------------------------------------------
# Emotion weights (server/tuning.py)
# ---------------------------------------------------------------------------
# Runtime only, not persisted, like the switches above. Only the keys in
# tuning.KNOBS are accepted, each clamped to its range, so this cannot write an
# arbitrary config attribute.

class TuningRequest(BaseModel):
    values: dict[str, float | int | bool] | None = None
    preset: str | None = None
    reset: bool = False


@router.get("/tuning")
async def tuning_state(token: str | None = Query(None),
                       x_auth_token: str | None = Header(None)) -> dict:
    _require_token(token, x_auth_token)
    return tuning.state()


@router.post("/tuning")
async def tuning_set(body: TuningRequest,
                     token: str | None = Query(None),
                     x_auth_token: str | None = Header(None)) -> dict:
    """Set some knobs (validated as a batch), or reset all to their defaults."""
    _require_token(token, x_auth_token)
    if body.reset:
        out = tuning.reset()
    elif body.preset is not None:
        try:
            out = tuning.apply_preset(body.preset)
        except KeyError:
            raise HTTPException(400, "unknown preset") from None
    else:
        try:
            out = tuning.set_values(body.values or {})
        except KeyError as exc:
            raise HTTPException(400, f"unknown knob {exc.args[0]!r}") from None
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
    log.info("dashboard: emotion weights -> %s", tuning.snapshot() or "defaults")
    return out
