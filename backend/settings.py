from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.getenv("AIFC_DATA_DIR", os.getenv("DATA_DIR", str(ROOT.parent / "data")))).expanduser().resolve()
_INTRO_AUDIO_CACHE_DIR_RAW = os.getenv("INTRO_AUDIO_CACHE_DIR", str(ROOT / "cache" / "intro"))
_INTRO_AUDIO_CACHE_DIR_PATH = Path(_INTRO_AUDIO_CACHE_DIR_RAW).expanduser()
if not _INTRO_AUDIO_CACHE_DIR_PATH.is_absolute():
    _INTRO_AUDIO_CACHE_DIR_PATH = ROOT / _INTRO_AUDIO_CACHE_DIR_PATH
INTRO_AUDIO_CACHE_DIR = _INTRO_AUDIO_CACHE_DIR_PATH.resolve()
INTRO_AVATAR_CACHE_KEY = os.getenv("INTRO_AVATAR_CACHE_KEY", os.getenv("SYNCTALK_AVATAR", "default"))


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


APP_HOST = os.getenv("WS_BACKEND_HOST", "0.0.0.0")
APP_PORT = env_int("WS_BACKEND_PORT", 8080)

SONIOX_API_KEY = os.getenv("SONIOX_API_KEY", os.getenv("SONIOX_KEY", ""))
SONIOX_STT_WS_URL = os.getenv("SONIOX_STT_WS_URL", "wss://stt-rt.soniox.com/transcribe-websocket")
SONIOX_STT_MODEL = os.getenv("SONIOX_STT_MODEL", "stt-rt-v4")
SONIOX_STT_AUDIO_FORMAT = os.getenv("SONIOX_STT_AUDIO_FORMAT", "pcm_s16le")
SONIOX_STT_SAMPLE_RATE = env_int("SONIOX_STT_SAMPLE_RATE", 16000)
SONIOX_STT_LANGUAGE_HINTS = [
    item.strip()
    for item in os.getenv("SONIOX_STT_LANGUAGE_HINTS", "en,ru,kk,zh").split(",")
    if item.strip()
]
SONIOX_STT_LANGUAGE_HINTS_STRICT = env_bool("SONIOX_STT_LANGUAGE_HINTS_STRICT", True)
SONIOX_STT_ENABLE_ENDPOINT_DETECTION = env_bool("SONIOX_STT_ENABLE_ENDPOINT_DETECTION", True)
SONIOX_STT_MAX_ENDPOINT_DELAY_MS = env_int("SONIOX_STT_MAX_ENDPOINT_DELAY_MS", 500)
SONIOX_STT_ENDPOINT_WAIT_S = env_float("SONIOX_STT_ENDPOINT_WAIT_S", 0.25)
SONIOX_STT_FINALIZE_TIMEOUT_S = env_float("SONIOX_STT_FINALIZE_TIMEOUT_S", 1.0)
SONIOX_STT_REALTIME_FINALIZE_TIMEOUT_S = env_float(
    "SONIOX_STT_REALTIME_FINALIZE_TIMEOUT_S",
    SONIOX_STT_FINALIZE_TIMEOUT_S,
)
SONIOX_STT_BATCH_FINALIZE_TIMEOUT_S = env_float("SONIOX_STT_BATCH_FINALIZE_TIMEOUT_S", 3.0)
SONIOX_STT_MIN_TOKEN_CONFIDENCE = env_float("SONIOX_STT_MIN_TOKEN_CONFIDENCE", 0.0)
SONIOX_STT_PRECONNECT = env_bool("SONIOX_STT_PRECONNECT", True)
SONIOX_STT_KEEPALIVE_INTERVAL_S = env_float("SONIOX_STT_KEEPALIVE_INTERVAL_S", 5.0)
SONIOX_STT_CONTEXT_MAX_CHARS = env_int("SONIOX_STT_CONTEXT_MAX_CHARS", 10000)

