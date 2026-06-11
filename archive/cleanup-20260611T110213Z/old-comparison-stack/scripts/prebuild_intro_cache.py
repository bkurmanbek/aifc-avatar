from __future__ import annotations

import argparse
import asyncio
import logging

from ws_backend.app import (
    _ensure_intro_audio_file,
    _intro_frame_path,
    _intro_frame_signature,
    _load_intro_blocks,
    _load_intro_frames_from_cache,
    _save_intro_frames_to_cache,
)
from ws_backend.settings import INTRO_AVATAR_CACHE_KEY, SYNCTALK_STREAM_URL
from ws_backend.synctalk import SyncTalkClient
from ws_backend.tts import ElevenTTS


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


async def prebuild_intro_cache(*, force_frames: bool = False) -> None:
    blocks = _load_intro_blocks()
    if not blocks:
        raise RuntimeError("No intro blocks found")

    log.info("prebuilding intro cache avatar=%s synctalk=%s", INTRO_AVATAR_CACHE_KEY, SYNCTALK_STREAM_URL)
    tts = ElevenTTS()
    synctalk = SyncTalkClient()
    try:
        for index, block in enumerate(blocks):
            audio_wav = await _ensure_intro_audio_file(tts, block)
            cached_frames = None if force_frames else _load_intro_frames_from_cache(block)
            if cached_frames:
                log.info(
                    "intro frames cached block=%s chunk=%d frames=%d path=%s",
                    block.key,
                    index,
                    len(cached_frames),
                    _intro_frame_path(block),
                )
                continue

            frames: list[str] = []
            log.info("generating intro frames block=%s chunk=%d", block.key, index)
            async for frame in synctalk.infer_stream(
                audio_wav,
                priority=0 if index == 0 else 1,
                chunk_idx=index,
            ):
                frames.append(frame)
            if not frames:
                raise RuntimeError(f"SyncTalk returned no frames for intro block {block.key}")
            _save_intro_frames_to_cache(block, frames)
            log.info(
                "intro frames generated block=%s chunk=%d frames=%d path=%s signature=%s",
                block.key,
                index,
                len(frames),
                _intro_frame_path(block),
                _intro_frame_signature(block)[:12],
            )
    finally:
        await synctalk.close()
        await tts.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Prebuild cached intro audio and SyncTalk frames.")
    parser.add_argument("--force-frames", action="store_true", help="Regenerate frame JSON even if cache metadata is valid.")
    args = parser.parse_args()
    asyncio.run(prebuild_intro_cache(force_frames=args.force_frames))


if __name__ == "__main__":
    main()
