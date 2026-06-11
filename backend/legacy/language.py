from __future__ import annotations

import re

KAZAKH_CHARS = set("ӘәҒғҚқҢңӨөҰұҮүҺһІі")
KAZAKH_HINT_WORDS = (
    "салем", "сәлем", "рахмет", "рақмет", "калай", "қалай", "каласыз", "қаласыз",
    "ия", "иә", "жок", "жоқ", "сау", "болыңыз", "көмек", "кужат", "құжат",
    "жумыс", "жұмыс", "орталык", "орталық", "аркылы", "арқылы", "және", "қызмет",
)
SUPPORTED_LANGS = {"en", "ru", "kk", "zh"}
STOP_KEYWORDS = {
    "stop",
    "stop speaking",
    "stop talking",
    "pause",
    "cancel",
    "enough",
    "stop please",
    "стоп",
    "стоп пожалуйста",
    "остановись",
    "остановитесь",
    "прекрати",
    "прекратите",
    "хватит",
    "достаточно",
    "тоқта",
    "тоқтаңыз",
    "тоқтатыңыз",
    "жетер",
    "болды",
    "停",
    "停止",
}
LANG_ALIASES = {
    "en": "en",
    "eng": "en",
    "en-us": "en",
    "en-gb": "en",
    "ru": "ru",
    "rus": "ru",
    "ru-ru": "ru",
    "kk": "kk",
    "kaz": "kk",
    "kk-kz": "kk",
    "zh": "zh",
    "zho": "zh",
    "chi": "zh",
    "cmn": "zh",
    "yue": "zh",
    "zh-cn": "zh",
    "zh-tw": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",
}
CHITCHAT = {
    "en": {
        "hi": "Hello, welcome. How can I help you with AIFC today?",
        "hello": "Hello, welcome. How can I help you with AIFC today?",
        "good morning": "Good morning, welcome. How can I help with AIFC today?",
        "good afternoon": "Good afternoon, welcome. How can I help with AIFC today?",
        "thanks": "You're welcome.",
        "thank you": "You're welcome.",
    },
    "ru": {
        "привет": "Здравствуйте, добро пожаловать. Чем могу помочь по вопросам МФЦА?",
        "здравствуйте": "Здравствуйте, добро пожаловать. Чем могу помочь по вопросам МФЦА?",
        "добрый день": "Добрый день, добро пожаловать. Чем могу помочь по вопросам МФЦА?",
        "спасибо": "Пожалуйста.",
    },
    "kk": {
        "сәлем": "Сәлеметсіз бе, қош келдіңіз. АХҚО бойынша қалай көмектесе аламын?",
        "сәлеметсіз бе": "Сәлеметсіз бе, қош келдіңіз. АХҚО бойынша қалай көмектесе аламын?",
        "қайырлы күн": "Қайырлы күн, қош келдіңіз. АХҚО бойынша қалай көмектесе аламын?",
        "рахмет": "Оқасы жоқ.",
    },
    "zh": {
        "你好": "您好，欢迎咨询。我可以帮您了解 AIFC 的哪些内容？",
        "您好": "您好，欢迎咨询。我可以帮您了解 AIFC 的哪些内容？",
        "早上好": "早上好，欢迎咨询。我可以帮您了解 AIFC 的哪些内容？",
        "晚上好": "晚上好，欢迎咨询。我可以帮您了解 AIFC 的哪些内容？",
        "谢谢": "不客气。",
        "再见": "再见。若您之后有 AIFC 相关问题，我可以继续帮助您。",
    },
}
GOODBYE = {
    "en": {"bye": "Goodbye. I will be here if you have more questions about AIFC.", "goodbye": "Goodbye. I will be here if you have more questions about AIFC."},
    "ru": {"пока": "До свидания. Я буду рад помочь, если появятся новые вопросы о МФЦА.", "до свидания": "До свидания. Я буду рад помочь, если появятся новые вопросы о МФЦА."},
    "kk": {"сау бол": "Сау болыңыз. АХҚО бойынша тағы сұрақтарыңыз болса, көмектесуге дайынмын.", "сау болыңыз": "Сау болыңыз. АХҚО бойынша тағы сұрақтарыңыз болса, көмектесуге дайынмын."},
    "zh": {"拜拜": "再见。如果之后您有 AIFC 相关问题，我可以继续帮助您。", "再见": "再见。如果之后您有 AIFC 相关问题，我可以继续帮助您。"},
}
UNSUPPORTED_LANGUAGE_MESSAGE = {
    "en": "Supported languages are English, Russian, Kazakh, and Chinese.",
    "ru": "Поддерживаются только английский, русский, казахский и китайский языки.",
    "kk": "Қолдау көрсетілетін тілдер: ағылшын, орыс, қазақ және қытай тілдері.",
    "zh": "目前仅支持英语、俄语、哈萨克语和中文。",
}
_UNSUPPORTED_SCRIPT_RE = re.compile(r"[\u0600-\u06FF\u0590-\u05FF\u0900-\u0D7F\u0E00-\u0E7F\u3040-\u30FF\uAC00-\uD7AF]")
_LATIN_EXTENDED_RE = re.compile(r"[À-ÖØ-öø-ÿĀ-ž]")
_LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі\u4e00-\u9fff]")
_NON_WORD_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі]+|[\u4e00-\u9fff]")
_WRAPPED_ANNOTATION_RE = re.compile(r"^\s*[\[\(（]\s*(.*?)\s*[\]\)）]\s*[.!?。！？]*\s*$")
_NON_SPEECH_PHRASES = {
    "noise", "noises", "background noise", "microphone noise", "mic noise", "ambient noise",
    "static", "white noise", "background chatter", "chatter", "background speech",
    "background talking", "background voices", "crosstalk", "cross talk", "conversation",
    "music", "applause", "clapping", "laughter", "laughing", "silence", "silent",
    "inaudible", "unintelligible", "mumbling", "rustling", "typing", "keyboard",
    "beep", "beeping", "clicking", "cough", "coughing", "breathing", "echo", "humming",
    "шум", "фоновый шум", "шум микрофона", "музыка", "аплодисменты", "смех",
    "тишина", "неразборчиво", "не слышно", "фоновые голоса", "разговоры",
    "шу", "фондық шу", "микрофон шуы", "музыка", "күлкі", "тыныштық",
    "түсініксіз", "естілмейді", "фондағы дауыс",
    "噪音", "背景噪音", "麦克风噪音", "音乐", "掌声", "笑声", "静音", "听不清",
    "背景人声", "背景说话", "杂音",
}
_NON_SPEECH_ANNOTATION_WORDS = {
    "background", "microphone", "mic", "ambient", "room", "noise", "noises",
    "sound", "sounds", "chatter", "speech", "voices", "voice", "talking",
    "conversation", "conversations", "music", "applause", "clapping", "laughter",
    "laughing", "silence", "silent", "inaudible", "unintelligible", "mumbling",
    "static", "rustling", "typing", "keyboard", "beep", "beeping", "click",
    "clicking", "cough", "coughing", "breath", "breathing", "echo", "hum",
    "humming", "crosstalk", "cross", "talk",
    "шум", "фоновый", "фоновые", "фон", "микрофона", "музыка", "аплодисменты",
    "смех", "тишина", "неразборчиво", "слышно", "голоса", "разговоры",
    "шу", "фондық", "микрофон", "шуы", "күлкі", "тыныштық", "түсініксіз",
    "естілмейді", "фондағы", "дауыс",
}
_NOISE_WORDS = {
    "", "uh", "um", "hmm", "mm", "mmm", "ah", "eh", "uhh",
    "эм", "мм", "м", "ээ", "ага", "а", "э",
    "эээ", "ммм", "хм", "хмм",
    "е", "ей", "аа", "ым", "мхм",
    "嗯", "啊", "呃",
}
_BARGE_SINGLE_WORDS = {
    "help", "question", "repeat",
    "помогите", "повтори", "вопрос",
    "көмек", "қайтала", "сұрақ",
    "重复",
}
_BARGE_QUERY_TERMS = {
    "what", "how", "why", "when", "where", "which", "who", "can", "could",
    "should", "need", "explain", "tell", "show", "apply", "submit", "find",
    "search", "document", "documents", "requirement", "requirements", "permit",
    "visa", "fintech", "lab", "aifc", "afsa", "portal", "register",
    "registration", "license", "licence", "cost", "fee", "deadline", "process",
    "steps", "help", "question", "repeat",
    "что", "как", "почему", "когда", "где", "какой", "какая", "какие", "кто",
    "могу", "можно", "нужно", "объясни", "объясните", "расскажи", "расскажите",
    "покажи", "покажите", "подать", "документ", "документы", "требование",
    "требования", "разрешение", "виза", "мфца", "афса", "финтех",
    "лаборатория", "портал", "регистрация", "лицензия", "стоимость", "срок",
    "процесс", "шаги", "помогите", "вопрос", "повтори", "повторите",
    "не", "қалай", "неге", "қашан", "қайда", "қандай", "кім", "мүмкін",
    "керек", "қажет", "түсіндір", "түсіндіріңіз", "айт", "айтыңыз", "көрсет",
    "көрсетіңіз", "өтініш", "құжат", "құжаттар", "талап", "талаптар",
    "рұқсат", "виза", "ахқо", "афса", "финтех", "зертхана", "портал",
    "тіркеу", "лицензия", "баға", "төлем", "мерзім", "процесс", "қадам",
    "көмек", "сұрақ", "қайтала", "қайталаңыз",
}
_MIN_INTERRUPTING_ALPHA_WORDS = 2
_MIN_INTERRUPTING_ALPHA_CHARS = 10
_ZH_BARGE_QUERY_TERMS = (
    "什么", "怎么", "如何", "为什么", "什么时候", "哪里", "哪个", "请问",
    "解释", "告诉", "申请", "文件", "要求", "许可证", "签证", "金融科技",
    "实验室", "门户", "注册", "牌照", "费用", "截止", "流程", "步骤",
    "帮助", "问题", "重复", "AIFC", "AFSA",
)


