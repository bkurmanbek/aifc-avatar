from __future__ import annotations

import asyncio
import contextlib
import logging
from time import perf_counter

import httpx

from backend.answer_race import common_tts_prewarm_items
from backend.original_backend import fast_answer_plan_retrieve
from backend.settings import (
    LOCAL_RAG_PREWARM_QUERY,
    LOCAL_RAG_STARTUP_PREWARM,
    LOCAL_TTS_STARTUP_PREWARM,
    LOCAL_TTS_URL,
    MEDIA_KEEPWARM_ENABLED,
    MEDIA_KEEPWARM_INTERVAL_S,
    MEDIA_KEEPWARM_LANG,
    MEDIA_KEEPWARM_TEXT,
    SYNCTALK_STREAM_URL,
    TTS_PROVIDER,
)

log = logging.getLogger(__name__)

_MEDIA_KEEPWARM_TASK: asyncio.Task | None = None


def log_background_task_error(task: asyncio.Task) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        exc = task.exception()
        if exc is not None:
            log.error("background task failed", exc_info=(type(exc), exc, exc.__traceback__))


async def _prewarm_local_tts_cache() -> None:
    if TTS_PROVIDER != "local" or not LOCAL_TTS_STARTUP_PREWARM:
        return
    items = common_tts_prewarm_items()
    warmed = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for text, language in items:
            try:
                response = await client.post(
                    LOCAL_TTS_URL,
                    json={"text": text, "lang": language, "priority": 1},
                )
                response.raise_for_status()
                warmed += 1
            except Exception as exc:
                log.warning("local TTS prewarm skipped item lang=%s: %s", language, exc)
    log.info("local TTS prewarm complete: %d/%d items", warmed, len(items))


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


async def _media_keepwarm_once() -> None:
    if TTS_PROVIDER != "local":
        return
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        tts_response = await client.post(
            LOCAL_TTS_URL,
            json={
                "text": MEDIA_KEEPWARM_TEXT,
                "lang": MEDIA_KEEPWARM_LANG,
                "priority": 1,
            },
        )
        tts_response.raise_for_status()
        audio_b64 = tts_response.json().get("audio_b64")
        if not audio_b64:
            return

        async with client.stream(
            "POST",
            SYNCTALK_STREAM_URL,
            json={
                "audio_b64": audio_b64,
                "priority": 1,
                "chunk_idx": 99,
            },
        ) as response:
            response.raise_for_status()
            frame_count = 0
            async for line in response.aiter_lines():
                if line.strip():
                    frame_count += 1
        log.info("media keepwarm complete: frames=%d", frame_count)


async def _media_keepwarm_loop() -> None:
    while True:
        try:
            await _media_keepwarm_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("media keepwarm skipped: %s", exc)
        await asyncio.sleep(max(10.0, MEDIA_KEEPWARM_INTERVAL_S))


async def startup_prewarm() -> None:
    global _MEDIA_KEEPWARM_TASK
    log.info("tts provider active: %s", TTS_PROVIDER)
    tts_task = asyncio.create_task(_prewarm_local_tts_cache())
    await _prewarm_local_rag()
    await tts_task
    if MEDIA_KEEPWARM_ENABLED and TTS_PROVIDER == "local" and _MEDIA_KEEPWARM_TASK is None:
        _MEDIA_KEEPWARM_TASK = asyncio.create_task(_media_keepwarm_loop())
    elif MEDIA_KEEPWARM_ENABLED:
        log.info("media keepwarm skipped for tts provider: %s", TTS_PROVIDER)


async def shutdown_keepwarm() -> None:
    global _MEDIA_KEEPWARM_TASK
    if _MEDIA_KEEPWARM_TASK is not None:
        _MEDIA_KEEPWARM_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _MEDIA_KEEPWARM_TASK
        _MEDIA_KEEPWARM_TASK = None
