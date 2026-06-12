from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from types import SimpleNamespace
from dataclasses import dataclass, field
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .language import (
    dedupe_repeated_transcript,
    detect_supported_text_language,
    detect_text_language,
    is_stop_command,
    is_noise_utterance,
    normalize_lang,
    smalltalk_reply,
    supported_lang_or_none,
    transcript_is_new_query_candidate,
    UNSUPPORTED_LANGUAGE_MESSAGE,
)
from .settings import (
    APP_HOST,
    APP_PORT,
    INTRO_AVATAR_CACHE_KEY,
    INTRO_AUDIO_CACHE_DIR,
    MAX_HISTORY_TURNS,
    SONIOX_STT_KEEPALIVE_INTERVAL_S,
    SONIOX_STT_ENDPOINT_WAIT_S,
    SONIOX_STT_PRECONNECT,
)
from .stt import SonioxBatchSTT, SonioxRealtimeSession, looks_like_pcm16_chunk
from .synctalk import SyncTalkClient
from .tts import ElevenTTS
from .ws_writer import ClientClosedError, WsWriter
from .answer_race import AnswerRaceResult, clear_answer_caches, run_answer_race
from .abbreviations import normalize_transcript_abbreviations
from .original_backend import (
    _prebuilt_chitchat_answer,
    update_conversation_memory,
    wrap_answer_for_voice_and_chat,
)

from backend.logging_config import configure_logging, log_event
from backend.answer_format import (
    _enforce_prompt_details,
    build_control_payload as _build_control_payload,
    build_sentence_splitter as _build_sentence_splitter,
    build_tts_chunks as _build_tts_chunks,
    coerce_prompt_contract_payload as _coerce_prompt_contract_payload,
    details_from_spoken as _details_from_spoken,
    extract_answer_from_json as _extract_answer_from_json,
    extract_json_any as _extract_json_any,
    is_final_turn_candidate as _is_final_turn_candidate,
    json_to_markdown_details as _json_to_markdown_details,
    limit_answer_details as _limit_answer_details,
    limit_text_for_answer_voice as _limit_text_for_answer_voice,
    normalize_followup_questions as _normalize_followup_questions,
    normalize_query_signature as _normalize_query_signature,
    normalize_spoken_for_tts as _normalize_spoken_for_tts,
    normalize_tagged_answer as _normalize_tagged_answer,
    normalize_tts_chunk_for_language as _normalize_tts_chunk_for_language,
    normalize_tts_chunks as _normalize_tts_chunks,
    remaining_spoken_suffix as _remaining_spoken_suffix,
    signature_for_interruption as _signature_for_interruption,
    tagged_answer_with_full_details_voice as _tagged_answer_with_full_details_voice,
)
from backend.intro import (
    INTRO_CACHED_FRAME_BATCH as _INTRO_CACHED_FRAME_BATCH,
    INTRO_FRAME_HEADROOM as _INTRO_FRAME_HEADROOM,
    IntroBlock,
    canonical_intro_key as _canonical_intro_key,
    clear_intro_token_in_progress as _clear_intro_token_in_progress,
    ensure_intro_audio_file as _ensure_intro_audio_file,
    intro_frame_cache_info as _intro_frame_cache_info,
    intro_frame_range_path as _intro_frame_range_path,
    intro_frame_signature as _intro_frame_signature,
    intro_token_in_progress as _intro_token_in_progress,
    intro_token_seen as _intro_token_seen,
    load_intro_blocks as _load_intro_blocks,
    load_intro_frames_from_cache as _load_intro_frames_from_cache,
    mark_intro_token_in_progress as _mark_intro_token_in_progress,
    mark_intro_token_played as _mark_intro_token_played,
    prebuild_intro_audio_cache as pipeline_prebuild_intro_audio_cache,
    safe_cache_key as _safe_cache_key,
    save_intro_frames_to_cache as _save_intro_frames_to_cache,
)
from backend.response_stream import ResponseStream
from backend.startup import (
    log_background_task_error as _log_background_task_error,
    shutdown_keepwarm as pipeline_shutdown_keepwarm,
    startup_prewarm as pipeline_startup_prewarm,
)