def normalize_lang(code: str | None) -> str:
    if not code:
        return "en"
    return LANG_ALIASES.get(code.lower().strip(), "en")


def supported_lang_or_none(code: str | None) -> str | None:
    if not code:
        return None
    return LANG_ALIASES.get(code.lower().strip())


def language_name(code: str) -> str:
    return {"en": "English", "ru": "Russian", "kk": "Kazakh", "zh": "Chinese"}.get(code, "English")


def detect_text_language(text: str) -> str:
    lowered = (text or "").lower()
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    if any(word in lowered for word in KAZAKH_HINT_WORDS):
        return "kk"
    if any(char in KAZAKH_CHARS for char in text):
        return "kk"
    if re.search(r"[А-Яа-яЁё]", text):
        return "ru"
    return "en"


def detect_supported_text_language(text: str) -> str | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    if _UNSUPPORTED_SCRIPT_RE.search(stripped):
        return None
    if _LATIN_EXTENDED_RE.search(stripped):
        return None
    return detect_text_language(stripped)


def _non_speech_ratio(text: str) -> float:
    lowered = (text or "").strip().lower()
    if not lowered:
        return 0.0
    words = [word for word in _NON_WORD_RE.split(lowered) if word]
    if not words:
        return 0.0
    noise_hits = 0
    for word in words:
        if word in _NON_SPEECH_ANNOTATION_WORDS or word in _NON_SPEECH_PHRASES:
            noise_hits += 1
            continue
        if any(term in word for term in ("noise", "background", "фон")):
            noise_hits += 1
    return noise_hits / len(words)


