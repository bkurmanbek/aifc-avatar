from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import wave
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from .abbreviations import normalize_transcript_abbreviations
from .language import (
    is_noise_utterance,
    normalize_lang,
    supported_lang_or_none,
    transcript_has_meaningful_speech,
    is_interrupt_candidate,
)
from .settings import (
    SONIOX_API_KEY,
    SONIOX_STT_AUDIO_FORMAT,
    SONIOX_STT_BATCH_FINALIZE_TIMEOUT_S,
    SONIOX_STT_CONTEXT_FILE,
    SONIOX_STT_CONTEXT_MAX_CHARS,
    SONIOX_STT_ENABLE_ENDPOINT_DETECTION,
    SONIOX_STT_LANGUAGE_HINTS,
    SONIOX_STT_LANGUAGE_HINTS_STRICT,
    SONIOX_STT_MAX_ENDPOINT_DELAY_MS,
    SONIOX_STT_MIN_TOKEN_CONFIDENCE,
    SONIOX_STT_MODEL,
    SONIOX_STT_REALTIME_FINALIZE_TIMEOUT_S,
    SONIOX_STT_SAMPLE_RATE,
    SONIOX_STT_WS_URL,
)
from .ws_writer import ClientClosedError, WsWriter

log = logging.getLogger(__name__)

RealtimeCallback = Callable[[str], Any]
RealtimePartialCallback = Callable[[str], Awaitable[None]]
RealtimeFinalCallback = Callable[[str, str], Awaitable[None]]


def looks_like_pcm16_chunk(data: bytes) -> bool:
    if len(data) < 80 or len(data) % 2 != 0:
        return False
    header = data[:12]
    if header.startswith(b"RIFF") or header.startswith(b"\x1a\x45\xdf\xa3") or header.startswith(b"ID3") or header.startswith(b"OggS"):
        return False
    # Ignore all-zero chunks; they provide no speech signal and can occur during pauses.
    if set(data) == {0}:
        return False
    return True


def guess_extension(data: bytes) -> str:
    if data.startswith(b"RIFF"):
        return "wav"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if data.startswith(b"OggS"):
        return "ogg"
    return "bin"


class SonioxBatchSTT:
    def __init__(self) -> None:
        self._closed = False

    async def transcribe(self, audio_bytes: bytes, language: str | None = None) -> tuple[str, str]:
        audio_format, sample_rate, payload = _prepare_one_shot_audio(audio_bytes)
        session = SonioxRealtimeSession(
            writer=None,
            batch_stt=None,
            on_meaningful_partial=None,
            preferred_language=language,
            audio_format=audio_format,
            sample_rate=sample_rate,
        )
        await session.start()
        for offset in range(0, len(payload), _stream_chunk_size(sample_rate)):
            await session.send_audio(payload[offset : offset + _stream_chunk_size(sample_rate)])
            await asyncio.sleep(0.02)
        return await session.finalize(timeout_s=SONIOX_STT_BATCH_FINALIZE_TIMEOUT_S)

    async def close(self) -> None:
        self._closed = True


