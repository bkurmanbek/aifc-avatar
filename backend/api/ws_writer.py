from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from fastapi import WebSocket, WebSocketDisconnect

from ..logging_config import log_event, preview_text

log = logging.getLogger(__name__)


class ClientClosedError(Exception):
    pass


class WsWriter:
    def __init__(self, websocket: WebSocket, on_send: Callable[[dict], None] | None = None):
        self._ws = websocket
        self._lock = asyncio.Lock()
        self._closed = False
        self._on_send = on_send
        self._active_turn_id: str | None = None
        self._session_id: str | None = None

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

    def set_on_send(self, on_send: Callable[[dict], None] | None) -> None:
        self._on_send = on_send

    def set_active_turn(self, turn_id: str) -> None:
        self._active_turn_id = turn_id

    def clear_active_turn(self, turn_id: str | None = None) -> None:
        if turn_id is None or self._active_turn_id == turn_id:
            self._active_turn_id = None

    async def send(self, data: dict) -> None:
        if self._closed:
            raise ClientClosedError()
        turn_id = data.get("turn_id")
        if turn_id is not None and turn_id != self._active_turn_id:
            self._log_send("ws_send_dropped_inactive_turn", data)
            return
        async with self._lock:
            if self._closed:
                raise ClientClosedError()
            turn_id = data.get("turn_id")
            if turn_id is not None and turn_id != self._active_turn_id:
                self._log_send("ws_send_dropped_inactive_turn", data)
                return
            try:
                await self._ws.send_json(data)
                self._log_send("ws_send", data)
                if self._on_send is not None:
                    self._on_send(data)
            except Exception as exc:
                self._closed = True
                self._log_send("ws_send_failed", data, exc=exc)
                raise ClientClosedError() from exc

    async def send_frame_binary(self, chunk: int, jpeg: bytes, *, turn_id: str | None = None) -> None:
        """Send one avatar JPEG frame as a binary WS message.

        Frames are the highest-frequency message (25/s); shipping them as raw bytes
        avoids the base64 inflation + per-frame JSON.parse + atob the client paid when
        frames were embedded in JSON. Binary layout (little-endian):
            byte 0      magic 0xF1 (frame)
            bytes 1-2   chunk index (uint16)
            byte 3      turn_id length (uint8)
            bytes 4..   turn_id (ascii)
            rest        JPEG bytes
        """
        if self._closed:
            raise ClientClosedError()
        if turn_id is not None and turn_id != self._active_turn_id:
            self._log_send("ws_send_dropped_inactive_turn", {"type": "frame", "chunk": chunk, "turn_id": turn_id})
            return
        tid = (turn_id or "").encode("ascii", "ignore")[:255]
        header = bytes([0xF1]) + (chunk & 0xFFFF).to_bytes(2, "little") + bytes([len(tid)]) + tid
        async with self._lock:
            if self._closed:
                raise ClientClosedError()
            if turn_id is not None and turn_id != self._active_turn_id:
                return
            try:
                await self._ws.send_bytes(header + jpeg)
                if self._on_send is not None:
                    self._on_send({"type": "frame", "chunk": chunk, "turn_id": turn_id})
            except Exception as exc:
                self._closed = True
                self._log_send("ws_send_failed", {"type": "frame", "chunk": chunk}, exc=exc)
                raise ClientClosedError() from exc

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def websocket(self) -> WebSocket:
        return self._ws

    def _log_send(self, event: str, data: dict, exc: BaseException | None = None) -> None:
        if not _should_log_payload(data):
            return
        log_event(
            log,
            event,
            session_id=self._session_id,
            request_id=str(data.get("turn_id") or self._active_turn_id or ""),
            error=exc,
            **_summarize_payload(data),
        )


def _should_log_payload(data: dict) -> bool:
    message_type = data.get("type")
    return message_type != "frame"


def _summarize_payload(data: dict) -> dict:
    message_type = data.get("type")
    summary: dict[str, object] = {"message_type": message_type}
    for key in (
        "turn_id",
        "chunk",
        "source_chunk",
        "frame_count",
        "chunks",
        "streaming",
        "cached",
        "answer_language",
        "state",
        "winner_source",
        "winner_confidence",
    ):
        if key in data:
            summary[key] = data.get(key)

    text = data.get("text")
    if isinstance(text, str):
        summary["text_chars"] = len(text)
        summary["text_preview"] = preview_text(text, 240)

    payload_data = data.get("data")
    if isinstance(payload_data, str):
        summary["data_chars"] = len(payload_data)

    if message_type == "answer_payload":
        spoken = str(data.get("spoken") or "")
        chat = str(data.get("chat") or "")
        summary["spoken_chars"] = len(spoken)
        summary["spoken_preview"] = preview_text(spoken, 240)
        summary["chat_chars"] = len(chat)
        summary["chat_preview"] = preview_text(chat, 320)
        summary["answer_id"] = data.get("answer_id")

    latency = data.get("latency_ms")
    if isinstance(latency, dict):
        summary["latency_ms_payload"] = latency
    elif latency is not None:
        summary["latency_ms_payload"] = latency

    if "url" in data:
        summary["url"] = data.get("url")
    return summary
