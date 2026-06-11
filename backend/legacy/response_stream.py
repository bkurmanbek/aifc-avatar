from __future__ import annotations

import asyncio
import base64
import logging
import re
from time import perf_counter
from uuid import uuid4

from backend.core.logging import log_event

from .spoken_text import sanitize_spoken_text
from .settings import (
    AVATAR_TTS_FIRST_SEGMENT_MS,
    AVATAR_TTS_MAX_SEGMENT_MS,
    AVATAR_TTS_MIN_SEGMENT_MS,
    AVATAR_TTS_SEGMENT_MS,
    SONIOX_TTS_FIRST_SEGMENT_MS,
    SONIOX_TTS_MAX_SEGMENT_MS,
    SONIOX_TTS_MIN_SEGMENT_MS,
    SONIOX_TTS_SEGMENT_MS,
)
from .tts import pcm_to_wav_bytes
from .ws_writer import ClientClosedError

log = logging.getLogger(__name__)

_SENTENCE_BATCH_SIZE = 3
_SENTENCE_BATCH_WAIT_S = 0.06
_AVATAR_WORKER_COUNT = 2

_TAG_RE = re.compile(r"\[\[(/?)(spoken|details|followups)\]\]", re.IGNORECASE)
_SECTION_TITLE_RE = re.compile(r"^#{1,6}\s+(.+)$")


