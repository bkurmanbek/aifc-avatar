from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

import httpx

from .rag_routing import EXTERNAL_INTERNAL_RAG_TOOL
from .settings import (
    EXTERNAL_RAG_API_KEY,
    EXTERNAL_RAG_AUTH_HEADER,
    EXTERNAL_RAG_ENABLED,
    EXTERNAL_RAG_HIGH_THRESHOLD,
    EXTERNAL_RAG_HYBRID,
    EXTERNAL_RAG_LIMIT,
    EXTERNAL_RAG_PARTIAL_THRESHOLD,
    EXTERNAL_RAG_TIMEOUT_S,
    EXTERNAL_RAG_TOP_N,
    EXTERNAL_RAG_URL,
    EXTERNAL_RAG_WITH_RERANK,
)

log = logging.getLogger(__name__)

_SOURCE_JOIN_LIMIT = 4
_EXTERNAL_RAG_CLIENT: httpx.AsyncClient | None = None
_EXTERNAL_RAG_CLIENT_LOCK: asyncio.Lock | None = None


@dataclass(frozen=True)
class ExternalRagConfig:
    enabled: bool
    url: str
    api_key: str
    auth_header: str
    timeout_s: float
    high_threshold: float
    partial_threshold: float
    hybrid: bool = True
    with_rerank: bool = True
    limit: int = 30
    top_n: int = 3


@dataclass
class ExternalRagResult:
    answer: str = ""
    raw_payload: object | None = None
    chunks: list[dict] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    confidence: str = "not_found"
    score: float = 0.0
    is_prompt_contract: bool = False
    error: str = ""


def default_external_rag_config() -> ExternalRagConfig:
    return ExternalRagConfig(
        enabled=EXTERNAL_RAG_ENABLED,
        url=EXTERNAL_RAG_URL,
        api_key=EXTERNAL_RAG_API_KEY,
        auth_header=EXTERNAL_RAG_AUTH_HEADER,
        timeout_s=EXTERNAL_RAG_TIMEOUT_S,
        high_threshold=EXTERNAL_RAG_HIGH_THRESHOLD,
        partial_threshold=EXTERNAL_RAG_PARTIAL_THRESHOLD,
        hybrid=EXTERNAL_RAG_HYBRID,
        with_rerank=EXTERNAL_RAG_WITH_RERANK,
        limit=EXTERNAL_RAG_LIMIT,
        top_n=EXTERNAL_RAG_TOP_N,
    )


async def query_external_rag(
    query: str,
    language: str,
    history: list[dict[str, str]],
    conversation_memory: dict | None,
    *,
    config: ExternalRagConfig | None = None,
) -> ExternalRagResult:
    config = config or default_external_rag_config()
    if not config.enabled:
        log.info("external RAG skipped: disabled")
        return ExternalRagResult(error="disabled")
    if not config.url:
        log.warning("external RAG skipped: EXTERNAL_RAG_URL is not configured")
        return ExternalRagResult(error="missing_url")

    request_payload = {
        "query": query,
        "language": language,
        "history": _compact_history(history),
        "conversationMemory": conversation_memory or {},
        "hybrid": config.hybrid,
        "withRerank": config.with_rerank,
        "limit": config.limit,
        "topN": config.top_n,
    }
    try:
        timeout = httpx.Timeout(config.timeout_s, connect=min(10.0, config.timeout_s))
        client = await _external_rag_client()
        response = await client.post(
            config.url,
            headers=_headers(config),
            json=request_payload,
            timeout=timeout,
        )
        response.raise_for_status()
        try:
            payload: object = response.json()
        except ValueError:
            payload = response.text
    except Exception:
        log.exception("external RAG request failed")
        return ExternalRagResult(error="request_failed")

    return normalize_external_rag_payload(payload, config=config)


def _client_lock() -> asyncio.Lock:
    global _EXTERNAL_RAG_CLIENT_LOCK
    if _EXTERNAL_RAG_CLIENT_LOCK is None:
        _EXTERNAL_RAG_CLIENT_LOCK = asyncio.Lock()
    return _EXTERNAL_RAG_CLIENT_LOCK


async def _external_rag_client() -> httpx.AsyncClient:
    global _EXTERNAL_RAG_CLIENT
    if _EXTERNAL_RAG_CLIENT is not None and not _EXTERNAL_RAG_CLIENT.is_closed:
        return _EXTERNAL_RAG_CLIENT
    async with _client_lock():
        if _EXTERNAL_RAG_CLIENT is None or _EXTERNAL_RAG_CLIENT.is_closed:
            _EXTERNAL_RAG_CLIENT = httpx.AsyncClient()
        return _EXTERNAL_RAG_CLIENT


async def close_external_rag_client() -> None:
    global _EXTERNAL_RAG_CLIENT
    client = _EXTERNAL_RAG_CLIENT
    _EXTERNAL_RAG_CLIENT = None
    if client is not None:
        with contextlib.suppress(Exception):
            await client.aclose()


def normalize_external_rag_payload(
    payload: object,
    *,
    config: ExternalRagConfig | None = None,
) -> ExternalRagResult:
    config = config or default_external_rag_config()
    chunks = _chunks(payload)
    answer = _answer_text(payload)
    citations = _citations(payload, chunks)
    confidence, score = _confidence(payload, chunks, bool(answer), config)
    return ExternalRagResult(
        answer=answer,
        raw_payload=payload,
        chunks=chunks,
        citations=citations,
        confidence=confidence,
        score=score,
        is_prompt_contract=_is_prompt_contract(payload),
    )


