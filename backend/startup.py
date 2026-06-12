from __future__ import annotations

import asyncio
import contextlib
import logging
from time import perf_counter

from backend.original_backend import fast_answer_plan_retrieve
from backend.settings import (
    LOCAL_RAG_PREWARM_QUERY,
    LOCAL_RAG_STARTUP_PREWARM,
)

log = logging.getLogger(__name__)

def log_background_task_error(task: asyncio.Task) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        exc = task.exception()
        if exc is not None:
            log.error("background task failed", exc_info=(type(exc), exc, exc.__traceback__))


async def _prewarm_local_rag() -> None:
    if not LOCAL_RAG_STARTUP_PREWARM:
        return
    started = perf_counter()
    try:
        await asyncio.to_thread(fast_answer_plan_retrieve, LOCAL_RAG_PREWARM_QUERY, [], None)
    except Exception as exc:
        log.warning("local RAG prewarm failed: %s", exc)
        return
    log.info("local RAG prewarm complete in %dms", int((perf_counter() - started) * 1000))


async def startup_prewarm() -> None:
    await _prewarm_local_rag()


async def shutdown_keepwarm() -> None:
    return