class ResponseStream:
    """Local WebSocket response streamer.

    This class intentionally mirrors the small public surface that the backend
    uses from the old production stream class, while keeping all runtime logic
    inside this demo workspace.
    """

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
        self._buffer = ""
        self._section: str | None = None
        self._chunk_tasks: list[asyncio.Task] = []
        self._chunk_count = 0
        self._streaming_media = bool(getattr(tts, "supports_streaming_avatar", False))
        self._media_queue: asyncio.Queue[tuple[str, int, str | None] | None] | None = asyncio.Queue()
        self._avatar_queue: asyncio.Queue[tuple[int, bytes] | None] | None = asyncio.Queue()
        self._media_worker_task: asyncio.Task | None = None
        self._avatar_worker_tasks: list[asyncio.Task] = []
        self._media_chunk_idx = 0
        self._media_closed = False
        self._first_spoken_chunk_recorded = False
        self.spoken_text = ""
        self.details_text = ""
        self.followups_text = ""
        self.full_reply = ""

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

    async def feed(self, text: str) -> None:
        if not text:
            return
        self._buffer += text
        await self._drain_pending(final=False)

    async def flush(self) -> None:
        await self._drain_pending(final=True)
        lang = self._get_lang()
        for sentence, idx in self._splitter.flush():
            await self._emit_spoken_sentence(sentence)
            self._schedule_spoken_chunk(sentence, idx, lang)

    async def _drain_pending(self, *, final: bool) -> None:
        while self._buffer:
            match = _TAG_RE.search(self._buffer)
            if match is None:
                if not final and "[[" in self._buffer[-16:]:
                    return
                text = self._buffer
                self._buffer = ""
                await self._emit_section_text(text)
                return

            before = self._buffer[: match.start()]
            if before:
                await self._emit_section_text(before)
            closing, section = match.group(1), match.group(2).lower()
            self._buffer = self._buffer[match.end() :]
            self._section = None if closing else section

    async def _emit_section_text(self, text: str) -> None:
        if not text:
            return
        section = self._section or "spoken"
        if section == "spoken":
            await self._emit_spoken_incremental(text)
        elif section == "details":
            self.details_text += text
            self.full_reply += text
        elif section == "followups":
            self.followups_text += text
            self.full_reply += text

    async def _emit_spoken_incremental(self, text: str) -> None:
        if not text:
            return
        self.spoken_text += text
        self.full_reply += text
        lang = self._get_lang()
        for sentence, idx in self._splitter.feed(text):
            await self._emit_spoken_sentence(sentence)
            self._schedule_spoken_chunk(sentence, idx, lang)

    async def _emit_spoken_sentence(self, sentence: str) -> None:
        cleaned = sentence.strip()
        if not cleaned:
            return
        await self._writer.send(self._event({"type": "response_chunk", "text": cleaned + " "}))

    def _schedule_spoken_chunk(self, sentence: str, idx: int, lang: str | None) -> None:
        cleaned = sanitize_spoken_text(sentence)
        if not cleaned:
            return
        self._ensure_media_worker()
        if self._media_queue is not None:
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
                close_after_batch = False
                if self._streaming_media and self._media_chunk_idx > 0:
                    while len(batch) < _SENTENCE_BATCH_SIZE:
                        try:
                            next_item = await asyncio.wait_for(
                                self._media_queue.get(),
                                timeout=_SENTENCE_BATCH_WAIT_S,
                            )
                        except asyncio.TimeoutError:
                            break
                        if next_item is None:
                            close_after_batch = True
                            break
                        batch.append(next_item)
                if self._streaming_media:
                    await self._run_streaming_sentence_batch(batch)
                else:
                    await self._run_limited_sentence_batch(batch)
                if close_after_batch and self._media_queue is not None:
                    self._media_queue.put_nowait(None)
                    return
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
            audio_wav = await self._tts.synthesize(text, lang=lang, priority=0 if idx == 0 else 1, voice=self._tts_voice)
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
        buffer = bytearray()
        source_idx = sentence_batch[0][1]
        batch_lang = None
        for _, _, lang in sentence_batch:
            if lang:
                batch_lang = lang
                break

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
            first_segment = True
            async for pcm in self._tts.synthesize_pcm_stream_from_texts(prepared_texts(), lang=batch_lang, voice=self._tts_voice):
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
                while True:
                    target_ms = AVATAR_TTS_FIRST_SEGMENT_MS if first_segment else AVATAR_TTS_SEGMENT_MS
                    target_bytes = _avatar_segment_target_bytes(sample_rate, target_ms)
                    if len(buffer) < target_bytes:
                        break
                    segment = bytes(buffer[:target_bytes])
                    del buffer[:target_bytes]
                    await self._queue_pcm_segment(segment, sample_rate, source_idx)
                    first_segment = False
            if buffer:
                await self._queue_pcm_segment(_pad_pcm_tail(bytes(buffer), min_tail_bytes), sample_rate, source_idx)
            log_event(
                log,
                "tts_stream_done",
                request_id=self._turn_id,
                source_chunk=source_idx,
                latency_ms=(perf_counter() - started) * 1000,
            )
        except ClientClosedError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("streaming TTS/avatar segment failed: source_idx=%s", source_idx)
            await self._fail_next_media_chunk(f"TTS failed: {exc}")

    async def _run_streaming_sentence(self, sentence: str, text_idx: int, lang: str | None) -> None:
        await self._run_streaming_sentence_batch([(sentence, text_idx, lang)])

    async def _run_streaming_avatar_worker(self) -> None:
        if self._avatar_queue is None:
            return
        while True:
            item = await self._avatar_queue.get()
            if item is None:
                return
            media_idx, audio_wav = item
            frame_count = 0
            started = perf_counter()
            try:
                log_event(log, "avatar_chunk_start", request_id=self._turn_id, chunk=media_idx, bytes=len(audio_wav))
                async for frame in self._synctalk.infer_stream(
                    audio_wav,
                    priority=0 if media_idx == 0 else 1,
                    chunk_idx=media_idx,
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
                    await self._writer.send(self._event({"type": "frame", "data": frame, "chunk": media_idx}))
            except ClientClosedError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("streaming avatar segment failed: chunk=%s", media_idx)
                await self._send_media_error(media_idx, f"SyncTalk failed: {exc}")
            finally:
                if frame_count == 0:
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

    async def _queue_pcm_segment(self, pcm: bytes, sample_rate: int, text_idx: int) -> None:
        if len(pcm) < 2:
            return
        if len(pcm) % 2:
            pcm = pcm[:-1]
        media_idx = self._media_chunk_idx
        self._media_chunk_idx += 1
        self._chunk_count = max(self._chunk_count, media_idx + 1)
        audio_wav = pcm_to_wav_bytes(pcm, sample_rate)
        await self._queue_wav_segment(audio_wav, media_idx, text_idx, streaming=True)

    async def _queue_wav_segment(self, audio_wav: bytes, media_idx: int, text_idx: int, *, streaming: bool) -> None:
        audio_b64 = base64.b64encode(audio_wav).decode("ascii")
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
                    }
                )
            )
            if self._avatar_queue is not None:
                self._avatar_queue.put_nowait((media_idx, audio_wav))
        except ClientClosedError:
            raise

    async def _send_media_error(self, chunk: int, text: str) -> None:
        try:
            await self._writer.send(self._event({"type": "media_error", "chunk": chunk, "text": text}))
        except ClientClosedError:
            pass

    async def _fail_next_media_chunk(self, text: str) -> None:
        idx = self._media_chunk_idx
        self._media_chunk_idx += 1
        self._chunk_count = max(self._chunk_count, idx + 1)
        await self._send_media_error(idx, text)

    async def _run_limited_chunk(self, sentence: str, idx: int, lang: str | None) -> None:
        try:
            audio_wav = await self._tts.synthesize(sentence, lang=lang, priority=0 if idx == 0 else 1, voice=self._tts_voice)
            await self._queue_wav_segment(audio_wav, idx, idx, streaming=False)
        except ClientClosedError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("response media chunk failed: chunk=%s", idx)
            await self._send_media_error(idx, f"media chunk failed: {exc}")
        finally:
            pass

    def _detail_summary(self) -> str:
        details = sanitize_spoken_text(self.details_text, keep_digits=True)
        spoken = sanitize_spoken_text(self.spoken_text, keep_digits=True)
        if details:
            return details.splitlines()[0][:500].strip()
        return spoken[:500].strip()

    def _detail_sections(self) -> list[dict]:
        details = (self.details_text or "").strip()
        if not details:
            spoken = sanitize_spoken_text(self.spoken_text, keep_digits=True)
            return [{"id": "spoken", "title": "Answer", "text": spoken, "items": []}] if spoken else []

        sections: list[dict] = []
        current = {"id": "details-1", "title": "Details", "text": "", "items": []}
        for raw_line in details.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            title_match = _SECTION_TITLE_RE.match(line)
            if title_match:
                if current["text"] or current["items"]:
                    sections.append(current)
                current = {
                    "id": f"details-{len(sections) + 1}",
                    "title": title_match.group(1).strip(),
                    "text": "",
                    "items": [],
                }
                continue
            if line.startswith("-"):
                current["items"].append(line.lstrip("- ").strip())
            else:
                current["text"] = f"{current['text']} {line}".strip()
        if current["text"] or current["items"]:
            sections.append(current)
        return sections

    def _followups(self) -> list[str]:
        items: list[str] = []
        for line in (self.followups_text or "").splitlines():
            cleaned = line.strip(" -\t")
            if cleaned:
                items.append(cleaned)
        return items[:3]

    def build_answer_payload(self) -> dict:
        spoken = sanitize_spoken_text(self.spoken_text, keep_digits=True)
        summary = self._detail_summary()
        sections = self._detail_sections()
        key_points: list[dict] = []
        for index, section in enumerate(sections[:4]):
            preview = section.get("text") or " ".join(section.get("items") or [])
            preview = str(preview).strip()
            if preview:
                key_points.append(
                    {
                        "id": f"point-{index + 1}",
                        "label": f"Point {index + 1}",
                        "preview": preview[:180],
                        "section_index": index,
                    }
                )
        return {
            "answer_id": uuid4().hex,
            "spoken": spoken,
            "details": {
                "summary": summary or spoken,
                "points": [spoken] if spoken else [],
                "sections": sections,
                "answer_kind": "direct",
                "confidence": 0.86,
                "requires_follow_up": False,
                "citations": [],
                "notes": [],
            },
            "key_points": key_points,
            "follow_up_questions": self._followups(),
        }

    async def wait_all(self) -> None:
        if self._media_queue is not None and not self._media_closed:
            self._media_closed = True
            if self._media_worker_task is not None:
                self._media_queue.put_nowait(None)
        if not self._chunk_tasks:
            return
        await asyncio.gather(*self._chunk_tasks, return_exceptions=True)

    def cancel_all(self) -> None:
        for task in self._chunk_tasks:
            if not task.done():
                task.cancel()


