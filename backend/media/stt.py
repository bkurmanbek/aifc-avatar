from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

import websockets

from ..utils.language import (
    is_noise_utterance,
    normalize_lang,
    supported_lang_or_none,
    transcript_has_meaningful_speech,
    is_interrupt_candidate,
)
from ..settings import (
    SONIOX_API_KEY,
    SONIOX_STT_AUDIO_FORMAT,
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
from ..logging_config import log_event, preview_text
from ..api.ws_writer import ClientClosedError, WsWriter

log = logging.getLogger(__name__)

SONIOX_STT_CONTEXT: dict[str, Any] = {
    "general": [
        {"key": "domain", "value": "Astana International Financial Centre (AIFC, МФЦА, АХҚО)"},
        {
            "key": "scope",
            "value": "AIFC Authority, AFSA, AIX, AIFC Expat Centre, IAC, AIFC Green Finance Centre, Carbon Platform, FinTech Lab",
        },
    ],
    "terms": [
        "AIFC", "МФЦА", "АХҚО", "AFSA", "AIFCA", "AIX", "AEC", "IAC", "GFC", "AML",
        "CTF", "KYC", "IIN", "ИИН", "EDS", "JSC", "TRP", "PE", "VAT", "CFC",
        "MCI", "VCM", "ETS", "СТВ", "GHG", "ПГ", "CO2", "VCS", "REC", "СВЭ",
        "ICAP", "ICVCM", "VCMI", "CCP", "CCB", "REDD", "CORSIA", "ICAO", "ESG",
        "ICROA", "IFM", "NFE", "MW", "KZT", "USD", "ВИЭ", "EUA", "FEAS", "CIBAFI",
        "CEO", "АФСА", "ЦЗФ", "ЖҚО", "ПОД", "ПФТ", "ЖСН", "ЭЦП", "ЭЦҚ", "АО",
        "АҚ", "РВП", "НДС", "ҚҚС", "КИК", "МРП", "АЕК", "ШСЖ", "GHGs", "CO₂",
        "I-REC", "I-RECs", "RECs", "ИКАО", "RES", "ЖЭК", "FinTech Lab", "Expat Centre",
        "AIFC Portal", "Public Register", "Carbon Platform", "Astana International Financial Centre",
        "Международный финансовый центр «Астана»", "Астана Халықаралық Қаржы Орталығы",
        "Astana Financial Services Authority", "Комитет МФЦА по регулированию финансовых услуг",
        "АХҚО Қаржылық қызметтер көрсетуді реттеу жөніндегі комитеті", "AIFC Authority",
        "Администрация МФЦА", "АХҚО әкімшілігі", "Astana International Exchange",
        "Астанинская международная биржа", "Астана Халықаралық Биржасы", "AIFC Expat Centre",
        "Экспат центр МФЦА", "АХҚО Экспат орталығы", "International Arbitration Centre",
        "AIFC Green Finance Centre", "Центр зеленых финансов МФЦА", "АХҚО Жасыл қаржы орталығы",
        "Anti-Money Laundering", "Counter-Terrorism Financing", "Know Your Customer",
        "Individual Identification Number", "Electronic Digital Signature", "Temporary Residence Permit",
        "Value Added Tax", "Controlled Foreign Companies", "Monthly Calculation Index",
        "Voluntary Carbon Market", "Добровольный углеродный рынок", "Ерікті көміртегі нарығы",
        "Emissions Trading System", "Система торговли выбросами", "Шығарындылармен сауда жүйесі",
        "Renewable Energy Certificate", "International Renewable Energy Certificate",
        "Core Carbon Principles", "Environmental, Social, and Governance",
        "Federation of Euro-Asian Stock Exchanges",
        "General Council for Islamic Banks and Financial Institutions",
    ],
}

RealtimeCallback = Callable[[str], Any]
RealtimePartialCallback = Callable[[str], Awaitable[None]]
RealtimeFinalCallback = Callable[[str, str], Awaitable[None]]


def looks_like_pcm16_chunk(data: bytes) -> bool:
    if len(data) < 80 or len(data) % 2 != 0:
        return False
    header = data[:12]
    if header.startswith(b"RIFF") or header.startswith(b"\x1a\x45\xdf\xa3") or header.startswith(b"ID3") or header.startswith(b"OggS"):
        return False
    return True


class SonioxRealtimeSession:
    def __init__(
        self,
        writer: WsWriter | None,
        on_meaningful_partial: RealtimePartialCallback | None = None,
        on_final_utterance: RealtimeFinalCallback | None = None,
        preferred_language: str | None = None,
        audio_format: str | None = None,
        sample_rate: int | None = None,
        session_id: str | None = None,
    ):
        self._writer = writer
        self._on_meaningful_partial = on_meaningful_partial
        self._on_final_utterance = on_final_utterance
        self._session_id = session_id
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
        self._audio_chunks_sent = 0

    async def start(self) -> None:
        if not SONIOX_API_KEY:
            raise RuntimeError("SONIOX_API_KEY is not configured")
        started = perf_counter()
        self._ws = await websockets.connect(SONIOX_STT_WS_URL, max_size=None)
        config = _soniox_config(self._language, self._audio_format, self._sample_rate)
        log_event(log, "stt_config_send", session_id=self._session_id, **_stt_config_summary(config))
        await self._ws.send(json.dumps(config))
        self._listener = asyncio.create_task(self._listen())
        log_event(log, "stt_ws_started", session_id=self._session_id, latency_ms=(perf_counter() - started) * 1000)

    async def _listen(self) -> None:
        if self._ws is None:
            return
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                if message.get("error_type") or message.get("error_code") or message.get("error_message"):
                    log.warning("Soniox STT error: %s", message)
                    log_event(
                        log,
                        "stt_provider_error",
                        session_id=self._session_id,
                        error_type=message.get("error_type"),
                        error_code=message.get("error_code"),
                        error_message=message.get("error_message"),
                    )
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

        partial_text = "".join(partial_parts).strip()
        if _valid_live_text(partial_text):
            log_event(
                log,
                "stt_partial_received",
                session_id=self._session_id,
                language=normalize_lang(self._language),
                chars=len(partial_text),
                text_preview=preview_text(partial_text, 160),
            )
            await self._send_partial(partial_text)

        if final_marker is not None:
            final_text = "".join(self._pending_final_tokens).strip()
            self._pending_final_tokens = []
            if _valid_committed_text(final_text):
                log_event(
                    log,
                    "stt_final_received",
                    session_id=self._session_id,
                    marker=final_marker,
                    language=normalize_lang(self._language),
                    chars=len(final_text),
                    audio_bytes=self._audio_bytes_sent,
                    audio_chunks=self._audio_chunks_sent,
                    text_preview=preview_text(final_text, 240),
                )
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
            else:
                log_event(
                    log,
                    "stt_final_ignored",
                    session_id=self._session_id,
                    marker=final_marker,
                    chars=len(final_text),
                    text_preview=preview_text(final_text, 160),
                )
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
                self._audio_chunks_sent += 1
                if self._audio_chunks_sent == 1 or self._audio_chunks_sent % 25 == 0:
                    log_event(
                        log,
                        "stt_audio_sent",
                        session_id=self._session_id,
                        bytes_total=self._audio_bytes_sent,
                        chunks=self._audio_chunks_sent,
                    )
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

    def reset_utterance_state(self) -> None:
        self._committed.clear()
        self._committed_norms.clear()
        self._pending_final_tokens.clear()
        self._commit_event.clear()
        self._finalization_claimed = False
        self._audio_bytes_sent = 0
        self._audio_chunks_sent = 0

    def _joined_committed(self) -> str:
        return " ".join(part.strip() for part in self._committed if part.strip()).strip()

    async def wait_committed(self, timeout_s: float) -> tuple[str, str]:
        try:
            await asyncio.wait_for(self._commit_event.wait(), timeout=max(0.0, timeout_s))
        except asyncio.TimeoutError:
            pass
        return self._joined_committed(), normalize_lang(self._language)

    async def finalize(
        self,
        timeout_s: float | None = None,
        close_after: bool = True,
    ) -> tuple[str, str]:
        if self._closed:
            return "", normalize_lang(self._language)
        started = perf_counter()
        log_event(
            log,
            "stt_finalize_send",
            session_id=self._session_id,
            audio_bytes=self._audio_bytes_sent,
            audio_chunks=self._audio_chunks_sent,
            close_after=close_after,
        )
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
        if close_after:
            await self.close()
        log_event(
            log,
            "stt_finalize_done",
            session_id=self._session_id,
            latency_ms=(perf_counter() - started) * 1000,
            language=lang,
            chars=len(text),
            text_preview=preview_text(text, 240),
        )
        return text, lang

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
        "context": _validate_soniox_context(SONIOX_STT_CONTEXT),
    }
    if audio_format in {"s16le", "pcm_s16le"}:
        config["sample_rate"] = sample_rate or SONIOX_STT_SAMPLE_RATE
        config["num_channels"] = 1
    config["context"] = _fit_context_to_limit(config["context"])
    return config


def _stt_config_summary(config: dict[str, Any]) -> dict[str, object]:
    context = config.get("context") if isinstance(config.get("context"), dict) else {}
    terms = context.get("terms") if isinstance(context, dict) else []
    general = context.get("general") if isinstance(context, dict) else []
    text = context.get("text") if isinstance(context, dict) else ""
    hints = config.get("language_hints") or []
    return {
        "model": config.get("model"),
        "audio_format": config.get("audio_format"),
        "sample_rate": config.get("sample_rate"),
        "num_channels": config.get("num_channels"),
        "language_hints": ",".join(str(item) for item in hints),
        "language_hints_strict": config.get("language_hints_strict"),
        "endpoint_detection": config.get("enable_endpoint_detection"),
        "max_endpoint_delay_ms": config.get("max_endpoint_delay_ms"),
        "context_chars": _context_size_chars(context) if isinstance(context, dict) else 0,
        "context_terms": len(terms) if isinstance(terms, list) else 0,
        "context_general": len(general) if isinstance(general, list) else 0,
        "context_text_chars": len(text) if isinstance(text, str) else 0,
    }


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
