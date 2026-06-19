from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from ..settings import DATA_DIR

log = logging.getLogger(__name__)

_FAQ_CACHE_PATH = DATA_DIR / "faq" / "aifc_faq_cache.txt"
# Spoken-only sidecar: maps sha256(answer) -> an already-pronounceable spoken rewrite of the
# answer (numbers/dates/ordinals spelled out, abbreviations expanded), in the answer's language.
# Built offline by scripts/rewrite_faq_spoken.py. The chat panel still shows the original
# written answer; only the VOICE uses this. Lets us drop the regex number normalizer.
_FAQ_SPOKEN_PATH = DATA_DIR / "faq" / "aifc_faq_spoken.json"
_FAQ_SPOKEN_MAP: dict[str, Any] | None = None


def _answer_hash(answer: str) -> str:
    return hashlib.sha256((answer or "").strip().encode("utf-8")).hexdigest()


def _load_faq_spoken() -> dict[str, Any]:
    global _FAQ_SPOKEN_MAP
    if _FAQ_SPOKEN_MAP is None:
        try:
            _FAQ_SPOKEN_MAP = json.loads(_FAQ_SPOKEN_PATH.read_text(encoding="utf-8")) if _FAQ_SPOKEN_PATH.exists() else {}
            log.info("loaded %d FAQ spoken rewrites from %s", len(_FAQ_SPOKEN_MAP), _FAQ_SPOKEN_PATH)
        except Exception:
            log.exception("FAQ spoken sidecar unreadable: %s", _FAQ_SPOKEN_PATH)
            _FAQ_SPOKEN_MAP = {}
    return _FAQ_SPOKEN_MAP


def faq_spoken_form(answer: str) -> str | None:
    """Precomputed pronounceable spoken form for a FAQ answer, or None (fall back to the answer)."""
    entry = _load_faq_spoken().get(_answer_hash(answer))
    if isinstance(entry, dict):
        return (str(entry.get("spoken") or "")).strip() or None
    if isinstance(entry, str):
        return entry.strip() or None
    return None
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9]+|[一-鿿]", re.UNICODE)
_FAQ_LANGUAGE_RE = re.compile(r"^Language:\s+.+\(([^)]+)\)\s*$")
_FAQ_INDEX_RE = re.compile(r"^\[\d+\]\s*$")
_CAPABILITY_RE = re.compile(
    r"\b(what can you|what do you|help me|capabilities|topics|services|можешь|умеешь|помочь|көмектесе|не істей|可以|帮助)\b",
    re.IGNORECASE,
)
_FAQ_STOPWORDS = {
    "a", "an", "and", "are", "can", "do", "does", "for", "how", "i", "in", "is", "it",
    "me", "of", "on", "or", "the", "to", "what", "where", "which", "who", "why",
    "как", "какие", "каким", "какова", "какой", "кто", "мне", "могу", "может", "нужно",
    "о", "об", "что", "это",
    "алай", "бола", "болады", "болып", "деген", "қандай", "қалай", "кім", "маған", "ма",
    "ме", "мен", "не", "үшін",
}

