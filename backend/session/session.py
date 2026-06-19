from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
from dataclasses import dataclass, field
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

from fastapi import WebSocket

from ..utils.language import (
    dedupe_repeated_transcript,
    detect_supported_text_language,
    detect_text_language,
    is_stop_command,
    is_noise_utterance,
    normalize_lang,
    smalltalk_reply,
    supported_lang_or_none,
    transcript_has_meaningful_speech,
    transcript_is_new_query_candidate,
    UNSUPPORTED_LANGUAGE_MESSAGE,
)
from ..settings import (
    FAQ_VIDEO_ENABLED,
    MAX_HISTORY_TURNS,
    SONIOX_STT_KEEPALIVE_INTERVAL_S,
    SONIOX_STT_ENDPOINT_WAIT_S,
    SONIOX_STT_PRECONNECT,
    TTS_PREWARM_QUERY_WAIT_S,
)
from ..media.tts import SonioxRealtimeTTS
from ..media.stt import SonioxRealtimeSession, looks_like_pcm16_chunk
from ..media.synctalk import SyncTalkClient
from ..api.ws_writer import ClientClosedError, WsWriter
from ..pipeline.answer_race import AnswerRaceResult, run_answer_race
from ..knowledge.faq import _prebuilt_chitchat_answer
from ..knowledge.memory import update_conversation_memory
from ..logging_config import log_event, preview_text
from .session_gate import GATE
from ..pipeline.answer_format import (
    build_sentence_splitter as _build_sentence_splitter,
    coerce_spoken_chat_payload as _coerce_spoken_chat_payload,
    extract_json_any as _extract_json_any,
    is_final_turn_candidate as _is_final_turn_candidate,
    normalize_query_signature as _normalize_query_signature,
    normalize_spoken_for_tts as _normalize_spoken_for_tts,
)
from ..intro import (
    INTRO_CACHED_FRAME_BATCH as _INTRO_CACHED_FRAME_BATCH,
    INTRO_FRAME_HEADROOM as _INTRO_FRAME_HEADROOM,
    IntroBlock,
    clear_intro_token_in_progress as _clear_intro_token_in_progress,
    ensure_intro_audio_file as _ensure_intro_audio_file,
    intro_audio_path as _intro_audio_path,
    intro_frame_cache_info as _intro_frame_cache_info,
    intro_video_is_valid as _intro_video_is_valid,
    intro_video_url as _intro_video_url,
    intro_token_in_progress as _intro_token_in_progress,
    intro_token_seen as _intro_token_seen,
    load_intro_blocks as _load_intro_blocks,
    load_intro_frames_from_cache as _load_intro_frames_from_cache,
    mark_intro_token_in_progress as _mark_intro_token_in_progress,
    mark_intro_token_played as _mark_intro_token_played,
    safe_cache_key as _safe_cache_key,
    save_intro_frames_to_cache as _save_intro_frames_to_cache,
)
from ..faq_video import lookup_faq_video as _lookup_faq_video
from ..pipeline.response_stream import ResponseStream
from ..startup import log_background_task_error as _log_background_task_error
from ..utils.debug_io import save_response as _save_response_debug
from .metrics import TurnMetrics

log = logging.getLogger(__name__)
ws_log = logging.getLogger("backend.websocket")

_INTERRUPT_COOLDOWN_S = 1.0
_DUP_QUERY_WINDOW_S = 1.5
# Max time interrupt() will block its caller (the WS receive loop / STT loop) waiting
# for pipeline teardown. Teardown is normally sub-second; if a wedged SyncTalk call
# makes it slower, we stop waiting and finish teardown in the background so the loop
# keeps reading messages (answering heartbeat pings) and consuming mic audio. Without
# this bound a slow interrupt starves pings → the client watchdog force-closes the WS.
_INTERRUPT_TEARDOWN_TIMEOUT_S = 1.5


def _summarize_client_payload(payload: dict) -> dict:
    message_type = payload.get("type")
    summary: dict[str, object] = {"message_type": message_type}
    for key in ("turn_id", "chunk", "source"):
        if key in payload:
            summary[key] = payload.get(key)
    if "level" in payload:
        summary["client_level"] = payload.get("level")
    text = payload.get("text")
    if isinstance(text, str):
        summary["text_chars"] = len(text)
        summary["text_preview"] = preview_text(text, 240)
    data = payload.get("data")
    if isinstance(data, str):
        summary["data_chars"] = len(data)
    detail = payload.get("detail")
    if detail is not None:
        summary["detail_preview"] = preview_text(json.dumps(detail, ensure_ascii=False, default=str), 400)
    return summary


