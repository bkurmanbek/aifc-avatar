from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from types import SimpleNamespace
from typing import Any

from rag.service import retrieve

from .language import detect_supported_text_language, normalize_lang
from .spoken_text import rebuild_blocks, sanitize_spoken_text

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9]+|[\u4e00-\u9fff]", re.UNICODE)
_CAPABILITY_RE = re.compile(
    r"\b(what can you|what do you|help me|capabilities|topics|services|можешь|умеешь|помочь|көмектесе|не істей|可以|帮助)\b",
    re.IGNORECASE,
)
_ANSWER_CACHE_MAX = 256
_semantic_answer_cache: list[dict[str, Any]] = []
_semantic_answer_cache_lock = threading.Lock()


_ANSWER_SYSTEM = """\
You are a helpful assistant for the AIFC (Astana International Financial Centre).
Answer only from the retrieved AIFC knowledge-base context. If the context does
not contain a reliable answer, say that clearly and direct the user to aifc.kz.
Keep the spoken answer concise, direct, and complete enough for a user who may
not read the details panel. Do not include a sources section. Include contact
details only when the user explicitly asks for contact details.
"""


_HARDCODED_FAQ: list[dict[str, Any]] = [
    {
        "questions": [
            "what is aifc",
            "tell me about aifc",
            "about astana international financial centre",
        ],
        "answer": (
            "AIFC is the Astana International Financial Centre in Kazakhstan, "
            "a financial hub with its own legal and regulatory environment."
        ),
        "citations": ["AIFC knowledge base"],
    },
    {
        "questions": [
            "what can you help with",
            "which aifc topics can you answer",
            "what are your capabilities",
        ],
        "answer": (
            "I can help with AIFC services, registration, regulation, AFSA, "
            "FinTech Lab, AIX, Expat Centre, AIFC Court, arbitration, policy, "
            "and investor-service topics covered by the local knowledge base."
        ),
        "citations": ["AIFC knowledge base"],
    },
    {
        "questions": [
            "what is afsa",
            "tell me about afsa",
            "aifc regulator",
        ],
        "answer": (
            "AFSA is the Astana Financial Services Authority, the independent "
            "regulator for financial services and related activities in the AIFC."
        ),
        "citations": ["AFSA knowledge base"],
    },
    {
        "questions": [
            "what is aix",
            "tell me about aix",
            "astana international exchange",
        ],
        "answer": (
            "AIX is the Astana International Exchange, the AIFC exchange platform "
            "supporting capital-market activity in Kazakhstan and the region."
        ),
        "citations": ["AIX knowledge base"],
    },
]


