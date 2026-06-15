from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace

from ..knowledge.llm import _extract_json_from_wrapped, _extract_json_payload
from ..utils.spoken_text import sanitize_spoken_text

log = logging.getLogger(__name__)

GeminiContextCallback = Callable[[object, list[dict]], Awaitable[None]]

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


@dataclass
class RaceCandidate:
    source: str
    confidence: str
    score: float
    raw_answer: str = ""
    plan: object | None = None
    chunks: list[dict] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    timings: dict[str, int] = field(default_factory=dict)
    cacheable: bool = False
    fallback: bool = False


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
        if cleaned.startswith("#") or cleaned.startswith("[PDF") or cleaned.startswith("|"):
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
        if key in seen or len(cleaned.split()) < 4:
            continue
        seen.add(key)
        selected.append(cleaned)
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
    if "?" in cleaned or re.search(r"\b(?:undefined|nan|null)\b", cleaned, re.IGNORECASE):
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


def _chat_for_candidate(spoken: str, chunks: list[dict] | None = None) -> str:
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
            if seen and any(key in existing or existing in key for existing in seen):
                continue
            seen.add(key)
            points.append(point)
            if len(points) >= 3:
                break
        if len(points) >= 3:
            break
    if points:
        lines.extend(["", "## Relevant information", *(f"- {point}" for point in points)])
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


def _raw_spoken_chat(spoken: str, chat: str) -> str:
    return json.dumps(
        {
            "spoken": (spoken or chat or "").strip(),
            "chat": (chat or spoken or "").strip(),
        },
        ensure_ascii=False,
    )


def candidate_from_answer(
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
    chat_answer = sanitize_spoken_text(answer, keep_digits=True) or spoken
    return RaceCandidate(
        source=source,
        confidence=confidence,
        score=_confidence_score(confidence, score),
        raw_answer=_raw_spoken_chat(spoken, _chat_for_candidate(chat_answer, chunks)),
        plan=plan,
        chunks=chunks,
        citations=citations,
        cacheable=cacheable,
    )


def json_answer_to_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    lines: list[str] = []
    for key in ("chat", "spoken", "answer", "response", "text", "content", "summary"):
        text = str(value.get(key) or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def is_fallback_raw_answer(raw_answer: str) -> bool:
    payload = _extract_json_payload(raw_answer) or _extract_json_from_wrapped(raw_answer)
    if not isinstance(payload, dict):
        return False
    answer_text = json_answer_to_text(payload).casefold()
    return "couldn't find a reliable answer" in answer_text


def fallback_message(language: str, query: str) -> RaceCandidate:
    spoken_by_lang = {
        "ru": "Извините, я не нашел надежный ответ в своей базе знаний. Пожалуйста, посетите aifc.kz.",
        "kk": "Кешіріңіз, мен білім базасынан сенімді жауап таба алмадым. aifc.kz сайтына кіріңіз.",
        "zh": "抱歉，我没有在知识库中找到可靠答案。请访问 aifc.kz。",
        "en": "Sorry, I couldn't find a reliable answer in my knowledge base. Please visit aifc.kz.",
    }
    spoken = spoken_by_lang.get(language, spoken_by_lang["en"])
    candidate = RaceCandidate(
        source="fallback",
        confidence="not_found",
        score=0.0,
        raw_answer=_raw_spoken_chat(spoken, spoken),
        plan=SimpleNamespace(answer_language=language, route="fallback", is_chitchat=False),
        chunks=[],
        citations=["aifc.kz"],
        cacheable=False,
        fallback=True,
    )
    log.info("knowledge_gap query=%r fallback_site=aifc.kz", query[:200])
    return candidate


def aifc_overview_candidate(query: str, language: str) -> RaceCandidate | None:
    if not _is_aifc_overview_query(query):
        return None
    spoken_by_lang = {
        "ru": "МФЦА — это Международный финансовый центр «Астана» в Казахстане, созданный как финансовый хаб с собственной правовой и регуляторной средой.",
        "kk": "АХҚО — Қазақстандағы Астана халықаралық қаржы орталығы, өз құқықтық және реттеуші ортасы бар қаржы хабы.",
        "zh": "AIFC 是哈萨克斯坦的阿斯塔纳国际金融中心，是一个拥有自身法律和监管环境的金融中心。",
        "en": "AIFC is the Astana International Financial Centre in Kazakhstan, a financial hub with its own legal and regulatory environment.",
    }
    chat_by_lang = {
        "ru": (
            "МФЦА поддерживает развитие финансовых услуг, рынков капитала, финтеха, "
            "зеленого финансирования и инвестиционной инфраструктуры в Казахстане и регионе.\n\n"
            "Я могу помочь с темами МФЦА, включая регистрацию, документы, AFSA и регулирование, "
            "FinTech Lab, AIX, Expat Centre, AIFC Court и International Arbitration Centre."
        ),
        "kk": (
            "АХҚО қаржы қызметтері мен капитал нарықтарын дамытуға қолдау көрсетеді, "
            "сондай-ақ Қазақстанда және өңірде финтехті, жасыл қаржыландыруды және "
            "инвестициялық инфрақұрылымды дамытады.\n\n"
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
    chat = chat_by_lang.get(language, chat_by_lang["en"])
    return RaceCandidate(
        source="aifc_overview",
        confidence="high",
        score=0.96,
        raw_answer=_raw_spoken_chat(spoken, chat),
        plan=SimpleNamespace(answer_language=language, route="overview", is_chitchat=False),
        citations=["AIFC knowledge base"],
        cacheable=True,
    )
