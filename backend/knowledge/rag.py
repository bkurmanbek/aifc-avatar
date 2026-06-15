from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any

from rag.service import retrieve

from ..utils.language import detect_supported_text_language, normalize_lang
from ..utils.spoken_text import sanitize_spoken_text
from .faq import _faq_fast_path_lookup, _prebuilt_chitchat_answer

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9]+|[一-鿿]", re.UNICODE)

_ANSWER_SYSTEM = """\
You are a helpful assistant for the AIFC (Astana International Financial Centre).
Answer only from the retrieved AIFC knowledge-base context. If the context does
not contain a reliable answer, say that clearly and direct the user to aifc.kz.
Keep the spoken answer concise, direct, and complete enough for a user who may
not read the full chat answer. Do not include a sources section in spoken.
Include contact details only when the user explicitly asks for contact details.
"""


def _tokens(text: str) -> set[str]:
    return {m.group(0).casefold() for m in _WORD_RE.finditer(text or "") if m.group(0).strip()}


def _chunk_text(chunk: dict) -> str:
    return str(chunk.get("text") or chunk.get("content") or chunk.get("context") or "").strip()


def _chunk_source(chunk: dict) -> str:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    return str(
        chunk.get("source_file")
        or metadata.get("source_file")
        or metadata.get("section_title")
        or chunk.get("documentName")
        or chunk.get("domain")
        or chunk.get("chunk_id")
        or "AIFC knowledge base"
    ).strip()


def _chunk_score(chunk: dict) -> float:
    value = chunk.get("rerank_score", chunk.get("similarity", chunk.get("ann_score", 0.0)))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _build_context(chunks: list[dict]) -> str:
    if not chunks:
        return "No retrieved context."

    blocks: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        text = _chunk_text(chunk)
        if not text:
            continue
        score = _chunk_score(chunk)
        score_line = f"score={score:.3f}" if score else "score=unknown"
        blocks.append(f"[{index}] {_chunk_source(chunk)} ({score_line})\n{text}")
    return "\n\n".join(blocks) if blocks else "No retrieved context."


def _detect_answer_language(query: str) -> str:
    return normalize_lang(detect_supported_text_language(query) or "en")


def _new_plan(query: str, chunks: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        answer_language=_detect_answer_language(query),
        route="rag" if chunks else "direct",
        is_chitchat=False,
        needs_clarification=False,
        clarification_question=None,
        retrieval_queries=[query],
        expert_mode=False,
        needs_widget=False,
    )


def fast_plan_from_rag(query: str, chunks: list[dict] | None = None) -> SimpleNamespace:
    plan = _new_plan(query, chunks)
    if _prebuilt_chitchat_answer(query, plan.answer_language):
        plan.is_chitchat = True
        plan.route = "chitchat"
    if chunks:
        plan.route = "rag"
    return plan


def _retrieve_with_fast_path(query: str, plan: object) -> tuple[list[dict], dict[str, Any] | None]:
    chunks: list[dict] = []
    try:
        chunks = retrieve(query)
    except Exception:
        log.exception("local RAG retrieve failed")

    language = normalize_lang(getattr(plan, "answer_language", "en"))
    fast_hit: dict[str, Any] | None = None
    if language == "en":
        hit = _faq_fast_path_lookup(query, language)
        if hit and float(hit.get("similarity", 0.0)) >= 0.9:
            hit["chunks"] = chunks
            fast_hit = hit
    return chunks, fast_hit


def fast_answer_plan_retrieve(
    query: str,
    history: list[dict] | None = None,
    conversation_memory: dict | None = None,
):
    del history, conversation_memory
    plan = fast_plan_from_rag(query, [])
    if plan.is_chitchat or (plan.needs_clarification and plan.clarification_question):
        return plan, [], None

    chunks, fast_hit = _retrieve_with_fast_path(query, plan)
    plan = fast_plan_from_rag(query, chunks)
    return plan, chunks, fast_hit
