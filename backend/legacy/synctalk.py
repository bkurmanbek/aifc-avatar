from __future__ import annotations

import base64
import asyncio
import json
import logging
from collections.abc import AsyncGenerator

import httpx

from .settings import SYNCTALK_FRAME_TIMEOUT_S, SYNCTALK_STREAM_URL, SYNCTALK_TIMEOUT_S

log = logging.getLogger(__name__)


class SyncTalkClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(SYNCTALK_TIMEOUT_S, connect=10.0))

    async def infer_stream(
        self,
        audio_wav: bytes,
        priority: int = 1,
        chunk_idx: int = 0,
    ) -> AsyncGenerator[str, None]:
        async with self._client.stream(
            "POST",
            SYNCTALK_STREAM_URL,
            json={
                "audio_b64": base64.b64encode(audio_wav).decode(),
                "priority": priority,
                "chunk_idx": chunk_idx,
            },
        ) as response:
            response.raise_for_status()
            frame_count = 0
            lines = response.aiter_lines().__aiter__()
            while True:
                try:
                    line = await asyncio.wait_for(lines.__anext__(), timeout=SYNCTALK_FRAME_TIMEOUT_S)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as exc:
                    raise TimeoutError(
                        f"SyncTalk frame timeout after {SYNCTALK_FRAME_TIMEOUT_S:.1f}s for chunk={chunk_idx}"
                    ) from exc
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("SyncTalk returned malformed JSON line: chunk=%s sample=%r", chunk_idx, line[:200])
                    continue
                error = item.get("error") or item.get("error_message")
                if error:
                    log.error("SyncTalk stream error: chunk=%s error=%s", chunk_idx, error)
                    raise RuntimeError(f"SyncTalk stream error for chunk={chunk_idx}: {error}")
                frame = item.get("frame")
                if frame:
                    frame_count += 1
                    yield frame
            if frame_count == 0:
                log.warning("SyncTalk stream returned zero frames: chunk=%s priority=%s", chunk_idx, priority)

    async def close(self) -> None:
        await self._client.aclose()
