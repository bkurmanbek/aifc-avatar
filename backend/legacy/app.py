from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
from types import SimpleNamespace
from dataclasses import dataclass, field
from time import perf_counter
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .language import (
    dedupe_repeated_transcript,
    detect_supported_text_language,
    detect_text_language,
    is_stop_command,
    is_noise_utterance,
    normalize_lang,
    smalltalk_reply,
    supported_lang_or_none,
    transcript_has_meaningful_speech,
    transcript_is_new_query_candidate,
    UNSUPPORTED_LANGUAGE_MESSAGE,
)
from .llm import (
    _extract_json_from_wrapped,
    _extract_json_payload,
    build_prompt,
    stream_answer,
)
from .settings import (
    APP_HOST,
    APP_PORT,
    ANSWER_DETAIL_MAX_POINTS,
    ANSWER_DETAIL_MAX_SECTIONS,
    ANSWER_DETAIL_MAX_SECTION_ITEMS,
    ANSWER_VOICE_MAX_CHARS,
    FIRST_TTS_CHARS,
    INTRO_AVATAR_CACHE_KEY,
    INTRO_AUDIO_CACHE_DIR,
    INTRO_AUDIO_CACHE_PREBUILD,
    LOCAL_RAG_PREWARM_QUERY,
    LOCAL_RAG_STARTUP_PREWARM,
    LOCAL_TTS_STARTUP_PREWARM,
    LOCAL_TTS_URL,
    MEDIA_KEEPWARM_ENABLED,
    MEDIA_KEEPWARM_INTERVAL_S,
    MEDIA_KEEPWARM_LANG,
    MEDIA_KEEPWARM_TEXT,
    MAX_HISTORY_TURNS,
    MAX_TTS_CHARS,
    MIN_TTS_CHARS,
    SHORT_SENTENCE_CHARS,
    SYNCTALK_STREAM_URL,
    SONIOX_STT_KEEPALIVE_INTERVAL_S,
    SONIOX_STT_ENDPOINT_WAIT_S,
    SONIOX_STT_PRECONNECT,
    SONIOX_TTS_CONTEXT_FILE,
    SONIOX_TTS_INTRO_LANGUAGE,
    SONIOX_TTS_INTRO_VOICE,
    ROOT,
    TTS_PROVIDER,
)
from .spoken_text import (
    extract_blocks,
    is_speakable_text,
    normalize_spoken_numbers,
    remove_repeated_sentences,
    rebuild_blocks,
    sanitize_spoken_text,
    sentenceize_spoken_text,
)
from .stt import SonioxBatchSTT, SonioxRealtimeSession, looks_like_pcm16_chunk
from .synctalk import SyncTalkClient
from .tts import ElevenTTS
from .tts_pronunciation import prepare_tts_text
from .voice_chunker import LowLatencyVoiceChunker
from .ws_writer import ClientClosedError, WsWriter
from .response_stream import ResponseStream
from .answer_race import AnswerRaceResult, clear_answer_caches, common_tts_prewarm_items, run_answer_race
from .abbreviations import normalize_transcript_abbreviations
from .original_backend import (
    _prebuilt_capability_answer,
    _prebuilt_capability_details,
    _prebuilt_chitchat_answer,
    fast_answer_plan_retrieve,
    is_capability_query,
    update_conversation_memory,
    wrap_answer_for_voice_and_chat,
    wrap_spoken_and_details,
)

from backend.core.logging import configure_logging, log_event

configure_logging(reset=False)
log = logging.getLogger(__name__)

app = FastAPI()
_MEDIA_KEEPWARM_TASK: asyncio.Task | None = None
_INTRO_PLAYED_TOKENS: set[str] = set()
_INTRO_PLAYED_TOKEN_ORDER: list[str] = []
_INTRO_IN_PROGRESS_TOKENS: set[str] = set()
_INTRO_PLAYED_TOKEN_LIMIT = 1024
_INTRO_BLOCKS_CACHE: list["IntroBlock"] | None = None
_INTRO_AUDIO_CACHE_LOCK: asyncio.Lock | None = None
_INTRO_PATH = ROOT / "intro.json"
_INTRO_BLOCK_ALIASES = {
    "kk": ("kk", "kazakh", "қазақ", "kz"),
    "en": ("en", "english"),
    "ru": ("ru", "russian", "русский"),
    "general": ("general", "general_part", "general-part", "general part"),
}
_INTRO_BLOCK_FILENAMES = {
    "kk": "01_kk.wav",
    "en": "02_en.wav",
    "ru": "03_ru.wav",
    "general": "04_general.wav",
}
_INTRO_FRAME_HEADROOM = 8
_INTRO_CACHED_FRAME_BATCH = 8


@dataclass(frozen=True)
class IntroBlock:
    key: str
    text: str
    language: str


def _intro_value_to_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(part for part in (_intro_value_to_text(item) for item in value) if part).strip()
    if isinstance(value, dict):
        for key in ("text", "spoken", "content", "body"):
            text = _intro_value_to_text(value.get(key))
            if text:
                return text
        return "\n".join(part for part in (_intro_value_to_text(item) for item in value.values()) if part).strip()
    return ""


def _intro_block_language(key: str) -> str:
    if key == "kk":
        return "kk"
    if key == "ru":
        return "ru"
    return SONIOX_TTS_INTRO_LANGUAGE


def _intro_block_from_key(key: str, text: str) -> IntroBlock | None:
    clean = text.strip()
    if not clean:
        return None
    return IntroBlock(key=key, text=clean, language=_intro_block_language(key))


def _canonical_intro_key(raw_key: object) -> str | None:
    key = str(raw_key or "").strip().lower().replace("_", "-")
    if not key:
        return None
    for canonical, aliases in _INTRO_BLOCK_ALIASES.items():
        normalized_aliases = {alias.lower().replace("_", "-") for alias in aliases}
        if key in normalized_aliases:
            return canonical
    return None


def _detect_intro_segment_language(text: str) -> str:
    if re.search(r"[ӘәҒғҚқҢңӨөҰұҮүҺһІі]", text):
        return "kk"
    if re.search(r"[А-Яа-яЁё]", text):
        return "ru"
    return "en"


def _split_raw_intro_blocks(raw: str) -> list[IntroBlock]:
    buckets = {"kk": [], "en": [], "ru": [], "general": []}
    seen_ru = False
    for part in re.split(r"\n\s*\n+", raw):
        text = part.strip()
        if not text:
            continue
        detected = _detect_intro_segment_language(text)
        if detected == "ru":
            seen_ru = True
            buckets["ru"].append(text)
        elif detected == "kk":
            buckets["kk"].append(text)
        elif seen_ru:
            buckets["general"].append(text)
        else:
            buckets["en"].append(text)
    blocks: list[IntroBlock] = []
    for key in ("kk", "en", "ru", "general"):
        block = _intro_block_from_key(key, "\n\n".join(buckets[key]))
        if block is not None:
            blocks.append(block)
    return blocks


def _load_intro_blocks() -> list[IntroBlock]:
    global _INTRO_BLOCKS_CACHE
    if _INTRO_BLOCKS_CACHE is not None:
        return _INTRO_BLOCKS_CACHE
    try:
        raw = _INTRO_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning("intro file unavailable: %s", exc)
        _INTRO_BLOCKS_CACHE = []
        return []
    if not raw:
        _INTRO_BLOCKS_CACHE = []
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("intro.json is not valid JSON; splitting raw text intro into ordered blocks")
        _INTRO_BLOCKS_CACHE = _split_raw_intro_blocks(raw)
        return _INTRO_BLOCKS_CACHE
    blocks = payload.get("blocks", payload) if isinstance(payload, dict) else payload
    parsed: dict[str, str] = {}
    if isinstance(blocks, dict):
        for raw_key, value in blocks.items():
            key = _canonical_intro_key(raw_key)
            if key is None:
                continue
            text = _intro_value_to_text(value)
            if text:
                parsed[key] = text
    elif isinstance(blocks, list):
        fallback_order = ("kk", "en", "ru", "general")
        for index, value in enumerate(blocks[:4]):
            key = None
            if isinstance(value, dict):
                key = _canonical_intro_key(value.get("key") or value.get("language") or value.get("name"))
            if key is None and index < len(fallback_order):
                key = fallback_order[index]
            text = _intro_value_to_text(value)
            if key and text:
                parsed[key] = text
    intro_blocks = [
        block
        for key in ("kk", "en", "ru", "general")
        if (block := _intro_block_from_key(key, parsed.get(key, ""))) is not None
    ]
    _INTRO_BLOCKS_CACHE = intro_blocks
    return intro_blocks


def _intro_audio_cache_lock() -> asyncio.Lock:
    global _INTRO_AUDIO_CACHE_LOCK
    if _INTRO_AUDIO_CACHE_LOCK is None:
        _INTRO_AUDIO_CACHE_LOCK = asyncio.Lock()
    return _INTRO_AUDIO_CACHE_LOCK


def _intro_audio_path(block: IntroBlock):
    return INTRO_AUDIO_CACHE_DIR / _INTRO_BLOCK_FILENAMES[block.key]


def _mark_intro_token_played(token: str) -> None:
    if not token or token in _INTRO_PLAYED_TOKENS:
        return
    _INTRO_IN_PROGRESS_TOKENS.discard(token)
    _INTRO_PLAYED_TOKENS.add(token)
    _INTRO_PLAYED_TOKEN_ORDER.append(token)
    while len(_INTRO_PLAYED_TOKEN_ORDER) > _INTRO_PLAYED_TOKEN_LIMIT:
        old = _INTRO_PLAYED_TOKEN_ORDER.pop(0)
        _INTRO_PLAYED_TOKENS.discard(old)