def _tokens(text: str) -> set[str]:
    return {match.group(0).casefold() for match in _WORD_RE.finditer(text or "") if match.group(0).strip()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _detect_answer_language(query: str) -> str:
    return normalize_lang(detect_supported_text_language(query) or "en")


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


def _new_plan(query: str, chunks: list[dict] | None = None) -> SimpleNamespace:
    language = _detect_answer_language(query)
    return SimpleNamespace(
        answer_language=language,
        route="rag" if chunks else "direct",
        is_chitchat=False,
        needs_clarification=False,
        clarification_question=None,
        retrieval_queries=[query],
        expert_mode=False,
        needs_widget=False,
    )


def _build_context(chunks: list[dict]) -> str:
    if not chunks:
        return "No retrieved context."
    blocks: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        source = _chunk_source(chunk)
        score = _chunk_score(chunk)
        text = _chunk_text(chunk)
        if not text:
            continue
        score_line = f"score={score:.3f}" if score else "score=unknown"
        blocks.append(f"[{index}] {source} ({score_line})\n{text}")
    return "\n\n".join(blocks) if blocks else "No retrieved context."


def generate_followup_questions(_: str, language: str = "en") -> list[str]:
    if language == "ru":
        return ["Какие требования самые важные?", "Какой следующий шаг?", "Где это можно подать?"]
    if language == "kk":
        return ["Негізгі талаптар қандай?", "Келесі қадам қандай?", "Қай жерден бастау керек?"]
    if language == "zh":
        return ["关键要求是什么？", "下一步是什么？", "应该从哪里开始？"]
    return ["What are the key requirements?", "What is the next step?", "Where should I start?"]


def is_capability_query(query: str) -> bool:
    return bool(_CAPABILITY_RE.search(query or ""))


def _prebuilt_capability_answer(language: str = "en") -> str:
    if language == "ru":
        return "Я могу помочь с темами МФЦА из базы знаний: регистрация, регулирование, AFSA, AIX, Expat Centre, суд, арбитраж, финтех и инвесторские сервисы."
    if language == "kk":
        return "Мен білім базасындағы АХҚО тақырыптары бойынша көмектесе аламын: тіркеу, реттеу, AFSA, AIX, Expat Centre, сот, арбитраж, финтех және инвестор қызметтері."
    if language == "zh":
        return "我可以帮助解答知识库中的 AIFC 主题，包括注册、监管、AFSA、AIX、Expat Centre、法院、仲裁、金融科技和投资者服务。"
    return "I can help with AIFC topics in the knowledge base, including registration, regulation, AFSA, AIX, Expat Centre, courts, arbitration, fintech, and investor services."


def _prebuilt_capability_details(language: str = "en") -> str:
    return _prebuilt_capability_answer(language)


def _prebuilt_chitchat_answer(query: str, language: str = "en") -> str | None:
    normalized = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", (query or "").casefold())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return None
    if normalized in {"hi", "hello", "hey"}:
        return "Hello, welcome. How can I help you with AIFC today?"
    if normalized in {"thanks", "thank you"}:
        return "You're welcome."
    if normalized in {"bye", "goodbye"}:
        return "Goodbye. I will be here if you have more questions about AIFC."
    if normalized in {"привет", "здравствуйте", "добрый день"}:
        return "Здравствуйте, добро пожаловать. Чем могу помочь по вопросам МФЦА?"
    if normalized in {"спасибо"}:
        return "Пожалуйста."
    if normalized in {"пока", "до свидания"}:
        return "До свидания. Я буду рад помочь, если появятся новые вопросы о МФЦА."
    if normalized in {"сәлем", "сәлеметсіз бе", "қайырлы күн"}:
        return "Сәлеметсіз бе, қош келдіңіз. АХҚО бойынша қалай көмектесе аламын?"
    if normalized in {"рахмет", "рақмет"}:
        return "Оқасы жоқ."
    if normalized in {"сау бол", "сау болыңыз"}:
        return "Сау болыңыз. АХҚО бойынша тағы сұрақтарыңыз болса, көмектесуге дайынмын."
    if normalized in {"你好", "您好"}:
        return "您好，欢迎咨询。我可以帮您了解 AIFC 的哪些内容？"
    if normalized in {"谢谢"}:
        return "不客气。"
    if normalized in {"再见"}:
        return "再见。如果之后您有 AIFC 相关问题，我可以继续帮助您。"
    if is_capability_query(normalized):
        return _prebuilt_capability_answer(language)
    return None


def wrap_spoken_and_details(spoken: str, details: str = "", followups: str = "") -> str:
    return rebuild_blocks(spoken or "", details or spoken or "", followups or "")


def wrap_answer_for_voice_and_chat(answer: str, *, include_details: bool = True) -> str:
    details = answer if include_details else ""
    return wrap_spoken_and_details(answer, details)


def fast_plan_from_rag(query: str, chunks: list[dict] | None = None) -> SimpleNamespace:
    reply = _prebuilt_chitchat_answer(query, _detect_answer_language(query))
    plan = _new_plan(query, chunks)
    if reply:
        plan.is_chitchat = True
        plan.route = "chitchat"
    if chunks:
        plan.route = "rag"
    return plan


def _faq_fast_path_lookup(query: str) -> dict[str, Any] | None:
    query_tokens = _tokens(query)
    best: tuple[float, dict[str, Any]] | None = None
    for entry in _HARDCODED_FAQ:
        for question in entry.get("questions", []):
            score = _jaccard(query_tokens, _tokens(str(question)))
            if best is None or score > best[0]:
                best = (score, entry)
    if best is None or best[0] <= 0:
        return None
    score, entry = best
    return {
        "answer": entry.get("answer", ""),
        "citations": list(entry.get("citations") or []),
        "similarity": score,
        "cache_type": "local_faq",
    }


def _semantic_answer_cache_lookup(query: str) -> dict[str, Any] | None:
    query_tokens = _tokens(query)
    normalized = " ".join(sorted(query_tokens))
    best_index = -1
    best_score = 0.0
    best_entry: dict[str, Any] | None = None
    with _semantic_answer_cache_lock:
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
    citations: list[str] | None = None,
    confidence: str = "high",
    follow_up: object | None = None,
    handoff_required: bool = False,
    tagged_answer: str = "",
    raw_answer: str = "",
    details: str = "",
    followups: str = "",
) -> None:
    del follow_up, handoff_required
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
        "tagged_answer": tagged_answer,
        "raw_answer": raw_answer,
        "spoken": answer,
        "details": details,
        "followups": followups,
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


def _parallel_fast_paths_and_retrieval(query: str, history: list[dict] | None, plan: object) -> tuple[list[dict], dict[str, Any] | None]:
    del history
    chunks: list[dict] = []
    try:
        chunks = retrieve(query)
    except Exception:
        log.exception("local RAG retrieve failed")

    fast_hit: dict[str, Any] | None = None
    if normalize_lang(getattr(plan, "answer_language", "en")) == "en":
        hit = _faq_fast_path_lookup(query)
        if hit and float(hit.get("similarity", 0.0)) >= 0.9:
            hit["chunks"] = chunks
            fast_hit = hit
    return chunks, fast_hit


def fast_answer_plan_retrieve(
    query: str,
    history: list[dict] | None = None,
    conversation_memory: dict | None = None,
):
    del conversation_memory
    history = history or []
    plan = fast_plan_from_rag(query, [])
    if plan.is_chitchat or (plan.needs_clarification and plan.clarification_question):
        return plan, [], None
    chunks, fast_hit = _parallel_fast_paths_and_retrieval(query, history, plan)
    plan = fast_plan_from_rag(query, chunks)
    return plan, chunks, fast_hit


def answer_plan_retrieve(
    query: str,
    history: list[dict] | None = None,
    conversation_memory: dict | None = None,
):
    return fast_answer_plan_retrieve(query, history, conversation_memory)


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


__all__ = [
    "_ANSWER_SYSTEM",
    "_HARDCODED_FAQ",
    "_build_context",
    "_faq_fast_path_lookup",
    "_parallel_fast_paths_and_retrieval",
    "_prebuilt_capability_answer",
    "_prebuilt_capability_details",
    "_prebuilt_chitchat_answer",
    "_semantic_answer_cache_lookup",
    "_semantic_answer_cache_put",
    "answer_plan_retrieve",
    "clear_semantic_answer_cache",
    "fast_answer_plan_retrieve",
    "fast_plan_from_rag",
    "format_conversation_memory",
    "generate_followup_questions",
    "is_capability_query",
    "update_conversation_memory",
    "wrap_answer_for_voice_and_chat",
    "wrap_spoken_and_details",
]