configure_logging(reset=False)
log = logging.getLogger(__name__)

app = FastAPI()


@app.on_event("startup")
async def _prebuild_intro_audio_cache() -> None:
    await pipeline_prebuild_intro_audio_cache()


@app.on_event("startup")
async def startup_prewarm() -> None:
    await pipeline_startup_prewarm()


@app.on_event("shutdown")
async def shutdown_keepwarm() -> None:
    await pipeline_shutdown_keepwarm()


_PARTIAL_INTERRUPT_WINDOW_S = 1.2
_PARTIAL_INTERRUPT_HITS = 2
_INTERRUPT_COOLDOWN_S = 1.0
_DUPLICATE_FINAL_AUDIO_IGNORE_S = 6.0
_DUP_QUERY_WINDOW_S = 1.5


@dataclass
class TurnMetrics:
    started_at: float
    mode: str
    stt_started_at: float | None = None
    stt_done_at: float | None = None
    plan_done_at: float | None = None
    llm_done_at: float | None = None
    spoken_ready_at: float | None = None
    postprocess_done_at: float | None = None
    payload_done_at: float | None = None
    first_audio_at: float | None = None
    first_frame_at: float | None = None
    client_first_render_at: float | None = None
    done_at: float | None = None
    race_timings: dict[str, object] = field(default_factory=dict)

    def as_ms(self) -> dict[str, int]:
        def delta(point: float | None) -> int | None:
            if point is None:
                return None
            return int((point - self.started_at) * 1000)

        payload = {
            "stt": None if self.stt_started_at is None or self.stt_done_at is None else int((self.stt_done_at - self.stt_started_at) * 1000),
            "plan_retrieve": delta(self.plan_done_at),
            "llm_generate": None if self.plan_done_at is None or self.llm_done_at is None else int((self.llm_done_at - self.plan_done_at) * 1000),
            "spoken_ready": delta(self.spoken_ready_at),
            "spoken_postprocess": None if self.llm_done_at is None or self.postprocess_done_at is None else int((self.postprocess_done_at - self.llm_done_at) * 1000),
            "payload_ready": delta(self.payload_done_at),
            "first_audio": delta(self.first_audio_at),
            "first_frame": delta(self.first_frame_at),
            "client_first_render": delta(self.client_first_render_at),
            "total": delta(self.done_at),
        }
        for key, value in self.race_timings.items():
            if isinstance(value, (int, float, str)):
                payload[key] = value
        return {key: value for key, value in payload.items() if value is not None}


