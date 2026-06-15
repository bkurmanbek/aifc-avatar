from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from time import perf_counter

from ..logging_config import log_event, preview_text
from .answer_common import GeminiContextCallback, RaceCandidate, fallback_message
from .answer_sources import (
    cache_candidate,
    cache_winner,
    clear_answer_caches as clear_source_caches,
    external_rag_candidate,
    faq_candidate,
    gemini_local_rag_candidate,
    local_rag_candidate,
    shutdown_answer_source_executor,
    timed_candidate,
)
from .rag_routing import EXTERNAL_INTERNAL_RAG_TOOL, select_rag_tool
from ..settings import (
    ANSWER_RACE_TIMEOUT_MS,
    EXTERNAL_RAG_FIRST_RESPONSE_TIMEOUT_S,
    GEMINI_RAG_MAX_WAIT_MS,
)

log = logging.getLogger(__name__)


@dataclass
class AnswerRaceResult:
    winner: RaceCandidate
    timings: dict[str, object]
    candidates: list[RaceCandidate]


async def clear_answer_caches() -> dict[str, int]:
    return await clear_source_caches()


def shutdown_answer_race_executor() -> None:
    shutdown_answer_source_executor()


def _log_candidate(candidate: RaceCandidate) -> None:
    log_event(
        log,
        "answer_race_candidate",
        source=candidate.source,
        confidence=candidate.confidence,
        score=f"{candidate.score:.3f}",
        has_answer=bool(candidate.raw_answer),
        answer_chars=len(candidate.raw_answer),
        answer_preview=preview_text(candidate.raw_answer, 240),
        chunks=len(candidate.chunks),
        citations=len(candidate.citations),
        cacheable=candidate.cacheable,
        fallback=candidate.fallback,
    )


def _record_candidate(
    candidate: RaceCandidate | None,
    candidates: list[RaceCandidate],
    timings: dict[str, object],
) -> RaceCandidate | None:
    if candidate is None:
        return None
    candidates.append(candidate)
    timings.update(candidate.timings)
    _log_candidate(candidate)
    return candidate if candidate.raw_answer else None


async def _external_only_race(
    query: str,
    language: str,
    history: list[dict[str, str]],
    conversation_memory: dict | None,
    started: float,
    timings: dict[str, object],
) -> AnswerRaceResult:
    candidates: list[RaceCandidate] = []
    try:
        candidate = await asyncio.wait_for(
            timed_candidate(
                EXTERNAL_INTERNAL_RAG_TOOL,
                external_rag_candidate(query, language, history, conversation_memory),
            ),
            timeout=max(0.5, EXTERNAL_RAG_FIRST_RESPONSE_TIMEOUT_S),
        )
    except asyncio.TimeoutError:
        log.warning(
            "external RAG first response timeout after %.2fs query=%r",
            EXTERNAL_RAG_FIRST_RESPONSE_TIMEOUT_S,
            query[:160],
        )
        candidate = None

    winner = _record_candidate(candidate, candidates, timings)
    if winner is None:
        winner = fallback_message(language, query)
        candidates.append(winner)

    _finish_timings(timings, winner, started)
    return AnswerRaceResult(winner=winner, timings=timings, candidates=candidates)


def _finish_timings(timings: dict[str, object], winner: RaceCandidate, started: float) -> None:
    timings["answer_race_ms"] = int((perf_counter() - started) * 1000)
    timings["winner_source"] = winner.source
    timings["winner_confidence"] = winner.confidence


async def _wait_for_local_candidate(
    local_task: asyncio.Task[RaceCandidate | None],
    deadline: float,
) -> RaceCandidate | None:
    try:
        return local_task.result() if local_task.done() else await asyncio.wait_for(
            asyncio.shield(local_task),
            timeout=max(0.0, deadline - perf_counter()),
        )
    except asyncio.TimeoutError:
        return None


async def _wait_for_gemini_candidate(
    gemini_task: asyncio.Task[RaceCandidate | None],
    deadline: float,
) -> RaceCandidate | None:
    try:
        return await asyncio.wait_for(
            asyncio.shield(gemini_task),
            timeout=max(0.0, deadline - perf_counter()),
        )
    except asyncio.TimeoutError:
        return None


async def run_answer_race(
    query: str,
    language: str,
    history: list[dict[str, str]],
    conversation_memory: dict | None,
    *,
    on_gemini_context_ready: GeminiContextCallback | None = None,
) -> AnswerRaceResult:
    started = perf_counter()
    candidates: list[RaceCandidate] = []
    timings: dict[str, object] = {}
    selected_tool = select_rag_tool(query)
    timings["selected_rag_tool"] = selected_tool
    log_event(
        log,
        "answer_race_start",
        selected_tool=selected_tool,
        language=language,
        query_chars=len(query),
        query_preview=preview_text(query, 240),
        history_items=len(history),
        has_memory=bool(conversation_memory),
    )

    if selected_tool == EXTERNAL_INTERNAL_RAG_TOOL:
        return await _external_only_race(query, language, history, conversation_memory, started, timings)

    created_tasks: set[asyncio.Task[RaceCandidate | None]] = set()

    def create_candidate_task(name: str, coro) -> asyncio.Task[RaceCandidate | None]:
        task = asyncio.create_task(timed_candidate(name, coro))
        created_tasks.add(task)
        return task

    local_task = create_candidate_task("local_rag", local_rag_candidate(query, language, history, conversation_memory))
    gemini_task = create_candidate_task(
        "gemini_local_rag",
        gemini_local_rag_candidate(
            query,
            language,
            history,
            conversation_memory,
            local_task,
            on_context_ready=on_gemini_context_ready,
        ),
    )
    tasks: set[asyncio.Task[RaceCandidate | None]] = {
        create_candidate_task("faq", faq_candidate(query, language)),
        create_candidate_task("cache", cache_candidate(query, language)),
        gemini_task,
    }
    deadline = started + ANSWER_RACE_TIMEOUT_MS / 1000.0
    max_wait_deadline = started + GEMINI_RAG_MAX_WAIT_MS / 1000.0
    winner: RaceCandidate | None = None

    try:
        while tasks and perf_counter() < deadline:
            done, tasks = await asyncio.wait(
                tasks,
                timeout=max(0.0, deadline - perf_counter()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break
            for task in done:
                winner = _record_candidate(task.result(), candidates, timings)
                if winner is not None:
                    break
            if winner is not None or tasks == {gemini_task}:
                break

        if winner is None:
            local_candidate = await _wait_for_local_candidate(local_task, max_wait_deadline)
            if local_candidate is not None and local_candidate not in candidates:
                winner = _record_candidate(local_candidate, candidates, timings)

        if winner is None:
            gemini_candidate = await _wait_for_gemini_candidate(gemini_task, max_wait_deadline)
            if gemini_candidate is not None and gemini_candidate not in candidates:
                winner = _record_candidate(gemini_candidate, candidates, timings)

        if winner is None:
            winner = fallback_message(language, query)
            candidates.append(winner)

        _finish_timings(timings, winner, started)
        if winner.cacheable:
            cache_task = asyncio.create_task(asyncio.to_thread(cache_winner, query, winner))
            cache_task.add_done_callback(_log_background_task_error)
        return AnswerRaceResult(winner=winner, timings=timings, candidates=candidates)
    finally:
        for task in created_tasks:
            if not task.done():
                task.cancel()
        if created_tasks:
            await asyncio.gather(*created_tasks, return_exceptions=True)


def _log_background_task_error(task: asyncio.Task) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        exc = task.exception()
        if exc is not None:
            log.error("answer race background task failed", exc_info=(type(exc), exc, exc.__traceback__))
