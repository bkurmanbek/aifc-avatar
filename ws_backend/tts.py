from __future__ import annotations

import io
import wave
from collections.abc import AsyncGenerator, AsyncIterable

import httpx

from .soniox_tts import SonioxRealtimeTTS
from .spoken_text import sanitize_spoken_text
from .settings import (
    ELEVEN_API_KEY,
    ELEVEN_TTS_MODEL,
    ELEVEN_TTS_MODEL_KK,
    ELEVEN_TTS_OUTPUT_FORMAT,
    ELEVEN_TTS_SIMILARITY_BOOST,
    ELEVEN_TTS_STABILITY,
    ELEVEN_TTS_STYLE,
    ELEVEN_TTS_USE_SPEAKER_BOOST,
    ELEVEN_TTS_VOICE_ID,
    LOCAL_TTS_URL,
    SONIOX_TTS_CONTEXT_FILE,
    SONIOX_TTS_CONNECT_TIMEOUT_S,
    SONIOX_TTS_SAMPLE_RATE,
    SONIOX_TTS_STREAMING_AVATAR,
    TTS_PROVIDER,
)
from .tts_pronunciation import prepare_tts_text

_SHARED_SONIOX_TTS = SonioxRealtimeTTS() if TTS_PROVIDER == "soniox" else None


def _pcm_sample_rate(output_format: str) -> int:
    try:
        return int(output_format.split("_")[1])
    except Exception:
        return 22050


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def silent_wav_bytes(sample_rate: int, duration_ms: int = 250) -> bytes:
    frames = int(sample_rate * duration_ms / 1000)
    return pcm_to_wav_bytes(b"\x00\x00" * frames, sample_rate)


class ElevenTTS:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=60)
        self._soniox = _SHARED_SONIOX_TTS
        self._sample_rate = SONIOX_TTS_SAMPLE_RATE if self._soniox else _pcm_sample_rate(ELEVEN_TTS_OUTPUT_FORMAT)

    @property
    def supports_streaming_avatar(self) -> bool:
        return bool(
            self._soniox is not None
            and self._soniox.is_pcm_s16le
            and SONIOX_TTS_STREAMING_AVATAR
        )

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def preconnect(self) -> None:
        if self._soniox is not None:
            await self._soniox.preconnect()

    async def synthesize(
        self,
        text: str,
        language: str | None = None,
        *,
        lang: str | None = None,
        priority: int | None = None,
    ) -> bytes:
        language = language or lang
        text = prepare_tts_text(text, language, SONIOX_TTS_CONTEXT_FILE)
        if not text:
            return silent_wav_bytes(self._sample_rate)
        if self._soniox is not None:
            pcm = bytearray()
            async for chunk in self._soniox.synthesize_pcm_stream(text, language=language):
                pcm.extend(chunk)
            return pcm_to_wav_bytes(bytes(pcm), self._sample_rate)
        if TTS_PROVIDER == "local":
            try:
                response = await self._client.post(
                    LOCAL_TTS_URL,
                    json={"text": text, "lang": language, "priority": 0 if priority is None else priority},
                )
                response.raise_for_status()
                payload = response.json()
                audio_b64 = payload.get("audio_b64")
                if audio_b64:
                    import base64

                    return base64.b64decode(audio_b64)
            except Exception:
                raise

        language_code = language if language in {"en", "ru", "kk", "zh"} else None
        requested_models = [ELEVEN_TTS_MODEL_KK if language == "kk" else ELEVEN_TTS_MODEL]
        if language == "kk" and ELEVEN_TTS_MODEL_KK != ELEVEN_TTS_MODEL:
            requested_models.append(ELEVEN_TTS_MODEL)
        if language == "en" and ELEVEN_TTS_MODEL == ELEVEN_TTS_MODEL_KK:
            requested_models = [ELEVEN_TTS_MODEL]

        last_error = None
        for model_id in requested_models:
            try:
                payload = {
                    "text": text,
                    "model_id": model_id,
                    "voice_settings": {
                        "stability": ELEVEN_TTS_STABILITY,
                        "similarity_boost": ELEVEN_TTS_SIMILARITY_BOOST,
                        "style": ELEVEN_TTS_STYLE,
                        "use_speaker_boost": ELEVEN_TTS_USE_SPEAKER_BOOST,
                    },
                }
                if language_code:
                    payload["language_code"] = language_code

                response = await self._client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_TTS_VOICE_ID}",
                    params={"output_format": ELEVEN_TTS_OUTPUT_FORMAT},
                    headers={"xi-api-key": ELEVEN_API_KEY, "Content-Type": "application/json"},
                    json=payload,
                )
                response.raise_for_status()
                return pcm_to_wav_bytes(response.content, self._sample_rate)
            except Exception as exc:  # pragma: no cover - network/service-level fallback behavior
                last_error = exc
                continue

        raise last_error if last_error is not None else RuntimeError("TTS synthesis failed")

    async def synthesize_pcm_stream(
        self,
        text: str,
        *,
        language: str | None = None,
        lang: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        language = language or lang
        text = prepare_tts_text(text, language, SONIOX_TTS_CONTEXT_FILE)
        if not text:
            return
        if self._soniox is None:
            wav = await self.synthesize(text, language=language)
            yield wav
            return
        async for chunk in self._soniox.synthesize_pcm_stream(text, language=language):
            if chunk:
                yield chunk

    async def synthesize_pcm_stream_from_texts(
        self,
        texts: AsyncIterable[str],
        *,
        language: str | None = None,
        lang: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        language = language or lang

        async def prepared_texts() -> AsyncGenerator[str, None]:
            async for text in texts:
                prepared = prepare_tts_text(text, language, SONIOX_TTS_CONTEXT_FILE)
                if prepared:
                    yield prepared if prepared.endswith((" ", "\n")) else f"{prepared} "

        if self._soniox is None:
            async for text in prepared_texts():
                wav = await self.synthesize(text, language=language)
                if wav:
                    yield wav
            return

        async for chunk in self._soniox.synthesize_pcm_stream_from_texts(
            prepared_texts(),
            language=language,
        ):
            if chunk:
                yield chunk

    async def close(self) -> None:
        await self._client.aclose()


async def preconnect_shared_tts() -> None:
    if _SHARED_SONIOX_TTS is not None:
        await _SHARED_SONIOX_TTS.preconnect(attempts=2, timeout_s=min(SONIOX_TTS_CONNECT_TIMEOUT_S, 8.0))


async def close_shared_tts() -> None:
    if _SHARED_SONIOX_TTS is not None:
        await _SHARED_SONIOX_TTS.close()