def _pcm_bytes_for_ms(sample_rate: int, ms: int) -> int:
    frames = max(1, int(sample_rate * max(1, ms) / 1000))
    return frames * 2


def _segment_target_bytes(sample_rate: int, ms: int) -> int:
    min_bytes = _pcm_bytes_for_ms(sample_rate, SONIOX_TTS_MIN_SEGMENT_MS)
    max_bytes = _pcm_bytes_for_ms(sample_rate, SONIOX_TTS_MAX_SEGMENT_MS)
    target_bytes = _pcm_bytes_for_ms(sample_rate, ms)
    return max(2, min(max_bytes, max(target_bytes, min_bytes)))


def _avatar_segment_target_bytes(sample_rate: int, ms: int) -> int:
    min_bytes = _pcm_bytes_for_ms(sample_rate, AVATAR_TTS_MIN_SEGMENT_MS)
    max_bytes = _pcm_bytes_for_ms(sample_rate, AVATAR_TTS_MAX_SEGMENT_MS)
    target_bytes = _pcm_bytes_for_ms(sample_rate, ms)
    return max(2, min(max_bytes, max(target_bytes, min_bytes)))


def _pad_pcm_tail(pcm: bytes, min_bytes: int) -> bytes:
    if len(pcm) >= min_bytes:
        return pcm
    padding = min_bytes - len(pcm)
    if padding % 2:
        padding += 1
    return pcm + (b"\x00" * padding)
