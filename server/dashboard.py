"""Diagnostics API for the dashboard: stats, and control over the Ollama model.

Kept out of `main.py` so that file stays what it says it is -- websocket wiring.
Everything here is read-only observation plus two deliberate write actions
(select a model, evict a model), and none of it touches the audio path.

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
import logging
import secrets
import time

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from . import config, metrics, ser

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
            "whisper_model": config.WHISPER_MODEL,
            "whisper_device": config.WHISPER_DEVICE,
            "whisper_compute": config.WHISPER_COMPUTE_TYPE,
            "ser_model": config.SER_MODEL if ser.available() else None,
            "ser_backend": getattr(ser, "_backend", None),
            "ser_available": ser.available(),
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
                            "stream": False})
        t0 = time.perf_counter()
        warm = await _ollama(client, "/api/generate",
                             json={"model": body.model, "prompt": "hi",
                                   "stream": False, "keep_alive": -1})
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
                                 "prompt": "", "stream": False})
    if "_error" in result:
        raise HTTPException(status_code=503, detail=result["_error"])
    log.info("dashboard: evicted %s from VRAM", body.model)
    return {"evicted": body.model}