class SonioxRealtimeSession:
    def __init__(
        self,
        writer: WsWriter | None,
        batch_stt: SonioxBatchSTT | None = None,
        on_meaningful_partial: RealtimePartialCallback | None = None,
        on_final_utterance: RealtimeFinalCallback | None = None,
        preferred_language: str | None = None,
        audio_format: str | None = None,
        sample_rate: int | None = None,
    ):
        self._writer = writer
        self._batch_stt = batch_stt
        self._on_meaningful_partial = on_meaningful_partial
        self._on_final_utterance = on_final_utterance
        self._ws = None
        self._listener: asyncio.Task | None = None
        self._committed: list[str] = []
        self._committed_norms: set[str] = set()
        self._language: str | None = supported_lang_or_none(preferred_language or "")
        self._commit_event = asyncio.Event()
        self._closed = False
        self._lock = asyncio.Lock()
        self._pending_final_tokens: list[str] = []
        self._finalization_claimed = False
        self._audio_format = audio_format or SONIOX_STT_AUDIO_FORMAT
        self._sample_rate = sample_rate or SONIOX_STT_SAMPLE_RATE
        self._audio_bytes_sent = 0

    async def start(self) -> None:
        if not SONIOX_API_KEY:
            raise RuntimeError("SONIOX_API_KEY is not configured")
        self._ws = await websockets.connect(SONIOX_STT_WS_URL, max_size=None)
        await self._ws.send(json.dumps(_soniox_config(self._language, self._audio_format, self._sample_rate)))
        self._listener = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                if message.get("error_type") or message.get("error_code") or message.get("error_message"):
                    log.warning("Soniox STT error: %s", message)
                    self._closed = True
                    self._commit_event.set()
                    break
                self._handle_language(message)
                await self._handle_tokens(message.get("tokens") or [])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Soniox STT listener failed")
            self._closed = True
            self._commit_event.set()
        else:
            self._closed = True
            self._commit_event.set()

    async def _handle_tokens(self, tokens: list[dict[str, Any]]) -> None:
        if not tokens:
            return

        partial_parts: list[str] = []
        final_marker: str | None = None
        for token in tokens:
            text = str(token.get("text", ""))
            if not text:
                continue
            if token.get("language"):
                self._language = supported_lang_or_none(str(token.get("language"))) or self._language
            marker = text.strip()
            if marker in {"<end>", "<fin>"}:
                if token.get("is_final", True):
                    final_marker = marker
                continue
            if not _token_confident_enough(token):
                continue
            if token.get("is_final"):
                self._pending_final_tokens.append(text)
            else:
                partial_parts.append(text)

        partial_text = normalize_transcript_abbreviations("".join(partial_parts).strip(), self._language)
        if _valid_live_text(partial_text):
            await self._send_partial(partial_text)

        if final_marker is not None:
            final_text = normalize_transcript_abbreviations("".join(self._pending_final_tokens).strip(), self._language)
            self._pending_final_tokens = []
            if _valid_committed_text(final_text):
                appended = self._append_committed(final_text)
                if (
                    appended
                    and final_marker == "<end>"
                    and self._on_final_utterance is not None
                    and self.claim_finalization()
                ):
                    task = asyncio.create_task(
                        self._on_final_utterance(final_text, normalize_lang(self._language))
                    )
                    task.add_done_callback(_log_callback_error)
            self._commit_event.set()

    async def _send_partial(self, text: str) -> None:
        if self._writer is None:
            return
        try:
            await self._writer.send({"type": "partial", "text": text})
            if self._on_meaningful_partial is not None and is_interrupt_candidate(text, avg_logprob=None):
                await self._on_meaningful_partial(text)
        except ClientClosedError:
            self._commit_event.set()

    def _handle_language(self, message: dict[str, Any]) -> None:
        language = supported_lang_or_none(str(message.get("language") or ""))
        if language:
            self._language = language

    async def send_audio(self, chunk: bytes) -> None:
        if self._closed or self._ws is None:
            return
        async with self._lock:
            try:
                await self._ws.send(chunk)
                self._audio_bytes_sent += len(chunk)
            except websockets.exceptions.ConnectionClosed:
                self._closed = True
                self._commit_event.set()
            except Exception:
                log.exception("Soniox STT audio send failed")
                self._closed = True
                self._commit_event.set()

    async def send_keepalive(self) -> None:
        if self._closed or self._ws is None:
            return
        async with self._lock:
            try:
                await self._ws.send(json.dumps({"type": "keepalive"}))
            except websockets.exceptions.ConnectionClosed:
                self._closed = True
                self._commit_event.set()
            except Exception:
                log.exception("Soniox STT keepalive failed")
                self._closed = True
                self._commit_event.set()

    async def send_silence(self, duration_ms: int = 200) -> None:
        if self._closed or self._ws is None or duration_ms <= 0:
            return
        samples = max(1, int(self._sample_rate * duration_ms / 1000))
        payload = b"\x00\x00" * samples
        chunk_size = _stream_chunk_size(self._sample_rate)
        for offset in range(0, len(payload), chunk_size):
            await self.send_audio(payload[offset : offset + chunk_size])
            await asyncio.sleep(0.02)

    def claim_finalization(self) -> bool:
        if self._finalization_claimed:
            return False
        self._finalization_claimed = True
        return True

    def _joined_committed(self) -> str:
        return normalize_transcript_abbreviations(
            " ".join(part.strip() for part in self._committed if part.strip()).strip(),
            self._language,
        )

    async def wait_committed(self, timeout_s: float) -> tuple[str, str]:
        try:
            await asyncio.wait_for(self._commit_event.wait(), timeout=max(0.0, timeout_s))
        except asyncio.TimeoutError:
            pass
        return self._joined_committed(), normalize_lang(self._language)

    async def wait_committed_with_keepalive(self, timeout_s: float, interval_s: float = 0.5) -> tuple[str, str]:
        timeout_s = max(0.0, timeout_s)
        interval_s = max(0.1, interval_s)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not self._commit_event.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._commit_event.wait(), timeout=min(interval_s, remaining))
            except asyncio.TimeoutError:
                await self.send_keepalive()
        return self._joined_committed(), normalize_lang(self._language)

    async def finalize(
        self,
        fallback_audio: bytes | None = None,
        allow_fallback: bool = True,
        timeout_s: float | None = None,
    ) -> tuple[str, str]:
        if self._closed:
            if allow_fallback and fallback_audio is not None and self._batch_stt is not None:
                return await self._batch_stt.transcribe(fallback_audio, language=self._language or None)
            return "", normalize_lang(self._language)
        if self._ws is not None:
            async with self._lock:
                try:
                    await self._ws.send(json.dumps({"type": "finalize"}))
                except websockets.exceptions.ConnectionClosed:
                    self._closed = True
                    self._commit_event.set()
                except Exception:
                    log.exception("Soniox STT finalize failed")
                    self._closed = True
                    self._commit_event.set()
        text, lang = await self.wait_committed(
            SONIOX_STT_REALTIME_FINALIZE_TIMEOUT_S if timeout_s is None else timeout_s
        )
        await self.close()
        if text or fallback_audio is None or not allow_fallback:
            return text, lang
        if self._batch_stt is None:
            return text, lang
        return await self._batch_stt.transcribe(fallback_audio, language=lang or None)

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener
            self._listener = None


    def _append_committed(self, text: str) -> bool:
        normalized = " ".join(text.lower().split())
        if not normalized or normalized in self._committed_norms:
            return False
        self._committed_norms.add(normalized)
        self._committed.append(text)
        return True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def has_audio(self) -> bool:
        return self._audio_bytes_sent > 0


