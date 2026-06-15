from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from types import SimpleNamespace
from typing import Any

from ..logging_config import log_event, preview_text
from ..external_rag import query_external_rag
from ..knowledge.cache import _semantic_answer_cache_lookup, _semantic_answer_cache_put, clear_semantic_answer_cache
from ..knowledge.faq import _faq_fast_path_lookup
from ..knowledge.llm import _extract_json_from_wrapped, _extract_json_payload, build_prompt, stream_answer
from ..knowledge.rag import fast_answer_plan_retrieve
from ..settings import (
    CACHE_WIN_THRESHOLD,
    FAQ_WIN_THRESHOLD,
    LOCAL_RAG_HIGH_THRESHOLD,
    LOCAL_RAG_MAX_CONCURRENCY,
    LOCAL_RAG_PARTIAL_THRESHOLD,
)
from ..utils.spoken_text import sanitize_spoken_text
from .answer_common import (
    GeminiContextCallback,
    RaceCandidate,
    _best_chunk_score,
    _citations,
    _confidence_from_score,
    _confidence_score,
    _coverage_adjusted_confidence,
    _extractive_summary,
    _query_coverage,
    aifc_overview_candidate,
    candidate_from_answer,
    fallback_message,
    is_fallback_raw_answer,
    json_answer_to_text,
)
from .rag_routing import EXTERNAL_INTERNAL_RAG_TOOL, GEMINI_PUBLIC_RAG_TOOL

log = logging.getLogger(__name__)

_LOCAL_RAG_SEMAPHORE: asyncio.Semaphore | None = None
_LOCAL_RAG_EXECUTOR: ThreadPoolExecutor | None = None


def _local_rag_semaphore() -> asyncio.Semaphore:
    global _LOCAL_RAG_SEMAPHORE
    if _LOCAL_RAG_SEMAPHORE is None:
        _LOCAL_RAG_SEMAPHORE = asyncio.Semaphore(max(1, LOCAL_RAG_MAX_CONCURRENCY))
    return _LOCAL_RAG_SEMAPHORE


def _local_rag_executor() -> ThreadPoolExecutor:
    global _LOCAL_RAG_EXECUTOR
    if _LOCAL_RAG_EXECUTOR is None:
        _LOCAL_RAG_EXECUTOR = ThreadPoolExecutor(
            max_workers=max(1, LOCAL_RAG_MAX_CONCURRENCY),
            thread_name_prefix="local-rag",
        )
    return _LOCAL_RAG_EXECUTOR


async def _run_local_rag_limited(
    query: str,
    history: list[dict[str, str]],
    conversation_memory: dict | None,
) -> tuple[object, list[dict], dict[str, Any] | None] | None:
    loop = asyncio.get_running_loop()
    async with _local_rag_semaphore():
        future = loop.run_in_executor(
            _local_rag_executor(),
            fast_answer_plan_retrieve,
            query,
            history,
            conversation_memory,
        )
        return await asyncio.shield(future)


async def clear_answer_caches() -> dict[str, int]:
    semantic_count = await asyncio.to_thread(clear_semantic_answer_cache)
    return {"cache": semantic_count}


def shutdown_answer_source_executor() -> None:
    global _LOCAL_RAG_EXECUTOR
    executor = _LOCAL_RAG_EXECUTOR
    _LOCAL_RAG_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


async def faq_candidate(query: str, language: str) -> RaceCandidate | None:
    overview = aifc_overview_candidate(query, language)
    if overview is not None:
        overview.source = "faq"
        if overview.plan is not None:
            setattr(overview.plan, "route", "faq_overview")
        return overview
    try:
        hit = await asyncio.to_thread(_faq_fast_path_lookup, query, language)
        if hit and float(hit.get("similarity", 0.0)) >= FAQ_WIN_THRESHOLD:
            return candidate_from_answer(
                source="faq",
                answer=str(hit.get("answer", "")),
                language=language,
                confidence="high",
                score=float(hit.get("similarity", 0.0)),
                citations=list(hit.get("citations") or []),
                cacheable=True,
            )
    except Exception as exc:
        log.exception("FAQ candidate lookup failed")
        log_event(log, "faq_candidate_failed", query_preview=preview_text(query, 200), language=language, error=exc, level=logging.ERROR)
    return None


