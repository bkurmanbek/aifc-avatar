from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

from ..logging_config import configure_logging
from ..intro import prebuild_intro_cache as _prebuild_intro_cache
from ..startup import startup_prewarm as _startup_prewarm
from ..external_rag import close_external_rag_client as _close_external_rag_client
from ..pipeline.answer_race import shutdown_answer_race_executor as _shutdown_answer_race_executor
from .routes import router as http_router
from .websocket import router as ws_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging(reset=False)
    try:
        await _prebuild_intro_cache()
    except Exception:
        log.exception("intro cache prebuild failed — continuing without cache")
    await _startup_prewarm()
    try:
        yield
    finally:
        try:
            await _close_external_rag_client()
        except Exception:
            log.exception("external RAG client close failed")
        try:
            _shutdown_answer_race_executor()
        except Exception:
            log.exception("answer race executor shutdown failed")


app = FastAPI(lifespan=lifespan)

# CORS for split deployments (e.g. frontend on Vercel calling this backend's HTTP asset
# routes — /intro-audio, /intro-cache — cross-origin). Comma-separated allowlist via
# CORS_ALLOW_ORIGINS; defaults to "*" since these routes serve only public, uncredentialed
# media. WebSocket and cross-origin <video>/<audio> playback are not subject to CORS.
_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

app.include_router(http_router)
app.include_router(ws_router)

# Serve the built frontend from the same origin when a production build exists.
# Mounted LAST so the API and /ws routes above always take precedence; the SPA
# lives at "/". This lets one process (behind one tunnel) serve UI + API + WS with
# no CORS, no rewrites, and a same-origin WebSocket. Falls back to API-only if the
# frontend hasn't been built (frontend/dist absent).
_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
    log.info("Serving frontend from %s", _FRONTEND_DIST)
else:
    log.info("Frontend build not found at %s — serving API only", _FRONTEND_DIST)