def _prepare_one_shot_audio(audio_bytes: bytes) -> tuple[str, int, bytes]:
    if guess_extension(audio_bytes) == "wav":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
                if wav.getnchannels() == 1 and wav.getsampwidth() == 2:
                    return SONIOX_STT_AUDIO_FORMAT, wav.getframerate(), wav.readframes(wav.getnframes())
        except wave.Error:
            pass
    if guess_extension(audio_bytes) != "bin":
        return "auto", SONIOX_STT_SAMPLE_RATE, audio_bytes
    return SONIOX_STT_AUDIO_FORMAT, SONIOX_STT_SAMPLE_RATE, audio_bytes


def _stream_chunk_size(sample_rate: int) -> int:
    return max(3200, int(sample_rate * 0.1) * 2)


def _soniox_config(
    preferred_language: str | None = None,
    audio_format: str | None = None,
    sample_rate: int | None = None,
) -> dict[str, Any]:
    audio_format = audio_format or SONIOX_STT_AUDIO_FORMAT
    hints = list(SONIOX_STT_LANGUAGE_HINTS or ["en", "ru", "kk"])
    if preferred_language and preferred_language in hints:
        hints = [preferred_language, *[item for item in hints if item != preferred_language]]
    config: dict[str, Any] = {
        "api_key": SONIOX_API_KEY,
        "model": SONIOX_STT_MODEL,
        "audio_format": audio_format,
        "language_hints": hints,
        "language_hints_strict": SONIOX_STT_LANGUAGE_HINTS_STRICT,
        "enable_language_identification": True,
        "enable_endpoint_detection": SONIOX_STT_ENABLE_ENDPOINT_DETECTION,
        "max_endpoint_delay_ms": SONIOX_STT_MAX_ENDPOINT_DELAY_MS,
        "context": _load_soniox_context_from_audio_json(),
    }
    if audio_format in {"s16le", "pcm_s16le"}:
        config["sample_rate"] = sample_rate or SONIOX_STT_SAMPLE_RATE
        config["num_channels"] = 1
    config["context"] = _fit_context_to_limit(config["context"])
    return config


