from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from backend.settings import (
    INTRO_AVATAR_CACHE_KEY,
    INTRO_AUDIO_CACHE_DIR,
    INTRO_AUDIO_CACHE_PREBUILD,
    ROOT,
    SONIOX_TTS_INTRO_LANGUAGE,
    SONIOX_TTS_INTRO_VOICE,
)
from backend.tts import ElevenTTS

log = logging.getLogger(__name__)

INTRO_FRAME_HEADROOM = 8
INTRO_CACHED_FRAME_BATCH = 8

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


def canonical_intro_key(raw_key: object) -> str | None:
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


def load_intro_blocks() -> list[IntroBlock]:
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
            key = canonical_intro_key(raw_key)
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
                key = canonical_intro_key(value.get("key") or value.get("language") or value.get("name"))
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


def _intro_audio_path(block: IntroBlock) -> Path:
    return INTRO_AUDIO_CACHE_DIR / _INTRO_BLOCK_FILENAMES[block.key]


def mark_intro_token_played(token: str) -> None:
    if not token or token in _INTRO_PLAYED_TOKENS:
        return
    _INTRO_IN_PROGRESS_TOKENS.discard(token)
    _INTRO_PLAYED_TOKENS.add(token)
    _INTRO_PLAYED_TOKEN_ORDER.append(token)
    while len(_INTRO_PLAYED_TOKEN_ORDER) > _INTRO_PLAYED_TOKEN_LIMIT:
        old = _INTRO_PLAYED_TOKEN_ORDER.pop(0)
        _INTRO_PLAYED_TOKENS.discard(old)


def mark_intro_token_in_progress(token: str) -> None:
    if token and token not in _INTRO_PLAYED_TOKENS:
        _INTRO_IN_PROGRESS_TOKENS.add(token)


def clear_intro_token_in_progress(token: str | None) -> None:
    if token:
        _INTRO_IN_PROGRESS_TOKENS.discard(token)


def intro_token_seen(token: str) -> bool:
    return bool(token and token in _INTRO_PLAYED_TOKENS)


def intro_token_in_progress(token: str | None) -> bool:
    return bool(token and token in _INTRO_IN_PROGRESS_TOKENS)


def _intro_audio_meta_path(block: IntroBlock) -> Path:
    return _intro_audio_path(block).with_suffix(".json")


def safe_cache_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "default").strip("-") or "default"


def _intro_frame_path(block: IntroBlock) -> Path:
    return INTRO_AUDIO_CACHE_DIR / "frames" / safe_cache_key(INTRO_AVATAR_CACHE_KEY) / f"{block.key}.json"


def _intro_frame_cache_url(block: IntroBlock) -> str:
    return f"/intro-cache/{safe_cache_key(INTRO_AVATAR_CACHE_KEY)}/{block.key}"


def intro_frame_range_path(avatar: str, block_key: str, start: int, limit: int) -> Path:
    return INTRO_AUDIO_CACHE_DIR / "frame_ranges" / safe_cache_key(avatar) / block_key / f"{start}_{limit}.json"


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


def intro_frame_signature(block: IntroBlock) -> str:
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


def load_intro_frames_from_cache(block: IntroBlock) -> list[str] | None:
    path = _intro_frame_path(block)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("signature") != intro_frame_signature(block):
        return None
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        return None
    return [str(frame) for frame in frames if frame]


def intro_frame_cache_info(block: IntroBlock) -> tuple[str, int] | None:
    path = _intro_frame_path(block)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("signature") != intro_frame_signature(block):
        return None
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        return None
    return _intro_frame_cache_url(block), len(frames)


def save_intro_frames_to_cache(block: IntroBlock, frames: list[str]) -> None:
    if not frames:
        return
    path = _intro_frame_path(block)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "signature": intro_frame_signature(block),
        "key": block.key,
        "avatar": INTRO_AVATAR_CACHE_KEY,
        "frames": frames,
    }
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


async def ensure_intro_audio_file(tts: ElevenTTS, block: IntroBlock) -> bytes:
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


async def prebuild_intro_audio_cache() -> None:
    if not INTRO_AUDIO_CACHE_PREBUILD:
        log.info("intro audio cache prebuild skipped")
        return
    blocks = load_intro_blocks()
    if not blocks:
        return
    missing = [block for block in blocks if not _intro_audio_cache_is_valid(block)]
    if not missing:
        log.info("intro audio cache ready: %s", INTRO_AUDIO_CACHE_DIR)
        return
    tts = ElevenTTS()
    try:
        for block in missing:
            await ensure_intro_audio_file(tts, block)
        log.info("intro audio cache generated: %s", INTRO_AUDIO_CACHE_DIR)
    except Exception:
        log.exception("intro audio cache prebuild failed; missing files will be generated on first session")
    finally:
        with contextlib.suppress(Exception):
            await tts.close()
