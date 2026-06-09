from __future__ import annotations

import argparse
import asyncio
import base64
import json
import wave
from pathlib import Path

import websockets


def wav_to_pcm16_16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        source_rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if sample_width != 2:
        raise ValueError("Only 16-bit WAV is supported")

    samples = [
        int.from_bytes(raw[i:i + 2], "little", signed=True)
        for i in range(0, len(raw), 2)
    ]
    if channels > 1:
        samples = [
            int(sum(samples[i:i + channels]) / channels)
            for i in range(0, len(samples), channels)
        ]
    if source_rate != 16000:
        target_len = int(len(samples) * 16000 / source_rate)
        samples = [samples[min(len(samples) - 1, int(i * source_rate / 16000))] for i in range(target_len)]
    return b"".join(max(-32768, min(32767, sample)).to_bytes(2, "little", signed=True) for sample in samples)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8080/ws")
    parser.add_argument("--initial-query", default="Explain the AIFC FinTech Lab in detail.")
    parser.add_argument("--barge-audio", required=True)
    args = parser.parse_args()

    audio_path = Path(args.barge_audio)
    final_audio_b64 = base64.b64encode(audio_path.read_bytes()).decode()
    pcm = wav_to_pcm16_16k(audio_path)
    chunk_bytes = 1600 * 2  # 100 ms at 16 kHz mono PCM16

    async with websockets.connect(args.url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "text", "text": args.initial_query}))
        streaming_barge = False
        final_sent = False
        frames = 0

        async def stream_barge() -> None:
            nonlocal final_sent
            for start in range(0, len(pcm), chunk_bytes):
                await ws.send(json.dumps({
                    "type": "audio_chunk",
                    "data": base64.b64encode(pcm[start:start + chunk_bytes]).decode(),
                }))
                await asyncio.sleep(0.08)
            await ws.send(json.dumps({"type": "audio", "data": final_audio_b64}))
            final_sent = True

        while True:
            payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=180))
            msg_type = payload.get("type")
            if msg_type == "frame":
                frames += 1
                continue
            print(msg_type, {k: v for k, v in payload.items() if k != "data"})
            if msg_type == "audio_ready" and not streaming_barge:
                streaming_barge = True
                asyncio.create_task(stream_barge())
            if final_sent and msg_type in {"done", "error", "transcript_empty", "stop_confirmed"}:
                break
        print(f"frames={frames}")


if __name__ == "__main__":
    asyncio.run(main())
