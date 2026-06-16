from __future__ import annotations

import contextlib
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from ..pipeline.answer_race import clear_answer_caches
from ..intro import (
    canonical_intro_key as _canonical_intro_key,
    ensure_intro_video as _ensure_intro_video,
    intro_audio_path as _intro_audio_path,
    intro_frame_cache_info as _intro_frame_cache_info,
    intro_frame_range_path as _intro_frame_range_path,
    intro_frame_signature as _intro_frame_signature,
    load_intro_blocks as _load_intro_blocks,
    safe_cache_key as _safe_cache_key,
)
from ..logging_config import log_event
from ..settings import INTRO_AUDIO_CACHE_DIR, INTRO_AVATAR_CACHE_KEY

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/admin/cache/clear")
async def clear_runtime_caches() -> dict[str, object]:
    cleared = await clear_answer_caches()
    log_event(log, "runtime_caches_cleared", caches=json.dumps(cleared, sort_keys=True))
    return {"status": "ok", "cleared": cleared}


@router.get("/intro-video/{avatar}/{name}")
async def intro_cache_video(avatar: str, name: str) -> FileResponse:
    if _safe_cache_key(avatar) != _safe_cache_key(INTRO_AVATAR_CACHE_KEY):
        raise HTTPException(status_code=404, detail="intro video not found")
    blocks = _load_intro_blocks()
    if not blocks:
        raise HTTPException(status_code=404, detail="intro not configured")
    path = await _ensure_intro_video(blocks)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="intro video not available")
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@router.get("/intro-audio/{avatar}/{block_key}")
async def intro_cache_audio(avatar: str, block_key: str) -> FileResponse:
    safe_key = _canonical_intro_key(block_key)
    if safe_key is None:
        raise HTTPException(status_code=404, detail="intro block not found")
    block = next((item for item in _load_intro_blocks() if item.key == safe_key), None)
    if block is None:
        raise HTTPException(status_code=404, detail="intro block not found")
    path = _intro_audio_path(block)
    if not path.exists():
        raise HTTPException(status_code=404, detail="intro audio not cached yet")
    return FileResponse(
        path,
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@router.get("/intro-cache/{avatar}/{block_key}")
async def intro_cache_frames(avatar: str, block_key: str, start: int = 0, limit: int = 0) -> JSONResponse:
    safe_avatar = _safe_cache_key(avatar)
    expected_avatar = _safe_cache_key(INTRO_AVATAR_CACHE_KEY)
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
                and cached_payload.get("avatar") == safe_avatar == expected_avatar
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