SONIOX_TTS_API_KEY = os.getenv("SONIOX_TTS_API_KEY", SONIOX_API_KEY)
SONIOX_TTS_WS_URL = os.getenv("SONIOX_TTS_WS_URL", "wss://tts-rt.soniox.com/tts-websocket")
SONIOX_TTS_MODEL = os.getenv("SONIOX_TTS_MODEL", "tts-rt-v1")
SONIOX_TTS_VOICE = os.getenv("SONIOX_TTS_VOICE", "Maya")
SONIOX_TTS_INTRO_LANGUAGE = os.getenv("SONIOX_TTS_INTRO_LANGUAGE", "en")
SONIOX_TTS_INTRO_VOICE = os.getenv("SONIOX_TTS_INTRO_VOICE", SONIOX_TTS_VOICE)
SONIOX_TTS_AUDIO_FORMAT = os.getenv("SONIOX_TTS_AUDIO_FORMAT", "pcm_s16le")
SONIOX_TTS_SAMPLE_RATE = env_int("SONIOX_TTS_SAMPLE_RATE", 24000)
SONIOX_TTS_BITRATE = env_int("SONIOX_TTS_BITRATE", 0)
SONIOX_TTS_KEEPALIVE_INTERVAL_S = env_float("SONIOX_TTS_KEEPALIVE_INTERVAL_S", 10.0)
SONIOX_TTS_CONNECT_TIMEOUT_S = env_float("SONIOX_TTS_CONNECT_TIMEOUT_S", 20.0)
SONIOX_TTS_PRECONNECT_TIMEOUT_S = env_float("SONIOX_TTS_PRECONNECT_TIMEOUT_S", 2.5)
SONIOX_TTS_PRECONNECT_ATTEMPTS = env_int("SONIOX_TTS_PRECONNECT_ATTEMPTS", 3)
TTS_PREWARM_QUERY_WAIT_S = env_float("TTS_PREWARM_QUERY_WAIT_S", 8.0)
SONIOX_TTS_STREAM_TIMEOUT_S = env_float("SONIOX_TTS_STREAM_TIMEOUT_S", 30.0)
SONIOX_TTS_FORCE_IPV4 = env_bool("SONIOX_TTS_FORCE_IPV4", True)
SONIOX_TTS_STREAMING_AVATAR = env_bool("SONIOX_TTS_STREAMING_AVATAR", True)
SONIOX_TTS_FIRST_SEGMENT_MS = env_int("SONIOX_TTS_FIRST_SEGMENT_MS", 220)
SONIOX_TTS_SEGMENT_MS = env_int("SONIOX_TTS_SEGMENT_MS", 800)
SONIOX_TTS_MIN_SEGMENT_MS = env_int("SONIOX_TTS_MIN_SEGMENT_MS", 220)
SONIOX_TTS_MAX_SEGMENT_MS = env_int("SONIOX_TTS_MAX_SEGMENT_MS", 900)
AVATAR_TTS_FIRST_SEGMENT_MS = env_int("AVATAR_TTS_FIRST_SEGMENT_MS", 320)
AVATAR_TTS_SEGMENT_MS = env_int("AVATAR_TTS_SEGMENT_MS", 900)
AVATAR_TTS_MIN_SEGMENT_MS = env_int("AVATAR_TTS_MIN_SEGMENT_MS", 450)
AVATAR_TTS_MAX_SEGMENT_MS = env_int("AVATAR_TTS_MAX_SEGMENT_MS", 1400)
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_TEMPERATURE = env_float("GEMINI_TEMPERATURE", 0.2)
GEMINI_MAX_OUTPUT_TOKENS = env_int("GEMINI_MAX_OUTPUT_TOKENS", 650)
ANSWER_DETAIL_MAX_POINTS = env_int("ANSWER_DETAIL_MAX_POINTS", 5)
ANSWER_DETAIL_MAX_SECTIONS = env_int("ANSWER_DETAIL_MAX_SECTIONS", 2)
ANSWER_DETAIL_MAX_SECTION_ITEMS = env_int("ANSWER_DETAIL_MAX_SECTION_ITEMS", 4)
ANSWER_VOICE_MAX_CHARS = env_int("ANSWER_VOICE_MAX_CHARS", 900)

SYNCTALK_STREAM_URL = os.getenv("SYNCTALK_STREAM_URL", "http://127.0.0.1:8005/infer_stream")
SYNCTALK_TIMEOUT_S = env_float("SYNCTALK_TIMEOUT_S", 120.0)
SYNCTALK_FRAME_TIMEOUT_S = env_float("SYNCTALK_FRAME_TIMEOUT_S", 8.0)
SYNCTALK_MAX_CONCURRENCY = env_int("SYNCTALK_MAX_CONCURRENCY", 2)
INTRO_AUDIO_CACHE_PREBUILD = env_bool("INTRO_AUDIO_CACHE_PREBUILD", True)