def _looks_like_background_noise(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if is_noise_utterance(normalized):
        return True
    if has_query_signal(normalized):
        return False
    return _non_speech_ratio(normalized) >= 0.66


def _normalized_phrase(text: str) -> str:
    lowered = (text or "").strip().lower()
    wrapped = _WRAPPED_ANNOTATION_RE.match(lowered)
    if wrapped:
        lowered = wrapped.group(1)
    collapsed = " ".join(_NON_WORD_RE.split(lowered)).strip()
    return collapsed


def _is_non_speech_annotation(text: str) -> bool:
    normalized = _normalized_phrase(text)
    if not normalized:
        return False
    if normalized in _NON_SPEECH_PHRASES:
        return True
    wrapped = _WRAPPED_ANNOTATION_RE.match(text or "")
    if wrapped:
        inner = _normalized_phrase(wrapped.group(1))
        words = inner.split()
        if inner in _NON_SPEECH_PHRASES:
            return True
        if words and all(word in _NON_SPEECH_ANNOTATION_WORDS for word in words):
            return True
    return False


def transcript_has_meaningful_speech(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _is_non_speech_annotation(stripped):
        return False
    collapsed = " ".join(_NON_WORD_RE.split(stripped.lower())).strip()
    if collapsed in _NOISE_WORDS:
        return False
    letters = _LETTER_RE.findall(stripped)
    if len(letters) >= 2:
        return True
    return any(ord(ch) >= 0x4E00 and ord(ch) <= 0x9FFF for ch in stripped)


def transcript_is_new_query_candidate(text: str) -> bool:
    """Stricter gate for barge-in partials than final transcript acceptance."""
    stripped = (text or "").strip()
    if is_stop_command(stripped):
        return True
    if is_noise_utterance(stripped):
        return False
    if _looks_like_background_noise(stripped):
        return False
    if not transcript_has_meaningful_speech(stripped):
        return False
    if detect_supported_text_language(stripped) is None:
        return False

    lowered = stripped.lower()
    words = _WORD_RE.findall(lowered)
    cjk_count = sum(1 for word in words if len(word) == 1 and "\u4e00" <= word <= "\u9fff")
    has_zh_query_term = any(term.lower() in lowered for term in _ZH_BARGE_QUERY_TERMS)

    alpha_words = [word for word in words if not ("\u4e00" <= word <= "\u9fff")]
    letter_count = sum(len(word) for word in alpha_words)
    has_query_term = has_zh_query_term or any(word in _BARGE_QUERY_TERMS for word in alpha_words)
    has_question_punctuation = any(mark in stripped for mark in ("?", "？"))
    if not has_query_term and not has_question_punctuation:
        return False

    if cjk_count >= 3:
        return True
    if len(alpha_words) >= 2 and letter_count >= 6:
        return True
    if len(alpha_words) == 1:
        word = alpha_words[0]
        return word in _BARGE_SINGLE_WORDS
    return False


def is_interrupt_candidate(text: str, avg_logprob: float | None = None) -> bool:
    """Stricter gate for realtime partials to reduce false interruptions."""
    if not text:
        return False
    if avg_logprob is not None and avg_logprob < -1.9:
        return False
    stripped = (text or "").strip()
    if is_noise_utterance(stripped):
        return False
    if is_stop_command(stripped):
        return True
    if detect_supported_text_language(stripped) is None:
        return False
    normalized = " ".join(stripped.lower().split())
    if not transcript_has_meaningful_speech(normalized):
        return False
    if _looks_like_background_noise(normalized):
        return False
    if has_query_signal(normalized):
        return True
    if "?" in normalized or "？" in normalized:
        return True

    words = [word for word in _WORD_RE.findall(normalized) if word]
    if not words:
        return False
    alpha_words = [word for word in words if not ("\u4e00" <= word <= "\u9fff")]

    # Extremely short fragments should not trigger interruptions unless they
    # match explicit command-like terms.
    if len(alpha_words) < _MIN_INTERRUPTING_ALPHA_WORDS:
        return bool(alpha_words and alpha_words[0] in _BARGE_SINGLE_WORDS)

    if len(alpha_words) >= _MIN_INTERRUPTING_ALPHA_WORDS:
        letter_count = sum(len(word) for word in alpha_words)
        if letter_count >= _MIN_INTERRUPTING_ALPHA_CHARS and not any(word in _NOISE_WORDS for word in alpha_words):
            return True
    if any(term in normalized for term in _ZH_BARGE_QUERY_TERMS):
        return True
    return False


def has_query_signal(text: str) -> bool:
    stripped = (text or "").strip().lower()
    if not stripped:
        return False
    words = [word for word in _WORD_RE.findall(stripped) if word]
    if any(word in _BARGE_QUERY_TERMS for word in words):
        return True
    if any(term in stripped for term in _ZH_BARGE_QUERY_TERMS):
        return True
    if len(words) >= 2 and any(any(char in word for char in "?？！。！？") for word in words):
        return True
    alpha_words = [word for word in words if not ("\u4e00" <= word <= "\u9fff")]
    # Allow very short interrupt cues only when explicitly query-like.
    if len(alpha_words) == 1 and alpha_words[0] in _BARGE_SINGLE_WORDS:
        return True
    return False


def dedupe_repeated_transcript(text: str) -> str:
    """Collapse exact repeated STT commits such as 'question question'."""
    stripped = " ".join((text or "").split()).strip()
    if not stripped:
        return ""
    words = stripped.split()
    if len(words) >= 2 and len(words) % 2 == 0:
        half = len(words) // 2
        if [word.lower() for word in words[:half]] == [word.lower() for word in words[half:]]:
            return " ".join(words[:half])

    midpoint = len(stripped) // 2
    if len(stripped) % 2 == 0 and stripped[:midpoint].strip().lower() == stripped[midpoint:].strip().lower():
        return stripped[:midpoint].strip()
    return stripped


def is_stop_command(text: str) -> bool:
    normalized = " ".join(text.lower().strip().split())
    return normalized in STOP_KEYWORDS or any(word in normalized for word in ("стоп", "stop", "тоқта"))


def is_noise_utterance(text: str) -> bool:
    """Detect obvious non-speech/non-query utterances (chatter/noise/background speech).

    This is intentionally strict so it does not suppress genuine user questions,
    but avoids treating ambient noise tags as conversational turns.
    """
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if normalized in _NON_SPEECH_PHRASES:
        return True
    inner = _normalized_phrase(normalized)
    if inner in _NON_SPEECH_PHRASES:
        return True
    words = [word for word in _NON_WORD_RE.split(inner) if word]
    if not words:
        return False
    if all(word in _NON_SPEECH_ANNOTATION_WORDS for word in words):
        return True
    return False


def smalltalk_reply(text: str, lang: str) -> str | None:
    normalized = " ".join(text.lower().strip().split())
    return CHITCHAT.get(lang, {}).get(normalized) or GOODBYE.get(lang, {}).get(normalized)
