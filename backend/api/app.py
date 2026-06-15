from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

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
app.include_router(http_router)
app.include_router(ws_router)
