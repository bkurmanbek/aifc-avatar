from __future__ import annotations

import json
import re

from backend.language import (
    detect_supported_text_language,
    detect_text_language,
    is_noise_utterance,
    is_stop_command,
    smalltalk_reply,
    transcript_has_meaningful_speech,
    transcript_is_new_query_candidate,
)
from backend.llm import _extract_json_from_wrapped, _extract_json_payload
from backend.original_backend import wrap_answer_for_voice_and_chat
from backend.settings import (
    ANSWER_DETAIL_MAX_POINTS,
    ANSWER_DETAIL_MAX_SECTIONS,
    ANSWER_DETAIL_MAX_SECTION_ITEMS,
    ANSWER_VOICE_MAX_CHARS,
    FIRST_TTS_CHARS,
    MAX_TTS_CHARS,
    MIN_TTS_CHARS,
    SHORT_SENTENCE_CHARS,
    SONIOX_TTS_CONTEXT_FILE,
)
from backend.spoken_text import (
    extract_blocks,
    is_speakable_text,
    rebuild_blocks,
    remove_repeated_sentences,
    sanitize_spoken_text,
)
from backend.tts_pronunciation import prepare_tts_text
from backend.voice_chunker import LowLatencyVoiceChunker

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?。！？])\s+")
_NORMALIZED_QUERY_PUNCT_RE = re.compile(r"[.!?,。！？;:]+")
_NOISE_REPLY_BY_LANGUAGE = {
    "en": "I could not hear a clear question. Please repeat it.",
    "ru": "Я не уловил вопрос. Пожалуйста, повторите его.",
    "kk": "Сұрағыңызды анық естімедім. Қайта айтып беріңіз.",
    "zh": "我没有听清问题，请再说一遍。",
}
_MAX_SPOKEN_WORDS = 28
_MAX_SPOKEN_CHARS = 180
_SPOKEN_SOFT_CUT_RE = re.compile(r"[,;:，；、]")
_FINAL_DOMAIN_TERMS = {
    "aifc", "afsa", "aix", "iac", "fintech", "expat", "centre", "center",
    "мфца", "ахқо", "афса", "экспат", "центр", "орталық", "орталығы",
    "сот", "арбитраж", "тіркеу", "реттеу", "құжат", "құжаттар",
    "注册", "监管", "法院", "仲裁", "金融科技",
}
_FINAL_REQUEST_TERMS = {
    "about", "tell", "explain", "information", "describe", "show", "help",
    "о", "об", "про", "расскажите", "объясните", "информация", "помогите",
    "туралы", "жөнінде", "жайлы", "айтып", "айтыңыз", "беріңіз", "түсіндіріңіз",
    "ақпарат", "көмектесіңіз",
    "关于", "告诉", "解释", "介绍", "信息", "帮助",
}


