from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..media.audio_utils import pcm_to_wav_bytes
from ..settings import DEBUG_RESPONSE_DIR, ROOT

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers — all run on a thread pool to avoid blocking the event loop
# ---------------------------------------------------------------------------

def _resolve_base() -> Path | None:
    raw = DEBUG_RESPONSE_DIR.strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _write(path: Path, data: bytes | str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)
    except Exception:
        log.exception("debug_io: write failed: %s", path)


def _append(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        log.exception("debug_io: append failed: %s", path)


# ---------------------------------------------------------------------------
# Public API — fire-and-forget; callers do not await these
# ---------------------------------------------------------------------------

def save_response(turn_id: str, spoken: str, chat: str) -> None:
    """Write spoken + chat to var/debug/responses/{turn_id}.txt."""
    base = _resolve_base()
    if base is None:
        return
    content = (
        f"=== TURN {turn_id} ===\n\n"
        f"--- spoken ---\n{spoken}\n\n"
        f"--- chat ---\n{chat}\n"
    )
    _fire(asyncio.to_thread(_write, base / "responses" / f"{turn_id}.txt", content))


def save_tts_chunk(turn_id: str, media_idx: int, sentence: str, pcm: bytes, sample_rate: int) -> None:
    """Append sentence to sentences.txt and save PCM as WAV in var/debug/tts_chunks/{turn_id}/."""
    base = _resolve_base()
    if base is None:
        return
    chunks_dir = base / "tts_chunks" / turn_id
    line = f"chunk_{media_idx:02d}: {sentence}\n"
    _fire(asyncio.to_thread(_append, chunks_dir / "sentences.txt", line))
    try:
        wav = pcm_to_wav_bytes(pcm, sample_rate)
    except Exception:
        log.warning("debug_io: pcm_to_wav failed for chunk %s, saving raw PCM", media_idx)
        wav = pcm
        _fire(asyncio.to_thread(_write, chunks_dir / f"chunk_{media_idx:02d}.pcm", wav))
        return
    _fire(asyncio.to_thread(_write, chunks_dir / f"chunk_{media_idx:02d}.wav", wav))


def _fire(coro: object) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)  # type: ignore[arg-type]
    except RuntimeError:
        pass
