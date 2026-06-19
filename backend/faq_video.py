from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path

from .settings import INTRO_AVATAR_CACHE_KEY, ROOT, SONIOX_TTS_VOICE
from .intro import encode_frames_to_mp4, file_cache_version, safe_cache_key
from .media.tts import SonioxRealtimeTTS
from .media.synctalk import SyncTalkClient

log = logging.getLogger(__name__)

# FAQ answer video cache. Mirrors the intro MP4 mechanism (intro.py): an FAQ answer is a
# static, identical-every-time utterance, so we render it through SyncTalk ONCE offline
# (scripts/build_faq_videos.py), store a hardware-decodable MP4, and serve that on a
# confident FAQ fast-path hit instead of re-running TTS -> SyncTalk live (zero GPU at
# serve time, low bandwidth, instant). A cache miss falls back to the live render path.
#
# The cache key is the exact final ``spoken`` string the live turn path would render
# (after candidate_from_answer -> extract_json_any -> coerce -> normalize_spoken_for_tts),
# plus avatar + voice + language. Because both the offline builder and the serve-time
# lookup key off that identical string, a hit guarantees the MP4 says exactly what we
# would otherwise have rendered live.

FAQ_VIDEO_CACHE_VERSION = 1

_RAW = os.getenv("FAQ_VIDEO_CACHE_DIR", str(ROOT / "cache" / "faq" / "video"))
_PATH = Path(_RAW).expanduser()
if not _PATH.is_absolute():
    _PATH = ROOT / _PATH
FAQ_VIDEO_CACHE_DIR = _PATH.resolve()


def _avatar_key() -> str:
    return safe_cache_key(INTRO_AVATAR_CACHE_KEY)


def faq_video_key(spoken: str, language: str, voice: str | None = None) -> str:
    """Stable content hash for an FAQ utterance. 24 hex chars (path/url-safe)."""
    voice = voice or SONIOX_TTS_VOICE
    payload = "\x00".join(
        [
            str(FAQ_VIDEO_CACHE_VERSION),
            INTRO_AVATAR_CACHE_KEY,
            voice,
            language or "",
            (spoken or "").strip(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def faq_video_dir() -> Path:
    return FAQ_VIDEO_CACHE_DIR / _avatar_key()


def faq_video_path(key: str) -> Path:
    return faq_video_dir() / f"{key}.mp4"


def faq_video_url(key: str) -> str:
    return f"/faq-video/{_avatar_key()}/{key}.mp4"


def _is_present(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def lookup_faq_video(spoken: str, language: str, voice: str | None = None) -> str | None:
    """Return the serve URL for a cached FAQ MP4 matching ``spoken``/``language``, or None."""
    if not (spoken or "").strip():
        return None
    key = faq_video_key(spoken, language, voice)
    path = faq_video_path(key)
    if _is_present(path):
        # ?v=<file version> busts CDN/browser caches when the MP4 is re-encoded (same key/URL).
        return f"{faq_video_url(key)}?v={file_cache_version(path)}"
    return None


async def build_faq_video(
    spoken: str,
    language: str,
    *,
    tts: SonioxRealtimeTTS,
    synctalk: SyncTalkClient,
    voice: str | None = None,
    force: bool = False,
) -> Path | None:
    """Render ``spoken`` to a cached FAQ MP4 (TTS -> SyncTalk -> ffmpeg). Off-thread encode.

    Idempotent: returns the existing file unless ``force``. Returns None on any failure
    (caller treats that as "not cached" and the live path renders it instead).
    """
    import asyncio

    spoken = (spoken or "").strip()
    if not spoken:
        return None
    voice = voice or SONIOX_TTS_VOICE
    key = faq_video_key(spoken, language, voice)
    out_path = faq_video_path(key)
    if _is_present(out_path) and not force:
        return out_path

    audio_wav = await tts.synthesize(
        spoken,
        language=language,
        priority=1,
        voice=voice,
        expand_context_terms=False,
    )
    frames: list[str] = []
    async for frame in synctalk.infer_stream(audio_wav, priority=1, chunk_idx=0):
        frames.append(frame)
    if not frames:
        log.warning("faq video build produced no frames: key=%s lang=%s", key, language)
        return None

    ok = await asyncio.to_thread(encode_frames_to_mp4, frames, audio_wav, out_path)
    if not ok:
        return None

    meta = {
        "version": FAQ_VIDEO_CACHE_VERSION,
        "key": key,
        "language": language,
        "voice": voice,
        "avatar": INTRO_AVATAR_CACHE_KEY,
        "frames": len(frames),
        "spoken": spoken,
    }
    with contextlib.suppress(Exception):
        out_path.with_suffix(".mp4.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    log.info("faq video built: key=%s lang=%s frames=%d path=%s", key, language, len(frames), out_path)
    return out_path
