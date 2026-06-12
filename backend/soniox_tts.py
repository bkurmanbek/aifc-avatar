from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import socket
from time import perf_counter
from collections.abc import AsyncGenerator, AsyncIterable
from uuid import uuid4

import websockets

from .settings import (
    SONIOX_TTS_API_KEY,
    SONIOX_TTS_AUDIO_FORMAT,
    SONIOX_TTS_BITRATE,
    SONIOX_TTS_CONNECT_TIMEOUT_S,
    SONIOX_TTS_FORCE_IPV4,
    SONIOX_TTS_KEEPALIVE_INTERVAL_S,
    SONIOX_TTS_MODEL,
    SONIOX_TTS_PRECONNECT_ATTEMPTS,
    SONIOX_TTS_PRECONNECT_TIMEOUT_S,
    SONIOX_TTS_SAMPLE_RATE,
    SONIOX_TTS_STREAM_TIMEOUT_S,
    SONIOX_TTS_STREAMING_AVATAR,
    SONIOX_TTS_VOICE,
    SONIOX_TTS_WS_URL,
)
from .audio_utils import pcm_to_wav_bytes, silent_wav_bytes
from .tts_pronunciation import prepare_tts_text

log = logging.getLogger(__name__)


class SonioxRealtimeTTS:
    """Soniox realtime TTS WebSocket client.

    The Soniox endpoint multiplexes independent TTS streams by stream_id on one
    WebSocket. This client keeps a single socket open and routes JSON audio
    messages to the request that owns the stream_id.
    """

    def __init__(self) -> None:
        self.sample_rate = SONIOX_TTS_SAMPLE_RATE
        self.audio_format = SONIOX_TTS_AUDIO_FORMAT
        self._ws = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._queues: dict[str, asyncio.Queue[dict]] = {}
        self._closed = False

    @property
    def is_pcm_s16le(self) -> bool:
        return self.audio_format == "pcm_s16le"

    @property
    def supports_streaming_avatar(self) -> bool:
        return self.is_pcm_s16le and SONIOX_TTS_STREAMING_AVATAR

    @property
    def closed(self) -> bool:
        return self._closed

    async def synthesize(
        self,
        text: str,
        language: str | None = None,
        *,
        lang: str | None = None,
        priority: int | None = None,
        voice: str | None = None,
        expand_context_terms: bool = False,
    ) -> bytes:
        del priority
        language = language or lang
        text = prepare_tts_text(text, language, expand_context_terms=expand_context_terms)
        if not text:
            return silent_wav_bytes(self.sample_rate)
        pcm = bytearray()
        async for chunk in self.synthesize_pcm_stream(text, language=language, voice=voice, expand_context_terms=False):
            pcm.extend(chunk)
        return pcm_to_wav_bytes(bytes(pcm), self.sample_rate)

    async def synthesize_pcm_stream(
        self,
        text: str,
        *,
        language: str | None = None,
        lang: str | None = None,
        voice: str | None = None,
        client_reference_id: str | None = None,
        expand_context_terms: bool = False,
    ) -> AsyncGenerator[bytes, None]:
        if not SONIOX_TTS_API_KEY:
            raise RuntimeError("SONIOX_TTS_API_KEY or SONIOX_API_KEY is required for Soniox TTS")
        if not self.is_pcm_s16le:
            raise RuntimeError("Streaming avatar mode requires SONIOX_TTS_AUDIO_FORMAT=pcm_s16le")
        language = language or lang
        text = prepare_tts_text(text, language, expand_context_terms=expand_context_terms)
        if not text:
            return

        stream_id = f"tts-{uuid4().hex}"
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._queues[stream_id] = queue
        terminated = False

        try:
            await self._send_config_with_retry(
                stream_id,
                language=language,
                voice=voice,
                client_reference_id=client_reference_id,
                queue=queue,
            )
            await self._send({"stream_id": stream_id, "text": text, "text_end": True})

            while True:
                message = await asyncio.wait_for(queue.get(), timeout=SONIOX_TTS_STREAM_TIMEOUT_S)
                if message.get("_connection_error"):
                    raise RuntimeError(str(message["_connection_error"]))
                if message.get("error_code") or message.get("error_type"):
                    raise RuntimeError(
                        f"Soniox TTS error {message.get('error_code')}: "
                        f"{message.get('error_type') or ''} {message.get('error_message') or ''}".strip()
                    )
                audio_b64 = message.get("audio")
                if audio_b64:
                    yield base64.b64decode(audio_b64)
                if message.get("terminated"):
                    terminated = True
                    return
        finally:
            self._queues.pop(stream_id, None)
            if not terminated:
                with contextlib.suppress(Exception):
                    await self._send_if_open({"stream_id": stream_id, "cancel": True})

    async def synthesize_pcm_stream_from_texts(
        self,
        texts: AsyncIterable[str],
        *,
        language: str | None = None,
        lang: str | None = None,
        voice: str | None = None,
        client_reference_id: str | None = None,
        expand_context_terms: bool = False,
    ) -> AsyncGenerator[bytes, None]:
        if not SONIOX_TTS_API_KEY:
            raise RuntimeError("SONIOX_TTS_API_KEY or SONIOX_API_KEY is required for Soniox TTS")
        if not self.is_pcm_s16le:
            raise RuntimeError("Streaming avatar mode requires SONIOX_TTS_AUDIO_FORMAT=pcm_s16le")
        language = language or lang

        async def prepared_texts() -> AsyncGenerator[str, None]:
            async for text in texts:
                prepared = prepare_tts_text(text, language, expand_context_terms=expand_context_terms)
                if prepared:
                    yield prepared if prepared.endswith((" ", "\n")) else f"{prepared} "

        stream_id = f"tts-{uuid4().hex}"
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._queues[stream_id] = queue
        terminated = False
        send_task: asyncio.Task | None = None

        try:
            await self._send_config_with_retry(
                stream_id,
                language=language,
                voice=voice,
                client_reference_id=client_reference_id,
                queue=queue,
            )
            send_task = asyncio.create_task(self._send_text_stream(stream_id, prepared_texts()))
            send_task.add_done_callback(lambda task: self._relay_send_task_error(task, queue))

            while True:
                message = await asyncio.wait_for(queue.get(), timeout=SONIOX_TTS_STREAM_TIMEOUT_S)
                if message.get("_connection_error"):
                    raise RuntimeError(str(message["_connection_error"]))
                if message.get("error_code") or message.get("error_type"):
                    raise RuntimeError(
                        f"Soniox TTS error {message.get('error_code')}: "
                        f"{message.get('error_type') or ''} {message.get('error_message') or ''}".strip()
                    )
                audio_b64 = message.get("audio")
                if audio_b64:
                    yield base64.b64decode(audio_b64)
                if message.get("terminated"):
                    terminated = True
                    return
        finally:
            if send_task is not None and not send_task.done():
                send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await send_task
            self._queues.pop(stream_id, None)
            if not terminated:
                with contextlib.suppress(Exception):
                    await self._send_if_open({"stream_id": stream_id, "cancel": True})

    async def preconnect(self, *, attempts: int | None = None, timeout_s: float | None = None) -> None:
        if not SONIOX_TTS_API_KEY:
            return
        max_attempts = max(1, SONIOX_TTS_PRECONNECT_ATTEMPTS if attempts is None else attempts)
        timeout = max(0.5, SONIOX_TTS_PRECONNECT_TIMEOUT_S if timeout_s is None else timeout_s)
        started = perf_counter()
        for attempt in range(max_attempts):
            try:
                await asyncio.wait_for(
                    self._ensure_ws(),
                    timeout=timeout,
                )
                log.info("Soniox TTS preconnect ready in %dms", int((perf_counter() - started) * 1000))
                return
            except Exception as exc:
                await self._close_ws()
                if attempt < max_attempts - 1:
                    log.warning("Retrying Soniox TTS preconnect after connection failure: %s", exc)
                    await asyncio.sleep(0.2)
                    continue
                log.warning("Soniox TTS preconnect failed; synthesis will connect on demand: %s", exc)

    async def _send_config_with_retry(
        self,
        stream_id: str,
        *,
        language: str | None,
        voice: str | None,
        client_reference_id: str | None,
        queue: asyncio.Queue[dict],
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                await self._ensure_ws()
                await self._send(
                    self._config_message(
                        stream_id,
                        language=language,
                        voice=voice,
                        client_reference_id=client_reference_id,
                    )
                )
                return
            except Exception as exc:
                last_error = exc
                await self._close_ws()
                _drain_queue(queue)
                if attempt == 0:
                    log.warning("Retrying Soniox TTS stream start after connection failure: %s", exc)
                    continue
                raise
        if last_error is not None:
            raise last_error

    async def _send_text_stream(self, stream_id: str, texts: AsyncIterable[str]) -> None:
        try:
            async for text in texts:
                clean = str(text or "").strip()
                if not clean:
                    continue
                await self._send({"stream_id": stream_id, "text": clean, "text_end": False})
        finally:
            await self._send_if_open({"stream_id": stream_id, "text": "", "text_end": True})

    def _relay_send_task_error(self, task: asyncio.Task, queue: asyncio.Queue[dict]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                with contextlib.suppress(Exception):
                    queue.put_nowait({"_connection_error": f"Soniox TTS send failed: {exc}"})

    def _config_message(
        self,
        stream_id: str,
        *,
        language: str | None,
        voice: str | None,
        client_reference_id: str | None,
    ) -> dict:
        config = {
            "api_key": SONIOX_TTS_API_KEY,
            "stream_id": stream_id,
            "model": SONIOX_TTS_MODEL,
            "language": _supported_language(language),
            "voice": (voice or SONIOX_TTS_VOICE).strip() or SONIOX_TTS_VOICE,
            "audio_format": SONIOX_TTS_AUDIO_FORMAT,
            "sample_rate": SONIOX_TTS_SAMPLE_RATE,
        }
        if SONIOX_TTS_BITRATE > 0:
            config["bitrate"] = SONIOX_TTS_BITRATE
        if client_reference_id:
            config["client_reference_id"] = client_reference_id[:256]
        return config

    async def _ensure_ws(self) -> None:
        async with self._connect_lock:
            if _ws_is_open(self._ws):
                return
            await self._close_ws()
            started = perf_counter()
            self._ws = await websockets.connect(
                SONIOX_TTS_WS_URL,
                ping_interval=None,
                max_size=16 * 1024 * 1024,
                open_timeout=SONIOX_TTS_CONNECT_TIMEOUT_S,
                family=socket.AF_INET if SONIOX_TTS_FORCE_IPV4 else 0,
            )
            log.info("Soniox TTS websocket connected in %dms", int((perf_counter() - started) * 1000))
            self._reader_task = asyncio.create_task(self._reader_loop(self._ws))
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _send(self, payload: dict) -> None:
        await self._ensure_ws()
        async with self._send_lock:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _send_if_open(self, payload: dict) -> None:
        if not _ws_is_open(self._ws):
            return
        async with self._send_lock:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _reader_loop(self, ws) -> None:
        ended_cleanly = False
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except Exception:
                    log.warning("Soniox TTS returned non-JSON message")
                    continue
                stream_id = message.get("stream_id")
                if stream_id and stream_id in self._queues:
                    await self._queues[stream_id].put(message)
            ended_cleanly = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(exc)
        finally:
            if self._ws is ws:
                self._ws = None
            if ended_cleanly and self._queues:
                self._fail_pending(RuntimeError("Soniox TTS connection closed"))

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(5.0, SONIOX_TTS_KEEPALIVE_INTERVAL_S))
                if not _ws_is_open(self._ws):
                    return
                await self._send_if_open({"type": "keepalive"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(exc)

    def _fail_pending(self, exc: Exception) -> None:
        message = {"_connection_error": f"Soniox TTS connection failed: {exc}"}
        for queue in list(self._queues.values()):
            with contextlib.suppress(Exception):
                queue.put_nowait(message)

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        for task in (self._reader_task, self._keepalive_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader_task = None
        self._keepalive_task = None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._close_ws()
        self._fail_pending(RuntimeError("Soniox TTS client closed"))
        self._queues.clear()
        log.info("Soniox TTS websocket closed")


def _supported_language(language: str | None) -> str:
    normalized = (language or "").strip().lower().replace("_", "-")
    if normalized in {"en-gb", "en-us", "en-au", "en-ca"}:
        return "en"
    if normalized in {"en", "ru", "kk", "zh"}:
        return normalized
    return "en"


def _ws_is_open(ws) -> bool:
    if ws is None:
        return False
    state = getattr(ws, "state", None)
    state_name = getattr(state, "name", None)
    if state_name is not None:
        return state_name == "OPEN"
    closed = getattr(ws, "closed", None)
    if closed is not None:
        return not bool(closed)
    return getattr(ws, "close_code", None) is None


def _drain_queue(queue: asyncio.Queue[dict]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