def _load_soniox_context_from_audio_json() -> dict[str, Any]:
    try:
        payload = json.loads(SONIOX_STT_CONTEXT_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Soniox STT context file not found: {SONIOX_STT_CONTEXT_FILE}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Soniox STT context file is invalid JSON: {SONIOX_STT_CONTEXT_FILE}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"Soniox STT context file must contain a JSON object: {SONIOX_STT_CONTEXT_FILE}")

    context = payload.get("context")
    if not isinstance(context, dict):
        raise RuntimeError(
            f"Soniox STT context file must contain a top-level object key named 'context': {SONIOX_STT_CONTEXT_FILE}"
        )

    return _validate_soniox_context(context)


def _validate_soniox_context(context: dict[str, Any]) -> dict[str, Any]:
    general = context.get("general", [])
    text = context.get("text", "")
    terms = context.get("terms", [])

    if not isinstance(general, list):
        raise RuntimeError("Soniox STT context.general must be a list")
    if not isinstance(text, str):
        raise RuntimeError("Soniox STT context.text must be a string")
    if not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
        raise RuntimeError("Soniox STT context.terms must be a list of strings")

    normalized_general: list[dict[str, str]] = []
    for item in general:
        if not isinstance(item, dict):
            raise RuntimeError("Soniox STT context.general items must be objects")
        key = item.get("key")
        value = item.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            raise RuntimeError("Soniox STT context.general items must contain string key and value")
        normalized_general.append({"key": key, "value": value})

    return {
        "general": normalized_general,
        "text": text,
        "terms": list(terms),
    }


def _context_size_chars(context: dict[str, Any]) -> int:
    return len(json.dumps(context, ensure_ascii=False, separators=(",", ":")))


def _fit_context_to_limit(context: dict[str, Any]) -> dict[str, Any]:
    limit = max(1000, SONIOX_STT_CONTEXT_MAX_CHARS)
    if _context_size_chars(context) <= limit:
        return context

    fitted = {
        "general": list(context.get("general") or []),
        "text": str(context.get("text") or ""),
        "terms": list(context.get("terms") or []),
    }
    original_terms = len(fitted["terms"])
    original_text_chars = len(fitted["text"])

    while fitted["terms"] and _context_size_chars(fitted) > limit:
        fitted["terms"].pop()

    while fitted["text"] and _context_size_chars(fitted) > limit:
        overflow = _context_size_chars(fitted) - limit
        keep_chars = max(0, len(fitted["text"]) - overflow - 64)
        fitted["text"] = fitted["text"][:keep_chars].rstrip()

    if _context_size_chars(fitted) > limit:
        fitted["general"] = fitted["general"][:1]

    if _context_size_chars(fitted) > limit:
        log.warning("Soniox STT context exceeds %d chars even after trimming", limit)
        return {"general": [], "text": "", "terms": []}

    log.warning(
        "Trimmed Soniox STT context to fit %d chars: terms %d->%d, text chars %d->%d",
        limit,
        original_terms,
        len(fitted["terms"]),
        original_text_chars,
        len(fitted["text"]),
    )
    return fitted


def _token_confident_enough(token: dict[str, Any]) -> bool:
    confidence = token.get("confidence")
    if not isinstance(confidence, (int, float)):
        return True
    return float(confidence) >= SONIOX_STT_MIN_TOKEN_CONFIDENCE


def _valid_live_text(text: str) -> bool:
    return (
        bool(text)
        and transcript_has_meaningful_speech(text)
        and not is_noise_utterance(text)
    )


def _valid_committed_text(text: str) -> bool:
    return _valid_live_text(text)


def _log_callback_error(task: asyncio.Task) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        exc = task.exception()
        if exc is not None:
            log.error("Soniox final callback failed", exc_info=(type(exc), exc, exc.__traceback__))