def _mark_intro_token_in_progress(token: str) -> None:
    if token and token not in _INTRO_PLAYED_TOKENS:
        _INTRO_IN_PROGRESS_TOKENS.add(token)


def _clear_intro_token_in_progress(token: str | None) -> None:
    if token:
        _INTRO_IN_PROGRESS_TOKENS.discard(token)


def _intro_token_seen(token: str) -> bool:
    return bool(token and token in _INTRO_PLAYED_TOKENS)


def _intro_token_in_progress(token: str | None) -> bool:
    return bool(token and token in _INTRO_IN_PROGRESS_TOKENS)


def _intro_audio_meta_path(block: IntroBlock):
    return _intro_audio_path(block).with_suffix(".json")


def _safe_cache_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "default").strip("-") or "default"


def _intro_frame_path(block: IntroBlock):
    return INTRO_AUDIO_CACHE_DIR / "frames" / _safe_cache_key(INTRO_AVATAR_CACHE_KEY) / f"{block.key}.json"


def _intro_frame_cache_url(block: IntroBlock) -> str:
    return f"/intro-cache/{_safe_cache_key(INTRO_AVATAR_CACHE_KEY)}/{block.key}"


def _intro_frame_range_path(avatar: str, block_key: str, start: int, limit: int) -> Path:
    return INTRO_AUDIO_CACHE_DIR / "frame_ranges" / _safe_cache_key(avatar) / block_key / f"{start}_{limit}.json"


def _intro_audio_signature(block: IntroBlock) -> str:
    payload = {
        "cache_version": 2,
        "key": block.key,
        "language": block.language,
        "voice": SONIOX_TTS_INTRO_VOICE,
        "expand_context_terms": False,
        "text": block.text,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _intro_frame_signature(block: IntroBlock) -> str:
    payload = {
        "cache_version": 1,
        "key": block.key,
        "audio_signature": _intro_audio_signature(block),
        "avatar": INTRO_AVATAR_CACHE_KEY,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _intro_audio_cache_is_valid(block: IntroBlock) -> bool:
    path = _intro_audio_path(block)
    meta_path = _intro_audio_meta_path(block)
    if not (path.exists() and path.stat().st_size > 44 and meta_path.exists()):
        return False
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return payload.get("signature") == _intro_audio_signature(block)


def _load_intro_frames_from_cache(block: IntroBlock) -> list[str] | None:
    path = _intro_frame_path(block)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("signature") != _intro_frame_signature(block):
        return None
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        return None
    return [str(frame) for frame in frames if frame]


def _intro_frame_cache_info(block: IntroBlock) -> tuple[str, int] | None:
    path = _intro_frame_path(block)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("signature") != _intro_frame_signature(block):
        return None
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        return None
    return _intro_frame_cache_url(block), len(frames)


def _save_intro_frames_to_cache(block: IntroBlock, frames: list[str]) -> None:
    if not frames:
        return
    path = _intro_frame_path(block)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "signature": _intro_frame_signature(block),
        "key": block.key,
        "avatar": INTRO_AVATAR_CACHE_KEY,
        "frames": frames,
    }
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


async def _ensure_intro_audio_file(tts: ElevenTTS, block: IntroBlock) -> bytes:
    path = _intro_audio_path(block)
    meta_path = _intro_audio_meta_path(block)
    if _intro_audio_cache_is_valid(block):
        return path.read_bytes()
    async with _intro_audio_cache_lock():
        if _intro_audio_cache_is_valid(block):
            return path.read_bytes()
        INTRO_AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        log.info("generating cached intro audio block=%s path=%s", block.key, path)
        audio_wav = await tts.synthesize(
            block.text,
            language=block.language,
            priority=0,
            voice=SONIOX_TTS_INTRO_VOICE,
            expand_context_terms=False,
        )
        meta = {
            "signature": _intro_audio_signature(block),
            "key": block.key,
            "language": block.language,
            "voice": SONIOX_TTS_INTRO_VOICE,
            "expand_context_terms": False,
        }
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_meta_path = meta_path.with_suffix(f"{meta_path.suffix}.tmp")
        tmp_path.write_bytes(audio_wav)
        tmp_meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
        tmp_meta_path.replace(meta_path)
        return audio_wav


@app.on_event("startup")
async def _prebuild_intro_audio_cache() -> None:
    if not INTRO_AUDIO_CACHE_PREBUILD:
        log.info("intro audio cache prebuild skipped")
        return
    blocks = _load_intro_blocks()
    if not blocks:
        return
    missing = [
        block
        for block in blocks
        if not _intro_audio_cache_is_valid(block)
    ]
    if not missing:
        log.info("intro audio cache ready: %s", INTRO_AUDIO_CACHE_DIR)
        return
    tts = ElevenTTS()
    try:
        for block in missing:
            await _ensure_intro_audio_file(tts, block)
        log.info("intro audio cache generated: %s", INTRO_AUDIO_CACHE_DIR)
    except Exception:
        log.exception("intro audio cache prebuild failed; missing files will be generated on first session")
    finally:
        with contextlib.suppress(Exception):
            await tts.close()


def _log_background_task_error(task: asyncio.Task) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        exc = task.exception()
        if exc is not None:
            log.error("background task failed", exc_info=(type(exc), exc, exc.__traceback__))


async def _prewarm_local_tts_cache() -> None:
    if TTS_PROVIDER != "local" or not LOCAL_TTS_STARTUP_PREWARM:
        return
    items = common_tts_prewarm_items()
    warmed = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for text, language in items:
            try:
                response = await client.post(
                    LOCAL_TTS_URL,
                    json={"text": text, "lang": language, "priority": 1},
                )
                response.raise_for_status()
                warmed += 1
            except Exception as exc:
                log.warning("local TTS prewarm skipped item lang=%s: %s", language, exc)
    log.info("local TTS prewarm complete: %d/%d items", warmed, len(items))


async def _prewarm_local_rag() -> None:
    if not LOCAL_RAG_STARTUP_PREWARM:
        return
    started = perf_counter()
    try:
        await asyncio.to_thread(fast_answer_plan_retrieve, LOCAL_RAG_PREWARM_QUERY, [], None)
    except Exception as exc:
        log.warning("local RAG prewarm failed: %s", exc)
        return
    log.info("local RAG prewarm complete in %dms", int((perf_counter() - started) * 1000))


async def _media_keepwarm_once() -> None:
    if TTS_PROVIDER != "local":
        return
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        tts_response = await client.post(
            LOCAL_TTS_URL,
            json={
                "text": MEDIA_KEEPWARM_TEXT,
                "lang": MEDIA_KEEPWARM_LANG,
                "priority": 1,
            },
        )
        tts_response.raise_for_status()
        audio_b64 = tts_response.json().get("audio_b64")
        if not audio_b64:
            return

        async with client.stream(
            "POST",
            SYNCTALK_STREAM_URL,
            json={
                "audio_b64": audio_b64,
                "priority": 1,
                "chunk_idx": 99,
            },
        ) as response:
            response.raise_for_status()
            frame_count = 0
            async for line in response.aiter_lines():
                if line.strip():
                    frame_count += 1
        log.info("media keepwarm complete: frames=%d", frame_count)


async def _media_keepwarm_loop() -> None:
    while True:
        try:
            await _media_keepwarm_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("media keepwarm skipped: %s", exc)
        await asyncio.sleep(max(10.0, MEDIA_KEEPWARM_INTERVAL_S))


@app.on_event("startup")
async def startup_prewarm() -> None:
    global _MEDIA_KEEPWARM_TASK
    log.info("tts provider active: %s", TTS_PROVIDER)
    tts_task = asyncio.create_task(_prewarm_local_tts_cache())
    await _prewarm_local_rag()
    await tts_task
    if MEDIA_KEEPWARM_ENABLED and TTS_PROVIDER == "local" and _MEDIA_KEEPWARM_TASK is None:
        _MEDIA_KEEPWARM_TASK = asyncio.create_task(_media_keepwarm_loop())
    elif MEDIA_KEEPWARM_ENABLED:
        log.info("media keepwarm skipped for tts provider: %s", TTS_PROVIDER)


@app.on_event("shutdown")
async def shutdown_keepwarm() -> None:
    global _MEDIA_KEEPWARM_TASK
    if _MEDIA_KEEPWARM_TASK is not None:
        _MEDIA_KEEPWARM_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _MEDIA_KEEPWARM_TASK
        _MEDIA_KEEPWARM_TASK = None

_SPOKEN_COMPLETE_RE = re.compile(r"\[\[spoken\]\](.*?)\[\[/spoken\]\]", re.IGNORECASE | re.DOTALL)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?。！？])\s+")
_NORMALIZED_QUERY_PUNCT_RE = re.compile(r"[.!?,。！？;:]+")
_NOISE_SPANISH = {
    "en": "I could not hear a clear question. Please repeat it.",
    "ru": "Я не уловил вопрос. Пожалуйста, повторите его.",
    "kk": "Сұрағыңызды анық естімедім. Қайта айтып беріңіз.",
    "zh": "我没有听清问题，请再说一遍。",
}

