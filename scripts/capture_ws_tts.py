from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import wave
from pathlib import Path

import websockets


def merge_wav_chunks(chunks: list[bytes]) -> bytes:
    if not chunks:
        raise RuntimeError("no audio_ready chunks received")

    params = None
    frames = bytearray()
    for chunk in chunks:
        with wave.open(io.BytesIO(chunk), "rb") as wav:
            current = wav.getparams()
            if params is None:
                params = current
            elif current[:3] != params[:3] or current.framerate != params.framerate:
                raise RuntimeError(f"incompatible WAV chunk params: {current} != {params}")
            frames.extend(wav.readframes(wav.getnframes()))

    out = io.BytesIO()
    assert params is not None
    with wave.open(out, "wb") as wav:
        wav.setnchannels(params.nchannels)
        wav.setsampwidth(params.sampwidth)
        wav.setframerate(params.framerate)
        wav.writeframes(bytes(frames))
    return out.getvalue()


async def capture(url: str, query: str, output: Path, timeout_s: float) -> None:
    audio_chunks: list[tuple[int, bytes]] = []
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "text", "text": query}))
        while True:
            payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
            msg_type = payload.get("type")
            if msg_type == "audio_ready":
                audio_chunks.append((int(payload.get("chunk", len(audio_chunks))), base64.b64decode(payload["data"])))
                print("audio_ready", {k: v for k, v in payload.items() if k != "data"})
            elif msg_type in {"response_start", "response_chunk", "answer_payload", "done", "error", "media_error", "status"}:
                print(msg_type, {k: v for k, v in payload.items() if k != "data"})
            if msg_type in {"done", "error"}:
                break

    audio_chunks.sort(key=lambda item: item[0])
    output.write_bytes(merge_wav_chunks([chunk for _, chunk in audio_chunks]))
    print(f"wrote {output} from {len(audio_chunks)} audio chunk(s)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8080/ws")
    parser.add_argument("--query", default="Hello")
    parser.add_argument("--output", default="rec_1.wav")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    asyncio.run(capture(args.url, args.query, Path(args.output), args.timeout))


if __name__ == "__main__":
    main()

