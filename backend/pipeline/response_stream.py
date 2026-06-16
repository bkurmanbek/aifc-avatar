from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import wave
from time import perf_counter
from uuid import uuid4

from ..logging_config import log_event, preview_text
from ..utils.spoken_text import sanitize_spoken_text
from ..utils.tts_pronunciation import prepare_tts_text
from ..settings import (
    AVATAR_TTS_FIRST_SEGMENT_MS,
    AVATAR_TTS_MIN_SEGMENT_MS,
    AVATAR_TTS_SEGMENT_MS,
    SYNCTALK_MAX_CONCURRENCY,
)
from ..media.audio_utils import pcm_to_wav_bytes
from ..api.ws_writer import ClientClosedError
from ..utils.debug_io import save_tts_chunk as _save_tts_chunk_debug

log = logging.getLogger(__name__)

_AVATAR_WORKER_COUNT = 2
_SYNCTALK_SEMAPHORE: asyncio.Semaphore | None = None


class ResponseStream:
    def __init__(
        self,
        writer,
        tts,
        synctalk,
        *,
        splitter,
        plan=None,
        turn_started_at: float | None = None,
        turn_id: str | None = None,
        query_text: str = "",
        chunks: list[dict] | None = None,
        tts_voice: str | None = None,
    ) -> None:
        self._writer = writer
        self._tts = tts
        self._synctalk = synctalk
        self._splitter = splitter
        self._plan = plan
        self._turn_started_at = turn_started_at
        self._turn_id = turn_id
        self._query_text = query_text
        self._chunks = chunks or []
        self._tts_voice = tts_voice
        self._chunk_tasks: list[asyncio.Task] = []
        self._chunk_count = 0
        self._streaming_media = bool(getattr(tts, "supports_streaming_avatar", False))
        self._media_queue: asyncio.Queue[tuple[str, int, str | None] | None] | None = asyncio.Queue()
        self._avatar_queue: asyncio.Queue[tuple[int, bytes, int] | None] | None = asyncio.Queue()
        self._media_worker_task: asyncio.Task | None = None
        self._avatar_worker_tasks: list[asyncio.Task] = []
        self._media_chunk_idx = 0
        # Cumulative head-pose frame offset for the turn. Each segment is rendered with
        # start_frame = this offset so SyncTalk's head-pose walk stays continuous across
        # the many small segments we now stream (see _queue_wav_segment).
        self._turn_frame_offset = 0
        self._first_segment_emitted = False
        self._media_closed = False
        self._first_spoken_chunk_recorded = False
        self.spoken_text = ""
        self.chat_text = ""

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    def update_context(self, *, plan=None, chunks: list[dict] | None = None) -> None:
        if plan is not None:
            self._plan = plan
        if chunks is not None:
            self._chunks = chunks

    def _event(self, data: dict) -> dict:
        if self._turn_id:
            data = {**data, "turn_id": self._turn_id}
        return data

    def _get_lang(self) -> str | None:
        value = getattr(self._plan, "answer_language", None)
        return str(value) if value else None

    async def flush(self) -> None:
        lang = self._get_lang()
        for sentence, idx in self._splitter.flush():
            self._schedule_spoken_chunk(sentence, idx, lang)

    async def emit_spoken_text(self, text: str) -> None:
        await self._emit_spoken_incremental(text)

    async def emit_chat_text(self, text: str) -> None:
        await self._emit_chat_incremental(text)

    async def _emit_spoken_incremental(self, text: str) -> None:
        if not text:
            return
        self.spoken_text += text
        lang = self._get_lang()
        for sentence, idx in self._splitter.feed(text):
            self._schedule_spoken_chunk(sentence, idx, lang)

    async def _emit_chat_incremental(self, text: str) -> None:
        if not text:
            return
        self.chat_text += text
        if text.strip():
            log_event(
                log,
                "chat_chunk_emit",
                request_id=self._turn_id,
                chars=len(text),
                text_preview=preview_text(text, 240),
            )
            await self._writer.send(self._event({"type": "response_chunk", "text": text}))

    def _schedule_spoken_chunk(self, sentence: str, idx: int, lang: str | None) -> None:
        cleaned = prepare_tts_text(sentence, lang) if lang else sanitize_spoken_text(sentence)
        if not cleaned:
            return
        self._ensure_media_worker()
        if self._media_queue is not None:
            log_event(
                log,
                "spoken_chunk_queued",
                request_id=self._turn_id,
                source_chunk=idx,
                language=lang,
                chars=len(cleaned),
                text_preview=preview_text(cleaned, 240),
            )
            self._media_queue.put_nowait((cleaned, idx, lang))

    def _ensure_media_worker(self) -> None:
        if self._media_worker_task is None or self._media_worker_task.done():
            self._media_worker_task = asyncio.create_task(self._run_streaming_media_worker())
            self._chunk_tasks.append(self._media_worker_task)
        self._avatar_worker_tasks = [task for task in self._avatar_worker_tasks if not task.done()]
        while len(self._avatar_worker_tasks) < _AVATAR_WORKER_COUNT:
            task = asyncio.create_task(self._run_streaming_avatar_worker())
            self._avatar_worker_tasks.append(task)
            self._chunk_tasks.append(task)

    async def _run_streaming_media_worker(self) -> None:
        if self._media_queue is None:
            return
        try:
            while True:
                item = await self._media_queue.get()
                if item is None:
                    return
                batch: list[tuple[str, int, str | None]] = [item]
                if self._streaming_media:
                    await self._run_streaming_sentence_batch(batch)
                else:
                    await self._run_limited_sentence_batch(batch)
        finally:
            if self._avatar_queue is not None:
                for _ in self._avatar_worker_tasks:
                    self._avatar_queue.put_nowait(None)

    async def _run_limited_sentence_batch(self, sentence_batch: list[tuple[str, int, str | None]]) -> None:
        if not sentence_batch:
            return
        text = " ".join(sentence for sentence, _, _ in sentence_batch if sentence).strip()
        if not text:
            return
        idx = self._media_chunk_idx
        self._media_chunk_idx += 1
        self._chunk_count = max(self._chunk_count, idx + 1)
        lang = next((item_lang for _, _, item_lang in sentence_batch if item_lang), None)
        started = perf_counter()
        try:
            log_event(log, "tts_chunk_start", request_id=self._turn_id, chunk=idx, streaming=False, chars=len(text))
            audio_wav = await self._tts.synthesize(
                text,
                lang=lang,
                priority=0 if idx == 0 else 1,
                voice=self._tts_voice,
                client_reference_id=self._turn_id,
            )
            log_event(
                log,
                "tts_chunk_done",
                request_id=self._turn_id,
                chunk=idx,
                streaming=False,
                latency_ms=(perf_counter() - started) * 1000,
                bytes=len(audio_wav),
            )
            await self._queue_wav_segment(audio_wav, idx, sentence_batch[0][1], streaming=False)
        except ClientClosedError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("TTS segment failed: chunk=%s", idx)
            await self._send_media_error(idx, f"TTS failed: {exc}")

    async def _run_streaming_sentence_batch(self, sentence_batch: list[tuple[str, int, str | None]]) -> None:
        if not sentence_batch:
            return
        sample_rate = int(getattr(self._tts, "sample_rate", 24000) or 24000)
        min_tail_bytes = _pcm_bytes_for_ms(sample_rate, AVATAR_TTS_MIN_SEGMENT_MS)
        first_seg_bytes = max(min_tail_bytes, _pcm_bytes_for_ms(sample_rate, AVATAR_TTS_FIRST_SEGMENT_MS))
        seg_bytes = max(min_tail_bytes, _pcm_bytes_for_ms(sample_rate, AVATAR_TTS_SEGMENT_MS))
        buffer = bytearray()
        source_idx = sentence_batch[0][1]
        batch_lang = None
        for _, _, lang in sentence_batch:
            if lang:
                batch_lang = lang
                break

        sentence_text = " | ".join(s for s, _, _ in sentence_batch if s)
        segments_emitted = 0

        async def flush_segment(pcm: bytes) -> None:
            nonlocal segments_emitted
            if not pcm:
                return
            seg_idx = self._media_chunk_idx  # _queue_pcm_segment allocates when media_idx=None
            _save_tts_chunk_debug(self._turn_id or "", seg_idx, sentence_text, pcm, sample_rate)
            await self._queue_pcm_segment(pcm, sample_rate, source_idx)
            self._first_segment_emitted = True
            segments_emitted += 1

        async def prepared_texts():
            for sentence, _, _ in sentence_batch:
                if sentence:
                    yield sentence

        started = perf_counter()
        first_audio_logged = False
        try:
            log_event(
                log,
                "tts_stream_start",
                request_id=self._turn_id,
                source_chunk=source_idx,
                sentences=len(sentence_batch),
                chars=sum(len(sentence) for sentence, _, _ in sentence_batch),
            )
            async for pcm in self._tts.synthesize_pcm_stream_from_texts(
                prepared_texts(),
                lang=batch_lang,
                voice=self._tts_voice,
                client_reference_id=self._turn_id,
            ):
                if not pcm:
                    continue
                if not first_audio_logged:
                    first_audio_logged = True
                    log_event(
                        log,
                        "tts_stream_first_pcm",
                        request_id=self._turn_id,
                        source_chunk=source_idx,
                        latency_ms=(perf_counter() - started) * 1000,
                        bytes=len(pcm),
                    )
                buffer.extend(pcm)
                # Flush whole segments as audio streams in so SyncTalk renders them
                # while TTS is still producing — the small first segment minimises
                # time-to-first-frame; later segments are larger to limit per-call overhead.
                target = seg_bytes if self._first_segment_emitted else first_seg_bytes
                while len(buffer) >= target:
                    await flush_segment(bytes(buffer[:target]))
                    del buffer[:target]
                    target = seg_bytes
            # Flush the remainder (pad short tails so SyncTalk has enough audio context).
            if buffer:
                await flush_segment(_pad_pcm_tail(bytes(buffer), min_tail_bytes))
            if segments_emitted == 0:
                await self._send_media_error(self._allocate_media_idx(), "TTS returned no audio")
            log_event(
                log,
                "tts_stream_done",
                request_id=self._turn_id,
                source_chunk=source_idx,
                latency_ms=(perf_counter() - started) * 1000,
                segments=segments_emitted,
            )
        except ClientClosedError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("streaming TTS/avatar segment failed: source_idx=%s", source_idx)
            if segments_emitted == 0:
                await self._send_media_error(self._allocate_media_idx(), f"TTS failed: {exc}")

    async def _run_streaming_avatar_worker(self) -> None:
        if self._avatar_queue is None:
            return
        while True:
            item = await self._avatar_queue.get()
            if item is None:
                return
            media_idx, audio_wav, start_frame = item
            frame_count = 0
            error_sent = False
            started = perf_counter()
            try:
                log_event(log, "avatar_chunk_start", request_id=self._turn_id, chunk=media_idx, bytes=len(audio_wav))
                async with _synctalk_semaphore():
                    async for frame in self._synctalk.infer_stream(
                        audio_wav,
                        priority=0 if media_idx == 0 else 1,
                        chunk_idx=media_idx,
                        start_frame=start_frame,
                    ):
                        frame_count += 1
                        if frame_count == 1:
                            log_event(
                                log,
                                "avatar_first_frame",
                                request_id=self._turn_id,
                                chunk=media_idx,
                                latency_ms=(perf_counter() - started) * 1000,
                            )
                        # SyncTalk yields base64 JPEG; decode here and ship raw bytes as a
                        # binary WS frame so the browser skips JSON.parse + atob per frame.
                        await self._writer.send_frame_binary(
                            media_idx, base64.b64decode(frame), turn_id=self._turn_id
                        )
            except ClientClosedError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_sent = True
                log.exception("streaming avatar segment failed: chunk=%s", media_idx)
                await self._send_media_error(media_idx, f"SyncTalk failed: {exc}")
            finally:
                if frame_count == 0 and not error_sent:
                    await self._send_media_error(media_idx, "SyncTalk returned no frames")
                log_event(
                    log,
                    "avatar_chunk_done",
                    request_id=self._turn_id,
                    chunk=media_idx,
                    latency_ms=(perf_counter() - started) * 1000,
                    frames=frame_count,
                )
                try:
                    await self._writer.send(self._event({"type": "chunk_done", "chunk": media_idx}))
                except ClientClosedError:
                    pass

    def _allocate_media_idx(self) -> int:
        media_idx = self._media_chunk_idx
        self._media_chunk_idx += 1
        self._chunk_count = max(self._chunk_count, media_idx + 1)
        return media_idx

    async def _queue_pcm_segment(self, pcm: bytes, sample_rate: int, text_idx: int, *, media_idx: int | None = None) -> None:
        if len(pcm) < 2:
            return
        if len(pcm) % 2:
            pcm = pcm[:-1]
        if media_idx is None:
            media_idx = self._allocate_media_idx()
        audio_wav = pcm_to_wav_bytes(pcm, sample_rate)
        await self._queue_wav_segment(audio_wav, media_idx, text_idx, streaming=True)

    async def _queue_wav_segment(self, audio_wav: bytes, media_idx: int, text_idx: int, *, streaming: bool) -> None:
        audio_b64 = base64.b64encode(audio_wav).decode("ascii")
        with wave.open(io.BytesIO(audio_wav)) as wf:
            expected_frames = max(1, round(wf.getnframes() / wf.getframerate() * 25))
        # Assign this segment's head-pose start offset in playback order, then advance
        # the turn cursor. +2 approximates SyncTalk's audio-feature edge padding so the
        # next segment resumes near where this one actually ends.
        start_frame = self._turn_frame_offset
        self._turn_frame_offset += expected_frames + 2
        try:
            await self._writer.send(
                self._event(
                    {
                        "type": "audio_ready",
                        "data": audio_b64,
                        "chunk": media_idx,
                        "source_chunk": text_idx,
                        "frame_stride": 1,
                        "streaming": streaming,
                        "expected_frames": expected_frames,
                    }
                )
            )
            if self._avatar_queue is not None:
                self._avatar_queue.put_nowait((media_idx, audio_wav, start_frame))
        except ClientClosedError:
            raise

    async def _send_media_error(self, chunk: int, text: str) -> None:
        try:
            await self._writer.send(self._event({"type": "media_error", "chunk": chunk, "text": text}))
        except ClientClosedError:
            pass

    def build_answer_payload(self) -> dict:
        spoken = sanitize_spoken_text(self.spoken_text, keep_digits=True)
        return {
            "answer_id": uuid4().hex,
            "spoken": spoken,
            "chat": self.chat_text.strip() or spoken,
        }

    async def wait_all(self) -> None:
        if self._media_queue is not None and not self._media_closed:
            self._media_closed = True
            if self._media_worker_task is not None:
                self._media_queue.put_nowait(None)
        if not self._chunk_tasks:
            return
        results = await asyncio.gather(*self._chunk_tasks, return_exceptions=True)
        _log_task_results(results)

    async def cancel_all(self) -> None:
        if self._media_queue is not None and not self._media_closed:
            self._media_closed = True
            with contextlib.suppress(Exception):
                self._media_queue.put_nowait(None)
        for task in self._chunk_tasks:
            if not task.done():
                task.cancel()
        if self._chunk_tasks:
            results = await asyncio.gather(*self._chunk_tasks, return_exceptions=True)
            _log_task_results(results)


def _synctalk_semaphore() -> asyncio.Semaphore:
    global _SYNCTALK_SEMAPHORE
    if _SYNCTALK_SEMAPHORE is None:
        _SYNCTALK_SEMAPHORE = asyncio.Semaphore(max(1, SYNCTALK_MAX_CONCURRENCY))
    return _SYNCTALK_SEMAPHORE


def _log_task_results(results: list[object]) -> None:
    for result in results:
        if isinstance(result, (asyncio.CancelledError, ClientClosedError)):
            continue
        if isinstance(result, BaseException):
            log.error("response media task failed", exc_info=(type(result), result, result.__traceback__))


def _pcm_bytes_for_ms(sample_rate: int, ms: int) -> int:
    frames = max(1, int(sample_rate * max(1, ms) / 1000))
    return frames * 2


def _pad_pcm_tail(pcm: bytes, min_bytes: int) -> bytes:
    if len(pcm) >= min_bytes:
        return pcm
    padding = min_bytes - len(pcm)
    if padding % 2:
        padding += 1
    return pcm + (b"\x00" * padding)