_PARTIAL_INTERRUPT_WINDOW_S = 1.2
_PARTIAL_INTERRUPT_HITS = 2
_INTERRUPT_COOLDOWN_S = 1.0
_DUPLICATE_FINAL_AUDIO_IGNORE_S = 6.0
_MAX_SPOKEN_WORDS = 28
_MAX_SPOKEN_CHARS = 180
_MAX_REALTIME_TTS_CHUNKS = 6
_SPOKEN_SOFT_CUT_RE = re.compile(r"[,;:，；、]")
_STREAM_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9]+(?:['’\-][A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9]+)?", re.UNICODE)
_TTS_MAX_WORD_COUNT = 7
_STREAM_TERMINAL_PUNCT = ".!?。！？"
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


def _signature_for_interruption(text: str) -> str:
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


def _coerce_confidence(value: object) -> float:
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


def _normalize_answer_kind(value: object) -> str:
    allowed = {"direct", "procedural", "clarification", "fallback", "error"}
    if not isinstance(value, str):
        return "direct"
    value_norm = value.strip().lower()
    return value_norm if value_norm in allowed else "direct"


def _normalize_tts_chunks(chunks: object) -> list[str]:
    if not isinstance(chunks, list):
        return []
    output: list[str] = []
    for chunk in chunks:
        text = str(chunk).strip()
        if text:
            output.append(text)
    return output


def _normalize_tts_chunk_for_language(text: str, language: str) -> str:
    has_leading_space = bool(text and text[0].isspace())
    has_trailing_space = bool(text and text[-1].isspace())
    cleaned = prepare_tts_text(text, language, SONIOX_TTS_CONTEXT_FILE)
    if cleaned and has_leading_space and not cleaned.startswith(" "):
        cleaned = " " + cleaned
    if cleaned and has_trailing_space and not cleaned.endswith(" "):
        cleaned += " "
    return cleaned


def _build_control_payload(payload: dict[str, object], interrupted_input: bool) -> dict[str, bool]:
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


def _is_final_turn_candidate(text: str, language: str, require_query_signal: bool) -> bool:
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


def _extract_json_any(raw: str) -> dict[str, object] | None:
    payload = _extract_json_payload(raw) or _extract_json_from_wrapped(raw)
    if isinstance(payload, dict):
        return payload
    return _extract_balanced_json(raw)


def _extract_json_string_field(raw: str, field: str) -> str | None:
    """Extract a JSON string field value from possibly partial model output."""
    marker = f'"{field}"'
    start = raw.find(marker)
    if start < 0:
        return None

    colon = raw.find(":", start + len(marker))
    if colon < 0:
        return None

    i = colon + 1
    while i < len(raw) and raw[i].isspace():
        i += 1
    if i >= len(raw) or raw[i] != '"':
        return None

    i += 1
    value_chars: list[str] = []
    escaped = False
    while i < len(raw):
        ch = raw[i]
        i += 1
        if escaped:
            value_chars.append("\\" + ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            break
        value_chars.append(ch)
    else:
        return None

    try:
        return json.loads('"' + "".join(value_chars) + '"')
    except Exception:
        return "".join(value_chars)


def _extract_json_string_field_progress(raw: str, field: str) -> tuple[str, bool] | None:
    """Return the current value of a JSON string field and whether it is closed."""
    marker = f'"{field}"'
    start = raw.find(marker)
    if start < 0:
        return None

    colon = raw.find(":", start + len(marker))
    if colon < 0:
        return None

    i = colon + 1
    while i < len(raw) and raw[i].isspace():
        i += 1
    if i >= len(raw) or raw[i] != '"':
        return None

    i += 1
    value_chars: list[str] = []
    escaped = False
    complete = False
    while i < len(raw):
        ch = raw[i]
        i += 1
        if escaped:
            value_chars.append("\\" + ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            complete = True
            break
        value_chars.append(ch)

    value = "".join(value_chars)
    if complete:
        try:
            return json.loads('"' + value + '"'), True
        except Exception:
            return value, True
    return value, False


def _streamable_spoken_prefix(text: str, language: str, complete: bool) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if complete:
        return text

    boundary_chars = ".。"
    last_boundary = -1
    for idx, char in enumerate(text):
        if char in boundary_chars:
            last_boundary = idx + 1
            break
    word_prefix = _word_count_streaming_prefix(text)
    if last_boundary > 0 and word_prefix:
        return text[: min(last_boundary, len(word_prefix))].strip()
    if last_boundary > 0:
        return text[:last_boundary].strip()
    return word_prefix


def _word_count_streaming_prefix(text: str) -> str:
    words = list(_STREAM_WORD_RE.finditer(text))
    if len(words) < _TTS_MAX_WORD_COUNT:
        return ""
    end = words[_TTS_MAX_WORD_COUNT - 1].end()
    lookahead = end
    while lookahead < len(text) and text[lookahead].isspace():
        lookahead += 1
    if lookahead < len(text) and text[lookahead] in _STREAM_TERMINAL_PUNCT:
        end = lookahead + 1
    return text[:end].strip()


_DUP_QUERY_WINDOW_S = 1.5


def _normalize_query_signature(text: str) -> str:
    normalized = (text or "").strip().lower()
    normalized = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _tts_splitter_profile(language: str | None) -> tuple[int, int, int, int]:
    if language == "zh":
        return 18, 24, 44, 14
    return max(FIRST_TTS_CHARS, 48), max(MIN_TTS_CHARS, 80), max(MAX_TTS_CHARS, 220), max(SHORT_SENTENCE_CHARS, 40)


def _build_sentence_splitter(language: str | None = None) -> LowLatencyVoiceChunker:
    first_chars, min_chars, max_chars, short_chars = _tts_splitter_profile(language)
    return LowLatencyVoiceChunker(
        min_chars=min_chars,
        first_chars=first_chars,
        max_chars=max_chars,
        short_chars=short_chars,
    )


def _remaining_spoken_suffix(final_text: str, already_streamed: str) -> str:
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


def _trim_spoken_to_focus(text: str, max_sentences: int = 5) -> str:
    if max_sentences <= 0:
        return ""
    sentences = [chunk.strip() for chunk in _SENTENCE_BOUNDARY_RE.split((text or "").strip()) if chunk.strip()]
    if not sentences:
        return ""
    return " ".join(sentences[:max_sentences])


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


def _limit_text_for_answer_voice(text: str, language: str | None = None) -> str:
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


def _limit_answer_details(details: object) -> dict:
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


def _build_tts_chunks(text: str, language: str | None = None) -> list[str]:
    splitter = _build_sentence_splitter(language)
    chunks: list[str] = []
    for chunk, _ in splitter.feed(text):
        if chunk:
            chunks.append(chunk.strip())
    for chunk, _ in splitter.flush():
        if chunk:
            chunks.append(chunk.strip())

    # Keep a deterministic fallback for languages where splitter is intentionally strict.
    if not chunks and text.strip():
        chunks = [text.strip()]

    return [chunk for chunk in chunks if chunk]


def _cap_tts_chunks_for_latency(chunks: object, spoken: str, language: str) -> list[str]:
    raw_chunks = _normalize_tts_chunks(chunks)
    combined = " ".join(raw_chunks).strip() or str(spoken or "").strip()
    capped = _trim_spoken_for_latency(combined, language)
    if not capped:
        return []
    return _build_tts_chunks(capped, language)[:_MAX_REALTIME_TTS_CHUNKS]


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
        "answer_kind": _normalize_answer_kind(answer_kind),
        "confidence": _coerce_confidence(confidence_value),
        "requires_follow_up": bool(follow_up_count),
        "citations": details.get("citations", []),
        "notes": details.get("notes", []),
    }


def _details_from_spoken(spoken: str, payload_details: object, follow_up_count: int) -> dict:
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


def _normalize_tagged_answer(text: str) -> str:
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


async def _postprocess_tagged_answer(tagged_answer: str, language: str) -> str:
    spoken, details, followups = extract_blocks(_normalize_tagged_answer(tagged_answer))
    details_voice = details.strip()
    if details_voice:
        spoken = await _normalize_spoken_for_tts(details_voice, language, trim_for_latency=False)
    else:
        spoken = await _normalize_spoken_for_tts(spoken, language)
    if not is_speakable_text(spoken):
        detail_lines = [line.strip(" -*\t") for line in details.splitlines() if line.strip()]
        spoken = sanitize_spoken_text(detail_lines[0] if detail_lines else details)
    if not is_speakable_text(spoken):
        spoken = {
            "ru": "Извините, я не могу корректно озвучить этот ответ. Повторите вопрос, пожалуйста.",
            "kk": "Кешіріңіз, бұл жауапты дұрыс дыбыстай алмадым. Сұрағыңызды қайталап айтыңыз.",
            "zh": "抱歉，我这次没有正确生成语音回答。请再说一遍您的问题。",
        }.get(language, "Sorry, I could not generate a clean spoken answer this time. Please ask again.")
    return rebuild_blocks(spoken, details, followups)


async def _normalize_spoken_for_tts(raw_spoken: str, language: str, *, trim_for_latency: bool = True) -> str:
    spoken = prepare_tts_text(raw_spoken, language, SONIOX_TTS_CONTEXT_FILE)
    if not spoken:
        spoken = remove_repeated_sentences(sanitize_spoken_text(raw_spoken))
    if trim_for_latency:
        return _trim_spoken_for_latency(spoken, language)
    return spoken


async def _postprocess_spoken_block(spoken_text: str, language: str) -> str:
    tagged = rebuild_blocks(spoken_text, "", "")
    spoken, _, _ = extract_blocks(await _postprocess_tagged_answer(tagged, language))
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


def _json_to_markdown_details(value: object) -> str:
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
    details = _json_to_markdown_details(payload.get("details"))
    spoken = "" if details else str(payload.get("spoken", "")).strip()
    followups = _json_to_markdown_followups(payload.get("followups") or payload.get("follow_up_questions"))
    if not followups:
        followups = "- See details for next steps."
    return rebuild_blocks(spoken=spoken, details=details or spoken, followups=followups)


async def _tagged_answer_with_full_details_voice(
    tagged_answer: str,
    contract_payload: dict | None,
    language: str,
) -> tuple[str, str]:
    tagged_answer = _normalize_tagged_answer(tagged_answer)
    _, details_block, followups_block = extract_blocks(tagged_answer)
    contract_details_voice = ""
    if contract_payload:
        contract_details_voice = _json_to_markdown_details(contract_payload.get("details"))
    details_voice = _limit_text_for_answer_voice(contract_details_voice or details_block, language)
    if not details_voice:
        spoken_block, _, _ = extract_blocks(tagged_answer)
        details_voice = _limit_text_for_answer_voice(spoken_block, language)
    spoken_voice = await _normalize_spoken_for_tts(details_voice, language, trim_for_latency=False)
    if not is_speakable_text(spoken_voice):
        spoken_voice = await _normalize_spoken_for_tts(
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


def _extract_answer_from_json(raw: str) -> str | None:
    payload = _extract_json_payload(raw) or _extract_json_from_wrapped(raw)
    if not isinstance(payload, dict):
        return None
    return _json_payload_to_tagged_answer(payload)


def _normalize_followup_questions(value: object) -> list[str]:
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


def _coerce_prompt_contract_payload(payload: dict, interrupted_input: bool, language: str) -> tuple[dict, list[str], list[str]]:
    if not isinstance(payload, dict):
        payload = {}
    spoken = str(payload.get("spoken", "")).strip()
    details_payload = payload.get("details")
    followups: list[str] = _normalize_followup_questions(payload.get("followups"))
    if not followups and "follow_up_questions" in payload:
        followups = _normalize_followup_questions(payload.get("follow_up_questions"))
    details = _enforce_prompt_details(details_payload, len(followups))
    details_voice = _json_to_markdown_details(details)
    if details_voice:
        spoken = prepare_tts_text(details_voice, language, SONIOX_TTS_CONTEXT_FILE)
    elif spoken:
        spoken = prepare_tts_text(spoken, language, SONIOX_TTS_CONTEXT_FILE)
    if not details["summary"]:
        details["summary"] = spoken[:220]
    if not spoken and not details["summary"]:
        details["summary"] = _NOISE_SPANISH.get(language, _NOISE_SPANISH["en"])
        spoken = ""

    if _coerce_confidence(details.get("confidence")) < 0.55 and details["summary"]:
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
    control = _build_control_payload(payload, interrupted_input)
    if control.get("handoff_greeting") and spoken:
        control["handoff_greeting"] = False

    tts_chunks: list[str] = []

    normalized = {
        "spoken": spoken,
        "details": details,
        "control": control,
        "tts_chunks": tts_chunks,
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


@dataclass
class TurnMetrics:
    started_at: float
    mode: str
    stt_started_at: float | None = None
    stt_done_at: float | None = None
    plan_done_at: float | None = None
    llm_done_at: float | None = None
    spoken_ready_at: float | None = None
    postprocess_done_at: float | None = None
    payload_done_at: float | None = None
    first_audio_at: float | None = None
    first_frame_at: float | None = None
    client_first_render_at: float | None = None
    done_at: float | None = None
    race_timings: dict[str, object] = field(default_factory=dict)

    def as_ms(self) -> dict[str, int]:
        def delta(point: float | None) -> int | None:
            if point is None:
                return None
            return int((point - self.started_at) * 1000)

        payload = {
            "stt": None if self.stt_started_at is None or self.stt_done_at is None else int((self.stt_done_at - self.stt_started_at) * 1000),
            "plan_retrieve": delta(self.plan_done_at),
            "llm_generate": None if self.plan_done_at is None or self.llm_done_at is None else int((self.llm_done_at - self.plan_done_at) * 1000),
            "spoken_ready": delta(self.spoken_ready_at),
            "spoken_postprocess": None if self.llm_done_at is None or self.postprocess_done_at is None else int((self.postprocess_done_at - self.llm_done_at) * 1000),
            "payload_ready": delta(self.payload_done_at),
            "first_audio": delta(self.first_audio_at),
            "first_frame": delta(self.first_frame_at),
            "client_first_render": delta(self.client_first_render_at),
            "total": delta(self.done_at),
        }
        for key, value in self.race_timings.items():
            if isinstance(value, (int, float, str)):
                payload[key] = value
        return {key: value for key, value in payload.items() if value is not None}


@dataclass
class ClientSession:
    websocket: WebSocket
    writer: WsWriter
    batch_stt: SonioxBatchSTT
    tts: ElevenTTS
    synctalk: SyncTalkClient
    session_id: str = field(default_factory=lambda: uuid4().hex)
    history: list[dict[str, str]] = field(default_factory=list)
    conversation_memory: dict | None = None
    realtime_stt: SonioxRealtimeSession | None = None
    realtime_stt_started_at: float | None = None
    realtime_stt_ready_at: float | None = None
    realtime_stt_audio_started_at: float | None = None
    stt_keepalive_task: asyncio.Task | None = None
    stt_prewarm_task: asyncio.Task | None = None
    tts_prewarm_task: asyncio.Task | None = None
    _stt_start_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    pipeline_task: asyncio.Task | None = None
    active_metrics: TurnMetrics | None = None
    active_turn_id: str | None = None
    ignore_audio_until: float = 0.0
    ignore_final_audio_until: float = 0.0
    barge_in_triggered: bool = False
    _last_query_signature: str = ""
    _last_query_at: float = 0.0
    _interrupt_signature: str = ""
    _interrupt_hits: int = 0
    _interrupt_started_at: float = 0.0
    _interrupt_last_at: float = 0.0

    def _reset_interrupt_state(self) -> None:
        self._interrupt_signature = ""
        self._interrupt_hits = 0
        self._interrupt_started_at = 0.0
        self._interrupt_last_at = 0.0

    def on_send(self, data: dict) -> None:
        metrics = self.active_metrics
        if metrics is None:
            return
        now = perf_counter()
        if data.get("type") == "audio_ready" and metrics.first_audio_at is None and int(data.get("chunk", 0)) == 0:
            metrics.first_audio_at = now
        elif data.get("type") == "frame" and metrics.first_frame_at is None and int(data.get("chunk", 0)) == 0:
            metrics.first_frame_at = now

    def on_client_first_render(self, turn_id: str | None, chunk: int | None) -> None:
        if chunk not in (None, 0):
            return
        if turn_id and self.active_turn_id and turn_id != self.active_turn_id:
            return
        if self.active_metrics is not None and self.active_metrics.client_first_render_at is None:
            self.active_metrics.client_first_render_at = perf_counter()

    async def _discard_closed_realtime_stt(self) -> None:
        session = self.realtime_stt
        if session is None or not session.closed:
            return
        if self.stt_keepalive_task is not None:
            self.stt_keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.stt_keepalive_task
            self.stt_keepalive_task = None
        if self.realtime_stt is session:
            self.realtime_stt = None
            self.realtime_stt_started_at = None
            self.realtime_stt_ready_at = None
            self.realtime_stt_audio_started_at = None
        with contextlib.suppress(Exception):
            await session.close()

    async def ensure_realtime_stt(self, *, status: bool = False) -> SonioxRealtimeSession | None:
        await self._discard_closed_realtime_stt()
        if self.realtime_stt is not None and not self.realtime_stt.closed:
            return self.realtime_stt
        async with self._stt_start_lock:
            await self._discard_closed_realtime_stt()
            if self.realtime_stt is not None and not self.realtime_stt.closed:
                return self.realtime_stt
            started = perf_counter()
            session = SonioxRealtimeSession(
                self.writer,
                self.batch_stt,
                on_meaningful_partial=self.on_meaningful_partial,
                on_final_utterance=self.on_realtime_final,
            )
            try:
                await session.start()
            except Exception:
                log.exception("Soniox realtime preconnect failed")
                log_event(log, "stt_realtime_preconnect_failed", session_id=self.session_id, level=logging.ERROR)
                with contextlib.suppress(Exception):
                    await session.close()
                return None
            self.realtime_stt = session
            self.realtime_stt_started_at = started
            self.realtime_stt_ready_at = perf_counter()
            self._start_stt_keepalive()
            ready_ms = int((self.realtime_stt_ready_at - started) * 1000)
            log_event(log, "stt_realtime_ready", session_id=self.session_id, latency_ms=ready_ms)
            if status:
                with contextlib.suppress(ClientClosedError):
                    await self.writer.send({"type": "stt_ready", "session_id": self.session_id, "ready_ms": ready_ms})
                with contextlib.suppress(ClientClosedError):
                    await self.writer.send({"type": "status", "text": "Transcribing..."})
            return session

    def _start_stt_keepalive(self) -> None:
        if self.stt_keepalive_task is not None and not self.stt_keepalive_task.done():
            return
        self.stt_keepalive_task = asyncio.create_task(self._stt_keepalive_loop())

    async def _stt_keepalive_loop(self) -> None:
        try:
            while not self.writer.closed:
                await asyncio.sleep(max(0.1, SONIOX_STT_KEEPALIVE_INTERVAL_S))
                session = self.realtime_stt
                if session is None or session.closed:
                    return
                await session.send_keepalive()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Soniox keepalive loop failed")

    async def _close_realtime_stt(self, expected: SonioxRealtimeSession | None = None, *, reason: str = "close") -> None:
        if self.stt_prewarm_task is not None and not self.stt_prewarm_task.done():
            self.stt_prewarm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.stt_prewarm_task
        self.stt_prewarm_task = None
        session = self.realtime_stt
        if expected is not None and session is not expected:
            session = expected
        if self.stt_keepalive_task is not None:
            self.stt_keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.stt_keepalive_task
            self.stt_keepalive_task = None
        if self.realtime_stt is session:
            self.realtime_stt = None
            self.realtime_stt_started_at = None
            self.realtime_stt_ready_at = None
            self.realtime_stt_audio_started_at = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
            log_event(log, "stt_realtime_closed", session_id=self.session_id, reason=reason)

    def prewarm_realtime_stt(self, *, force: bool = False) -> None:
        if not force and not SONIOX_STT_PRECONNECT:
            return
        if self.stt_prewarm_task is not None and not self.stt_prewarm_task.done():
            return
        self.stt_prewarm_task = asyncio.create_task(self.ensure_realtime_stt(status=False))
        self.stt_prewarm_task.add_done_callback(_log_background_task_error)

    def prewarm_realtime_tts(self) -> None:
        preconnect = getattr(self.tts, "preconnect", None)
        if preconnect is None:
            return
        if self.tts_prewarm_task is not None and not self.tts_prewarm_task.done():
            return
        self.tts_prewarm_task = asyncio.create_task(preconnect())
        self.tts_prewarm_task.add_done_callback(_log_background_task_error)

    async def _close_realtime_tts(self, *, reason: str = "close", recreate: bool = False, prewarm: bool = False) -> None:
        if self.tts_prewarm_task is not None and not self.tts_prewarm_task.done():
            self.tts_prewarm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.tts_prewarm_task
        self.tts_prewarm_task = None
        was_closed = bool(getattr(self.tts, "closed", False))
        with contextlib.suppress(Exception):
            await self.tts.close()
        if not was_closed:
            log_event(log, "tts_realtime_closed", session_id=self.session_id, reason=reason)
        if recreate:
            self.tts = ElevenTTS()
            if prewarm:
                self.prewarm_realtime_tts()

    def start_intro(self, intro_token: str | None = None) -> bool:
        if self.pipeline_task is not None and not self.pipeline_task.done():
            return False
        intro_blocks = _load_intro_blocks()
        if not intro_blocks:
            return False
        if not intro_token or _intro_token_seen(intro_token) or _intro_token_in_progress(intro_token):
            return False
        _mark_intro_token_played(intro_token)
        _mark_intro_token_in_progress(intro_token or "")
        self.pipeline_task = asyncio.create_task(self.run_intro(intro_blocks, intro_token=intro_token))
        self.pipeline_task.add_done_callback(_log_background_task_error)
        log_event(log, "intro_started", session_id=self.session_id)
        return True

    async def _ensure_intro_audio(self, block: IntroBlock) -> bytes:
        return await _ensure_intro_audio_file(self.tts, block)

    async def _play_intro_block(self, block: IntroBlock, index: int, turn_id: str) -> None:
        audio_wav = await self._ensure_intro_audio(block)
        audio_b64 = base64.b64encode(audio_wav).decode("ascii")

        async def send_audio_ready() -> None:
            await self.writer.send(
                {
                    "type": "audio_ready",
                    "data": audio_b64,
                    "chunk": index,
                    "source_chunk": index,
                    "frame_stride": 1,
                    "streaming": True,
                    "cached": True,
                    "turn_id": turn_id,
                }
            )

        cache_info = _intro_frame_cache_info(block)
        if cache_info is not None:
            frame_url, frame_count = cache_info
            await self.writer.send(
                {
                    "type": "frame_cache",
                    "url": frame_url,
                    "chunk": index,
                    "turn_id": turn_id,
                    "frame_count": frame_count,
                }
            )
            await send_audio_ready()
            return
        cached_frames = _load_intro_frames_from_cache(block)
        if cached_frames:
            headroom = min(_INTRO_FRAME_HEADROOM, len(cached_frames))
            for frame in cached_frames[:headroom]:
                await self.writer.send({"type": "frame", "data": frame, "chunk": index, "turn_id": turn_id})
            await send_audio_ready()
            for offset, frame in enumerate(cached_frames[headroom:], start=1):
                await self.writer.send({"type": "frame", "data": frame, "chunk": index, "turn_id": turn_id})
                if offset % _INTRO_CACHED_FRAME_BATCH == 0:
                    await asyncio.sleep(0)
            await self.writer.send({"type": "chunk_done", "chunk": index, "turn_id": turn_id})
            return

        audio_sent = False
        frame_count = 0
        frames: list[str] = []
        try:
            async for frame in self.synctalk.infer_stream(
                audio_wav,
                priority=0 if index == 0 else 1,
                chunk_idx=index,
            ):
                frames.append(frame)
                await self.writer.send({"type": "frame", "data": frame, "chunk": index, "turn_id": turn_id})
                frame_count += 1
                if not audio_sent and frame_count >= _INTRO_FRAME_HEADROOM:
                    await send_audio_ready()
                    audio_sent = True
        except ClientClosedError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("intro avatar generation failed: block=%s chunk=%s", block.key, index)

        if not audio_sent:
            await send_audio_ready()
            audio_sent = True
        if frame_count == 0:
            log.warning("intro avatar generation returned no frames: block=%s chunk=%s", block.key, index)
        else:
            _save_intro_frames_to_cache(block, frames)
        await self.writer.send({"type": "chunk_done", "chunk": index, "turn_id": turn_id})

    async def run_intro(self, intro_blocks: list[IntroBlock], intro_token: str | None = None) -> None:
        turn_id = uuid4().hex
        metrics = TurnMetrics(started_at=perf_counter(), mode="intro")
        full_text = "\n\n".join(block.text for block in intro_blocks).strip()
        intro_token_played = False
        self.active_metrics = metrics
        self.active_turn_id = turn_id
        self.writer.set_active_turn(turn_id)
        try:
            log_event(log, "pipeline_intro_start", session_id=self.session_id, request_id=turn_id)
            await self.writer.send({"type": "policy_state", "turn_id": turn_id, "answer_language": "en"})
            await self.writer.send({"type": "response_start", "turn_id": turn_id})
            await self.writer.send({"type": "status", "turn_id": turn_id, "text": "Starting introduction..."})
            await self.writer.send({"type": "response_chunk", "text": f"{full_text} ", "turn_id": turn_id})
            for index, block in enumerate(intro_blocks):
                await self.writer.send({"type": "status", "turn_id": turn_id, "text": f"Streaming cached intro block {index + 1}/{len(intro_blocks)}: {block.key}"})
                await self._play_intro_block(block, index, turn_id)
            details = _details_from_spoken(full_text, {}, 0)
            payload = {
                "answer_id": turn_id,
                "spoken": full_text,
                "details": details,
                "key_points": [],
                "follow_up_questions": [],
            }
            payload["answer_contract"] = payload["details"]
            payload["winner_source"] = "session_intro"
            payload["winner_confidence"] = "high"
            await self.writer.send({"type": "answer_payload", "turn_id": turn_id, **payload})
            metrics.done_at = perf_counter()
            await self.writer.send({"type": "done", "chunks": len(intro_blocks), "turn_id": turn_id, "latency_ms": metrics.as_ms()})
            log_event(log, "pipeline_intro_done", session_id=self.session_id, request_id=turn_id, latency_ms=metrics.as_ms().get("total"))
            if intro_token and not intro_token_played:
                _mark_intro_token_played(intro_token)
                intro_token_played = True
        except asyncio.CancelledError:
            raise
        except ClientClosedError:
            pass
        except Exception:
            log.exception("session intro failed")
            log_event(log, "pipeline_intro_failed", session_id=self.session_id, request_id=turn_id, level=logging.ERROR)
            with contextlib.suppress(ClientClosedError):
                await self.writer.send({"type": "error", "text": "Introduction failed", "turn_id": turn_id})
        finally:
            if intro_token and not intro_token_played:
                _clear_intro_token_in_progress(intro_token)
            self.pipeline_task = None
            self.active_metrics = None
            self.active_turn_id = None
            self.writer.clear_active_turn(turn_id)

    async def on_meaningful_partial(self, text: str) -> None:
        if self.barge_in_triggered:
            return
        if self.pipeline_task is None or self.pipeline_task.done():
            return
        if is_stop_command(text):
            log_event(log, "barge_in_stop", session_id=self.session_id, request_id=self.active_turn_id, partial=text[:80])
            self.barge_in_triggered = True
            await self.interrupt(send_event=True)
            self.ignore_audio_until = perf_counter() + 1.5
            self.ignore_final_audio_until = perf_counter() + _DUPLICATE_FINAL_AUDIO_IGNORE_S
            self._reset_interrupt_state()
            return
        now = perf_counter()
        if now - self._interrupt_last_at < _INTERRUPT_COOLDOWN_S and self._interrupt_last_at > 0:
            return

        signature = _signature_for_interruption(text)
        if not signature:
            self._reset_interrupt_state()
            return

        if signature == self._interrupt_signature and now - self._interrupt_started_at <= _PARTIAL_INTERRUPT_WINDOW_S:
            self._interrupt_hits += 1
        else:
            self._interrupt_signature = signature
            self._interrupt_started_at = now
            self._interrupt_hits = 1

        self._interrupt_last_at = now

        if self._interrupt_hits < _PARTIAL_INTERRUPT_HITS:
            return

        log_event(log, "barge_in_partial", session_id=self.session_id, request_id=self.active_turn_id, partial=text[:80])
        self.barge_in_triggered = True
        await self.interrupt(send_event=True)
        self._reset_interrupt_state()

    async def close(self) -> None:
        await self._close_realtime_stt(reason="session_close")
        if self.pipeline_task is not None:
            self.pipeline_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.pipeline_task
        await self.batch_stt.close()
        await self._close_realtime_tts(reason="session_close")
        await self.synctalk.close()

    async def interrupt(self, send_event: bool) -> None:
        self.writer.clear_active_turn()
        if self.pipeline_task is not None and not self.pipeline_task.done():
            log_event(log, "pipeline_cancel_requested", session_id=self.session_id, request_id=self.active_turn_id, send_event=send_event)
            self.pipeline_task.cancel()
            try:
                await self.pipeline_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("pipeline cancellation failed")
            if send_event:
                await self.writer.send({"type": "interrupted", "session_id": self.session_id})
        self.pipeline_task = None

    async def reset(self, *, reopen_transports: bool = True, reason: str = "reset") -> None:
        self.history.clear()
        self.barge_in_triggered = False
        self.ignore_audio_until = 0.0
        self.ignore_final_audio_until = 0.0
        self.realtime_stt_started_at = None
        self.realtime_stt_ready_at = None
        self.realtime_stt_audio_started_at = None
        self._last_query_signature = ""
        self._last_query_at = 0.0
        self._reset_interrupt_state()
        await self.interrupt(send_event=False)
        await self._close_realtime_stt(reason=reason)
        await self._close_realtime_tts(reason=reason, recreate=reopen_transports, prewarm=reopen_transports)
        if reopen_transports:
            self.prewarm_realtime_stt(force=True)
        log_event(log, "session_reset", session_id=self.session_id)

    async def handle_message(self, payload: dict) -> None:
        msg_type = payload.get("type")
        log_event(log, "ws_message", session_id=self.session_id, request_id=self.active_turn_id, message_type=msg_type)
        if msg_type == "audio_chunk":
            await self.handle_audio_chunk(base64.b64decode(payload["data"]))
        elif msg_type == "audio":
            await self.handle_audio(base64.b64decode(payload["data"]))
        elif msg_type == "prepare_stt":
            await self.ensure_realtime_stt(status=True)
        elif msg_type == "close_stt":
            log_event(log, "stt_close_ignored_persistent", session_id=self.session_id)
        elif msg_type == "text":
            await self.handle_text(payload.get("text", ""))
        elif msg_type == "interrupt":
            await self.interrupt(send_event=False)
        elif msg_type == "reset":
            await self.reset()
        elif msg_type == "client_first_render":
            chunk = payload.get("chunk")
            self.on_client_first_render(
                payload.get("turn_id"),
                int(chunk) if isinstance(chunk, int) or str(chunk).isdigit() else None,
            )
        elif msg_type == "client_log":
            level_name = str(payload.get("level") or "info").lower()
            level = logging.ERROR if level_name == "error" else logging.WARNING if level_name == "warning" else logging.INFO
            log_event(
                logging.getLogger("backend.websocket.client"),
                "client_log",
                session_id=self.session_id,
                request_id=str(payload.get("turn_id") or self.active_turn_id or ""),
                level=level,
                source=str(payload.get("source") or "frontend"),
                message=str(payload.get("message") or ""),
                detail=json.dumps(payload.get("detail"), ensure_ascii=False, default=str)[:1200],
            )

    async def handle_audio_chunk(self, chunk: bytes) -> None:
        if perf_counter() < self.ignore_audio_until:
            return
        if not looks_like_pcm16_chunk(chunk):
            self.ignore_audio_until = max(self.ignore_audio_until, perf_counter() + 0.4)
            return
        try:
            if self.realtime_stt is None or self.realtime_stt.closed:
                self.barge_in_triggered = False
            session = await self.ensure_realtime_stt(status=True)
            if session is None:
                with contextlib.suppress(ClientClosedError):
                    await self.writer.send({"type": "transcript_empty"})
                return
            if not session.has_audio:
                self.realtime_stt_audio_started_at = perf_counter()
                log_event(log, "stt_audio_started", session_id=self.session_id)
            await session.send_audio(chunk)
            if session.closed:
                await self._close_realtime_stt(session, reason="provider_closed")
                with contextlib.suppress(ClientClosedError):
                    await self.writer.send({"type": "transcript_empty"})
        except ClientClosedError:
            await self._close_realtime_stt(reason="client_closed")
            raise
        except Exception:
            log.exception("realtime stt failed")
            await self._close_realtime_stt(reason="send_error")
            with contextlib.suppress(ClientClosedError):
                await self.writer.send({"type": "transcript_empty"})

    async def on_realtime_final(self, text: str, language: str) -> None:
        started = self.realtime_stt_audio_started_at or self.realtime_stt_started_at or perf_counter()
        metrics = TurnMetrics(
            started_at=started,
            mode="audio",
            stt_started_at=started,
            stt_done_at=perf_counter(),
        )
        active_session = self.realtime_stt
        self.ignore_audio_until = perf_counter() + 1.0
        self.ignore_final_audio_until = perf_counter() + _DUPLICATE_FINAL_AUDIO_IGNORE_S
        if active_session is not None and not active_session.closed:
            active_session.reset_utterance_state()
            self.realtime_stt_audio_started_at = None
            log_event(log, "stt_realtime_reused_after_final", session_id=self.session_id)
        log_event(log, "stt_final", session_id=self.session_id, latency_ms=(metrics.stt_done_at - started) * 1000, language=language, chars=len(text))
        await self.process_final_transcript(text, language, metrics)

    async def handle_audio(self, audio_bytes: bytes) -> None:
        if perf_counter() < self.ignore_final_audio_until:
            log.info("dropping duplicate client final audio after Soniox endpoint")
            return
        if self.realtime_stt is not None and self.realtime_stt.closed:
            await self._close_realtime_stt(self.realtime_stt, reason="provider_closed")
        metric_started = (
            self.realtime_stt_audio_started_at
            if self.realtime_stt is not None and self.realtime_stt.has_audio and self.realtime_stt_audio_started_at is not None
            else perf_counter()
        )
        metrics = TurnMetrics(started_at=metric_started, mode="audio", stt_started_at=metric_started)
        try:
            if self.realtime_stt is not None and self.realtime_stt.has_audio and not self.realtime_stt.closed:
                active_session = self.realtime_stt
                if not active_session.claim_finalization():
                    log.info("dropping duplicate client final audio while Soniox endpoint is in flight")
                    return
                text, language = await active_session.wait_committed_with_keepalive(
                    SONIOX_STT_ENDPOINT_WAIT_S,
                    interval_s=0.5,
                )
                if not text:
                    await active_session.send_silence(200)
                    text, language = await active_session.finalize(audio_bytes, allow_fallback=True, close_after=False)
                if not active_session.closed:
                    active_session.reset_utterance_state()
                    self.realtime_stt_audio_started_at = None
                    log_event(log, "stt_realtime_reused_after_final_audio", session_id=self.session_id)
                self.ignore_final_audio_until = perf_counter() + _DUPLICATE_FINAL_AUDIO_IGNORE_S
            else:
                text, language = await self.batch_stt.transcribe(audio_bytes)
        except Exception:
            log.exception("audio transcription failed")
            await self._close_realtime_stt(reason="transcription_error")
            self.prewarm_realtime_stt(force=True)
            if self.pipeline_task is not None and not self.pipeline_task.done():
                log.info("suppressing late audio transcription failure during active response")
                self.ignore_final_audio_until = perf_counter() + _DUPLICATE_FINAL_AUDIO_IGNORE_S
                return
            await self.writer.send({"type": "transcript_empty"})
            return
        metrics.stt_done_at = perf_counter()
        log_event(
            log,
            "stt_final",
            session_id=self.session_id,
            latency_ms=(metrics.stt_done_at - metric_started) * 1000,
            language=language,
            chars=len(text),
        )
        await self.process_final_transcript(text, language, metrics)

    async def process_final_transcript(self, text: str, language: str, metrics: TurnMetrics) -> None:
        provider_language = language
        provider_language_norm = supported_lang_or_none(language)
        text = normalize_transcript_abbreviations(dedupe_repeated_transcript(text), provider_language_norm)
        detected_language = provider_language_norm or detect_supported_text_language(text)
        if not text or not transcript_has_meaningful_speech(text):
            log_event(log, "transcript_rejected", session_id=self.session_id, reason="empty_or_not_speech", provider_lang=provider_language, text=text[:120])
            await self.writer.send({"type": "transcript_empty"})
            return

        query_signature = _normalize_query_signature(text)
        if query_signature and query_signature == self._last_query_signature and (perf_counter() - self._last_query_at) < _DUP_QUERY_WINDOW_S:
            log_event(log, "transcript_rejected", session_id=self.session_id, reason="duplicate", text=text[:120])
            await self.writer.send({"type": "transcript_empty", "text": "duplicate query ignored"})
            return
        if detected_language is None:
            fallback_lang = detect_text_language(text)
            log_event(log, "transcript_rejected", session_id=self.session_id, reason="unsupported_language", text=text[:120])
            await self.writer.send({"type": "error", "text": UNSUPPORTED_LANGUAGE_MESSAGE[fallback_lang]})
            return
        language = normalize_lang(detected_language)
        if is_stop_command(text):
            await self.interrupt(send_event=False)
            self.ignore_audio_until = perf_counter() + 1.5
            await self.writer.send({"type": "stop_confirmed"})
            return
        pipeline_active = self.pipeline_task is not None and not self.pipeline_task.done()
        turn_candidate = _is_final_turn_candidate(text, language, require_query_signal=pipeline_active)
        self._reset_interrupt_state()
        if pipeline_active and not turn_candidate:
            log.info("ignored non-query transcript during active response: %r", text[:100])
            self.ignore_audio_until = perf_counter() + 0.8
            return
        if not turn_candidate:
            log.info(
                "transcript rejected: non_query text=%r language=%s provider_lang=%r",
                text[:120],
                language,
                provider_language,
            )
            await self.writer.send({"type": "transcript_empty"})
            return
        self._last_query_signature = query_signature
        self._last_query_at = perf_counter()
        if pipeline_active:
            await self.interrupt(send_event=True)
        self._reset_interrupt_state()
        log_event(log, "transcript_final_accepted", session_id=self.session_id, language=language, interrupted=pipeline_active, chars=len(text))
        await self.writer.send({"type": "transcript", "session_id": self.session_id, "text": text})
        self.pipeline_task = asyncio.create_task(self.run_query(text, language, metrics, interrupted_input=pipeline_active))

    async def handle_text(self, text: str) -> None:
        raw_text = text.strip()
        detected_language = detect_supported_text_language(raw_text)
        text = normalize_transcript_abbreviations(raw_text, detected_language)
        if not text:
            return
        detected_language = detected_language or detect_supported_text_language(text)
        if detected_language is None:
            fallback_lang = detect_text_language(text)
            await self.writer.send({"type": "error", "text": UNSUPPORTED_LANGUAGE_MESSAGE[fallback_lang]})
            return

        query_signature = _normalize_query_signature(text)
        if query_signature and query_signature == self._last_query_signature and (perf_counter() - self._last_query_at) < _DUP_QUERY_WINDOW_S:
            log_event(log, "text_query_rejected", session_id=self.session_id, reason="duplicate", text=text[:120])
            return

        interrupted_input = self.pipeline_task is not None and not self.pipeline_task.done()
        self._reset_interrupt_state()
        if interrupted_input:
            await self.interrupt(send_event=True)

        self._last_query_signature = query_signature
        self._last_query_at = perf_counter()
        log_event(log, "text_query_accepted", session_id=self.session_id, language=detected_language, interrupted=interrupted_input, chars=len(text))
        self.pipeline_task = asyncio.create_task(
            self.run_query(text, detected_language, TurnMetrics(started_at=perf_counter(), mode="text"), interrupted_input=interrupted_input)
        )

    async def run_query(self, query: str, language: str, metrics: TurnMetrics, interrupted_input: bool = False) -> None:
        turn_id = uuid4().hex
        stream: ResponseStream | None = None
        self.active_metrics = metrics
        self.active_turn_id = turn_id
        self.writer.set_active_turn(turn_id)
        raw_answer = ""
        json_payload: dict[str, object] | None = None
        contract_payload: dict | None = None
        contract_followups: list[str] = []
        tagged_answer = ""
        race_result: AnswerRaceResult | None = None
        plan = SimpleNamespace(answer_language=language)
        chunks: list[dict] = []
        policy_language: str | None = None
        live_voice_text = ""

        async def ensure_response_stream(
            plan_update: object | None = None,
            chunks_update: list[dict] | None = None,
        ) -> ResponseStream:
            nonlocal stream, plan, chunks, language, policy_language
            if plan_update is not None:
                plan = plan_update
                language = normalize_lang(getattr(plan, "answer_language", language) or language)
            if chunks_update is not None:
                chunks = chunks_update
            if policy_language != language:
                await self.writer.send({"type": "policy_state", "turn_id": turn_id, "answer_language": language})
                policy_language = language
            if stream is None:
                stream = ResponseStream(
                    self.writer,
                    self.tts,
                    self.synctalk,
                    splitter=_build_sentence_splitter(language),
                    plan=plan,
                    turn_started_at=metrics.started_at,
                    turn_id=turn_id,
                    query_text=query,
                    chunks=chunks,
                )
            else:
                stream.update_context(plan=plan, chunks=chunks)
            return stream

        async def on_gemini_context_ready(plan_update: object, chunks_update: list[dict]) -> None:
            if metrics.plan_done_at is None:
                metrics.plan_done_at = perf_counter()
            await ensure_response_stream(plan_update, chunks_update)

        async def on_gemini_voice_delta(text_delta: str, emitted_chars: int, complete: bool) -> None:
            del emitted_chars, complete
            nonlocal live_voice_text
            if not text_delta.strip():
                return
            response_stream = await ensure_response_stream()
            live_voice_text += text_delta
            await response_stream.feed(text_delta)

        try:
            log_event(log, "pipeline_start", session_id=self.session_id, request_id=turn_id, mode=metrics.mode, language=language, interrupted=interrupted_input, query=query[:160])
            self.history.append({"role": "user", "content": query})
            self.history[:] = self.history[-(MAX_HISTORY_TURNS * 2):]
            history_before_current = self.history[:-1]
            await self.writer.send({"type": "response_start", "turn_id": turn_id})
            log_event(log, "llm_start", session_id=self.session_id, request_id=turn_id)
            direct_reply = _prebuilt_chitchat_answer(query, language) or smalltalk_reply(query, language)
            if direct_reply:
                metrics.plan_done_at = perf_counter()
                fast_hit = None
                stream = await ensure_response_stream()
                tagged_answer = wrap_answer_for_voice_and_chat(direct_reply, include_details=False)
                metrics.llm_done_at = perf_counter()
                log_event(log, "llm_direct_reply", session_id=self.session_id, request_id=turn_id, latency_ms=(metrics.llm_done_at - metrics.started_at) * 1000)
            else:
                await self.writer.send({"type": "status", "turn_id": turn_id, "text": "Racing answer sources..."})
                race_result = await run_answer_race(
                    query,
                    language,
                    history_before_current,
                    self.conversation_memory,
                    on_gemini_context_ready=on_gemini_context_ready,
                    on_gemini_voice_delta=on_gemini_voice_delta,
                )
                metrics.race_timings.update(race_result.timings)
                winner = race_result.winner
                plan = winner.plan or SimpleNamespace(answer_language=language)
                chunks = winner.chunks
                fast_hit = None
                if metrics.plan_done_at is None:
                    metrics.plan_done_at = perf_counter()
                language = normalize_lang(getattr(plan, "answer_language", language) or language)
                stream = await ensure_response_stream(plan, chunks)
                if winner.raw_answer:
                    raw_answer = winner.raw_answer
                else:
                    tagged_answer = winner.tagged_answer
                metrics.llm_done_at = perf_counter()
                log_event(log, "llm_done", session_id=self.session_id, request_id=turn_id, latency_ms=(metrics.llm_done_at - (metrics.plan_done_at or metrics.started_at)) * 1000, source=getattr(race_result.winner, "source", "unknown") if race_result else "unknown")

            json_payload = _extract_json_any(raw_answer)
            if isinstance(json_payload, dict):
                contract_payload, _, contract_followups = _coerce_prompt_contract_payload(json_payload, interrupted_input, language)

            if metrics.llm_done_at is None:
                metrics.llm_done_at = perf_counter()

            if not direct_reply and not tagged_answer:
                tagged_from_json = _json_payload_to_tagged_answer(json_payload) if isinstance(json_payload, dict) else None
                if not tagged_from_json:
                    tagged_from_json = _extract_answer_from_json(raw_answer)
                if not tagged_from_json:
                    tagged_from_json = _normalize_tagged_answer(raw_answer)
                tagged_answer = tagged_from_json

            tagged_answer, full_details_voice = await _tagged_answer_with_full_details_voice(
                tagged_answer,
                contract_payload,
                language,
            )
            metrics.spoken_ready_at = perf_counter()
            log_event(log, "spoken_ready", session_id=self.session_id, request_id=turn_id, latency_ms=(metrics.spoken_ready_at - metrics.started_at) * 1000)

            metrics.postprocess_done_at = perf_counter()
            if not stream.spoken_text.strip():
                await stream.feed(_normalize_tagged_answer(tagged_answer))
                await stream.flush()
            payload = stream.build_answer_payload()
            if contract_payload is not None:
                payload["control"] = contract_payload.get("control", _build_control_payload({}, interrupted_input))
                payload["details"] = contract_payload.get("details", payload.get("details", {}))
                payload["tts_chunks"] = _normalize_tts_chunks(contract_payload.get("tts_chunks"))
                payload["follow_up_questions"] = _normalize_followup_questions(contract_payload.get("follow_up_questions"))
                payload["answer_contract"] = contract_payload.get("answer_contract", {})
                contract_details = contract_payload.get("details", {})
                if isinstance(contract_details, dict):
                    contract_points = [str(item).strip() for item in contract_details.get("points", []) if str(item).strip()]
                    if contract_points:
                        payload["key_points"] = [
                            {
                                "id": f"point-{idx + 1}",
                                "label": f"Point {idx + 1}",
                                "preview": point,
                                "section_index": idx,
                            }
                            for idx, point in enumerate(contract_points[:4])
                        ]
                if contract_followups:
                    payload["follow_up_questions"] = contract_followups[:3]
                if not payload.get("tts_chunks"):
                    payload["tts_chunks"] = _build_tts_chunks(payload.get("spoken", ""), language)
            else:
                payload["control"] = _build_control_payload({}, interrupted_input)
                payload.setdefault("tts_chunks", _build_tts_chunks(payload.get("spoken", ""), language))
                payload["spoken"] = await _normalize_spoken_for_tts(payload.get("spoken", ""), language, trim_for_latency=False)
                if not payload.get("tts_chunks"):
                    payload["tts_chunks"] = _build_tts_chunks(payload.get("spoken", ""), language)
                payload.setdefault("answer_contract", _enforce_prompt_details(payload.get("details"), len(payload.get("follow_up_questions", []))))
            if not payload.get("follow_up_questions"):
                payload["follow_up_questions"] = []
            details_payload = payload.get("details")
            payload["details"] = _limit_answer_details(
                _enforce_prompt_details(details_payload, len(payload.get("follow_up_questions", [])))
            )
            if direct_reply:
                payload["spoken"] = await _normalize_spoken_for_tts(
                    direct_reply,
                    language,
                    trim_for_latency=False,
                )
                payload["details"] = _details_from_spoken(
                    str(payload.get("spoken", "")),
                    payload.get("details"),
                    len(payload.get("follow_up_questions", [])),
                )
                log_event(
                    log,
                    "direct_reply_voice_finalized",
                    session_id=self.session_id,
                    request_id=turn_id,
                    chars=len(str(payload.get("spoken", ""))),
                )
            else:
                details_voice = _json_to_markdown_details(payload.get("details")).strip()
                if not details_voice and full_details_voice:
                    details_voice = full_details_voice
                details_voice = _limit_text_for_answer_voice(details_voice, language)
                payload["spoken"] = await _normalize_spoken_for_tts(
                    details_voice or _limit_text_for_answer_voice(str(payload.get("spoken", "")), language),
                    language,
                    trim_for_latency=False,
                )
                remaining_voice = _remaining_spoken_suffix(str(payload.get("spoken", "")), live_voice_text or stream.spoken_text)
                if remaining_voice:
                    await stream.feed(remaining_voice)
            await stream.flush()
            payload["tts_chunks"] = [
                chunk
                for chunk in (
                    _normalize_tts_chunk_for_language(str(item), language)
                    for item in _build_tts_chunks(str(payload.get("spoken", "")), language)
                )
                if chunk
            ]
            if not payload["tts_chunks"]:
                payload["tts_chunks"] = _build_tts_chunks(payload.get("spoken", ""), language)
            if race_result is not None:
                winner = race_result.winner
                details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
                details["confidence"] = winner.score
                details["answer_kind"] = "fallback" if winner.fallback else details.get("answer_kind", "direct")
                details["citations"] = winner.citations or details.get("citations", [])
                if winner.fallback:
                    details["requires_follow_up"] = True
                    details["fallback_site"] = "aifc.kz"
                    details["knowledge_gap_query"] = query
                    payload["follow_up_questions"] = []
                payload["details"] = details
                payload["winner_source"] = winner.source
                payload["winner_confidence"] = winner.confidence
                if race_result.timings.get("selected_rag_tool"):
                    payload["selected_rag_tool"] = race_result.timings["selected_rag_tool"]
            enforced_details = _limit_answer_details(
                _enforce_prompt_details(
                    payload.get("details"),
                    len(payload.get("follow_up_questions", [])),
                )
            )
            if enforced_details.get("summary") or enforced_details.get("points") or enforced_details.get("sections"):
                payload["details"] = enforced_details
            else:
                payload["details"] = _details_from_spoken(
                    str(payload.get("spoken", "")),
                    payload.get("details"),
                    len(payload.get("follow_up_questions", [])),
                )
            payload["answer_contract"] = payload["details"]
            metrics.payload_done_at = perf_counter()
            await self.writer.send({"type": "answer_payload", "turn_id": turn_id, **payload})
            log_event(log, "answer_payload_sent", session_id=self.session_id, request_id=turn_id, latency_ms=(metrics.payload_done_at - metrics.started_at) * 1000)
            if not stream.full_reply:
                await self.writer.send({"type": "error", "text": "Empty response"})
                return
            self.history.append({"role": "assistant", "content": stream.full_reply})
            self.history[:] = self.history[-(MAX_HISTORY_TURNS * 2):]
            self.conversation_memory = await asyncio.to_thread(
                update_conversation_memory,
                self.conversation_memory,
                query,
                (payload.get("details") or {}).get("summary", ""),
                chunks,
            )
            await self.writer.send({"type": "status", "text": "Generating speech...", "turn_id": turn_id})
            await stream.wait_all()
            metrics.done_at = perf_counter()
            latency_ms = metrics.as_ms()
            log_event(log, "pipeline_done", session_id=self.session_id, request_id=turn_id, latency_ms=latency_ms.get("total"), metrics=json.dumps(latency_ms, default=str))
            await self.writer.send({"type": "done", "chunks": stream.chunk_count, "turn_id": turn_id, "latency_ms": latency_ms})
        except asyncio.CancelledError:
            if stream is not None:
                stream.cancel_all()
            raise
        except ClientClosedError:
            if stream is not None:
                stream.cancel_all()
        except Exception:
            log.exception("query pipeline failed")
            log_event(log, "pipeline_failed", session_id=self.session_id, request_id=turn_id, level=logging.ERROR)
            if stream is not None:
                stream.cancel_all()
            await self.writer.send({"type": "error", "text": "Response generation failed", "turn_id": turn_id})
        finally:
            self.pipeline_task = None
            self.active_metrics = None
            self.active_turn_id = None
            self.writer.clear_active_turn(turn_id)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/admin/cache/clear")
async def clear_runtime_caches() -> dict[str, object]:
    cleared = await clear_answer_caches()
    log_event(log, "runtime_caches_cleared", caches=json.dumps(cleared, sort_keys=True))
    return {"status": "ok", "cleared": cleared}


@app.get("/intro-cache/{avatar}/{block_key}")
async def intro_cache_frames(avatar: str, block_key: str, start: int = 0, limit: int = 0) -> JSONResponse:
    safe_avatar = _safe_cache_key(avatar)
    safe_key = _canonical_intro_key(block_key)
    if safe_key is None:
        raise HTTPException(status_code=404, detail="intro block not found")
    block = next((item for item in _load_intro_blocks() if item.key == safe_key), None)
    if block is None:
        raise HTTPException(status_code=404, detail="intro block not found")
    start = max(0, start)
    limit = max(0, min(limit, 500))
    range_path = _intro_frame_range_path(safe_avatar, safe_key, start, limit)
    if range_path.exists():
        try:
            cached_payload = json.loads(range_path.read_text(encoding="utf-8"))
            if (
                cached_payload.get("signature") == _intro_frame_signature(block)
                and cached_payload.get("avatar") == safe_avatar == INTRO_AVATAR_CACHE_KEY
                and isinstance(cached_payload.get("frames"), list)
            ):
                return JSONResponse(
                    cached_payload,
                    headers={"Cache-Control": "public, max-age=86400, immutable"},
                )
        except Exception:
            pass
        with contextlib.suppress(Exception):
            range_path.unlink()
    path = INTRO_AUDIO_CACHE_DIR / "frames" / safe_avatar / f"{safe_key}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="intro cache not found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail="intro cache unreadable") from exc
    if payload.get("signature") != _intro_frame_signature(block):
        raise HTTPException(status_code=409, detail="intro cache signature mismatch")
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        raise HTTPException(status_code=404, detail="intro cache empty")
    total = len(frames)
    start = max(0, min(start, total))
    end = total if limit <= 0 else min(total, start + limit)
    selected_frames = frames[start:end]
    response_payload = {
        "signature": payload.get("signature"),
        "key": safe_key,
        "avatar": safe_avatar,
        "start": start,
        "end": end,
        "total": total,
        "has_more": end < total,
        "frames": selected_frames,
    }
    if limit > 0:
        with contextlib.suppress(Exception):
            range_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = range_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(response_payload, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(range_path)
    return JSONResponse(response_payload, headers={"Cache-Control": "public, max-age=86400, immutable"})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    intro_token = (websocket.query_params.get("intro_token") or "").strip()
    writer = WsWriter(websocket)
    session = ClientSession(
        websocket=websocket,
        writer=writer,
        batch_stt=SonioxBatchSTT(),
        tts=ElevenTTS(),
        synctalk=SyncTalkClient(),
    )
    writer._on_send = session.on_send
    log_event(log, "websocket_connected", session_id=session.session_id)
    await writer.send({"type": "session_state", "session_id": session.session_id, "state": "connected"})
    intro_started = False
    intro_started = session.start_intro(intro_token or None)
    session.prewarm_realtime_stt(force=True)
    session.prewarm_realtime_tts()
    try:
        while True:
            payload = json.loads(await websocket.receive_text())
            await session.handle_message(payload)
    except WebSocketDisconnect:
        log.info("ws disconnected")
    except Exception:
        log.exception("ws session error")
    finally:
        try:
            await session.reset(reopen_transports=False, reason="disconnect")
        except Exception:
            log.exception("session reset failed")
        await session.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app.main:app", host=APP_HOST, port=APP_PORT, reload=False)