def signature_for_interruption(text: str) -> str:
    normalized = (text or "").strip().lower()
    normalized = _NORMALIZED_QUERY_PUNCT_RE.sub("", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _default_followups(language: str) -> list[str]:
    if language == "ru":
        return [
            "Можете перечислить ключевые требования?",
            "Какой следующий шаг важнее всего?",
            "Куда лучше обратиться для подачи?",
        ]
    if language == "kk":
        return [
            "Негізгі талаптарды қысқаша айтасыз ба?",
            "Ең бірінші орында не істеу керек?",
            "Қай бөлімі бойынша көмек керек?",
        ]
    if language == "zh":
        return [
            "你能给出关键要求吗？",
            "下一步最重要的是什么？",
            "你建议我从哪里开始？",
        ]
    return [
        "Can you list the key requirements?",
        "What is the next most important step?",
        "Where should I start first?",
    ]


def coerce_confidence(value: object) -> float:
    if isinstance(value, (int, float)):
        try:
            value_f = float(value)
            if value_f < 0:
                return 0.0
            if value_f > 1:
                return 1.0
            return value_f
        except (TypeError, ValueError):
            return 0.86
    if not isinstance(value, str):
        return 0.86
    normalized = value.strip().lower()
    if not normalized:
        return 0.86
    if normalized in {"high", "high_confidence", "high-confidence"}:
        return 0.92
    if normalized in {"partial", "medium", "medium_confidence", "medium-confidence"}:
        return 0.67
    if normalized in {"low", "low_confidence", "low-confidence", "not_found", "none", "unknown"}:
        return 0.31
    try:
        parsed = float(normalized)
        if parsed > 1:
            return min(1.0, parsed / 100)
        return max(0.0, min(1.0, parsed))
    except ValueError:
        return 0.86


def normalize_answer_kind(value: object) -> str:
    allowed = {"direct", "procedural", "clarification", "fallback", "error"}
    if not isinstance(value, str):
        return "direct"
    value_norm = value.strip().lower()
    return value_norm if value_norm in allowed else "direct"


def normalize_tts_chunks(chunks: object) -> list[str]:
    if not isinstance(chunks, list):
        return []
    output: list[str] = []
    for chunk in chunks:
        text = str(chunk).strip()
        if text:
            output.append(text)
    return output


def normalize_tts_chunk_for_language(text: str, language: str) -> str:
    has_leading_space = bool(text and text[0].isspace())
    has_trailing_space = bool(text and text[-1].isspace())
    cleaned = prepare_tts_text(text, language, SONIOX_TTS_CONTEXT_FILE)
    if cleaned and has_leading_space and not cleaned.startswith(" "):
        cleaned = " " + cleaned
    if cleaned and has_trailing_space and not cleaned.endswith(" "):
        cleaned += " "
    return cleaned


def build_control_payload(payload: dict[str, object], interrupted_input: bool) -> dict[str, bool]:
    control = {
        "interrupt_ack": bool(interrupted_input),
        "handoff_greeting": False,
    }
    raw_control = payload.get("control") if isinstance(payload, dict) else None
    if isinstance(raw_control, dict):
        control["interrupt_ack"] = bool(raw_control.get("interrupt_ack", control["interrupt_ack"]))
        control["handoff_greeting"] = bool(raw_control.get("handoff_greeting", control["handoff_greeting"]))
    return control


def _is_turn_candidate(text: str, language: str) -> bool:
    if not text:
        return False
    normalized = " ".join((text or "").lower().strip().split())
    if is_noise_utterance(normalized):
        return False
    normalized_words = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", normalized)
    normalized_words = " ".join(normalized_words.split())
    if is_stop_command(normalized_words):
        return True
    if smalltalk_reply(normalized_words, language):
        return True
    return transcript_is_new_query_candidate(normalized_words)


def is_final_turn_candidate(text: str, language: str, require_query_signal: bool) -> bool:
    if _is_turn_candidate(text, language):
        return True
    normalized = " ".join((text or "").lower().strip().split())
    if not normalized or is_noise_utterance(normalized):
        return False
    if not transcript_has_meaningful_speech(normalized):
        return False
    if detect_supported_text_language(normalized) is None:
        return False

    words = re.findall(r"[^\W\d_]+", normalized, flags=re.UNICODE)
    letter_count = sum(len(word) for word in words)
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", normalized))
    has_domain_term = any(term in normalized for term in _FINAL_DOMAIN_TERMS)
    has_request_term = any(term in normalized for term in _FINAL_REQUEST_TERMS)

    if has_domain_term and (words or cjk_count >= 2):
        return True
    if has_request_term and (len(words) >= 2 or cjk_count >= 3):
        return True
    if require_query_signal:
        return False
    if cjk_count >= 4:
        return True
    return len(words) >= 3 and letter_count >= 12


def _extract_balanced_json(text: str) -> dict[str, object] | None:
    if not text:
        return None
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            char = text[i]
            if in_string:
                if escaped:
                    escaped = False
                    continue
                if char == "\\":
                    escaped = True
                elif char == "\"":
                    in_string = False
                continue
            if char == "\"":
                in_string = True
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    parsed = _extract_json_payload(candidate)
                    if isinstance(parsed, dict):
                        return parsed
                    break
    return None


def extract_json_any(raw: str) -> dict[str, object] | None:
    payload = _extract_json_payload(raw) or _extract_json_from_wrapped(raw)
    if isinstance(payload, dict):
        return payload
    return _extract_balanced_json(raw)


def normalize_query_signature(text: str) -> str:
    normalized = (text or "").strip().lower()
    normalized = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _tts_splitter_profile(language: str | None) -> tuple[int, int, int, int]:
    if language == "zh":
        return 18, 24, 44, 14
    return max(FIRST_TTS_CHARS, 48), max(MIN_TTS_CHARS, 80), max(MAX_TTS_CHARS, 220), max(SHORT_SENTENCE_CHARS, 40)


def build_sentence_splitter(language: str | None = None) -> LowLatencyVoiceChunker:
    first_chars, min_chars, max_chars, short_chars = _tts_splitter_profile(language)
    return LowLatencyVoiceChunker(
        min_chars=min_chars,
        first_chars=first_chars,
        max_chars=max_chars,
        short_chars=short_chars,
    )


def remaining_spoken_suffix(final_text: str, already_streamed: str) -> str:
    final_text = (final_text or "").strip()
    already_streamed = (already_streamed or "").strip()
    if not final_text:
        return ""
    if not already_streamed:
        return final_text

    def sentence_key(value: str) -> str:
        value = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", value.casefold())
        return re.sub(r"\s+", " ", value).strip()

    streamed_keys = {
        key
        for part in _SENTENCE_BOUNDARY_RE.split(already_streamed)
        if (key := sentence_key(part))
    }
    remaining: list[str] = []
    for sentence in _SENTENCE_BOUNDARY_RE.split(final_text):
        cleaned = sentence.strip()
        if not cleaned:
            continue
        key = sentence_key(cleaned)
        if key and key in streamed_keys:
            continue
        remaining.append(cleaned)
    return " ".join(remaining).strip()


def _trim_spoken_for_latency(text: str, language: str) -> str:
    spoken = (text or "").strip()
    if not spoken:
        return ""
    if language == "zh":
        if len(spoken) <= 45:
            return spoken
        for idx, char in enumerate(spoken):
            if char in "，；、,;:" and 12 <= idx <= 45:
                return spoken[:idx].rstrip("，；、,;: ") + "。"
        return spoken[:45].rstrip("，；、,;: ") + "。"

    words = spoken.split()
    if len(words) <= _MAX_SPOKEN_WORDS and len(spoken) <= _MAX_SPOKEN_CHARS:
        return spoken

    for match in _SPOKEN_SOFT_CUT_RE.finditer(spoken):
        idx = match.start()
        if 45 <= idx <= _MAX_SPOKEN_CHARS:
            return spoken[:idx].rstrip(" ,;:，；、") + "."

    if len(words) > _MAX_SPOKEN_WORDS:
        return " ".join(words[:_MAX_SPOKEN_WORDS]).rstrip(" ,;:") + "."
    return spoken[:_MAX_SPOKEN_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:") + "."


def limit_text_for_answer_voice(text: str, language: str | None = None) -> str:
    spoken = (text or "").strip()
    max_chars = max(200, ANSWER_VOICE_MAX_CHARS)
    if not spoken or len(spoken) <= max_chars:
        return spoken

    if language == "zh":
        cut = spoken[:max_chars]
        for idx in range(len(cut) - 1, max(0, max_chars // 2), -1):
            if cut[idx] in "。！？；，、":
                return cut[: idx + 1].strip()
        return cut.rstrip("，；、,;: ") + "。"

    lines: list[str] = []
    total = 0
    for line in spoken.splitlines():
        clean = line.strip()
        if not clean:
            if lines and total + 1 <= max_chars:
                lines.append("")
                total += 1
            continue
        added = len(clean) + (1 if lines else 0)
        if total + added > max_chars:
            break
        lines.append(clean)
        total += added
    limited = "\n".join(lines).strip()
    if limited:
        return limited

    sentences = [chunk.strip() for chunk in _SENTENCE_BOUNDARY_RE.split(spoken) if chunk.strip()]
    out: list[str] = []
    total = 0
    for sentence in sentences:
        added = len(sentence) + (1 if out else 0)
        if total + added > max_chars:
            break
        out.append(sentence)
        total += added
    if out:
        return " ".join(out).strip()

    return spoken[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:") + "."


def limit_answer_details(details: object) -> dict:
    if not isinstance(details, dict):
        return {}
    limited = dict(details)
    points = [str(item).strip() for item in limited.get("points", []) if str(item).strip()]
    limited["points"] = points[: max(0, ANSWER_DETAIL_MAX_POINTS)]

    sections: list[dict] = []
    sections_raw = limited.get("sections", [])
    if not isinstance(sections_raw, list):
        sections_raw = []
    for section in sections_raw:
        if not isinstance(section, dict):
            continue
        section_copy = dict(section)
        items = [str(item).strip() for item in section_copy.get("items", []) if str(item).strip()]
        section_copy["items"] = items[: max(0, ANSWER_DETAIL_MAX_SECTION_ITEMS)]
        if str(section_copy.get("title", "")).strip() or str(section_copy.get("text", "")).strip() or section_copy["items"]:
            sections.append(section_copy)
        if len(sections) >= max(0, ANSWER_DETAIL_MAX_SECTIONS):
            break
    limited["sections"] = sections
    return limited


def build_tts_chunks(text: str, language: str | None = None) -> list[str]:
    splitter = build_sentence_splitter(language)
    chunks: list[str] = []
    for chunk, _ in splitter.feed(text):
        if chunk:
            chunks.append(chunk.strip())
    for chunk, _ in splitter.flush():
        if chunk:
            chunks.append(chunk.strip())
    if not chunks and text.strip():
        chunks = [text.strip()]
    return [chunk for chunk in chunks if chunk]


def _enforce_prompt_details(payload_details: object, follow_up_count: int) -> dict:
    if not isinstance(payload_details, dict):
        payload_details = {}
    details = dict(payload_details)

    sections = details.get("sections", [])
    if not isinstance(sections, list):
        sections = []

    summary = str(details.get("summary", "")).strip()
    if not summary and details.get("sections"):
        for section in details["sections"]:
            if not isinstance(section, dict):
                continue
            text = str(section.get("text", "")).strip()
            items = section.get("items")
            if text:
                summary = text
                break
            if isinstance(items, list) and items:
                item = str(items[0]).strip()
                if item:
                    summary = item
                    break
    if not summary and details.get("sections") is None:
        summary = ""

    points: list[str] = []
    raw_points = details.get("points")
    if isinstance(raw_points, list):
        points = [str(item).strip() for item in raw_points if str(item).strip()]

    if not points:
        for section in details.get("sections", []):
            if not isinstance(section, dict):
                continue
            for section_item in section.get("items", []):
                text = str(section_item).strip()
                if text:
                    points.append(text)
            text = str(section.get("text", "")).strip()
            if text:
                points.append(text)

    if not points:
        fallback = summary or str(details.get("summary", "")).strip()
        if fallback:
            points.append(fallback)

    answer_kind = str(details.get("answer_kind", "direct"))
    confidence = details.get("confidence", 0.86)
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.86

    return {
        "summary": summary,
        "points": points,
        "sections": sections,
        "answer_kind": normalize_answer_kind(answer_kind),
        "confidence": coerce_confidence(confidence_value),
        "requires_follow_up": bool(follow_up_count),
        "citations": details.get("citations", []),
        "notes": details.get("notes", []),
    }


def details_from_spoken(spoken: str, payload_details: object, follow_up_count: int) -> dict:
    details = _enforce_prompt_details(payload_details, follow_up_count)
    if isinstance(payload_details, dict):
        for key in ("fallback_site", "knowledge_gap_query", "language"):
            if key in payload_details:
                details[key] = payload_details[key]
    spoken = sanitize_spoken_text(spoken or "").strip()
    if not spoken:
        return details
    details["summary"] = spoken
    details["points"] = [spoken]
    details["sections"] = [
        {
            "id": "spoken",
            "title": "Answer",
            "text": spoken,
            "items": [],
        }
    ]
    details["requires_follow_up"] = bool(follow_up_count)
    return details


def normalize_tagged_answer(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return wrap_answer_for_voice_and_chat("I do not have enough information to answer that accurately.")

    if "[[spoken]]" not in text:
        text = f"[[spoken]]{text}"
    if "[[details]]" in text and "[[/spoken]]" not in text.split("[[details]]", 1)[0]:
        text = text.replace("[[details]]", "[[/spoken]][[details]]", 1)
    if "[[followups]]" in text and "[[/details]]" not in text.split("[[followups]]", 1)[0]:
        text = text.replace("[[followups]]", "[[/details]][[followups]]", 1)

    if "[[details]]" not in text:
        if "[[/spoken]]" not in text:
            text += "[[/spoken]]"
        text += "[[details]][[/details]]"
    elif "[[/details]]" not in text.split("[[details]]", 1)[1]:
        if "[[followups]]" in text:
            text = text.replace("[[followups]]", "[[/details]][[followups]]", 1)
        else:
            text += "[[/details]]"

    if "[[followups]]" not in text:
        text += "[[followups]][[/followups]]"
    elif "[[/followups]]" not in text.split("[[followups]]", 1)[1]:
        text += "[[/followups]]"

    return text


async def normalize_spoken_for_tts(raw_spoken: str, language: str, *, trim_for_latency: bool = True) -> str:
    spoken = prepare_tts_text(raw_spoken, language, SONIOX_TTS_CONTEXT_FILE)
    if not spoken:
        spoken = remove_repeated_sentences(sanitize_spoken_text(raw_spoken))
    if trim_for_latency:
        return _trim_spoken_for_latency(spoken, language)
    return spoken


def _json_to_markdown_followups(value: object) -> str:
    if not value:
        return ""
    if isinstance(value, (str, bytes)):
        text = (value.decode() if isinstance(value, bytes) else value).strip()
        if not text:
            return ""
        return "\n".join(f"- {line.strip().lstrip('- ').strip()}" for line in text.splitlines() if line.strip())
    if not isinstance(value, (list, tuple)):
        return ""
    lines: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)


def json_to_markdown_details(value: object) -> str:
    if not value:
        return ""
    if isinstance(value, (str, bytes)):
        return (value.decode() if isinstance(value, bytes) else value).strip()
    if not isinstance(value, dict):
        return str(value).strip()
    summary = str(value.get("summary", "")).strip()
    points = value.get("points")
    lines: list[str] = []
    if summary:
        lines.append(summary)
    if isinstance(points, list):
        lines.extend(str(item).strip() for item in points if str(item).strip())
    for section in value.get("sections", []):
        if not isinstance(section, dict):
            continue
        title = str(section.get("title", "")).strip()
        text = str(section.get("text", "")).strip()
        items = section.get("items")
        if title:
            lines.append(f"### {title}")
        if text:
            lines.append(text)
        if isinstance(items, list):
            for item in items:
                line = str(item).strip()
                if line:
                    lines.append(f"- {line}")
        if items and text:
            lines.append("")
    return "\n".join(line for line in lines if line).strip()


def _json_payload_to_tagged_answer(payload: dict) -> str:
    details = json_to_markdown_details(payload.get("details"))
    spoken = "" if details else str(payload.get("spoken", "")).strip()
    followups = _json_to_markdown_followups(payload.get("followups") or payload.get("follow_up_questions"))
    if not followups:
        followups = "- See details for next steps."
    return rebuild_blocks(spoken=spoken, details=details or spoken, followups=followups)


async def tagged_answer_with_full_details_voice(
    tagged_answer: str,
    contract_payload: dict | None,
    language: str,
) -> tuple[str, str]:
    tagged_answer = normalize_tagged_answer(tagged_answer)
    _, details_block, followups_block = extract_blocks(tagged_answer)
    contract_details_voice = ""
    if contract_payload:
        contract_details_voice = json_to_markdown_details(contract_payload.get("details"))
    details_voice = limit_text_for_answer_voice(contract_details_voice or details_block, language)
    if not details_voice:
        spoken_block, _, _ = extract_blocks(tagged_answer)
        details_voice = limit_text_for_answer_voice(spoken_block, language)
    spoken_voice = await normalize_spoken_for_tts(details_voice, language, trim_for_latency=False)
    if not is_speakable_text(spoken_voice):
        spoken_voice = await normalize_spoken_for_tts(
            str(contract_payload.get("spoken", "")) if contract_payload else "",
            language,
            trim_for_latency=False,
        )
    if not is_speakable_text(spoken_voice):
        spoken_voice = {
            "ru": "Извините, я не могу корректно озвучить этот ответ. Повторите вопрос, пожалуйста.",
            "kk": "Кешіріңіз, бұл жауапты дұрыс дыбыстай алмадым. Сұрағыңызды қайталап айтыңыз.",
            "zh": "抱歉，我这次没有正确生成语音回答。请再说一遍您的问题。",
        }.get(language, "Sorry, I could not generate a clean spoken answer this time. Please ask again.")
    return rebuild_blocks(spoken_voice, details_voice, followups_block), spoken_voice


def extract_answer_from_json(raw: str) -> str | None:
    payload = _extract_json_payload(raw) or _extract_json_from_wrapped(raw)
    if not isinstance(payload, dict):
        return None
    return _json_payload_to_tagged_answer(payload)


def normalize_followup_questions(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, (str, bytes)):
        raw = (value.decode() if isinstance(value, bytes) else value).strip()
        if not raw:
            return []
        return [line.strip(" -\t") for line in raw.splitlines() if line.strip()]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in out:
        key = re.sub(r"\s+", " ", item.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def coerce_prompt_contract_payload(payload: dict, interrupted_input: bool, language: str) -> tuple[dict, list[str], list[str]]:
    if not isinstance(payload, dict):
        payload = {}
    spoken = str(payload.get("spoken", "")).strip()
    details_payload = payload.get("details")
    followups: list[str] = normalize_followup_questions(payload.get("followups"))
    if not followups and "follow_up_questions" in payload:
        followups = normalize_followup_questions(payload.get("follow_up_questions"))
    details = _enforce_prompt_details(details_payload, len(followups))
    details_voice = json_to_markdown_details(details)
    if details_voice:
        spoken = prepare_tts_text(details_voice, language, SONIOX_TTS_CONTEXT_FILE)
    elif spoken:
        spoken = prepare_tts_text(spoken, language, SONIOX_TTS_CONTEXT_FILE)
    if not details["summary"]:
        details["summary"] = spoken[:220]
    if not spoken and not details["summary"]:
        details["summary"] = _NOISE_REPLY_BY_LANGUAGE.get(language, _NOISE_REPLY_BY_LANGUAGE["en"])
        spoken = ""

    if coerce_confidence(details.get("confidence")) < 0.55 and details["summary"]:
        follow_up_guidance = {
            "en": "I’m not fully sure. Please rephrase in a shorter way.",
            "ru": "Я не уверен. Уточните, пожалуйста, короче.",
            "kk": "Нақты емес сияқты. Қысқаша нақтылап айтып көріңіз.",
            "zh": "我不够确定，请您更简要地重新表达一次。",
        }
        details["summary"] = f"{details['summary']} {follow_up_guidance.get(language, follow_up_guidance['en'])}".strip()
        if not followups:
            followups = _default_followups(language)

    followups = followups[:3]
    control = build_control_payload(payload, interrupted_input)
    if control.get("handoff_greeting") and spoken:
        control["handoff_greeting"] = False

    normalized = {
        "spoken": spoken,
        "details": details,
        "control": control,
        "tts_chunks": [],
        "follow_up_questions": followups,
        "answer_contract": {
            "summary": details.get("summary", ""),
            "points": details.get("points", []),
            "sections": details.get("sections", []),
            "answer_kind": details.get("answer_kind", "direct"),
            "confidence": details.get("confidence", 0.86),
            "requires_follow_up": bool(followups),
            "citations": details.get("citations", []),
            "notes": details.get("notes", []),
        },
    }

    if language and language in {"en", "ru", "kk", "zh"}:
        normalized["details"]["language"] = language

    return normalized, details.get("sections", []), followups
