from __future__ import annotations

import json
import re

from ..utils.language import (
    detect_supported_text_language,
    is_noise_utterance,
    is_stop_command,
    smalltalk_reply,
    transcript_has_meaningful_speech,
    transcript_is_new_query_candidate,
)
from ..knowledge.llm import _extract_json_from_wrapped, _extract_json_payload
from ..settings import (
    FIRST_TTS_CHARS,
    MAX_TTS_CHARS,
    MIN_TTS_CHARS,
    SHORT_SENTENCE_CHARS,
)
from ..utils.spoken_text import (
    remove_repeated_sentences,
    sanitize_spoken_text,
)
from ..utils.tts_pronunciation import prepare_tts_text
from ..utils.voice_chunker import LowLatencyVoiceChunker

_NOISE_REPLY_BY_LANGUAGE = {
    "en": "I could not hear a clear question. Please repeat it.",
    "ru": "Я не уловил вопрос. Пожалуйста, повторите его.",
    "kk": "Сұрағыңызды анық естімедім. Қайта айтып беріңіз.",
    "zh": "我没有听清问题，请再说一遍。",
}
_MAX_SPOKEN_WORDS = 80
_MAX_SPOKEN_CHARS = 600
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


def _is_turn_candidate(text: str, language: str) -> bool:
    if not text:
        return False
    normalized = " ".join((text or "").lower().strip().split())
    if is_noise_utterance(normalized):
        return False
    normalized_words = re.sub(r"[^\w\s一-鿿]", " ", normalized)
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
    cjk_count = len(re.findall(r"[一-鿿]", normalized))
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
    normalized = re.sub(r"[^\w\s一-鿿]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _tts_splitter_profile(language: str | None) -> tuple[int, int, int, int]:
    if language == "zh":
        return 18, 24, 44, 14
    return FIRST_TTS_CHARS, MIN_TTS_CHARS, MAX_TTS_CHARS, SHORT_SENTENCE_CHARS


def build_sentence_splitter(language: str | None = None) -> LowLatencyVoiceChunker:
    first_chars, min_chars, max_chars, short_chars = _tts_splitter_profile(language)
    return LowLatencyVoiceChunker(
        min_chars=min_chars,
        first_chars=first_chars,
        max_chars=max_chars,
        short_chars=short_chars,
    )


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
    for match in _SPOKEN_SOFT_CUT_RE.finditer(spoken):
        idx = match.start()
        if 45 <= idx <= _MAX_SPOKEN_CHARS:
            return spoken[:idx].rstrip(" ,;:，；、") + "."

    if len(words) <= _MAX_SPOKEN_WORDS and len(spoken) <= _MAX_SPOKEN_CHARS:
        return spoken

    if len(words) > _MAX_SPOKEN_WORDS:
        return " ".join(words[:_MAX_SPOKEN_WORDS]).rstrip(" ,;:") + "."
    return spoken[:_MAX_SPOKEN_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:") + "."


def _string_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _string_value(item))).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "answer", "response", "summary"):
            text = _string_value(value.get(key))
            if text:
                return text
        return json.dumps(value, ensure_ascii=False).strip()
    return ""


def coerce_spoken_chat_payload(payload: object, language: str) -> dict[str, str]:
    source = payload if isinstance(payload, dict) else {}
    spoken = _string_value(source.get("spoken"))
    chat = _string_value(source.get("chat"))
    if not chat:
        for key in ("answer", "response", "text", "content", "summary"):
            chat = _string_value(source.get(key))
            if chat:
                break
    if not spoken and chat:
        spoken = _trim_spoken_for_latency(chat, language)
    if not chat and spoken:
        chat = spoken
    if not spoken and not chat:
        fallback = _NOISE_REPLY_BY_LANGUAGE.get(language, _NOISE_REPLY_BY_LANGUAGE["en"])
        spoken = fallback
        chat = fallback
    return {"spoken": spoken, "chat": chat}


async def normalize_spoken_for_tts(raw_spoken: str, language: str, *, trim_for_latency: bool = True) -> str:
    spoken = prepare_tts_text(raw_spoken or "", language)
    if not spoken:
        spoken = remove_repeated_sentences(sanitize_spoken_text(raw_spoken))
    if trim_for_latency:
        return _trim_spoken_for_latency(spoken, language)
    return spoken