async def cache_candidate(query: str, language: str) -> RaceCandidate | None:
    try:
        hit = await asyncio.to_thread(_semantic_answer_cache_lookup, query)
        if not hit:
            return None
        score = float(hit.get("similarity", 0.0))
        plan = hit.get("plan")
        answer_language = getattr(plan, "answer_language", language)
        if score < CACHE_WIN_THRESHOLD or answer_language != language:
            return None

        raw_answer = str(hit.get("raw_answer") or "")
        if raw_answer:
            return RaceCandidate(
                source="semantic_cache",
                confidence=str(hit.get("confidence", "high")),
                score=_confidence_score(str(hit.get("confidence", "high")), score),
                raw_answer=raw_answer,
                plan=plan,
                chunks=list(hit.get("chunks") or []),
                citations=list(hit.get("citations") or []),
                cacheable=False,
            )
        return candidate_from_answer(
            source="semantic_cache",
            answer=str(hit.get("answer", "")),
            language=language,
            confidence=str(hit.get("confidence", "high")),
            score=score,
            plan=plan,
            chunks=list(hit.get("chunks") or []),
            citations=list(hit.get("citations") or []),
            cacheable=False,
        )
    except Exception as exc:
        log.exception("semantic cache candidate lookup failed")
        log_event(log, "semantic_cache_candidate_failed", query_preview=preview_text(query, 200), language=language, error=exc, level=logging.ERROR)
    return None


def _external_rag_plan(language: str) -> SimpleNamespace:
    return SimpleNamespace(answer_language=language, route=EXTERNAL_INTERNAL_RAG_TOOL, is_chitchat=False)


async def external_rag_candidate(
    query: str,
    language: str,
    history: list[dict[str, str]],
    conversation_memory: dict | None,
) -> RaceCandidate | None:
    result = await query_external_rag(query, language, history, conversation_memory)
    chunks = result.chunks
    answer = result.answer
    if not answer and chunks:
        answer = _extractive_summary(query, chunks, language)
    citations = result.citations or _citations(chunks)
    confidence = result.confidence
    raw_score = result.score
    score = _confidence_score(confidence, raw_score)
    plan = _external_rag_plan(language)

    if not answer:
        return RaceCandidate(
            EXTERNAL_INTERNAL_RAG_TOOL,
            confidence,
            score,
            plan=plan,
            chunks=chunks,
            citations=citations,
            cacheable=False,
        )

    if result.is_prompt_contract:
        contract = dict(result.raw_payload) if isinstance(result.raw_payload, dict) else {"spoken": answer}
        return RaceCandidate(
            source=EXTERNAL_INTERNAL_RAG_TOOL,
            confidence=confidence,
            score=score,
            raw_answer=json.dumps(contract, ensure_ascii=False),
            plan=plan,
            chunks=chunks,
            citations=citations,
            cacheable=False,
        )

    return candidate_from_answer(
        source=EXTERNAL_INTERNAL_RAG_TOOL,
        answer=answer,
        language=language,
        confidence=confidence,
        score=raw_score,
        plan=plan,
        chunks=chunks,
        citations=citations,
        cacheable=False,
    )


async def local_rag_candidate(
    query: str,
    language: str,
    history: list[dict[str, str]],
    conversation_memory: dict | None,
) -> RaceCandidate | None:
    result = await _run_local_rag_limited(query, history, conversation_memory)
    if result is None:
        return RaceCandidate("local_rag", "not_found", 0.0, chunks=[])

    plan, chunks, fast_hit = result
    answer_language = getattr(plan, "answer_language", language) or language
    if fast_hit and fast_hit.get("answer") and answer_language == language == "en":
        return candidate_from_answer(
            source=str(fast_hit.get("cache_type", "local_fast_hit")),
            answer=str(fast_hit.get("answer", "")),
            language=language,
            confidence=str(fast_hit.get("confidence", "high")),
            score=0.94,
            plan=plan,
            chunks=list(fast_hit.get("chunks") or chunks or []),
            citations=list(fast_hit.get("citations") or _citations(chunks)),
            cacheable=True,
        )

    score = _best_chunk_score(chunks)
    confidence = _confidence_from_score(score, LOCAL_RAG_HIGH_THRESHOLD, LOCAL_RAG_PARTIAL_THRESHOLD)
    if confidence == "not_found" or language != "en":
        return RaceCandidate("local_rag", confidence, _confidence_score(confidence, score), plan=plan, chunks=chunks)

    answer = _extractive_summary(query, chunks, language)
    if not answer:
        return RaceCandidate("local_rag", "not_found", 0.0, plan=plan, chunks=chunks)

    coverage, token_count = _query_coverage(query, [], answer)
    confidence = _coverage_adjusted_confidence(confidence, coverage, token_count)
    if confidence == "not_found":
        log.info(
            "local_rag rejected by query coverage: coverage=%.2f query=%r answer=%r",
            coverage,
            query[:160],
            answer[:160],
        )
        return RaceCandidate("local_rag", "not_found", _confidence_score("not_found", score), plan=plan, chunks=chunks)

    return candidate_from_answer(
        source="local_rag",
        answer=answer,
        language=language,
        confidence=confidence,
        score=score,
        plan=plan,
        chunks=chunks,
        cacheable=confidence == "high",
    )


