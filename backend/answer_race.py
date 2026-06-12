from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from types import SimpleNamespace
from typing import Any

from .external_rag import query_external_rag
from .llm import _extract_json_from_wrapped, _extract_json_payload, build_prompt, stream_answer
from .original_backend import (
    _HARDCODED_FAQ,
    _faq_fast_path_lookup,
    _semantic_answer_cache_lookup,
    _semantic_answer_cache_put,
    clear_semantic_answer_cache,
    fast_answer_plan_retrieve,
    wrap_spoken_and_details,
)
from .settings import (
    ANSWER_RACE_TIMEOUT_MS,
    CACHE_WIN_THRESHOLD,
    EXTERNAL_RAG_FIRST_RESPONSE_TIMEOUT_S,
    FAQ_WIN_THRESHOLD,
    GEMINI_RAG_MAX_WAIT_MS,
    LOCAL_RAG_HIGH_THRESHOLD,
    LOCAL_RAG_PARTIAL_THRESHOLD,
)
from .rag_routing import (
    EXTERNAL_INTERNAL_RAG_TOOL,
    GEMINI_PUBLIC_RAG_TOOL,
    select_rag_tool,
)
from .spoken_text import extract_blocks, rebuild_blocks, sanitize_spoken_text

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9]+")
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")
_METADATA_LINE_RE = re.compile(r"^(URL|Title|Section|Category|Question)\s*:", re.IGNORECASE)
_SOURCE_JOIN_LIMIT = 4
_QUERY_STOPWORDS = {
    "a", "an", "and", "are", "can", "could", "do", "does", "for", "from", "how",
    "in", "is", "it", "me", "of", "on", "or", "please", "tell", "that", "the",
    "this", "to", "what", "when", "where", "which", "who", "why", "with", "you",
    "your",
}
_LOCAL_RAG_BUSY = False
_LOCAL_RAG_BUSY_LOCK: asyncio.Lock | None = None
_ANSWER_CACHE_MAX = 128
_ANSWER_CACHE: list[dict[str, Any]] = []
_ANSWER_CACHE_LOCK: asyncio.Lock | None = None
GeminiSpokenCallback = Callable[[str, int, bool], Awaitable[None]]
GeminiContextCallback = Callable[[object, list[dict]], Awaitable[None]]
_STREAM_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9]+(?:['’\-][A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9]+)?", re.UNICODE)
_TTS_MAX_WORD_COUNT = 7
_STREAM_TERMINAL_PUNCT = ".!?。！？"


@dataclass
class RaceCandidate:
    source: str
    confidence: str
    score: float
    tagged_answer: str = ""
    raw_answer: str = ""
    plan: object | None = None
    chunks: list[dict] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    timings: dict[str, int] = field(default_factory=dict)
    cacheable: bool = False
    fallback: bool = False


@dataclass
class AnswerRaceResult:
    winner: RaceCandidate
    timings: dict[str, object]
    candidates: list[RaceCandidate]


def _token_forms(token: str) -> set[str]:
    token = token.casefold()
    forms = {token}
    if token == "center":
        forms.add("centre")
    elif token == "centre":
        forms.add("center")
    if token.endswith("ies") and len(token) > 5:
        forms.add(token[:-3] + "y")
    for suffix in ("ing", "tion", "sion", "ions", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) > len(suffix) + 3:
            forms.add(token[: -len(suffix)])
    if token.endswith("e") and len(token) > 4:
        forms.add(token[:-1])
    return {item for item in forms if len(item) > 2}


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in _WORD_RE.finditer(text or ""):
        token = match.group(0).casefold()
        if len(token) > 2:
            tokens.update(_token_forms(token))
    return tokens


def _important_query_tokens(query: str) -> set[str]:
    tokens = _tokens(query)
    important = {token for token in tokens if token not in _QUERY_STOPWORDS}
    return important or tokens


def _query_coverage(query: str, chunks: list[dict], answer: str = "") -> tuple[float, int]:
    important = _important_query_tokens(query)
    if not important:
        return 1.0, 0
    haystack_parts = [answer]
    for chunk in chunks[:5]:
        haystack_parts.append(_chunk_text(chunk))
        haystack_parts.append(_chunk_source(chunk))
    haystack_tokens = _tokens(" ".join(haystack_parts))
    return len(important & haystack_tokens) / len(important), len(important)


def _coverage_adjusted_confidence(confidence: str, coverage: float, token_count: int) -> str:
    if confidence == "not_found" or token_count <= 0:
        return confidence
    high_min = 0.67 if token_count >= 2 else 1.0
    partial_min = 0.40 if token_count >= 2 else 1.0
    if coverage >= high_min:
        return confidence
    if coverage >= partial_min:
        return "partial" if confidence == "high" else confidence
    return "not_found"


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _normalized_query(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.casefold())).strip()


def _is_aifc_overview_query(query: str) -> bool:
    tokens = _tokens(query)
    if "aifc" not in tokens:
        return False
    specific_terms = {
        "afsa", "aix", "court", "iac", "fintech", "lab", "sandbox", "capital",
        "requirement", "requirements", "insurance", "intermediary", "broker",
        "firm", "firms", "license", "licensed", "licensing", "authorisation",
        "authorization", "rule", "rules", "regulation", "regulations", "register",
        "registration", "company", "participant", "participants", "visa", "tax",
        "dispute", "arbitration", "recognition", "market", "exchange",
    }
    if tokens & specific_terms:
        return False
    overview_terms = {
        "about", "overview", "intro", "introduction", "explain", "describe",
        "tell", "information", "info", "what",
    }
    return bool(tokens & overview_terms)


def _confidence_from_score(score: float, high: float, partial: float) -> str:
    if score >= high:
        return "high"
    if score >= partial:
        return "partial"
    return "not_found"


def _confidence_score(label: str, raw_score: float = 0.0) -> float:
    if label == "high":
        return max(0.86, min(1.0, raw_score if raw_score else 0.92))
    if label == "partial":
        return max(0.55, min(0.74, raw_score if raw_score else 0.62))
    return min(0.31, raw_score if raw_score else 0.31)


def _chunk_text(chunk: dict) -> str:
    return str(chunk.get("text") or chunk.get("content") or chunk.get("context") or "").strip()


def _answer_body_text(chunk: dict) -> str:
    raw = _chunk_text(chunk)
    lines: list[str] = []
    for line in raw.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        if re.match(r"^(Category|Question)\s*:", cleaned, re.IGNORECASE) and "Answer:" in cleaned:
            cleaned = cleaned.split("Answer:", 1)[1].strip()
            if not cleaned:
                continue
        cleaned = re.sub(r"^(?:Answer|FAQ-A)\s*:\s*", "", cleaned, flags=re.IGNORECASE).strip()
        if _METADATA_LINE_RE.match(cleaned):
            continue
        if cleaned.startswith("#"):
            continue
        if cleaned.startswith("[PDF") or cleaned.startswith("|"):
            continue
        lines.append(cleaned)
    body = " ".join(lines).strip()
    return body or raw


def _chunk_source(chunk: dict) -> str:
    return str(
        chunk.get("source_file")
        or chunk.get("documentName")
        or chunk.get("domain")
        or chunk.get("chunk_id")
        or "AIFC knowledge base"
    ).strip()


def _best_chunk_score(chunks: list[dict]) -> float:
    scores: list[float] = []
    for chunk in chunks:
        value = chunk.get("rerank_score", chunk.get("similarity", chunk.get("ann_score", 0.0)))
        try:
            scores.append(float(value))
        except (TypeError, ValueError):
            continue
    return max(scores) if scores else 0.0


def _citations(chunks: list[dict]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for chunk in chunks:
        source = _chunk_source(chunk).replace(".md", "").strip()
        if source and source not in seen:
            seen.add(source)
            out.append(source)
        if len(out) >= _SOURCE_JOIN_LIMIT:
            break
    return out


def _sentence_candidates(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_RE.split(text or "") if part.strip()]
    if parts:
        return parts
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    return [text[:320].strip()]


def _extractive_summary(query: str, chunks: list[dict], language: str) -> str:
    q_tokens = _tokens(query)
    scored: list[tuple[float, int, str]] = []
    for chunk_index, chunk in enumerate(chunks[:5]):
        for sentence in _sentence_candidates(_answer_body_text(chunk))[:8]:
            s_tokens = _tokens(sentence)
            if not s_tokens:
                continue
            overlap = len(q_tokens & s_tokens)
            score = overlap / max(1, len(q_tokens)) + 0.04 * max(0, 5 - chunk_index)
            scored.append((score, chunk_index, sentence))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected: list[str] = []
    seen: set[str] = set()
    for _, _, sentence in scored:
        cleaned = sanitize_spoken_text(sentence, keep_digits=True)
        if not cleaned:
            continue
        key = re.sub(r"\W+", " ", cleaned.casefold()).strip()
        if key in seen:
            continue
        if len(cleaned.split()) < 4:
            continue
        seen.add(key)
        selected.append(cleaned)
        if len(selected) >= 1:
            break
    if not selected:
        fallback = sanitize_spoken_text(_answer_body_text(chunks[0])[:260], keep_digits=True) if chunks else ""
        selected = [fallback] if fallback else []
    summary = " ".join(selected).strip()
    if language == "zh":
        return summary[:90].strip()
    words = summary.split()
    if len(words) > 52:
        summary = " ".join(words[:52]).rstrip(" ,;:") + "."
    return summary


def _short_detail_point(text: str, max_words: int = 34) -> str:
    cleaned = sanitize_spoken_text(text, keep_digits=True)
    cleaned = cleaned.replace("Expat Cente ", "Expat Centre ")
    cleaned = cleaned.replace("Expat Cente.", "Expat Centre.")
    cleaned = re.sub(r"\bEC\b", "Expat Centre", cleaned)
    cleaned = re.sub(
        r"^The Expat Centre as a one-stop shop centre assisting in obtaining",
        "The Expat Centre is a one-stop shop that assists with obtaining",
        cleaned,
    )
    if "�" in cleaned or re.search(r"\b(?:undefined|nan|null)\b", cleaned, re.IGNORECASE):
        return ""
    words = cleaned.split()
    if len(words) > max_words and cleaned.count(";") >= 3:
        return ""
    if len(words) > max_words:
        cleaned = " ".join(words[:max_words]).rstrip(" ,;:") + "."
    cleaned = re.sub(r"\s+for\s+\d+\.?$", ".", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if re.search(r"\b(?:and|or|for|of|with|to|in)\.?$", cleaned, re.IGNORECASE):
        return ""
    return cleaned


def _details_for_candidate(
    spoken: str,
    source: str,
    citations: list[str],
    confidence: str,
    chunks: list[dict] | None = None,
) -> str:
    del source, citations, confidence
    lines = [spoken.strip()]
    points: list[str] = []
    seen: set[str] = {re.sub(r"\W+", " ", spoken.casefold()).strip()}
    for chunk in (chunks or [])[:4]:
        for sentence in _sentence_candidates(_answer_body_text(chunk))[:4]:
            point = _short_detail_point(sentence)
            if not point or len(point.split()) < 5:
                continue
            key = re.sub(r"\W+", " ", point.casefold()).strip()
            if not key or key in seen:
                continue
            if key in seen or seen and any(key in existing or existing in key for existing in seen):
                continue
            seen.add(key)
            points.append(point)
            if len(points) >= 3:
                break
        if len(points) >= 3:
            break
    if points:
        lines.extend(["", "## Relevant details", *(f"- {point}" for point in points)])
    return "\n".join(line for line in lines if line is not None).strip()


def _trim_for_first_spoken(text: str) -> str:
    cleaned = sanitize_spoken_text(text, keep_digits=True)
    if not cleaned:
        return ""
    parts = [part.strip() for part in _SENTENCE_RE.split(cleaned) if part.strip()]
    if parts:
        cleaned = " ".join(parts[:2])
    words = cleaned.split()
    if len(words) > 40:
        cleaned = " ".join(words[:40]).rstrip(" ,;:") + "."
    return cleaned


def _extract_json_string_field_progress(raw: str, field: str) -> tuple[str, bool] | None:
    """Return the decoded value of a JSON string field from partial model output."""
    marker = f'"{field}"'
    start = raw.find(marker)
    if start < 0:
        return None

    colon = raw.find(":", start + len(marker))
    if colon < 0:
        return None

    i = colon + 1
    while i < len(raw) and raw[i].isspace():
        i += 1
    if i >= len(raw) or raw[i] != '"':
        return None

    i += 1
    value_chars: list[str] = []
    escaped = False
    complete = False
    while i < len(raw):
        ch = raw[i]
        i += 1
        if escaped:
            value_chars.append("\\" + ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            complete = True
            break
        value_chars.append(ch)

    value = "".join(value_chars)
    if complete:
        try:
            return json.loads('"' + value + '"'), True
        except Exception:
            return value, True
    return value, False


def _parse_json_string_at(raw: str, start_quote: int) -> tuple[str, int, bool] | None:
    if start_quote >= len(raw) or raw[start_quote] != '"':
        return None
    i = start_quote + 1
    value_chars: list[str] = []
    escaped = False
    while i < len(raw):
        ch = raw[i]
        i += 1
        if escaped:
            value_chars.append("\\" + ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            value = "".join(value_chars)
            try:
                return json.loads('"' + value + '"'), i, True
            except Exception:
                return value, i, True
        value_chars.append(ch)
    return "".join(value_chars), i, False


def _extract_json_array_string_values_progress(raw: str, field: str, *, start_at: int = 0) -> list[str]:
    marker = f'"{field}"'
    start = raw.find(marker, start_at)
    if start < 0:
        return []
    colon = raw.find(":", start + len(marker))
    if colon < 0:
        return []
    i = colon + 1
    while i < len(raw) and raw[i].isspace():
        i += 1
    if i >= len(raw) or raw[i] != "[":
        return []
    i += 1
    values: list[str] = []
    while i < len(raw):
        ch = raw[i]
        if ch == "]":
            return values
        if ch == '"':
            parsed = _parse_json_string_at(raw, i)
            if parsed is None:
                return values
            value, next_i, complete = parsed
            if complete and value.strip():
                values.append(value.strip())
                i = next_i
                continue
            return values
        i += 1
    return values


def _extract_completed_string_fields_after(raw: str, field: str, *, marker: str) -> list[str]:
    start = raw.find(marker)
    if start < 0:
        return []
    values: list[str] = []
    search_from = start + len(marker)
    field_marker = f'"{field}"'
    while True:
        pos = raw.find(field_marker, search_from)
        if pos < 0:
            return values
        colon = raw.find(":", pos + len(field_marker))
        if colon < 0:
            return values
        i = colon + 1
        while i < len(raw) and raw[i].isspace():
            i += 1
        if i < len(raw) and raw[i] == '"':
            parsed = _parse_json_string_at(raw, i)
            if parsed is None:
                return values
            value, next_i, complete = parsed
            if complete and value.strip():
                values.append(value.strip())
                search_from = next_i
                continue
            return values
        search_from = i + 1


def _details_voice_prefix_from_partial_json(raw: str) -> str:
    parts: list[str] = []
    summary_progress = _extract_json_string_field_progress(raw, "summary")
    if summary_progress is not None:
        summary, complete = summary_progress
        summary_prefix = _streamable_sentence_prefix(summary, complete)
        if summary_prefix:
            parts.append(summary_prefix)
    parts.extend(_extract_json_array_string_values_progress(raw, "points"))
    sections_start = raw.find('"sections"')
    if sections_start >= 0:
        parts.extend(_extract_completed_string_fields_after(raw, "text", marker='"sections"'))
        search_from = sections_start
        while True:
            items = _extract_json_array_string_values_progress(raw, "items", start_at=search_from)
            if not items:
                break
            parts.extend(items)
            next_pos = raw.find('"items"', search_from + 7)
            if next_pos < 0 or next_pos <= search_from:
                break
            search_from = next_pos
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = re.sub(r"\s+", " ", part).strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return " ".join(out).strip()


def _streamable_spoken_prefix(text: str, language: str, complete: bool) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if complete:
        return text

    boundary_chars = ".。"
    last_boundary = -1
    for idx, char in enumerate(text):
        if char in boundary_chars:
            last_boundary = idx + 1
            break
    word_prefix = _word_count_streaming_prefix(text)
    if last_boundary > 0 and word_prefix:
        return text[: min(last_boundary, len(word_prefix))].strip()
    if last_boundary > 0:
        return text[:last_boundary].strip()
    return word_prefix


def _streamable_sentence_prefix(text: str, complete: bool) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if complete:
        return text
    last_boundary = -1
    for idx, char in enumerate(text):
        if char in _STREAM_TERMINAL_PUNCT:
            last_boundary = idx + 1
    if last_boundary > 0:
        return text[:last_boundary].strip()
    return ""


def _word_count_streaming_prefix(text: str) -> str:
    words = list(_STREAM_WORD_RE.finditer(text))
    if len(words) < _TTS_MAX_WORD_COUNT:
        return ""
    end = words[_TTS_MAX_WORD_COUNT - 1].end()
    lookahead = end
    while lookahead < len(text) and text[lookahead].isspace():
        lookahead += 1
    if lookahead < len(text) and text[lookahead] in _STREAM_TERMINAL_PUNCT:
        end = lookahead + 1
    return text[:end].strip()


def _spoken_delta_from_partial_json(
    raw: str,
    language: str,
    emitted_chars: int,
) -> tuple[str, int, bool] | None:
    progress = _extract_json_string_field_progress(raw, "spoken")
    if progress is None:
        return None
    spoken, complete = progress
    prefix = _streamable_spoken_prefix(spoken, language, complete)
    if len(prefix) <= emitted_chars:
        return None
    delta = prefix[emitted_chars:]
    if not delta.strip():
        return None
    return delta, len(prefix), complete and len(prefix) >= len(spoken.strip())


def _voice_delta_from_partial_json(
    raw: str,
    language: str,
    emitted_chars: int,
) -> tuple[str, int, bool] | None:
    spoken_delta = _spoken_delta_from_partial_json(raw, language, emitted_chars)
    if spoken_delta is not None:
        return spoken_delta

    prefix = _details_voice_prefix_from_partial_json(raw)
    if not prefix:
        return None
    if len(prefix) <= emitted_chars:
        return None
    delta = prefix[emitted_chars:]
    if not delta.strip():
        return None
    return delta, len(prefix), False


def _local_rag_lock() -> asyncio.Lock:
    global _LOCAL_RAG_BUSY_LOCK
    if _LOCAL_RAG_BUSY_LOCK is None:
        _LOCAL_RAG_BUSY_LOCK = asyncio.Lock()
    return _LOCAL_RAG_BUSY_LOCK


def _answer_cache_lock() -> asyncio.Lock:
    global _ANSWER_CACHE_LOCK
    if _ANSWER_CACHE_LOCK is None:
        _ANSWER_CACHE_LOCK = asyncio.Lock()
    return _ANSWER_CACHE_LOCK


def _release_local_rag_slot(_: object) -> None:
    global _LOCAL_RAG_BUSY
    _LOCAL_RAG_BUSY = False


async def _run_local_rag_singleflight(
    query: str,
    history: list[dict[str, str]],
    conversation_memory: dict | None,
) -> tuple[object, list[dict], dict[str, Any] | None] | None:
    global _LOCAL_RAG_BUSY
    async with _local_rag_lock():
        if _LOCAL_RAG_BUSY:
            return None
        _LOCAL_RAG_BUSY = True

    released = False

    def release_slot(_: object | None = None) -> None:
        nonlocal released
        global _LOCAL_RAG_BUSY
        if released:
            return
        released = True
        _LOCAL_RAG_BUSY = False

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        None,
        fast_answer_plan_retrieve,
        query,
        history,
        conversation_memory,
    )
    future.add_done_callback(release_slot)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        release_slot()
        raise


async def _memory_cache_candidate(query: str, language: str) -> RaceCandidate | None:
    normalized = _normalized_query(query)
    query_tokens = _tokens(query)
    async with _answer_cache_lock():
        best_entry: dict[str, Any] | None = None
        best_score = 0.0
        best_idx = -1
        for idx, entry in enumerate(_ANSWER_CACHE):
            if entry.get("language") != language:
                continue
            score = 1.0 if entry.get("normalized_query") == normalized else _jaccard(query_tokens, entry.get("tokens", set()))
            if score > best_score:
                best_score = score
                best_entry = entry
                best_idx = idx
        if best_entry is None or best_score < CACHE_WIN_THRESHOLD:
            return None
        _ANSWER_CACHE.append(_ANSWER_CACHE.pop(best_idx))
        return RaceCandidate(
            source="answer_cache",
            confidence=str(best_entry.get("confidence", "high")),
            score=_confidence_score(str(best_entry.get("confidence", "high")), best_score),
            tagged_answer=str(best_entry.get("tagged_answer") or ""),
            raw_answer=str(best_entry.get("raw_answer") or ""),
            plan=best_entry.get("plan"),
            chunks=list(best_entry.get("chunks") or []),
            citations=list(best_entry.get("citations") or []),
            cacheable=False,
        )


async def _remember_answer(query: str, winner: RaceCandidate) -> None:
    if not winner.cacheable or winner.fallback or not (winner.tagged_answer or winner.raw_answer):
        return
    entry = {
        "normalized_query": _normalized_query(query),
        "tokens": _tokens(query),
        "language": getattr(winner.plan, "answer_language", "en"),
        "confidence": winner.confidence,
        "tagged_answer": winner.tagged_answer,
        "raw_answer": winner.raw_answer,
        "plan": winner.plan,
        "chunks": winner.chunks,
        "citations": winner.citations,
    }
    async with _answer_cache_lock():
        _ANSWER_CACHE.append(entry)
        while len(_ANSWER_CACHE) > _ANSWER_CACHE_MAX:
            _ANSWER_CACHE.pop(0)


async def clear_answer_caches() -> dict[str, int]:
    semantic_count = await asyncio.to_thread(clear_semantic_answer_cache)
    async with _answer_cache_lock():
        answer_count = len(_ANSWER_CACHE)
        _ANSWER_CACHE.clear()
    return {
        "semantic_answer_cache": semantic_count,
        "answer_cache": answer_count,
    }


def _candidate_from_answer(
    *,
    source: str,
    answer: str,
    language: str,
    confidence: str = "high",
    score: float = 0.92,
    plan: object | None = None,
    chunks: list[dict] | None = None,
    citations: list[str] | None = None,
    cacheable: bool = False,
) -> RaceCandidate:
    chunks = chunks or []
    citations = citations if citations is not None else _citations(chunks)
    spoken = _trim_for_first_spoken(answer)
    details_answer = sanitize_spoken_text(answer, keep_digits=True) or spoken
    details = _details_for_candidate(details_answer, source, citations, confidence, chunks)
    return RaceCandidate(
        source=source,
        confidence=confidence,
        score=_confidence_score(confidence, score),
        tagged_answer=wrap_spoken_and_details(spoken, details),
        plan=plan,
        chunks=chunks,
        citations=citations,
        cacheable=cacheable,
    )


def _json_details_to_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    lines: list[str] = []
    summary = str(value.get("summary") or "").strip()
    if summary:
        lines.append(summary)
    points = value.get("points")
    if isinstance(points, list):
        for item in points:
            text = str(item).strip()
            if text:
                lines.append(f"- {text}")
    sections = value.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            title = str(section.get("title") or "").strip()
            text = str(section.get("text") or "").strip()
            items = [str(item).strip() for item in section.get("items") or [] if str(item).strip()]
            if title and title.lower() != "details" and (text or items):
                lines.append(f"### {title}")
            if text:
                lines.append(text)
            lines.extend(f"- {item}" for item in items)
    return "\n".join(lines).strip()


def _json_followups_to_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    return "\n".join(f"- {str(item).strip()}" for item in value if str(item).strip())


def _tagged_from_raw_json(raw_answer: str) -> str:
    payload = _extract_json_payload(raw_answer) or _extract_json_from_wrapped(raw_answer)
    if not isinstance(payload, dict):
        return ""
    spoken = str(payload.get("spoken") or "").strip()
    details = _json_details_to_text(payload.get("details"))
    followups = _json_followups_to_text(payload.get("follow_up_questions") or payload.get("followups"))
    if not spoken and not details and not followups:
        return ""
    return rebuild_blocks(spoken, details or spoken, followups)


def _is_fallback_raw_answer(raw_answer: str) -> bool:
    payload = _extract_json_payload(raw_answer) or _extract_json_from_wrapped(raw_answer)
    if not isinstance(payload, dict):
        return False
    details = payload.get("details")
    answer_kind = ""
    if isinstance(details, dict):
        answer_kind = str(details.get("answer_kind") or "").strip().lower()
    spoken = str(payload.get("spoken") or "").casefold()
    return answer_kind == "fallback" or "couldn't find a reliable answer" in spoken or "aifc.kz" in spoken


def _fallback_message(language: str, query: str) -> RaceCandidate:
    spoken_by_lang = {
        "ru": "Извините, я не нашел надежный ответ в своей базе знаний. Пожалуйста, посетите aifc.kz.",
        "kk": "Кешіріңіз, мен білім базасынан сенімді жауап таба алмадым. aifc.kz сайтына кіріңіз.",
        "zh": "抱歉，我没有在知识库中找到可靠答案。请访问 aifc.kz。",
        "en": "Sorry, I couldn't find a reliable answer in my knowledge base. Please visit aifc.kz.",
    }
    details_by_lang = {
        "ru": "Извините, я не нашел надежный ответ в своей базе знаний. Пожалуйста, посетите aifc.kz.",
        "kk": "Кешіріңіз, мен білім базасынан сенімді жауап таба алмадым. aifc.kz сайтына кіріңіз.",
        "zh": "抱歉，我没有在知识库中找到可靠答案。请访问 aifc.kz。",
        "en": "Sorry, I couldn't find a reliable answer in my knowledge base. Please visit aifc.kz.",
    }
    spoken = spoken_by_lang.get(language, spoken_by_lang["en"])
    details = details_by_lang.get(language, details_by_lang["en"])
    candidate = RaceCandidate(
        source="fallback",
        confidence="not_found",
        score=0.0,
        tagged_answer=rebuild_blocks(spoken, details, ""),
        plan=SimpleNamespace(answer_language=language, route="fallback", is_chitchat=False),
        chunks=[],
        citations=["aifc.kz"],
        cacheable=False,
        fallback=True,
    )
    log.info("knowledge_gap query=%r fallback_site=aifc.kz", query[:200])
    return candidate


def _aifc_overview_candidate(query: str, language: str) -> RaceCandidate | None:
    if not _is_aifc_overview_query(query):
        return None
    spoken_by_lang = {
        "ru": "МФЦА — это Международный финансовый центр «Астана» в Казахстане, созданный как финансовый хаб с собственной правовой и регуляторной средой.",
        "kk": "АХҚО — Қазақстандағы Астана халықаралық қаржы орталығы, өз құқықтық және реттеуші ортасы бар қаржы хабы.",
        "zh": "AIFC 是哈萨克斯坦的阿斯塔纳国际金融中心，是一个拥有自身法律和监管环境的金融中心。",
        "en": "AIFC is the Astana International Financial Centre in Kazakhstan, a financial hub with its own legal and regulatory environment.",
    }
    details_by_lang = {
        "ru": (
            "МФЦА поддерживает развитие финансовых услуг, рынков капитала, финтеха, "
            "зеленого финансирования и инвестиционной инфраструктуры в Казахстане и регионе.\n\n"
            "Я могу помочь с темами МФЦА, включая регистрацию, документы, AFSA и регулирование, "
            "FinTech Lab, AIX, Expat Centre, AIFC Court и International Arbitration Centre."
        ),
        "kk": (
            "АХҚО Қазақстанда және өңірде қаржы қызметтерін, капитал нарықтарын, финтехті, "
            "жасыл қаржыландыруды және инвестициялық инфрақұрылымды дамытуға қолдау көрсетеді.\n\n"
            "Мен АХҚО бойынша тіркеу, құжаттар, AFSA және реттеу, FinTech Lab, AIX, Expat Centre, "
            "AIFC Court және International Arbitration Centre тақырыптарында көмектесе аламын."
        ),
        "zh": (
            "AIFC 支持哈萨克斯坦及区域内的金融服务、资本市场、金融科技、绿色金融和投资基础设施发展。\n\n"
            "我可以帮助解答 AIFC 注册、文件、AFSA 和监管、FinTech Lab、AIX、Expat Centre、"
            "AIFC Court 以及 International Arbitration Centre 相关问题。"
        ),
        "en": (
            "AIFC supports financial services, capital markets, fintech, green finance, "
            "and investment infrastructure in Kazakhstan and the wider region.\n\n"
            "I can help with AIFC topics such as registration, documents, AFSA and regulation, "
            "the FinTech Lab, AIX, Expat Centre, AIFC Court, and the International Arbitration Centre."
        ),
    }
    spoken = spoken_by_lang.get(language, spoken_by_lang["en"])
    details = details_by_lang.get(language, details_by_lang["en"])
    return RaceCandidate(
        source="aifc_overview",
        confidence="high",
        score=0.96,
        tagged_answer=rebuild_blocks(spoken, details, ""),
        plan=SimpleNamespace(answer_language=language, route="overview", is_chitchat=False),
        citations=["AIFC knowledge base"],
        cacheable=True,
    )


def common_tts_prewarm_items() -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = [
        ("Hello, welcome. I can help with AIFC services, registration, regulation, courts, arbitration, fintech, and investor topics.", "en"),
        ("AIFC is the Astana International Financial Centre in Kazakhstan, a financial hub with its own legal and regulatory environment.", "en"),
        ("AIFC is the Astana International Financial Centre", "en"),
        ("The Expat Centre offers streamlined access to", "en"),
        ("To register a company at the AIFC,", "en"),
        ("Goodbye. I will be here if you have more questions about AIFC.", "en"),
        ("Sorry, I couldn't find a reliable answer in my knowledge base. Please visit aifc.kz.", "en"),
        ("Здравствуйте, добро пожаловать. Я могу помочь с вопросами о МФЦА, регистрации, регулировании, суде, арбитраже и финтехе.", "ru"),
        ("До свидания. Я буду рад помочь, если появятся новые вопросы о МФЦА.", "ru"),
        ("Извините, я не нашел надежный ответ в своей базе знаний. Пожалуйста, посетите aifc.kz.", "ru"),
        ("Сәлеметсіз бе, қош келдіңіз. Мен АХҚО қызметтері, тіркеу, реттеу, сот, арбитраж және финтех бойынша көмектесе аламын.", "kk"),
        ("Сау болыңыз. АХҚО бойынша тағы сұрақтарыңыз болса, көмектесуге дайынмын.", "kk"),
        ("Кешіріңіз, мен білім базасынан сенімді жауап таба алмадым. aifc.kz сайтына кіріңіз.", "kk"),
        ("您好，欢迎咨询。我可以帮助解答 AIFC 服务、注册、监管、法院、仲裁和金融科技相关问题。", "zh"),
        ("再见。如果之后您有 AIFC 相关问题，我可以继续帮助您。", "zh"),
        ("抱歉，我没有在知识库中找到可靠答案。请访问 aifc.kz。", "zh"),
    ]
    deduped: list[tuple[str, str]] = []
    seen = set()
    for text, language in items:
        key = (text, language)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((text, language))
    items = deduped
    seen_texts = {text for text, _ in items}
    for entry in _HARDCODED_FAQ[:4]:
        spoken = _trim_for_first_spoken(str(entry.get("answer", "")))
        if spoken and spoken not in seen_texts:
            items.append((spoken, "en"))
            seen_texts.add(spoken)
    return items


async def _faq_candidate(query: str, language: str) -> RaceCandidate | None:
    overview = _aifc_overview_candidate(query, language)
    if overview is not None:
        overview.source = "faq"
        if overview.plan is not None:
            setattr(overview.plan, "route", "faq_overview")
        return overview
    if language != "en":
        return None
    q_tokens = _tokens(query)
    best_entry: dict[str, Any] | None = None
    best_score = 0.0
    for entry in _HARDCODED_FAQ:
        for question in entry.get("questions", []):
            score = _jaccard(q_tokens, _tokens(str(question)))
            if score > best_score:
                best_score = score
                best_entry = entry
    if best_entry and best_score >= FAQ_WIN_THRESHOLD:
        return _candidate_from_answer(
            source="faq",
            answer=str(best_entry.get("answer", "")),
            language=language,
            confidence="high",
            score=best_score,
            citations=list(best_entry.get("citations") or []),
            cacheable=True,
        )
    with contextlib.suppress(Exception):
        hit = await asyncio.to_thread(_faq_fast_path_lookup, query)
        if hit and float(hit.get("similarity", 0.0)) >= CACHE_WIN_THRESHOLD:
            return _candidate_from_answer(
                source="semantic_faq",
                answer=str(hit.get("answer", "")),
                language=language,
                confidence="high",
                score=float(hit.get("similarity", 0.0)),
                citations=list(hit.get("citations") or []),
                cacheable=True,
            )
    return None


async def _cache_candidate(query: str, language: str) -> RaceCandidate | None:
    with contextlib.suppress(Exception):
        hit = await asyncio.to_thread(_semantic_answer_cache_lookup, query)
        if not hit:
            return None
        score = float(hit.get("similarity", 0.0))
        plan = hit.get("plan")
        answer_language = getattr(plan, "answer_language", language)
        if score < CACHE_WIN_THRESHOLD or answer_language != language:
            return None
        raw_answer = str(hit.get("raw_answer") or "")
        tagged_answer = str(hit.get("tagged_answer") or "")
        if raw_answer or tagged_answer:
            return RaceCandidate(
                source="semantic_cache",
                confidence=str(hit.get("confidence", "high")),
                score=_confidence_score(str(hit.get("confidence", "high")), score),
                raw_answer=raw_answer,
                tagged_answer=tagged_answer,
                plan=plan,
                chunks=list(hit.get("chunks") or []),
                citations=list(hit.get("citations") or []),
                cacheable=False,
            )
        return _candidate_from_answer(
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
    return None


def _external_rag_plan(language: str) -> SimpleNamespace:
    return SimpleNamespace(answer_language=language, route=EXTERNAL_INTERNAL_RAG_TOOL, is_chitchat=False)


def _external_not_found_candidate(language: str) -> RaceCandidate:
    return RaceCandidate(
        EXTERNAL_INTERNAL_RAG_TOOL,
        "not_found",
        0.0,
        plan=_external_rag_plan(language),
        chunks=[],
        cacheable=False,
    )


async def _external_rag_candidate(
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
        if citations and isinstance(contract.get("details"), dict):
            details = dict(contract["details"])
            details.setdefault("citations", citations)
            details.setdefault("answer_kind", "internal_rag")
            contract["details"] = details
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

    return _candidate_from_answer(
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


async def _local_rag_candidate(
    query: str,
    language: str,
    history: list[dict[str, str]],
    conversation_memory: dict | None,
) -> RaceCandidate | None:
    result = await _run_local_rag_singleflight(query, history, conversation_memory)
    if result is None:
        return RaceCandidate("local_rag", "not_found", 0.0, chunks=[])
    plan, chunks, fast_hit = result
    answer_language = getattr(plan, "answer_language", language) or language
    if fast_hit and fast_hit.get("answer") and answer_language == language == "en":
        return _candidate_from_answer(
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
    return _candidate_from_answer(
        source="local_rag",
        answer=answer,
        language=language,
        confidence=confidence,
        score=score,
        plan=plan,
        chunks=chunks,
        cacheable=confidence == "high",
    )


async def _gemini_local_rag_candidate(
    query: str,
    language: str,
    history: list[dict[str, str]],
    conversation_memory: dict | None,
    local_task: asyncio.Task[RaceCandidate | None],
    *,
    on_context_ready: GeminiContextCallback | None = None,
    on_voice_delta: GeminiSpokenCallback | None = None,
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
    emitted_voice_chars = 0
    async for delta in stream_answer(history_msgs, prompt):
        raw += delta or ""
        if on_voice_delta is not None:
            voice_delta = _voice_delta_from_partial_json(raw, language, emitted_voice_chars)
            if voice_delta is not None:
                text_delta, emitted_voice_chars, complete = voice_delta
                await on_voice_delta(text_delta, emitted_voice_chars, complete)
    if not raw.strip():
        return None
    is_fallback = _is_fallback_raw_answer(raw)
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


async def _timed(name: str, coro) -> RaceCandidate | None:
    started = perf_counter()
    try:
        candidate = await coro
        if candidate is not None:
            candidate.timings[f"{name}_ms"] = int((perf_counter() - started) * 1000)
        return candidate
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("answer race candidate failed: %s", name)
        return None


def _cache_winner(query: str, winner: RaceCandidate) -> None:
    if not winner.cacheable:
        return
    if winner.fallback or not (winner.tagged_answer or winner.raw_answer):
        return
    tagged_answer = winner.tagged_answer or _tagged_from_raw_json(winner.raw_answer)
    spoken, details, followups = extract_blocks(tagged_answer)
    answer = sanitize_spoken_text(spoken or tagged_answer, keep_digits=True)
    if not answer:
        return
    plan = winner.plan or SimpleNamespace(answer_language="en", retrieval_queries=[query])
    retrieval_queries = [
        str(item).strip()
        for item in getattr(plan, "retrieval_queries", [])[:3]
        if str(item).strip()
    ]
    rewritten_query = " ; ".join(retrieval_queries) or query
    with contextlib.suppress(Exception):
        _semantic_answer_cache_put(
            rewritten_query=rewritten_query,
            plan=plan,
            chunks=winner.chunks,
            answer=answer,
            citations=winner.citations,
            confidence=winner.confidence,
            follow_up=None,
            handoff_required=False,
            tagged_answer=tagged_answer,
            raw_answer=winner.raw_answer,
            details=details,
            followups=followups,
        )


async def run_answer_race(
    query: str,
    language: str,
    history: list[dict[str, str]],
    conversation_memory: dict | None,
    *,
    on_gemini_context_ready: GeminiContextCallback | None = None,
    on_gemini_voice_delta: GeminiSpokenCallback | None = None,
) -> AnswerRaceResult:
    started = perf_counter()
    candidates: list[RaceCandidate] = []
    timings: dict[str, object] = {}
    selected_tool = select_rag_tool(query)
    timings["selected_rag_tool"] = selected_tool
    log.info("answer RAG tool selected=%s query=%r", selected_tool, query[:160])

    if selected_tool == EXTERNAL_INTERNAL_RAG_TOOL:
        try:
            candidate = await asyncio.wait_for(
                _timed(
                    EXTERNAL_INTERNAL_RAG_TOOL,
                    _external_rag_candidate(query, language, history, conversation_memory),
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
        winner = candidate if candidate is not None and (candidate.tagged_answer or candidate.raw_answer) else None
        if candidate is not None:
            candidates.append(candidate)
            timings.update(candidate.timings)
            log.info(
                "answer race candidate source=%s confidence=%s score=%.3f has_answer=%s chunks=%d",
                candidate.source,
                candidate.confidence,
                candidate.score,
                bool(candidate.tagged_answer or candidate.raw_answer),
                len(candidate.chunks),
            )
        if winner is None:
            winner = _fallback_message(language, query)
            candidates.append(winner)
        timings["answer_race_ms"] = int((perf_counter() - started) * 1000)
        timings["winner_source"] = winner.source
        timings["winner_confidence"] = winner.confidence
        return AnswerRaceResult(winner=winner, timings=timings, candidates=candidates)

    created_tasks: set[asyncio.Task[RaceCandidate | None]] = set()
    gemini_voice_buffer: list[tuple[str, int, bool]] = []
    gemini_voice_text = ""
    gemini_voice_committed = False

    async def buffered_gemini_voice_delta(text_delta: str, emitted_chars: int, complete: bool) -> None:
        nonlocal gemini_voice_text
        if not text_delta:
            return
        gemini_voice_text += text_delta
        if gemini_voice_committed:
            if on_gemini_voice_delta is not None:
                await on_gemini_voice_delta(text_delta, emitted_chars, complete)
            return
        gemini_voice_buffer.append((text_delta, emitted_chars, complete))

    async def commit_gemini_voice() -> None:
        nonlocal gemini_voice_committed
        if gemini_voice_committed:
            return
        gemini_voice_committed = True
        if on_gemini_voice_delta is None:
            gemini_voice_buffer.clear()
            return
        for text_delta, emitted_chars, complete in gemini_voice_buffer:
            await on_gemini_voice_delta(text_delta, emitted_chars, complete)
        gemini_voice_buffer.clear()

    def create_candidate_task(name: str, coro) -> asyncio.Task[RaceCandidate | None]:
        task = asyncio.create_task(_timed(name, coro))
        created_tasks.add(task)
        return task

    local_task = create_candidate_task("local_rag", _local_rag_candidate(query, language, history, conversation_memory))
    gemini_task = create_candidate_task(
        "gemini_local_rag",
        _gemini_local_rag_candidate(
            query,
            language,
            history,
            conversation_memory,
            local_task,
            on_context_ready=on_gemini_context_ready,
            on_voice_delta=buffered_gemini_voice_delta,
        ),
    )
    tasks: set[asyncio.Task[RaceCandidate | None]] = {
        # Public RAG tool: fast public caches/FAQ may win; local RAG retrieves context for Gemini.
        create_candidate_task("answer_cache", _memory_cache_candidate(query, language)),
        create_candidate_task("faq", _faq_candidate(query, language)),
        create_candidate_task("cache", _cache_candidate(query, language)),
        gemini_task,
    }
    winner: RaceCandidate | None = None
    deadline = started + ANSWER_RACE_TIMEOUT_MS / 1000.0
    max_wait_deadline = started + GEMINI_RAG_MAX_WAIT_MS / 1000.0

    try:
        while tasks and perf_counter() < deadline:
            timeout = max(0.0, deadline - perf_counter())
            done, tasks = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            for task in done:
                candidate = task.result()
                if candidate is None:
                    continue
                candidates.append(candidate)
                timings.update(candidate.timings)
                log.info(
                    "answer race candidate source=%s confidence=%s score=%.3f has_answer=%s chunks=%d",
                    candidate.source,
                    candidate.confidence,
                    candidate.score,
                    bool(candidate.tagged_answer or candidate.raw_answer),
                    len(candidate.chunks),
                )
                if candidate.tagged_answer or candidate.raw_answer:
                    if task is gemini_task:
                        await commit_gemini_voice()
                    winner = candidate
                    break
            if winner is not None:
                break
            if tasks == {gemini_task}:
                break

        if winner is None:
            local_candidate: RaceCandidate | None = None
            try:
                local_candidate = local_task.result() if local_task.done() else await asyncio.wait_for(
                    asyncio.shield(local_task),
                    timeout=max(0.0, max_wait_deadline - perf_counter()),
                )
            except asyncio.TimeoutError:
                local_candidate = None
            if local_candidate is not None:
                if local_candidate not in candidates:
                    candidates.append(local_candidate)
                timings.update(local_candidate.timings)
                log.info(
                    "answer race candidate source=%s confidence=%s score=%.3f has_answer=%s chunks=%d",
                    local_candidate.source,
                    local_candidate.confidence,
                    local_candidate.score,
                    bool(local_candidate.tagged_answer or local_candidate.raw_answer),
                    len(local_candidate.chunks),
                )
                if local_candidate.tagged_answer or local_candidate.raw_answer:
                    winner = local_candidate

        if winner is None:
            await commit_gemini_voice()
            try:
                candidate = await asyncio.wait_for(
                    asyncio.shield(gemini_task),
                    timeout=max(0.0, max_wait_deadline - perf_counter()),
                )
            except asyncio.TimeoutError:
                candidate = None
            if candidate is None and gemini_voice_committed and gemini_voice_text.strip():
                candidate = _candidate_from_answer(
                    source="gemini_partial",
                    answer=gemini_voice_text.strip(),
                    language=language,
                    confidence="partial",
                    score=0.5,
                    plan=SimpleNamespace(answer_language=language, route=GEMINI_PUBLIC_RAG_TOOL, is_chitchat=False),
                    chunks=[],
                    cacheable=False,
                )
            if candidate is not None:
                if candidate not in candidates:
                    candidates.append(candidate)
                timings.update(candidate.timings)
                log.info(
                    "answer race candidate source=%s confidence=%s score=%.3f has_answer=%s chunks=%d",
                    candidate.source,
                    candidate.confidence,
                    candidate.score,
                    bool(candidate.tagged_answer or candidate.raw_answer),
                    len(candidate.chunks),
                )
                if candidate.tagged_answer or candidate.raw_answer:
                    winner = candidate

        if winner is None:
            winner = _fallback_message(language, query)
            candidates.append(winner)

        timings["answer_race_ms"] = int((perf_counter() - started) * 1000)
        timings["winner_source"] = winner.source
        timings["winner_confidence"] = winner.confidence
        if winner.cacheable:
            await _remember_answer(query, winner)
            asyncio.create_task(asyncio.to_thread(_cache_winner, query, winner))
        return AnswerRaceResult(winner=winner, timings=timings, candidates=candidates)
    finally:
        for task in created_tasks:
            if not task.done():
                task.cancel()
        if created_tasks:
            await asyncio.gather(*created_tasks, return_exceptions=True)
