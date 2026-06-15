from __future__ import annotations

import re
import threading
from time import time
from typing import Any

from ..settings import ANSWER_CACHE_TTL_S

_ANSWER_CACHE_MAX = 256
_semantic_answer_cache: list[dict[str, Any]] = []
_semantic_answer_cache_lock = threading.Lock()

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9]+|[一-鿿]", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {m.group(0).casefold() for m in _WORD_RE.finditer(text or "") if m.group(0).strip()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _semantic_answer_cache_lookup(query: str) -> dict[str, Any] | None:
    query_tokens = _tokens(query)
    normalized = " ".join(sorted(query_tokens))
    best_index = -1
    best_score = 0.0
    best_entry: dict[str, Any] | None = None
    now = time()

    with _semantic_answer_cache_lock:
        if ANSWER_CACHE_TTL_S > 0:
            _semantic_answer_cache[:] = [
                entry
                for entry in _semantic_answer_cache
                if now - float(entry.get("cached_at", 0.0) or 0.0) <= ANSWER_CACHE_TTL_S
            ]
        for index, entry in enumerate(_semantic_answer_cache):
            entry_tokens = entry.get("tokens")
            if not isinstance(entry_tokens, set):
                entry_tokens = _tokens(str(entry.get("rewritten_query") or entry.get("query") or ""))
            score = 1.0 if entry.get("normalized") == normalized else _jaccard(query_tokens, entry_tokens)
            if score > best_score:
                best_index = index
                best_score = score
                best_entry = entry
        if best_entry is None:
            return None
        _semantic_answer_cache.append(_semantic_answer_cache.pop(best_index))
        result = dict(best_entry)

    result["similarity"] = best_score
    return result


def _semantic_answer_cache_put(
    *,
    rewritten_query: str,
    plan: object,
    chunks: list[dict],
    answer: str,
    chat: str,
    citations: list[str] | None = None,
    confidence: str = "high",
    raw_answer: str = "",
) -> None:
    tokens = _tokens(rewritten_query)
    entry = {
        "query": rewritten_query,
        "rewritten_query": rewritten_query,
        "normalized": " ".join(sorted(tokens)),
        "tokens": tokens,
        "plan": plan,
        "chunks": list(chunks or []),
        "answer": answer,
        "citations": list(citations or []),
        "confidence": confidence,
        "raw_answer": raw_answer,
        "spoken": answer,
        "chat": chat,
        "cached_at": time(),
    }
    with _semantic_answer_cache_lock:
        _semantic_answer_cache.append(entry)
        while len(_semantic_answer_cache) > _ANSWER_CACHE_MAX:
            _semantic_answer_cache.pop(0)


def clear_semantic_answer_cache() -> int:
    with _semantic_answer_cache_lock:
        count = len(_semantic_answer_cache)
        _semantic_answer_cache.clear()
    return count
