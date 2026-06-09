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
    parser.add_argument("--query", default="Explain the AIFC FinTech Lab in detail.")
    parser.add_argument("--stop-audio", required=True)
    args = parser.parse_args()

    stop_audio = base64.b64encode(Path(args.stop_audio).read_bytes()).decode()

    async with websockets.connect(args.url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "text", "text": args.query}))
        sent_stop = False
        while True:
            payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
            msg_type = payload.get("type")
            if msg_type != "frame":
                print(msg_type, {k: v for k, v in payload.items() if k != "data"})
            if msg_type == "audio_ready" and not sent_stop:
                sent_stop = True
                await ws.send(json.dumps({"type": "audio", "data": stop_audio}))
            if msg_type in {"stop_confirmed", "error", "done"}:
                break


if __name__ == "__main__":
    asyncio.run(main())
