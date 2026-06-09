from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator

import httpx

from .settings import SYNCTALK_STREAM_URL, SYNCTALK_TIMEOUT_S


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
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                item = json.loads(line)
                frame = item.get("frame")
                if frame:
                    yield frame

    async def close(self) -> None:
        await self._client.aclose()
