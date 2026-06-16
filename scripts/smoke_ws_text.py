from __future__ import annotations

import argparse
import asyncio
import json

import websockets


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8080/ws")
    parser.add_argument("--query", default="What is the AIFC FinTech Lab?")
    args = parser.parse_args()

    async with websockets.connect(args.url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "text", "text": args.query}))
        frames = 0
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            # Avatar frames arrive as binary WS messages (magic 0xF1); JSON for everything else.
            if isinstance(raw, (bytes, bytearray)):
                frames += 1
                continue
            payload = json.loads(raw)
            msg_type = payload.get("type")
            if msg_type == "frame":  # legacy JSON frame path (intro fallback / cached)
                frames += 1
                continue
            print(msg_type, {k: v for k, v in payload.items() if k != "data"})
            if msg_type in {"done", "error"}:
                break
        print(f"frames={frames}")


if __name__ == "__main__":
    asyncio.run(main())