_FALLBACK_FAQ: list[dict[str, Any]] = [
    {
        "questions": ["what is aifc", "tell me about aifc", "about astana international financial centre"],
        "answer": (
            "AIFC is the Astana International Financial Centre in Kazakhstan, "
            "a financial hub with its own legal and regulatory environment."
        ),
        "citations": ["AIFC knowledge base"],
    },
    {
        "questions": ["what can you help with", "which aifc topics can you answer", "what are your capabilities"],
        "answer": (
            "I can help with AIFC services, registration, regulation, AFSA, "
            "FinTech Lab, AIX, Expat Centre, AIFC Court, arbitration, policy, "
            "and investor-service topics covered by the local knowledge base."
        ),
        "citations": ["AIFC knowledge base"],
    },
    {
        "questions": ["what is afsa", "tell me about afsa", "aifc regulator"],
        "answer": (
            "AFSA is the Astana Financial Services Authority, the independent "
            "regulator for financial services and related activities in the AIFC."
        ),
        "citations": ["AFSA knowledge base"],
    },
    {
        "questions": ["what is aix", "tell me about aix", "astana international exchange"],
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


def _normalise_faq_language(raw: str) -> str:
    return {"kz": "kk"}.get(raw.strip(), raw.strip())


def _load_faq_cache(path: Path = _FAQ_CACHE_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        log.warning("FAQ cache not found: %s", path)
        return list(_FALLBACK_FAQ)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        log.exception("FAQ cache unreadable: %s", path)
        return list(_FALLBACK_FAQ)

    entries: list[dict[str, Any]] = []
    language = ""
    page = ""
    source = ""
    modified = ""
    category = ""
    idx = 0

    while idx < len(lines):
        line = lines[idx].strip()
        language_match = _FAQ_LANGUAGE_RE.match(line)
        if language_match:
            language = _normalise_faq_language(language_match.group(1))
            idx += 1
            continue
        if line.startswith("Page:"):
            page = line.split(":", 1)[1].strip()
            idx += 1
            continue
        if line.startswith("Source:"):
            source = line.split(":", 1)[1].strip()
            idx += 1
            continue
        if line.startswith("Modified:"):
            modified = line.split(":", 1)[1].strip()
            idx += 1
            continue
        if line.startswith("Category:"):
            category = line.split(":", 1)[1].strip()
            idx += 1
            continue
        if not line.startswith("Q:"):
            idx += 1
            continue

        question = line.split(":", 1)[1].strip()
        idx += 1
        if idx >= len(lines) or lines[idx].strip() != "A:":
            continue
        idx += 1

        answer_lines: list[str] = []
        while idx < len(lines):
            next_line = lines[idx]
            stripped = next_line.strip()
            if (
                _FAQ_INDEX_RE.match(stripped)
                or stripped.startswith("Language:")
                or stripped.startswith("Page:")
                or stripped.startswith("Source:")
                or stripped.startswith("Modified:")
                or stripped.startswith("Pairs on page:")
                or stripped.startswith("===")
            ):
                break
            answer_lines.append(next_line.rstrip())
            idx += 1

        answer = "\n".join(answer_lines).strip()
        if not question or not answer:
            continue
        entry: dict[str, Any] = {
            "questions": [question],
            "answer": answer,
            "citations": [source or page or "AIFC FAQ cache"],
            "language": language,
            "category": category,
        }
        if page:
            entry["page"] = page
        if modified:
            entry["modified"] = modified
        entries.append(entry)

    if not entries:
        log.warning("FAQ cache contained no parseable entries: %s", path)
        return list(_FALLBACK_FAQ)
    log.info("loaded %d FAQ cache entries from %s", len(entries), path)
    return entries


_FAQ_ENTRIES = _load_faq_cache()


def _important_faq_tokens(text: str) -> set[str]:
    tokens = _tokens(text)
    important = {token for token in tokens if token not in _FAQ_STOPWORDS}
    return important or tokens


def _faq_match_score(query: str, question: str) -> float:
    query_tokens = _tokens(query)
    question_tokens = _tokens(question)
    if query_tokens and query_tokens == question_tokens:
        return 1.0

    base_score = _jaccard(query_tokens, question_tokens)
    query_important = _important_faq_tokens(query)
    question_important = _important_faq_tokens(question)
    overlap = query_important & question_important
    if not overlap:
        return base_score

    recall = len(overlap) / max(1, len(query_important))
    precision = len(overlap) / max(1, len(question_important))
    if recall >= 0.95 and precision >= 0.95:
        return max(base_score, 0.98)
    if recall >= 0.80 and precision >= 0.80 and len(overlap) >= 2:
        return max(base_score, 0.91)
    if recall >= 0.75 and precision >= 0.75 and len(overlap) >= 3:
        return max(base_score, 0.89)
    return base_score


def _faq_fast_path_lookup(query: str, language: str | None = None) -> dict[str, Any] | None:
    best: tuple[float, dict[str, Any]] | None = None
    for entry in _FAQ_ENTRIES:
        if language and entry.get("language") and entry.get("language") != language:
            continue
        for question in entry.get("questions", []):
            score = _faq_match_score(query, str(question))
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


def faq_cacheable_entries() -> list[dict[str, Any]]:
    """Entries eligible for offline video pre-rendering (scripts/build_faq_videos.py).

    Each returned dict carries the spoken ``answer`` and its ``language`` (may be "" for
    fallback entries that match any conversation language — the builder renders those for
    every supported language). One entry per parsed FAQ pair.
    """
    out: list[dict[str, Any]] = []
    for entry in _FAQ_ENTRIES:
        answer = str(entry.get("answer") or "").strip()
        if not answer:
            continue
        out.append(
            {
                "answer": answer,
                "language": str(entry.get("language") or "").strip(),
                "questions": list(entry.get("questions") or []),
            }
        )
    return out


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


def _prebuilt_chitchat_answer(query: str, language: str = "en") -> str | None:
    normalized = re.sub(r"[^\w\s一-鿿]", " ", (query or "").casefold())
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