@dataclass
class ClientSession:
    websocket: WebSocket
    writer: WsWriter
    batch_stt: SonioxBatchSTT
    tts: ElevenTTS
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
    active_metrics: TurnMetrics | None = None
    active_turn_id: str | None = None
    ignore_audio_until: float = 0.0
    ignore_final_audio_until: float = 0.0
    barge_in_triggered: bool = False
    _last_query_signature: str = ""
    _last_query_at: float = 0.0
    _interrupt_signature: str = ""
    _interrupt_hits: int = 0
    _interrupt_started_at: float = 0.0
    _interrupt_last_at: float = 0.0

    def _reset_interrupt_state(self) -> None:
        self._interrupt_signature = ""
        self._interrupt_hits = 0
        self._interrupt_started_at = 0.0
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
                self.batch_stt,
                on_meaningful_partial=self.on_meaningful_partial,
                on_final_utterance=self.on_realtime_final,
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
            self.tts = ElevenTTS()
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
        _mark_intro_token_played(intro_token)
        _mark_intro_token_in_progress(intro_token or "")
        self.pipeline_task = asyncio.create_task(self.run_intro(intro_blocks, intro_token=intro_token))
        self.pipeline_task.add_done_callback(_log_background_task_error)
        log_event(log, "intro_started", session_id=self.session_id)
        return True

    async def _ensure_intro_audio(self, block: IntroBlock) -> bytes:
        return await _ensure_intro_audio_file(self.tts, block)

    async def _play_intro_block(self, block: IntroBlock, index: int, turn_id: str) -> None:
        audio_wav = await self._ensure_intro_audio(block)
        audio_b64 = base64.b64encode(audio_wav).decode("ascii")

        async def send_audio_ready() -> None:
            await self.writer.send(
                {
                    "type": "audio_ready",
                    "data": audio_b64,
                    "chunk": index,
                    "source_chunk": index,
                    "frame_stride": 1,
                    "streaming": True,
                    "cached": True,
                    "turn_id": turn_id,
                }
            )

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
            await send_audio_ready()
            return
        cached_frames = _load_intro_frames_from_cache(block)
        if cached_frames:
            headroom = min(_INTRO_FRAME_HEADROOM, len(cached_frames))
            for frame in cached_frames[:headroom]:
                await self.writer.send({"type": "frame", "data": frame, "chunk": index, "turn_id": turn_id})
            await send_audio_ready()
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
            for index, block in enumerate(intro_blocks):
                await self.writer.send({"type": "status", "turn_id": turn_id, "text": f"Streaming cached intro block {index + 1}/{len(intro_blocks)}: {block.key}"})
                await self._play_intro_block(block, index, turn_id)
            details = _details_from_spoken(full_text, {}, 0)
            payload = {
                "answer_id": turn_id,
                "spoken": full_text,
                "details": details,
                "key_points": [],
                "follow_up_questions": [],
            }
            payload["answer_contract"] = payload["details"]
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
            self.ignore_audio_until = perf_counter() + 1.5
            self.ignore_final_audio_until = perf_counter() + _DUPLICATE_FINAL_AUDIO_IGNORE_S
            self._reset_interrupt_state()
            return
        now = perf_counter()
        if now - self._interrupt_last_at < _INTERRUPT_COOLDOWN_S and self._interrupt_last_at > 0:
            return

        signature = _signature_for_interruption(text)
        if not signature:
            self._reset_interrupt_state()
            return

        if signature == self._interrupt_signature and now - self._interrupt_started_at <= _PARTIAL_INTERRUPT_WINDOW_S:
            self._interrupt_hits += 1
        else:
            self._interrupt_signature = signature
            self._interrupt_started_at = now
            self._interrupt_hits = 1

        self._interrupt_last_at = now

        if self._interrupt_hits < _PARTIAL_INTERRUPT_HITS:
            return

        log_event(log, "barge_in_partial", session_id=self.session_id, request_id=self.active_turn_id, partial=text[:80])
        self.barge_in_triggered = True
        await self.interrupt(send_event=True)
        self._reset_interrupt_state()

    async def close(self) -> None:
        await self._close_realtime_stt(reason="session_close")
        if self.pipeline_task is not None:
            self.pipeline_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.pipeline_task
        await self.batch_stt.close()
        await self._close_realtime_tts(reason="session_close")
        await self.synctalk.close()

    async def interrupt(self, send_event: bool) -> None:
        self.writer.clear_active_turn()
        if self.pipeline_task is not None and not self.pipeline_task.done():
            log_event(log, "pipeline_cancel_requested", session_id=self.session_id, request_id=self.active_turn_id, send_event=send_event)
            self.pipeline_task.cancel()
            try:
                await self.pipeline_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("pipeline cancellation failed")
            if send_event:
                await self.writer.send({"type": "interrupted", "session_id": self.session_id})
        self.pipeline_task = None

    async def reset(self, *, reopen_transports: bool = True, reason: str = "reset") -> None:
        self.history.clear()
        self.barge_in_triggered = False
        self.ignore_audio_until = 0.0
        self.ignore_final_audio_until = 0.0
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
        msg_type = payload.get("type")
        log_event(log, "ws_message", session_id=self.session_id, request_id=self.active_turn_id, message_type=msg_type)
        if msg_type == "audio_chunk":
            await self.handle_audio_chunk(base64.b64decode(payload["data"]))
        elif msg_type == "audio":
            await self.handle_audio(base64.b64decode(payload["data"]))
        elif msg_type == "prepare_stt":
            await self.ensure_realtime_stt(status=True)
        elif msg_type == "close_stt":
            log_event(log, "stt_close_ignored_persistent", session_id=self.session_id)
        elif msg_type == "text":
            await self.handle_text(payload.get("text", ""))
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

    async def handle_audio_chunk(self, chunk: bytes) -> None:
        if perf_counter() < self.ignore_audio_until:
            return
        if not looks_like_pcm16_chunk(chunk):
            self.ignore_audio_until = max(self.ignore_audio_until, perf_counter() + 0.4)
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
        self.ignore_final_audio_until = perf_counter() + _DUPLICATE_FINAL_AUDIO_IGNORE_S
        if active_session is not None and not active_session.closed:
            active_session.reset_utterance_state()
            self.realtime_stt_audio_started_at = None
            log_event(log, "stt_realtime_reused_after_final", session_id=self.session_id)
        log_event(log, "stt_final", session_id=self.session_id, latency_ms=(metrics.stt_done_at - started) * 1000, language=language, chars=len(text))
        await self.process_final_transcript(text, language, metrics)

    async def handle_audio(self, audio_bytes: bytes) -> None:
        if perf_counter() < self.ignore_final_audio_until:
            log.info("dropping duplicate client final audio after Soniox endpoint")
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
                    log.info("dropping duplicate client final audio while Soniox endpoint is in flight")
                    return
                text, language = await active_session.wait_committed_with_keepalive(
                    SONIOX_STT_ENDPOINT_WAIT_S,
                    interval_s=0.5,
                )
                if not text:
                    await active_session.send_silence(200)
                    text, language = await active_session.finalize(audio_bytes, allow_fallback=True, close_after=False)
                if not active_session.closed:
                    active_session.reset_utterance_state()
                    self.realtime_stt_audio_started_at = None
                    log_event(log, "stt_realtime_reused_after_final_audio", session_id=self.session_id)
                self.ignore_final_audio_until = perf_counter() + _DUPLICATE_FINAL_AUDIO_IGNORE_S
            else:
                text, language = await self.batch_stt.transcribe(audio_bytes)
        except Exception:
            log.exception("audio transcription failed")
            await self._close_realtime_stt(reason="transcription_error")
            self.prewarm_realtime_stt(force=True)
            if self.pipeline_task is not None and not self.pipeline_task.done():
                log.info("suppressing late audio transcription failure during active response")
                self.ignore_final_audio_until = perf_counter() + _DUPLICATE_FINAL_AUDIO_IGNORE_S
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
        )
        await self.process_final_transcript(text, language, metrics)

    async def process_final_transcript(self, text: str, language: str, metrics: TurnMetrics) -> None:
        provider_language = language
        provider_language_norm = supported_lang_or_none(language)
        text = normalize_transcript_abbreviations(dedupe_repeated_transcript(text), provider_language_norm)
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
        log_event(log, "transcript_final_accepted", session_id=self.session_id, language=language, interrupted=pipeline_active, chars=len(text))
        await self.writer.send({"type": "transcript", "session_id": self.session_id, "text": text})
        self.pipeline_task = asyncio.create_task(self.run_query(text, language, metrics, interrupted_input=pipeline_active))

    async def handle_text(self, text: str) -> None:
        raw_text = text.strip()
        detected_language = detect_supported_text_language(raw_text)
        text = normalize_transcript_abbreviations(raw_text, detected_language)
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
        if interrupted_input:
            await self.interrupt(send_event=True)

        self._last_query_signature = query_signature
        self._last_query_at = perf_counter()
        log_event(log, "text_query_accepted", session_id=self.session_id, language=detected_language, interrupted=interrupted_input, chars=len(text))
        self.pipeline_task = asyncio.create_task(
            self.run_query(text, detected_language, TurnMetrics(started_at=perf_counter(), mode="text"), interrupted_input=interrupted_input)
        )

    async def run_query(self, query: str, language: str, metrics: TurnMetrics, interrupted_input: bool = False) -> None:
        turn_id = uuid4().hex
        stream: ResponseStream | None = None
        self.active_metrics = metrics
        self.active_turn_id = turn_id
        self.writer.set_active_turn(turn_id)
        raw_answer = ""
        json_payload: dict[str, object] | None = None
        contract_payload: dict | None = None
        contract_followups: list[str] = []
        tagged_answer = ""
        race_result: AnswerRaceResult | None = None
        plan = SimpleNamespace(answer_language=language)
        chunks: list[dict] = []
        policy_language: str | None = None
        live_voice_text = ""

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

        async def on_gemini_voice_delta(text_delta: str, emitted_chars: int, complete: bool) -> None:
            del emitted_chars, complete
            nonlocal live_voice_text
            if not text_delta.strip():
                return
            response_stream = await ensure_response_stream()
            live_voice_text += text_delta
            await response_stream.feed(text_delta)

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
                fast_hit = None
                stream = await ensure_response_stream()
                tagged_answer = wrap_answer_for_voice_and_chat(direct_reply, include_details=False)
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
                    on_gemini_voice_delta=on_gemini_voice_delta,
                )
                metrics.race_timings.update(race_result.timings)
                winner = race_result.winner
                plan = winner.plan or SimpleNamespace(answer_language=language)
                chunks = winner.chunks
                fast_hit = None
                if metrics.plan_done_at is None:
                    metrics.plan_done_at = perf_counter()
                language = normalize_lang(getattr(plan, "answer_language", language) or language)
                stream = await ensure_response_stream(plan, chunks)
                if winner.raw_answer:
                    raw_answer = winner.raw_answer
                else:
                    tagged_answer = winner.tagged_answer
                metrics.llm_done_at = perf_counter()
                log_event(log, "llm_done", session_id=self.session_id, request_id=turn_id, latency_ms=(metrics.llm_done_at - (metrics.plan_done_at or metrics.started_at)) * 1000, source=getattr(race_result.winner, "source", "unknown") if race_result else "unknown")

            json_payload = _extract_json_any(raw_answer)
            if isinstance(json_payload, dict):
                contract_payload, _, contract_followups = _coerce_prompt_contract_payload(json_payload, interrupted_input, language)

            if metrics.llm_done_at is None:
                metrics.llm_done_at = perf_counter()

            if not direct_reply and not tagged_answer:
                tagged_from_json = _json_payload_to_tagged_answer(json_payload) if isinstance(json_payload, dict) else None
                if not tagged_from_json:
                    tagged_from_json = _extract_answer_from_json(raw_answer)
                if not tagged_from_json:
                    tagged_from_json = _normalize_tagged_answer(raw_answer)
                tagged_answer = tagged_from_json

            tagged_answer, full_details_voice = await _tagged_answer_with_full_details_voice(
                tagged_answer,
                contract_payload,
                language,
            )
            metrics.spoken_ready_at = perf_counter()
            log_event(log, "spoken_ready", session_id=self.session_id, request_id=turn_id, latency_ms=(metrics.spoken_ready_at - metrics.started_at) * 1000)

            metrics.postprocess_done_at = perf_counter()
            if not stream.spoken_text.strip():
                await stream.feed(_normalize_tagged_answer(tagged_answer))
                await stream.flush()
            payload = stream.build_answer_payload()
            if contract_payload is not None:
                payload["control"] = contract_payload.get("control", _build_control_payload({}, interrupted_input))
                payload["details"] = contract_payload.get("details", payload.get("details", {}))
                payload["tts_chunks"] = _normalize_tts_chunks(contract_payload.get("tts_chunks"))
                payload["follow_up_questions"] = _normalize_followup_questions(contract_payload.get("follow_up_questions"))
                payload["answer_contract"] = contract_payload.get("answer_contract", {})
                contract_details = contract_payload.get("details", {})
                if isinstance(contract_details, dict):
                    contract_points = [str(item).strip() for item in contract_details.get("points", []) if str(item).strip()]
                    if contract_points:
                        payload["key_points"] = [
                            {
                                "id": f"point-{idx + 1}",
                                "label": f"Point {idx + 1}",
                                "preview": point,
                                "section_index": idx,
                            }
                            for idx, point in enumerate(contract_points[:4])
                        ]
                if contract_followups:
                    payload["follow_up_questions"] = contract_followups[:3]
                if not payload.get("tts_chunks"):
                    payload["tts_chunks"] = _build_tts_chunks(payload.get("spoken", ""), language)
            else:
                payload["control"] = _build_control_payload({}, interrupted_input)
                payload.setdefault("tts_chunks", _build_tts_chunks(payload.get("spoken", ""), language))
                payload["spoken"] = await _normalize_spoken_for_tts(payload.get("spoken", ""), language, trim_for_latency=False)
                if not payload.get("tts_chunks"):
                    payload["tts_chunks"] = _build_tts_chunks(payload.get("spoken", ""), language)
                payload.setdefault("answer_contract", _enforce_prompt_details(payload.get("details"), len(payload.get("follow_up_questions", []))))
            if not payload.get("follow_up_questions"):
                payload["follow_up_questions"] = []
            details_payload = payload.get("details")
            payload["details"] = _limit_answer_details(
                _enforce_prompt_details(details_payload, len(payload.get("follow_up_questions", [])))
            )
            if direct_reply:
                payload["spoken"] = await _normalize_spoken_for_tts(
                    direct_reply,
                    language,
                    trim_for_latency=False,
                )
                payload["details"] = _details_from_spoken(
                    str(payload.get("spoken", "")),
                    payload.get("details"),
                    len(payload.get("follow_up_questions", [])),
                )
                log_event(
                    log,
                    "direct_reply_voice_finalized",
                    session_id=self.session_id,
                    request_id=turn_id,
                    chars=len(str(payload.get("spoken", ""))),
                )
            else:
                details_voice = _json_to_markdown_details(payload.get("details")).strip()
                if not details_voice and full_details_voice:
                    details_voice = full_details_voice
                details_voice = _limit_text_for_answer_voice(details_voice, language)
                payload["spoken"] = await _normalize_spoken_for_tts(
                    details_voice or _limit_text_for_answer_voice(str(payload.get("spoken", "")), language),
                    language,
                    trim_for_latency=False,
                )
                remaining_voice = _remaining_spoken_suffix(str(payload.get("spoken", "")), live_voice_text or stream.spoken_text)
                if remaining_voice:
                    await stream.feed(remaining_voice)
            await stream.flush()
            payload["tts_chunks"] = [
                chunk
                for chunk in (
                    _normalize_tts_chunk_for_language(str(item), language)
                    for item in _build_tts_chunks(str(payload.get("spoken", "")), language)
                )
                if chunk
            ]
            if not payload["tts_chunks"]:
                payload["tts_chunks"] = _build_tts_chunks(payload.get("spoken", ""), language)
            if race_result is not None:
                winner = race_result.winner
                details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
                details["confidence"] = winner.score
                details["answer_kind"] = "fallback" if winner.fallback else details.get("answer_kind", "direct")
                details["citations"] = winner.citations or details.get("citations", [])
                if winner.fallback:
                    details["requires_follow_up"] = True
                    details["fallback_site"] = "aifc.kz"
                    details["knowledge_gap_query"] = query
                    payload["follow_up_questions"] = []
                payload["details"] = details
                payload["winner_source"] = winner.source
                payload["winner_confidence"] = winner.confidence
                if race_result.timings.get("selected_rag_tool"):
                    payload["selected_rag_tool"] = race_result.timings["selected_rag_tool"]
            enforced_details = _limit_answer_details(
                _enforce_prompt_details(
                    payload.get("details"),
                    len(payload.get("follow_up_questions", [])),
                )
            )
            if enforced_details.get("summary") or enforced_details.get("points") or enforced_details.get("sections"):
                payload["details"] = enforced_details
            else:
                payload["details"] = _details_from_spoken(
                    str(payload.get("spoken", "")),
                    payload.get("details"),
                    len(payload.get("follow_up_questions", [])),
                )
            payload["answer_contract"] = payload["details"]
            metrics.payload_done_at = perf_counter()
            await self.writer.send({"type": "answer_payload", "turn_id": turn_id, **payload})
            log_event(log, "answer_payload_sent", session_id=self.session_id, request_id=turn_id, latency_ms=(metrics.payload_done_at - metrics.started_at) * 1000)
            if not stream.full_reply:
                await self.writer.send({"type": "error", "text": "Empty response"})
                return
            self.history.append({"role": "assistant", "content": stream.full_reply})
            self.history[:] = self.history[-(MAX_HISTORY_TURNS * 2):]
            self.conversation_memory = await asyncio.to_thread(
                update_conversation_memory,
                self.conversation_memory,
                query,
                (payload.get("details") or {}).get("summary", ""),
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
                stream.cancel_all()
            raise
        except ClientClosedError:
            if stream is not None:
                stream.cancel_all()
        except Exception:
            log.exception("query pipeline failed")
            log_event(log, "pipeline_failed", session_id=self.session_id, request_id=turn_id, level=logging.ERROR)
            if stream is not None:
                stream.cancel_all()
            await self.writer.send({"type": "error", "text": "Response generation failed", "turn_id": turn_id})
        finally:
            self.pipeline_task = None
            self.active_metrics = None
            self.active_turn_id = None
            self.writer.clear_active_turn(turn_id)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/admin/cache/clear")
async def clear_runtime_caches() -> dict[str, object]:
    cleared = await clear_answer_caches()
    log_event(log, "runtime_caches_cleared", caches=json.dumps(cleared, sort_keys=True))
    return {"status": "ok", "cleared": cleared}


@app.get("/intro-cache/{avatar}/{block_key}")
async def intro_cache_frames(avatar: str, block_key: str, start: int = 0, limit: int = 0) -> JSONResponse:
    safe_avatar = _safe_cache_key(avatar)
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
                and cached_payload.get("avatar") == safe_avatar == INTRO_AVATAR_CACHE_KEY
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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    intro_token = (websocket.query_params.get("intro_token") or "").strip()
    writer = WsWriter(websocket)
    session = ClientSession(
        websocket=websocket,
        writer=writer,
        batch_stt=SonioxBatchSTT(),
        tts=ElevenTTS(),
        synctalk=SyncTalkClient(),
    )
    writer._on_send = session.on_send
    log_event(log, "websocket_connected", session_id=session.session_id)
    await writer.send({"type": "session_state", "session_id": session.session_id, "state": "connected"})
    intro_started = False
    intro_started = session.start_intro(intro_token or None)
    session.prewarm_realtime_stt(force=True)
    session.prewarm_realtime_tts()
    try:
        while True:
            payload = json.loads(await websocket.receive_text())
            await session.handle_message(payload)
    except WebSocketDisconnect:
        log.info("ws disconnected")
    except Exception:
        log.exception("ws session error")
    finally:
        try:
            await session.reset(reopen_transports=False, reason="disconnect")
        except Exception:
            log.exception("session reset failed")
        await session.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=APP_HOST, port=APP_PORT, reload=False)