ANSWER_RACE_TIMEOUT_MS = env_int("ANSWER_RACE_TIMEOUT_MS", 250)
ANSWER_CACHE_TTL_S = env_float("ANSWER_CACHE_TTL_S", 1800.0)
GEMINI_RAG_MAX_WAIT_MS = env_int("GEMINI_RAG_MAX_WAIT_MS", 12000)
FAQ_WIN_THRESHOLD = env_float("FAQ_WIN_THRESHOLD", 0.90)
CACHE_WIN_THRESHOLD = env_float("CACHE_WIN_THRESHOLD", 0.90)
LOCAL_RAG_HIGH_THRESHOLD = env_float("LOCAL_RAG_HIGH_THRESHOLD", 0.65)
LOCAL_RAG_PARTIAL_THRESHOLD = env_float("LOCAL_RAG_PARTIAL_THRESHOLD", 0.45)
LOCAL_RAG_MAX_CONCURRENCY = env_int("LOCAL_RAG_MAX_CONCURRENCY", 2)
EXTERNAL_RAG_HIGH_THRESHOLD = env_float("EXTERNAL_RAG_HIGH_THRESHOLD", 0.25)
EXTERNAL_RAG_PARTIAL_THRESHOLD = env_float("EXTERNAL_RAG_PARTIAL_THRESHOLD", 0.10)
EXTERNAL_RAG_ENABLED = env_bool("EXTERNAL_RAG_ENABLED", True)
EXTERNAL_RAG_URL = os.getenv("EXTERNAL_RAG_URL", "").strip()
EXTERNAL_RAG_API_KEY = os.getenv("EXTERNAL_RAG_API_KEY", os.getenv("EXTERNAL_RAG_BEARER_TOKEN", "")).strip()
EXTERNAL_RAG_AUTH_HEADER = os.getenv("EXTERNAL_RAG_AUTH_HEADER", "Authorization").strip()
EXTERNAL_RAG_TIMEOUT_S = env_float("EXTERNAL_RAG_TIMEOUT_S", 8.0)
EXTERNAL_RAG_FIRST_RESPONSE_TIMEOUT_S = env_float("EXTERNAL_RAG_FIRST_RESPONSE_TIMEOUT_S", EXTERNAL_RAG_TIMEOUT_S)
EXTERNAL_RAG_HYBRID = env_bool("EXTERNAL_RAG_HYBRID", True)
EXTERNAL_RAG_WITH_RERANK = env_bool("EXTERNAL_RAG_WITH_RERANK", True)
EXTERNAL_RAG_LIMIT = env_int("EXTERNAL_RAG_LIMIT", 30)
EXTERNAL_RAG_TOP_N = env_int("EXTERNAL_RAG_TOP_N", 3)
LOCAL_RAG_STARTUP_PREWARM = env_bool("LOCAL_RAG_STARTUP_PREWARM", True)
LOCAL_RAG_PREWARM_QUERY = os.getenv("LOCAL_RAG_PREWARM_QUERY", "What is the AIFC FinTech Lab?")

FIRST_TTS_CHARS = env_int("FIRST_TTS_CHARS", 12)
MIN_TTS_CHARS = env_int("MIN_TTS_CHARS", 24)
MAX_TTS_CHARS = env_int("MAX_TTS_CHARS", 80)
SHORT_SENTENCE_CHARS = env_int("SHORT_SENTENCE_CHARS", 12)
MAX_HISTORY_TURNS = env_int("MAX_HISTORY_TURNS", 6)

SYSTEM_PROMPT = """You are an AIFC talking avatar.
Answer only from the retrieved context and the short conversation history.
If the retrieved context does not contain the answer, say that clearly.
Reply in the user's language.
If the user only greets you, greet them briefly and offer help with AIFC topics.
If the user says goodbye, answer with a brief goodbye in the same language.
Keep answers concise, practical, and suitable for speech output.
Do not invent citations, phone numbers, policies, deadlines, or legal requirements."""
