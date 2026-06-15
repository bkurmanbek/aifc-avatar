from __future__ import annotations

import io
import wave


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
