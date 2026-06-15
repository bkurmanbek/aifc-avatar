from __future__ import annotations

from collections import Counter
from typing import Any

from ..utils.spoken_text import sanitize_spoken_text
from .rag import _chunk_source, _tokens


def format_conversation_memory(memory: dict | None) -> str:
    if not memory:
        return "No persistent conversation memory."

    topics = memory.get("topics") or []
    last_question = str(memory.get("last_question") or "").strip()
    last_answer = str(memory.get("last_answer") or "").strip()
    parts: list[str] = []
    if topics:
        parts.append("Recent topics: " + ", ".join(str(item) for item in topics[:8]))
    if last_question:
        parts.append(f"Previous user question: {last_question}")
    if last_answer:
        parts.append(f"Previous assistant answer summary: {last_answer[:500]}")
    return "\n".join(parts) if parts else "No persistent conversation memory."


def update_conversation_memory(
    memory: dict | None,
    user_query: str,
    assistant_summary: str,
    chunks: list[dict] | None = None,
) -> dict:
    memory = dict(memory or {})
    topic_counter = Counter(memory.get("topic_counts") or {})
    for token in _tokens(user_query):
        if len(token) >= 4:
            topic_counter[token] += 1
    for chunk in (chunks or [])[:3]:
        source = _chunk_source(chunk)
        if source and source != "AIFC knowledge base":
            topic_counter[source] += 1
    memory["topic_counts"] = dict(topic_counter.most_common(24))
    memory["topics"] = [item for item, _ in topic_counter.most_common(8)]
    memory["last_question"] = sanitize_spoken_text(user_query, keep_digits=True)
    memory["last_answer"] = sanitize_spoken_text(assistant_summary, keep_digits=True)
    return memory