@dataclass
class ClientSession:
    websocket: WebSocket
    writer: WsWriter
    tts: SonioxRealtimeTTS
    synctalk: SyncTalkClient
    session_id: str = field(default_factory=lambda: uuid4().hex)
    history: list[dict[str, str]] = field(default_factory=list)
    conversation_memory: dict | None = None
    realtime_stt: SonioxRealtimeSession | None = None
    realtime_stt_started_at: float | None = None
    realtime_stt_ready_at: float | None = None
    realtime_stt_audio_started_at: float | None = None
    stt_keepalive_task: asyncio.Task | None = None
    stt_prewarm_task: asyncio.Task | None = None
    tts_prewarm_task: asyncio.Task | None = None
    _stt_start_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    pipeline_task: asyncio.Task | None = None
    # Strong refs to background teardown drains so the GC can't collect them mid-await
    # (asyncio holds only a weak ref to bare create_task() results).
    _drain_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)
    active_metrics: TurnMetrics | None = None
    active_turn_id: str | None = None
    ignore_audio_until: float = 0.0
    barge_in_triggered: bool = False
    _last_query_signature: str = ""
    _last_query_at: float = 0.0
    _interrupt_last_at: float = 0.0

    def _reset_interrupt_state(self) -> None:
        self._interrupt_last_at = 0.0

    def on_send(self, data: dict) -> None:
        metrics = self.active_metrics
        if metrics is None:
            return
        now = perf_counter()
        if data.get("type") == "audio_ready" and metrics.first_audio_at is None and int(data.get("chunk", 0)) == 0:
            metrics.first_audio_at = now
        elif data.get("type") == "frame" and metrics.first_frame_at is None and int(data.get("chunk", 0)) == 0:
            metrics.first_frame_at = now

    def on_client_first_render(self, turn_id: str | None, chunk: int | None) -> None:
        if chunk not in (None, 0):
            return
        if turn_id and self.active_turn_id and turn_id != self.active_turn_id:
            return
        if self.active_metrics is not None and self.active_metrics.client_first_render_at is None:
            self.active_metrics.client_first_render_at = perf_counter()

    async def _discard_closed_realtime_stt(self) -> None:
        session = self.realtime_stt
        if session is None or not session.closed:
            return
        if self.stt_keepalive_task is not None:
            self.stt_keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.stt_keepalive_task
            self.stt_keepalive_task = None
        if self.realtime_stt is session:
            self.realtime_stt = None
            self.realtime_stt_started_at = None
            self.realtime_stt_ready_at = None
            self.realtime_stt_audio_started_at = None
        with contextlib.suppress(Exception):
            await session.close()

    async def ensure_realtime_stt(self, *, status: bool = False) -> SonioxRealtimeSession | None:
        await self._discard_closed_realtime_stt()
        if self.realtime_stt is not None and not self.realtime_stt.closed:
            return self.realtime_stt
        async with self._stt_start_lock:
            await self._discard_closed_realtime_stt()
            if self.realtime_stt is not None and not self.realtime_stt.closed:
                return self.realtime_stt
            started = perf_counter()
            session = SonioxRealtimeSession(
                self.writer,
                on_meaningful_partial=self.on_meaningful_partial,
                on_final_utterance=self.on_realtime_final,
                session_id=self.session_id,
            )
            try:
                await session.start()
            except Exception:
                log.exception("Soniox realtime preconnect failed")
                log_event(log, "stt_realtime_preconnect_failed", session_id=self.session_id, level=logging.ERROR)
                with contextlib.suppress(Exception):
                    await session.close()
                return None
            self.realtime_stt = session
            self.realtime_stt_started_at = started
            self.realtime_stt_ready_at = perf_counter()
            self._start_stt_keepalive()
            ready_ms = int((self.realtime_stt_ready_at - started) * 1000)
            log_event(log, "stt_realtime_ready", session_id=self.session_id, latency_ms=ready_ms)
            if status:
                with contextlib.suppress(ClientClosedError):
                    await self.writer.send({"type": "stt_ready", "session_id": self.session_id, "ready_ms": ready_ms})
                with contextlib.suppress(ClientClosedError):
                    await self.writer.send({"type": "status", "text": "Transcribing..."})
            return session

    def _start_stt_keepalive(self) -> None:
        if self.stt_keepalive_task is not None and not self.stt_keepalive_task.done():
            return
        self.stt_keepalive_task = asyncio.create_task(self._stt_keepalive_loop())

    async def _stt_keepalive_loop(self) -> None:
        try:
            while not self.writer.closed:
                await asyncio.sleep(max(0.1, SONIOX_STT_KEEPALIVE_INTERVAL_S))
                session = self.realtime_stt
                if session is None or session.closed:
                    return
                await session.send_keepalive()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Soniox keepalive loop failed")

    async def _close_realtime_stt(self, expected: SonioxRealtimeSession | None = None, *, reason: str = "close") -> None:
        if self.stt_prewarm_task is not None and not self.stt_prewarm_task.done():
            self.stt_prewarm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.stt_prewarm_task
        self.stt_prewarm_task = None
        session = self.realtime_stt
        if expected is not None and session is not expected:
            session = expected
        if self.stt_keepalive_task is not None:
            self.stt_keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.stt_keepalive_task
            self.stt_keepalive_task = None
        if self.realtime_stt is session:
            self.realtime_stt = None
            self.realtime_stt_started_at = None
            self.realtime_stt_ready_at = None
            self.realtime_stt_audio_started_at = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
            log_event(log, "stt_realtime_closed", session_id=self.session_id, reason=reason)

    def prewarm_realtime_stt(self, *, force: bool = False) -> None:
        if not force and not SONIOX_STT_PRECONNECT:
            return
        if self.stt_prewarm_task is not None and not self.stt_prewarm_task.done():
            return
        self.stt_prewarm_task = asyncio.create_task(self.ensure_realtime_stt(status=False))
        self.stt_prewarm_task.add_done_callback(_log_background_task_error)

    def prewarm_realtime_tts(self) -> None:
        preconnect = getattr(self.tts, "preconnect", None)
        if preconnect is None:
            return
        if self.tts_prewarm_task is not None and not self.tts_prewarm_task.done():
            return
        self.tts_prewarm_task = asyncio.create_task(preconnect())
        self.tts_prewarm_task.add_done_callback(_log_background_task_error)

    async def wait_realtime_tts_ready(self, *, timeout_s: float | None = None) -> None:
        preconnect = getattr(self.tts, "preconnect", None)
        if preconnect is None:
            return
        if self.tts_prewarm_task is None:
            self.prewarm_realtime_tts()
        task = self.tts_prewarm_task
        if task is None or task.done():
            return
        timeout = TTS_PREWARM_QUERY_WAIT_S if timeout_s is None else timeout_s
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(0.1, timeout))
        except asyncio.TimeoutError:
            log_event(log, "tts_prewarm_wait_timeout", session_id=self.session_id, timeout_s=timeout)

    async def _close_realtime_tts(self, *, reason: str = "close", recreate: bool = False, prewarm: bool = False) -> None:
        if self.tts_prewarm_task is not None and not self.tts_prewarm_task.done():
            self.tts_prewarm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.tts_prewarm_task
        self.tts_prewarm_task = None
        was_closed = bool(getattr(self.tts, "closed", False))
        with contextlib.suppress(Exception):
            await self.tts.close()
        if not was_closed:
            log_event(log, "tts_realtime_closed", session_id=self.session_id, reason=reason)
        if recreate:
            self.tts = SonioxRealtimeTTS()
            set_session_id = getattr(self.tts, "set_session_id", None)
            if set_session_id is not None:
                set_session_id(self.session_id)
            if prewarm:
                self.prewarm_realtime_tts()

    def start_intro(self, intro_token: str | None = None) -> bool:
        if self.pipeline_task is not None and not self.pipeline_task.done():
            return False
        intro_blocks = _load_intro_blocks()
        if not intro_blocks:
            return False
        if not intro_token or _intro_token_seen(intro_token) or _intro_token_in_progress(intro_token):
            return False
        _mark_intro_token_in_progress(intro_token or "")
        self.pipeline_task = asyncio.create_task(self.run_intro(intro_blocks, intro_token=intro_token))
        self.pipeline_task.add_done_callback(_log_background_task_error)
        log_event(log, "intro_started", session_id=self.session_id)
        return True

    async def _ensure_intro_audio(self, block: IntroBlock) -> bytes:
        return await _ensure_intro_audio_file(self.tts, block)

    async def _play_intro_block(self, block: IntroBlock, index: int, turn_id: str) -> None:
        await self._ensure_intro_audio(block)
        from ..settings import INTRO_AVATAR_CACHE_KEY
        audio_url = f"/intro-audio/{_safe_cache_key(INTRO_AVATAR_CACHE_KEY)}/{block.key}"

        async def send_audio_ready(expected_frames: int | None = None) -> None:
            msg: dict = {
                "type": "audio_ready",
                "audio_url": audio_url,
                "chunk": index,
                "source_chunk": index,
                "frame_stride": 1,
                "streaming": True,
                "cached": True,
                "turn_id": turn_id,
            }
            if expected_frames is not None:
                msg["expected_frames"] = expected_frames
            await self.writer.send(msg)

        cache_info = _intro_frame_cache_info(block)
        if cache_info is not None:
            frame_url, frame_count = cache_info
            await self.writer.send(
                {
                    "type": "frame_cache",
                    "url": frame_url,
                    "chunk": index,
                    "turn_id": turn_id,
                    "frame_count": frame_count,
                }
            )
            await send_audio_ready(expected_frames=frame_count)
            return
        cached_frames = _load_intro_frames_from_cache(block)
        if cached_frames:
            headroom = min(_INTRO_FRAME_HEADROOM, len(cached_frames))
            for frame in cached_frames[:headroom]:
                await self.writer.send({"type": "frame", "data": frame, "chunk": index, "turn_id": turn_id})
            await send_audio_ready(expected_frames=len(cached_frames))
            for offset, frame in enumerate(cached_frames[headroom:], start=1):
                await self.writer.send({"type": "frame", "data": frame, "chunk": index, "turn_id": turn_id})
                if offset % _INTRO_CACHED_FRAME_BATCH == 0:
                    await asyncio.sleep(0)
            await self.writer.send({"type": "chunk_done", "chunk": index, "turn_id": turn_id})
            return

        audio_sent = False
        frame_count = 0
        frames: list[str] = []
        try:
            async for frame in self.synctalk.infer_stream(
                audio_wav,
                priority=0 if index == 0 else 1,
                chunk_idx=index,
            ):
                frames.append(frame)
                await self.writer.send({"type": "frame", "data": frame, "chunk": index, "turn_id": turn_id})
                frame_count += 1
                if not audio_sent and frame_count >= _INTRO_FRAME_HEADROOM:
                    await send_audio_ready()
                    audio_sent = True
        except ClientClosedError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("intro avatar generation failed: block=%s chunk=%s", block.key, index)

        if not audio_sent:
            await send_audio_ready()
            audio_sent = True
        if frame_count == 0:
            log.warning("intro avatar generation returned no frames: block=%s chunk=%s", block.key, index)
        else:
            _save_intro_frames_to_cache(block, frames)
        await self.writer.send({"type": "chunk_done", "chunk": index, "turn_id": turn_id})

    async def run_intro(self, intro_blocks: list[IntroBlock], intro_token: str | None = None) -> None:
        turn_id = uuid4().hex
        current_task = asyncio.current_task()
        metrics = TurnMetrics(started_at=perf_counter(), mode="intro")
        full_text = "\n\n".join(block.text for block in intro_blocks).strip()
        intro_token_played = False
        self.active_metrics = metrics
        self.active_turn_id = turn_id
        self.writer.set_active_turn(turn_id)
        try:
            log_event(log, "pipeline_intro_start", session_id=self.session_id, request_id=turn_id)
            await self.writer.send({"type": "policy_state", "turn_id": turn_id, "answer_language": "en"})
            await self.writer.send({"type": "response_start", "turn_id": turn_id})
            await self.writer.send({"type": "status", "turn_id": turn_id, "text": "Starting introduction..."})
            await self.writer.send({"type": "response_chunk", "text": f"{full_text} ", "turn_id": turn_id})
            # Prefer the prebuilt combined MP4 (hardware-decoded, stutter-free). Falls back
            # to per-block canvas streaming when the video isn't cached yet.
            if _intro_video_is_valid(intro_blocks):
                await self.writer.send({"type": "intro_video", "url": _intro_video_url(), "turn_id": turn_id})
            else:
                for index, block in enumerate(intro_blocks):
                    await self.writer.send({"type": "status", "turn_id": turn_id, "text": f"Streaming cached intro block {index + 1}/{len(intro_blocks)}: {block.key}"})
                    await self._play_intro_block(block, index, turn_id)
            payload = {
                "answer_id": turn_id,
                "spoken": full_text,
                "chat": full_text,
            }
            payload["winner_source"] = "session_intro"
            payload["winner_confidence"] = "high"
            await self.writer.send({"type": "answer_payload", "turn_id": turn_id, **payload})
            metrics.done_at = perf_counter()
            await self.writer.send({"type": "done", "chunks": len(intro_blocks), "turn_id": turn_id, "latency_ms": metrics.as_ms()})
            log_event(log, "pipeline_intro_done", session_id=self.session_id, request_id=turn_id, latency_ms=metrics.as_ms().get("total"))
            if intro_token and not intro_token_played:
                _mark_intro_token_played(intro_token)
                intro_token_played = True
        except asyncio.CancelledError:
            raise
        except ClientClosedError:
            pass
        except Exception:
            log.exception("session intro failed")
            log_event(log, "pipeline_intro_failed", session_id=self.session_id, request_id=turn_id, level=logging.ERROR)
            with contextlib.suppress(ClientClosedError):
                await self.writer.send({"type": "error", "text": "Introduction failed", "turn_id": turn_id})
        finally:
            if intro_token and not intro_token_played:
                _clear_intro_token_in_progress(intro_token)
            # Only clear shared turn state if it still points at THIS task. interrupt()
            # detaches pipeline_task and drains us in the background, so a slow teardown
            # can finish AFTER the next turn was assigned — nulling it here would clobber
            # the live turn (barge-in/Stop would silently stop working for it).
            if self.pipeline_task is current_task:
                self.pipeline_task = None
                self.active_metrics = None
                self.active_turn_id = None
            self.writer.clear_active_turn(turn_id)

    async def on_meaningful_partial(self, text: str) -> None:
        if self.barge_in_triggered:
            return
        if self.pipeline_task is None or self.pipeline_task.done():
            return
        if is_stop_command(text):
            log_event(log, "barge_in_stop", session_id=self.session_id, request_id=self.active_turn_id, partial=text[:80])
            self.barge_in_triggered = True
            await self.interrupt(send_event=True)
            # Brief mute after a stop to swallow the avatar's audio tail/echo before we
            # listen again. 1.0s (was 1.5s) — cancellation + SyncTalk abort stop the
            # avatar fast now, so a shorter window lets the user re-ask sooner.
            self.ignore_audio_until = perf_counter() + 1.0
            self._reset_interrupt_state()
            return
        now = perf_counter()
        if now - self._interrupt_last_at < _INTERRUPT_COOLDOWN_S and self._interrupt_last_at > 0:
            return
        self._interrupt_last_at = now

        log_event(log, "barge_in_partial", session_id=self.session_id, request_id=self.active_turn_id, partial=text[:80])
        self.barge_in_triggered = True
        await self.interrupt(send_event=True)
        self._reset_interrupt_state()

    async def force_disconnect(self, reason: str) -> None:
        """Evict this session: tell the client why, then close the socket so its
        receive loop unwinds (which releases the gate slot via the finally block)."""
        log_event(log, "session_force_disconnect", session_id=self.session_id, reason=reason)
        with contextlib.suppress(Exception):
            await self.writer.send({"type": "evicted", "reason": reason,
                                    "text": "Session ended due to inactivity."})
        with contextlib.suppress(Exception):
            await self.websocket.close(code=1000)

    async def close(self) -> None:
        await self._close_realtime_stt(reason="session_close")
        if self.pipeline_task is not None:
            self.pipeline_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.pipeline_task
        await self._close_realtime_tts(reason="session_close")
        await self.synctalk.close()

    async def interrupt(self, send_event: bool) -> None:
        self.writer.clear_active_turn()
        task = self.pipeline_task
        # Detach immediately so a new turn / the caller never sees a stale pipeline.
        # Clear the turn-scoped state here too: the cancelled task's `finally` now only
        # clears state it still owns (guarded by `is current_task`), so it won't do it
        # for us. A new turn started right after will set its own values.
        self.pipeline_task = None
        self.active_turn_id = None
        self.active_metrics = None
        if task is not None and not task.done():
            log_event(log, "pipeline_cancel_requested", session_id=self.session_id, request_id=self.active_turn_id, send_event=send_event)
            task.cancel()
            # Wait briefly for clean teardown, but NEVER block the caller (WS receive /
            # STT loop) indefinitely — a wedged SyncTalk call would otherwise starve
            # heartbeat pings and freeze mic capture. asyncio.wait does not re-cancel
            # on timeout, so on timeout the task keeps unwinding in the background.
            done, pending = await asyncio.wait({task}, timeout=_INTERRUPT_TEARDOWN_TIMEOUT_S)
            if pending:
                log_event(log, "pipeline_cancel_slow", session_id=self.session_id, request_id=self.active_turn_id)
                # Drain in the background so the exception is retrieved (no warnings)
                # and SyncTalk finishes aborting without holding up this loop. Keep a
                # strong ref (asyncio only weak-refs tasks) so the GC can't drop it mid-await.
                drain = asyncio.create_task(self._drain_cancelled(task))
                self._drain_tasks.add(drain)
                drain.add_done_callback(self._drain_tasks.discard)
            else:
                exc = task.exception() if not task.cancelled() else None
                if exc is not None:
                    log.error("pipeline cancellation failed", exc_info=exc)
            if send_event:
                await self.writer.send({"type": "interrupted", "session_id": self.session_id})

    @staticmethod
    async def _drain_cancelled(task: "asyncio.Task") -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def reset(self, *, reopen_transports: bool = True, reason: str = "reset") -> None:
        self.history.clear()
        self.barge_in_triggered = False
        self.ignore_audio_until = 0.0
        self.realtime_stt_started_at = None
        self.realtime_stt_ready_at = None
        self.realtime_stt_audio_started_at = None
        self._last_query_signature = ""
        self._last_query_at = 0.0
        self._reset_interrupt_state()
        await self.interrupt(send_event=False)
        await self._close_realtime_stt(reason=reason)
        await self._close_realtime_tts(reason=reason, recreate=reopen_transports, prewarm=reopen_transports)
        if reopen_transports:
            self.prewarm_realtime_stt(force=True)
        log_event(log, "session_reset", session_id=self.session_id)

    async def handle_message(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            await self.writer.send({"type": "error", "text": "Invalid message payload"})
            return
        msg_type = payload.get("type")
        # Liveness ping from the client heartbeat — reply immediately and skip the
        # receive log so the ~5s cadence doesn't spam the event stream. Pings are
        # deliberately NOT counted as activity by the session gate (an abandoned tab
        # keeps pinging); only the real messages below refresh the idle timer.
        if msg_type == "ping":
            with contextlib.suppress(ClientClosedError):
                await self.writer.send({"type": "pong", "t": payload.get("t")})
            return
        GATE.touch(self.session_id)
        log_event(ws_log, "ws_receive", session_id=self.session_id, request_id=self.active_turn_id, **_summarize_client_payload(payload))
        if msg_type == "audio_chunk":
            chunk = await self._decode_audio_payload(payload)
            if chunk is None:
                return
            await self.handle_audio_chunk(chunk)
        elif msg_type == "audio_end":
            await self.handle_audio_end()
        elif msg_type == "prepare_stt":
            await self.ensure_realtime_stt(status=True)
        elif msg_type == "close_stt":
            log_event(log, "stt_close_ignored_persistent", session_id=self.session_id)
        elif msg_type == "text":
            text = payload.get("text", "")
            if not isinstance(text, str):
                await self.writer.send({"type": "error", "text": "Invalid text payload"})
                return
            await self.handle_text(text)
        elif msg_type == "interrupt":
            await self.interrupt(send_event=False)
        elif msg_type == "reset":
            await self.reset()
        elif msg_type == "client_first_render":
            chunk = payload.get("chunk")
            self.on_client_first_render(
                payload.get("turn_id"),
                int(chunk) if isinstance(chunk, int) or str(chunk).isdigit() else None,
            )
        elif msg_type == "client_log":
            level_name = str(payload.get("level") or "info").lower()
            level = logging.ERROR if level_name == "error" else logging.WARNING if level_name == "warning" else logging.INFO
            log_event(
                logging.getLogger("backend.client"),
                "client_log",
                session_id=self.session_id,
                request_id=str(payload.get("turn_id") or self.active_turn_id or ""),
                level=level,
                source=str(payload.get("source") or "frontend"),
                message=str(payload.get("message") or ""),
                detail=json.dumps(payload.get("detail"), ensure_ascii=False, default=str)[:1200],
            )
        else:
            await self.writer.send({"type": "error", "text": f"Unsupported message type: {msg_type}"})

    async def _decode_audio_payload(self, payload: dict) -> bytes | None:
        data = payload.get("data")
        if not isinstance(data, str) or not data:
            log_event(ws_log, "ws_audio_decode_failed", session_id=self.session_id, reason="missing_audio_data")
            await self.writer.send({"type": "error", "text": "Missing audio data"})
            return None
        try:
            return base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            log_event(ws_log, "ws_audio_decode_failed", session_id=self.session_id, reason="invalid_base64", data_chars=len(data))
            await self.writer.send({"type": "error", "text": "Invalid audio data"})
            return None

    async def handle_audio_chunk(self, chunk: bytes) -> None:
        if perf_counter() < self.ignore_audio_until:
            log_event(log, "audio_chunk_ignored", session_id=self.session_id, reason="ignore_window", bytes=len(chunk))
            return
        if not looks_like_pcm16_chunk(chunk):
            self.ignore_audio_until = max(self.ignore_audio_until, perf_counter() + 0.4)
            log_event(log, "audio_chunk_ignored", session_id=self.session_id, reason="invalid_pcm16", bytes=len(chunk))
            return
        try:
            if self.realtime_stt is None or self.realtime_stt.closed:
                self.barge_in_triggered = False
            session = await self.ensure_realtime_stt(status=True)
            if session is None:
                with contextlib.suppress(ClientClosedError):
                    await self.writer.send({"type": "transcript_empty"})
                return
            if not session.has_audio:
                self.realtime_stt_audio_started_at = perf_counter()
                log_event(log, "stt_audio_started", session_id=self.session_id)
            await session.send_audio(chunk)
            if session.closed:
                await self._close_realtime_stt(session, reason="provider_closed")
                with contextlib.suppress(ClientClosedError):
                    await self.writer.send({"type": "transcript_empty"})
        except ClientClosedError:
            await self._close_realtime_stt(reason="client_closed")
            raise
        except Exception:
            log.exception("realtime stt failed")
            await self._close_realtime_stt(reason="send_error")
            with contextlib.suppress(ClientClosedError):
                await self.writer.send({"type": "transcript_empty"})

    async def on_realtime_final(self, text: str, language: str) -> None:
        started = self.realtime_stt_audio_started_at or self.realtime_stt_started_at or perf_counter()
        metrics = TurnMetrics(
            started_at=started,
            mode="audio",
            stt_started_at=started,
            stt_done_at=perf_counter(),
        )
        active_session = self.realtime_stt
        self.ignore_audio_until = perf_counter() + 1.0
        if active_session is not None and not active_session.closed:
            active_session.reset_utterance_state()
            self.realtime_stt_audio_started_at = None
            log_event(log, "stt_realtime_reused_after_final", session_id=self.session_id)
        log_event(
            log,
            "stt_final",
            session_id=self.session_id,
            latency_ms=(metrics.stt_done_at - started) * 1000,
            language=language,
            chars=len(text),
            text_preview=preview_text(text, 240),
        )
        await self.process_final_transcript(text, language, metrics)

    async def handle_audio_end(self) -> None:
        if perf_counter() < self.ignore_audio_until:
            log_event(log, "audio_end_ignored", session_id=self.session_id, reason="ignore_window")
            return
        if self.realtime_stt is not None and self.realtime_stt.closed:
            await self._close_realtime_stt(self.realtime_stt, reason="provider_closed")
        metric_started = (
            self.realtime_stt_audio_started_at
            if self.realtime_stt is not None and self.realtime_stt.has_audio and self.realtime_stt_audio_started_at is not None
            else perf_counter()
        )
        metrics = TurnMetrics(started_at=metric_started, mode="audio", stt_started_at=metric_started)
        try:
            if self.realtime_stt is not None and self.realtime_stt.has_audio and not self.realtime_stt.closed:
                active_session = self.realtime_stt
                if not active_session.claim_finalization():
                    log_event(log, "audio_end_ignored", session_id=self.session_id, reason="duplicate_finalization")
                    return
                text, language = await active_session.wait_committed(SONIOX_STT_ENDPOINT_WAIT_S)
                if not text:
                    await active_session.send_silence(300)
                    text, language = await active_session.finalize(close_after=False)
                if not active_session.closed:
                    active_session.reset_utterance_state()
                    self.realtime_stt_audio_started_at = None
                    log_event(log, "stt_realtime_reused_after_audio_end", session_id=self.session_id)
            else:
                log_event(log, "audio_end_ignored", session_id=self.session_id, reason="no_active_audio")
                self.prewarm_realtime_stt(force=True)
                await self.writer.send({"type": "transcript_empty"})
                return
        except Exception:
            log.exception("realtime STT finalization failed")
            await self._close_realtime_stt(reason="transcription_error")
            self.prewarm_realtime_stt(force=True)
            if self.pipeline_task is not None and not self.pipeline_task.done():
                log.info("suppressing late realtime STT finalization failure during active response")
                return
            await self.writer.send({"type": "transcript_empty"})
            return
        metrics.stt_done_at = perf_counter()
        log_event(
            log,
            "stt_final",
            session_id=self.session_id,
            latency_ms=(metrics.stt_done_at - metric_started) * 1000,
            language=language,
            chars=len(text),
            text_preview=preview_text(text, 240),
        )
        await self.process_final_transcript(text, language, metrics)

    async def process_final_transcript(self, text: str, language: str, metrics: TurnMetrics) -> None:
        provider_language = language
        provider_language_norm = supported_lang_or_none(language)
        text = dedupe_repeated_transcript(text)
        detected_language = provider_language_norm or detect_supported_text_language(text)
        if not text or not transcript_has_meaningful_speech(text):
            log_event(log, "transcript_rejected", session_id=self.session_id, reason="empty_or_not_speech", provider_lang=provider_language, text=text[:120])
            await self.writer.send({"type": "transcript_empty"})
            return

        query_signature = _normalize_query_signature(text)
        if query_signature and query_signature == self._last_query_signature and (perf_counter() - self._last_query_at) < _DUP_QUERY_WINDOW_S:
            log_event(log, "transcript_rejected", session_id=self.session_id, reason="duplicate", text=text[:120])
            await self.writer.send({"type": "transcript_empty", "text": "duplicate query ignored"})
            return
        if detected_language is None:
            fallback_lang = detect_text_language(text)
            log_event(log, "transcript_rejected", session_id=self.session_id, reason="unsupported_language", text=text[:120])
            await self.writer.send({"type": "error", "text": UNSUPPORTED_LANGUAGE_MESSAGE[fallback_lang]})
            return
        language = normalize_lang(detected_language)
        if is_stop_command(text):
            await self.interrupt(send_event=False)
            self.ignore_audio_until = perf_counter() + 1.5
            await self.writer.send({"type": "stop_confirmed"})
            return
        pipeline_active = self.pipeline_task is not None and not self.pipeline_task.done()
        turn_candidate = _is_final_turn_candidate(text, language, require_query_signal=pipeline_active)
        self._reset_interrupt_state()
        if pipeline_active and not turn_candidate:
            log.info("ignored non-query transcript during active response: %r", text[:100])
            self.ignore_audio_until = perf_counter() + 0.8
            return
        if not turn_candidate:
            log.info(
                "transcript rejected: non_query text=%r language=%s provider_lang=%r",
                text[:120],
                language,
                provider_language,
            )
            await self.writer.send({"type": "transcript_empty"})
            return
        self._last_query_signature = query_signature
        self._last_query_at = perf_counter()
        if pipeline_active:
            await self.interrupt(send_event=True)
        self._reset_interrupt_state()
        self.barge_in_triggered = False
        log_event(
            log,
            "transcript_final_accepted",
            session_id=self.session_id,
            language=language,
            interrupted=pipeline_active,
            chars=len(text),
            text_preview=preview_text(text, 240),
        )
        await self.writer.send({"type": "transcript", "session_id": self.session_id, "text": text})
        self.pipeline_task = asyncio.create_task(self.run_query(text, language, metrics, interrupted_input=pipeline_active))

    async def handle_text(self, text: str) -> None:
        raw_text = text.strip()
        detected_language = detect_supported_text_language(raw_text)
        text = raw_text
        if not text:
            return
        detected_language = detected_language or detect_supported_text_language(text)
        if detected_language is None:
            fallback_lang = detect_text_language(text)
            await self.writer.send({"type": "error", "text": UNSUPPORTED_LANGUAGE_MESSAGE[fallback_lang]})
            return

        query_signature = _normalize_query_signature(text)
        if query_signature and query_signature == self._last_query_signature and (perf_counter() - self._last_query_at) < _DUP_QUERY_WINDOW_S:
            log_event(log, "text_query_rejected", session_id=self.session_id, reason="duplicate", text=text[:120])
            return

        interrupted_input = self.pipeline_task is not None and not self.pipeline_task.done()
        self._reset_interrupt_state()
        self.barge_in_triggered = False
        if interrupted_input:
            await self.interrupt(send_event=True)

        self._last_query_signature = query_signature
        self._last_query_at = perf_counter()
        log_event(
            log,
            "text_query_accepted",
            session_id=self.session_id,
            language=detected_language,
            interrupted=interrupted_input,
            chars=len(text),
            text_preview=preview_text(text, 240),
        )
        self.pipeline_task = asyncio.create_task(
            self.run_query(text, detected_language, TurnMetrics(started_at=perf_counter(), mode="text"), interrupted_input=interrupted_input)
        )

    async def run_query(self, query: str, language: str, metrics: TurnMetrics, interrupted_input: bool = False) -> None:
        turn_id = uuid4().hex
        current_task = asyncio.current_task()
        stream: ResponseStream | None = None
        self.active_metrics = metrics
        self.active_turn_id = turn_id
        self.writer.set_active_turn(turn_id)
        raw_answer = ""
        json_payload: dict[str, object] | None = None
        answer_payload: dict[str, str] | None = None
        race_result: AnswerRaceResult | None = None
        plan = SimpleNamespace(answer_language=language)
        chunks: list[dict] = []
        policy_language: str | None = None

        async def ensure_response_stream(
            plan_update: object | None = None,
            chunks_update: list[dict] | None = None,
        ) -> ResponseStream:
            nonlocal stream, plan, chunks, language, policy_language
            if plan_update is not None:
                plan = plan_update
                language = normalize_lang(getattr(plan, "answer_language", language) or language)
            if chunks_update is not None:
                chunks = chunks_update
            if policy_language != language:
                await self.writer.send({"type": "policy_state", "turn_id": turn_id, "answer_language": language})
                policy_language = language
            if stream is None:
                stream = ResponseStream(
                    self.writer,
                    self.tts,
                    self.synctalk,
                    splitter=_build_sentence_splitter(language),
                    plan=plan,
                    turn_started_at=metrics.started_at,
                    turn_id=turn_id,
                    query_text=query,
                    chunks=chunks,
                )
            else:
                stream.update_context(plan=plan, chunks=chunks)
            return stream

        async def on_gemini_context_ready(plan_update: object, chunks_update: list[dict]) -> None:
            if metrics.plan_done_at is None:
                metrics.plan_done_at = perf_counter()
            await ensure_response_stream(plan_update, chunks_update)

        spoken_streaming_active = False

        async def on_spoken_delta(delta: str) -> None:
            nonlocal spoken_streaming_active
            if stream is None or not delta:
                return
            spoken_streaming_active = True
            await stream.emit_spoken_text(delta)

        try:
            log_event(log, "pipeline_start", session_id=self.session_id, request_id=turn_id, mode=metrics.mode, language=language, interrupted=interrupted_input, query=query[:160])
            self.history.append({"role": "user", "content": query})
            self.history[:] = self.history[-(MAX_HISTORY_TURNS * 2):]
            history_before_current = self.history[:-1]
            await self.writer.send({"type": "response_start", "turn_id": turn_id})
            log_event(log, "llm_start", session_id=self.session_id, request_id=turn_id)
            direct_reply = _prebuilt_chitchat_answer(query, language) or smalltalk_reply(query, language)
            if direct_reply:
                metrics.plan_done_at = perf_counter()
                stream = await ensure_response_stream()
                raw_answer = json.dumps({"spoken": direct_reply, "chat": direct_reply}, ensure_ascii=False)
                metrics.llm_done_at = perf_counter()
                log_event(log, "llm_direct_reply", session_id=self.session_id, request_id=turn_id, latency_ms=(metrics.llm_done_at - metrics.started_at) * 1000)
            else:
                await self.writer.send({"type": "status", "turn_id": turn_id, "text": "Racing answer sources..."})
                race_result = await run_answer_race(
                    query,
                    language,
                    history_before_current,
                    self.conversation_memory,
                    on_gemini_context_ready=on_gemini_context_ready,
                    on_spoken_delta=on_spoken_delta,
                )
                metrics.race_timings.update(race_result.timings)
                winner = race_result.winner
                plan = winner.plan or SimpleNamespace(answer_language=language)
                chunks = winner.chunks
                if metrics.plan_done_at is None:
                    metrics.plan_done_at = perf_counter()
                language = normalize_lang(getattr(plan, "answer_language", language) or language)
                stream = await ensure_response_stream(plan, chunks)
                raw_answer = winner.raw_answer
                metrics.llm_done_at = perf_counter()
                log_event(log, "llm_done", session_id=self.session_id, request_id=turn_id, latency_ms=(metrics.llm_done_at - (metrics.plan_done_at or metrics.started_at)) * 1000, source=getattr(race_result.winner, "source", "unknown") if race_result else "unknown")

            json_payload = _extract_json_any(raw_answer)
            if isinstance(json_payload, dict):
                answer_payload = _coerce_spoken_chat_payload(json_payload, language)
            elif raw_answer.strip():
                answer_payload = _coerce_spoken_chat_payload(
                    {"spoken": raw_answer, "chat": raw_answer},
                    language,
                )

            if metrics.llm_done_at is None:
                metrics.llm_done_at = perf_counter()

            metrics.spoken_ready_at = perf_counter()
            log_event(log, "spoken_ready", session_id=self.session_id, request_id=turn_id, latency_ms=(metrics.spoken_ready_at - metrics.started_at) * 1000)

            metrics.postprocess_done_at = perf_counter()
            await self.wait_realtime_tts_ready()
            if answer_payload is None:
                answer_payload = _coerce_spoken_chat_payload({}, language)
            spoken = await _normalize_spoken_for_tts(
                answer_payload.get("spoken", ""),
                language,
                trim_for_latency=False,
            )
            chat = str(answer_payload.get("chat") or spoken).strip() or spoken
            _save_response_debug(turn_id, spoken, chat)
            # If ANY spoken text was already streamed to TTS via on_spoken_delta, do not
            # re-emit the full winner text — that would double the audio. Key off the fact
            # that streaming happened, NOT the winner's source: Gemini streams its spoken
            # field as it generates, but a faster source (FAQ/cache) or the fallback can
            # still win the race afterwards. Re-emitting then layered the winner's text on
            # top of the already-spoken Gemini fragment (garbled/doubled audio).
            winner_already_streamed = spoken_streaming_active
            if winner_already_streamed and race_result is not None and race_result.winner.source not in ("gemini_local_rag", "external_internal_rag"):
                # Rare race: a non-streaming source won after Gemini had already streamed a
                # fragment. We keep the streamed fragment (can't un-speak it) and skip the
                # winner's text to avoid doubling — log it so it's visible if it recurs.
                log_event(log, "spoken_stream_winner_mismatch", session_id=self.session_id, request_id=turn_id, winner_source=race_result.winner.source, level=logging.WARNING)
            # FAQ video cache: on a confident FAQ fast-path win, if a prebuilt MP4 exists for
            # this exact spoken text, send it instead of rendering TTS->SyncTalk live. The MP4
            # carries audio + frames (hardware-decoded on the client), so we skip emitting the
            # spoken text to the avatar pipeline. Chat text still flows so the panel updates.
            # Cache miss -> falls through to the normal live render. (FAQ never streams via
            # on_spoken_delta, so winner_already_streamed is False on a real FAQ win.)
            faq_video_served = False
            if (
                FAQ_VIDEO_ENABLED
                and not winner_already_streamed
                and race_result is not None
                and race_result.winner.source == "faq"
            ):
                faq_url = _lookup_faq_video(spoken, language)
                if faq_url:
                    await self.writer.send({"type": "faq_video", "url": faq_url, "turn_id": turn_id})
                    log_event(log, "faq_video_served", session_id=self.session_id, request_id=turn_id, url=faq_url)
                    faq_video_served = True
            if not winner_already_streamed and not faq_video_served:
                await stream.emit_spoken_text(spoken)
            if chat:
                await stream.emit_chat_text(chat)
            await stream.flush()
            payload = stream.build_answer_payload()
            payload["answer_id"] = turn_id
            payload["spoken"] = spoken
            payload["chat"] = chat
            winner_source = None
            winner_confidence = None
            winner_score = None
            if race_result is not None:
                winner = race_result.winner
                winner_source = winner.source
                winner_confidence = winner.confidence
                winner_score = winner.score
                payload["winner_source"] = winner_source
                payload["winner_confidence"] = winner_confidence
                payload["winner_score"] = winner_score
                log_event(
                    log,
                    "answer_source_selected",
                    session_id=self.session_id,
                    request_id=turn_id,
                    source=winner.source,
                    confidence=winner.confidence,
                    score=winner.score,
                )
            metrics.payload_done_at = perf_counter()
            log_event(
                log,
                "answer_final",
                session_id=self.session_id,
                request_id=turn_id,
                latency_ms=(metrics.payload_done_at - metrics.started_at) * 1000,
                source=winner_source,
                confidence=winner_confidence,
                score=winner_score,
                spoken_chars=len(spoken),
                spoken_preview=preview_text(spoken, 240),
                chat_chars=len(chat),
                chat_preview=preview_text(chat, 400),
            )
            await self.writer.send({"type": "answer_payload", "turn_id": turn_id, **payload})
            log_event(log, "answer_payload_sent", session_id=self.session_id, request_id=turn_id, latency_ms=(metrics.payload_done_at - metrics.started_at) * 1000)
            if not (chat or spoken):
                await self.writer.send({"type": "error", "text": "Empty response"})
                return
            self.history.append({"role": "assistant", "content": chat or spoken})
            self.history[:] = self.history[-(MAX_HISTORY_TURNS * 2):]
            self.conversation_memory = await asyncio.to_thread(
                update_conversation_memory,
                self.conversation_memory,
                query,
                chat or spoken,
                chunks,
            )
            await self.writer.send({"type": "status", "text": "Generating speech...", "turn_id": turn_id})
            await stream.wait_all()
            metrics.done_at = perf_counter()
            latency_ms = metrics.as_ms()
            log_event(log, "pipeline_done", session_id=self.session_id, request_id=turn_id, latency_ms=latency_ms.get("total"), metrics=json.dumps(latency_ms, default=str))
            await self.writer.send({"type": "done", "chunks": stream.chunk_count, "turn_id": turn_id, "latency_ms": latency_ms})
        except asyncio.CancelledError:
            if stream is not None:
                await stream.cancel_all()
            if self.history and self.history[-1] == {"role": "user", "content": query}:
                self.history.pop()
            raise
        except ClientClosedError:
            if stream is not None:
                await stream.cancel_all()
            if self.history and self.history[-1] == {"role": "user", "content": query}:
                self.history.pop()
        except Exception as exc:
            log.exception("query pipeline failed")
            log_event(log, "pipeline_failed", session_id=self.session_id, request_id=turn_id, level=logging.ERROR, error=exc)
            if stream is not None:
                await stream.cancel_all()
            if self.history and self.history[-1] == {"role": "user", "content": query}:
                self.history.pop()
            await self.writer.send({"type": "error", "text": "Response generation failed", "turn_id": turn_id})
        finally:
            # Only clear shared turn state if it still points at THIS task — a background
            # drain of a slow cancelled turn must not null out a newer turn's state.
            if self.pipeline_task is current_task:
                self.pipeline_task = None
                self.active_metrics = None
                self.active_turn_id = None
            self.writer.clear_active_turn(turn_id)
