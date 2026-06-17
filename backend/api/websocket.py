from __future__ import annotations

import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..media.tts import SonioxRealtimeTTS
from ..media.synctalk import SyncTalkClient
from ..logging_config import log_event
from ..session.session import ClientSession
from ..session.session_gate import GATE
from .ws_writer import WsWriter

log = logging.getLogger(__name__)
ws_log = logging.getLogger("backend.websocket")

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    intro_token = (websocket.query_params.get("intro_token") or "").strip()
    writer = WsWriter(websocket)
    session = ClientSession(
        websocket=websocket,
        writer=writer,
        tts=SonioxRealtimeTTS(),
        synctalk=SyncTalkClient(),
    )
    writer.set_session_id(session.session_id)
    writer.set_on_send(session.on_send)
    set_tts_session_id = getattr(session.tts, "set_session_id", None)
    if set_tts_session_id is not None:
        set_tts_session_id(session.session_id)
    log_event(ws_log, "websocket_connected", session_id=session.session_id)
    # Single-pipeline guard: admit at most MAX_CONCURRENT_SESSIONS. If the slot is
    # taken (by an active user), tell this client it's busy and close — the frontend
    # shows a "please wait" overlay and its reconnect loop retries until a slot frees.
    # Done BEFORE intro/prewarm so a rejected client never touches the GPU pipeline.
    if not await GATE.acquire(session):
        await writer.send({"type": "busy", "text": "The avatar is currently in use. Please wait a moment…"})
        with contextlib.suppress(Exception):
            await websocket.close(code=1013)  # 1013 = Try Again Later
        await session.close()
        return
    await writer.send({"type": "session_state", "session_id": session.session_id, "state": "connected"})
    session.start_intro(intro_token or None)
    session.prewarm_realtime_stt(force=True)
    session.prewarm_realtime_tts()
    try:
        while True:
            raw_payload = await websocket.receive_text()
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                log_event(ws_log, "ws_receive_invalid_json", session_id=session.session_id, payload_chars=len(raw_payload))
                await writer.send({"type": "error", "text": "Invalid JSON payload"})
                continue
            if not isinstance(payload, dict):
                log_event(ws_log, "ws_receive_invalid_payload", session_id=session.session_id, payload_type=type(payload).__name__)
                await writer.send({"type": "error", "text": "Invalid message payload"})
                continue
            await session.handle_message(payload)
    except WebSocketDisconnect:
        log_event(ws_log, "websocket_disconnected", session_id=session.session_id)
    except Exception as exc:
        log.exception("ws session error")
        log_event(ws_log, "websocket_session_error", session_id=session.session_id, error=exc, level=logging.ERROR)
    finally:
        await GATE.release(session.session_id)
        try:
            await session.reset(reopen_transports=False, reason="disconnect")
        except Exception:
            log.exception("session reset failed")
        await session.close()
        log_event(ws_log, "websocket_closed", session_id=session.session_id)
