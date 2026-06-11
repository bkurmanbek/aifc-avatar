from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import WebSocket, WebSocketDisconnect


class ClientClosedError(Exception):
    pass


class WsWriter:
    def __init__(self, websocket: WebSocket, on_send: Callable[[dict], None] | None = None):
        self._ws = websocket
        self._lock = asyncio.Lock()
        self._closed = False
        self._on_send = on_send
        self._active_turn_id: str | None = None

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
            return
        async with self._lock:
            if self._closed:
                raise ClientClosedError()
            turn_id = data.get("turn_id")
            if turn_id is not None and turn_id != self._active_turn_id:
                return
            try:
                await self._ws.send_json(data)
                if self._on_send is not None:
                    self._on_send(data)
            except (WebSocketDisconnect, RuntimeError) as exc:
                self._closed = True
                raise ClientClosedError() from exc

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def websocket(self) -> WebSocket:
        return self._ws