async def gemini_local_rag_candidate(
    query: str,
    language: str,
    history: list[dict[str, str]],
    conversation_memory: dict | None,
    local_task: asyncio.Task[RaceCandidate | None],
    *,
    on_context_ready: GeminiContextCallback | None = None,
) -> RaceCandidate | None:
    local = await local_task
    chunks = list(local.chunks)[:5] if local is not None else []
    plan = local.plan if local is not None and local.plan is not None else SimpleNamespace(answer_language=language)
    with contextlib.suppress(Exception):
        setattr(plan, "route", GEMINI_PUBLIC_RAG_TOOL)
    score = _best_chunk_score(chunks)
    if on_context_ready is not None:
        await on_context_ready(plan, chunks)

    history_msgs, prompt = build_prompt(
        query,
        language,
        chunks,
        history,
        conversation_memory=conversation_memory,
        expert_mode=bool(getattr(plan, "expert_mode", False)),
        needs_widget=bool(getattr(plan, "needs_widget", False)),
    )
    raw = ""
    async for delta in stream_answer(history_msgs, prompt):
        raw += delta or ""
    if not raw.strip():
        return None

    is_fallback = is_fallback_raw_answer(raw)
    confidence = "not_found" if is_fallback else "high"
    timings = dict(local.timings) if local is not None else {}
    return RaceCandidate(
        source="gemini_local_rag",
        confidence=confidence,
        score=_confidence_score(confidence, score),
        raw_answer=raw,
        plan=plan,
        chunks=chunks,
        citations=_citations(chunks),
        timings=timings,
        cacheable=not is_fallback,
    )


def cache_winner(query: str, winner: RaceCandidate) -> None:
    if not winner.cacheable:
        return
    if winner.fallback or not winner.raw_answer:
        return

    chat = ""
    answer = ""
    payload = _extract_json_payload(winner.raw_answer) or _extract_json_from_wrapped(winner.raw_answer)
    if isinstance(payload, dict):
        chat = json_answer_to_text(payload)
        answer = sanitize_spoken_text(str(payload.get("spoken") or chat), keep_digits=True)
    if not answer:
        return

    plan = winner.plan or SimpleNamespace(answer_language="en", retrieval_queries=[query])
    retrieval_queries = [
        str(item).strip()
        for item in getattr(plan, "retrieval_queries", [])[:3]
        if str(item).strip()
    ]
    rewritten_query = " ; ".join(retrieval_queries) or query
    try:
        _semantic_answer_cache_put(
            rewritten_query=rewritten_query,
            plan=plan,
            chunks=winner.chunks,
            answer=answer,
            chat=chat,
            citations=winner.citations,
            confidence=winner.confidence,
            raw_answer=winner.raw_answer,
        )
        log_event(
            log,
            "answer_cache_saved",
            source=winner.source,
            confidence=winner.confidence,
            query_chars=len(query),
            query_preview=preview_text(query, 200),
            answer_chars=len(answer),
            answer_preview=preview_text(answer, 240),
        )
    except Exception as exc:
        log.exception("semantic answer cache save failed")
        log_event(
            log,
            "answer_cache_save_failed",
            source=winner.source,
            confidence=winner.confidence,
            query_preview=preview_text(query, 200),
            error=exc,
            level=logging.ERROR,
        )


async def timed_candidate(name: str, coro) -> RaceCandidate | None:
    started = perf_counter()
    try:
        candidate = await coro
        if candidate is not None:
            candidate.timings[f"{name}_ms"] = int((perf_counter() - started) * 1000)
        return candidate
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("answer race candidate failed: %s", name)
        log_event(log, "answer_candidate_failed", source=name, latency_ms=(perf_counter() - started) * 1000, error=exc, level=logging.ERROR)
        return None