def _headers(config: ExternalRagConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.api_key and config.auth_header:
        value = config.api_key
        if config.auth_header.lower() == "authorization" and not value.lower().startswith(("bearer ", "basic ")):
            value = f"Bearer {value}"
        headers[config.auth_header] = value
    return headers


def _compact_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    compact: list[dict[str, str]] = []
    for item in history[-6:]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        role = str(item.get("role") or "user").strip() or "user"
        compact.append({"role": role, "content": content})
    return compact


def _string_field(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        for key in ("answer", "spoken", "response", "text", "content", "summary"):
            text = _string_field(value.get(key))
            if text:
                return text
    return ""


def _answer_text(payload: object) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    for key in ("answer", "spoken", "response", "text", "content", "summary"):
        text = _string_field(payload.get(key))
        if text:
            return text
    for key in ("result", "data", "message"):
        text = _answer_text(payload.get(key))
        if text:
            return text
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            return _answer_text(first.get("message") or first)
    return ""


def _chunk_list(value: object) -> list[dict]:
    if isinstance(value, str):
        text = value.strip()
        return [{"text": text, "source_file": "external_rag"}] if text else []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []

    chunks: list[dict] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                chunks.append({"text": text, "source_file": "external_rag"})
            continue
        if not isinstance(item, dict):
            continue
        chunk = dict(item)
        text = _string_field(
            chunk.get("text")
            or chunk.get("content")
            or chunk.get("context")
            or chunk.get("page_content")
            or chunk.get("body")
        )
        if text and not chunk.get("text"):
            chunk["text"] = text
        source = (
            chunk.get("source_file")
            or chunk.get("source")
            or chunk.get("documentName")
            or chunk.get("document")
            or chunk.get("title")
            or chunk.get("url")
        )
        if source and not chunk.get("source_file"):
            chunk["source_file"] = str(source)
        if chunk.get("text") or chunk.get("content") or chunk.get("context"):
            chunks.append(chunk)
    return chunks


def _chunks(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    for key in ("chunks", "contexts", "context", "documents", "docs", "results", "sources"):
        chunks = _chunk_list(payload.get(key))
        if chunks:
            return chunks
    for key in ("result", "data"):
        chunks = _chunks(payload.get(key))
        if chunks:
            return chunks
    return []


def _citations(payload: object, chunks: list[dict]) -> list[str]:
    raw: object = None
    if isinstance(payload, dict):
        for key in ("citations", "citation", "sources", "source", "references"):
            raw = payload.get(key)
            if raw:
                break
    values = raw if isinstance(raw, list) else ([raw] if raw else [])
    citations: list[str] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, dict):
            value = _string_field(
                item.get("title")
                or item.get("source")
                or item.get("source_file")
                or item.get("document")
                or item.get("url")
                or item.get("name")
            )
        else:
            value = _string_field(item)
        if value and value not in seen:
            seen.add(value)
            citations.append(value)
        if len(citations) >= _SOURCE_JOIN_LIMIT:
            break
    return citations or _chunk_citations(chunks)


def _chunk_citations(chunks: list[dict]) -> list[str]:
    seen: set[str] = set()
    citations: list[str] = []
    for chunk in chunks:
        source = str(
            chunk.get("source_file")
            or chunk.get("documentName")
            or chunk.get("domain")
            or chunk.get("chunk_id")
            or "external_rag"
        ).strip()
        if source and source not in seen:
            seen.add(source)
            citations.append(source)
        if len(citations) >= _SOURCE_JOIN_LIMIT:
            break
    return citations


def _numeric_score(payload: object, chunks: list[dict]) -> float:
    if isinstance(payload, dict):
        for key in ("score", "confidence_score", "confidenceScore", "similarity", "rerank_score"):
            value = payload.get(key)
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return _best_chunk_score(chunks)


def _best_chunk_score(chunks: list[dict]) -> float:
    scores: list[float] = []
    for chunk in chunks:
        value = chunk.get("rerank_score", chunk.get("similarity", chunk.get("ann_score", 0.0)))
        try:
            scores.append(float(value))
        except (TypeError, ValueError):
            continue
    return max(scores) if scores else 0.0


def _confidence(payload: object, chunks: list[dict], has_answer: bool, config: ExternalRagConfig) -> tuple[str, float]:
    raw_confidence = payload.get("confidence") if isinstance(payload, dict) else None
    if isinstance(raw_confidence, str):
        label = raw_confidence.strip().lower()
        if label in {"high", "partial", "not_found"}:
            return label, _numeric_score(payload, chunks)
        try:
            raw_confidence = float(label)
        except ValueError:
            raw_confidence = None
    if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool):
        score = float(raw_confidence)
        return _confidence_from_score(score, config), score

    score = _numeric_score(payload, chunks)
    if score > 0:
        return _confidence_from_score(score, config), score
    if has_answer:
        return "high", 0.92
    return "not_found", 0.0


def _confidence_from_score(score: float, config: ExternalRagConfig) -> str:
    if score >= config.high_threshold:
        return "high"
    if score >= config.partial_threshold:
        return "partial"
    return "not_found"


def _is_prompt_contract(payload: object) -> bool:
    return isinstance(payload, dict) and "details" in payload
