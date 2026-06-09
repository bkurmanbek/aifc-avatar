from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path

import websockets


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8080/ws")
    parser.add_argument("--audio", required=True)
    args = parser.parse_args()

    audio_path = Path(args.audio)
    audio_bytes = audio_path.read_bytes()

    async with websockets.connect(args.url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "audio", "data": base64.b64encode(audio_bytes).decode()}))
        frames = 0
        while True:
            payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=180))
            msg_type = payload.get("type")
            if msg_type == "frame":
                frames += 1
                continue
            print(msg_type, {k: v for k, v in payload.items() if k != "data"})
            if msg_type in {"done", "error", "transcript_empty", "stop_confirmed"}:
                break
        print(f"frames={frames}")


if __name__ == "__main__":
    asyncio.run(main())
