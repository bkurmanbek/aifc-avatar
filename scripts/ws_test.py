"""
WebSocket end-to-end test: sends text queries, measures latency,
checks answer quality, and reports TTS audio + SyncTalk frame stats.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field

import websockets

WS_URL = "ws://localhost:8080/ws"

QUERIES = [
    "What is AIFC?",
    "How can I register a company in AIFC?",
    "What is AFSA?",
]


@dataclass
class TurnResult:
    query: str
    t_send: float = 0.0
    t_response_start: float = 0.0
    t_first_chunk: float = 0.0
    t_first_audio: float = 0.0
    t_first_frame: float = 0.0
    t_answer_payload: float = 0.0
    t_done: float = 0.0
    chat_chunks: list[str] = field(default_factory=list)
    spoken: str = ""
    chat: str = ""
    winner_source: str = ""
    winner_confidence: str = ""
    winner_score: float = 0.0
    audio_chunks: int = 0
    frame_count: int = 0
    server_latency: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ttfb_ms(self) -> float:
        if self.t_response_start and self.t_send:
            return (self.t_response_start - self.t_send) * 1000
        return 0.0

    @property
    def first_chunk_ms(self) -> float:
        if self.t_first_chunk and self.t_send:
            return (self.t_first_chunk - self.t_send) * 1000
        return 0.0

    @property
    def first_audio_ms(self) -> float:
        if self.t_first_audio and self.t_send:
            return (self.t_first_audio - self.t_send) * 1000
        return 0.0

    @property
    def first_frame_ms(self) -> float:
        if self.t_first_frame and self.t_send:
            return (self.t_first_frame - self.t_send) * 1000
        return 0.0

    @property
    def total_ms(self) -> float:
        if self.t_done and self.t_send:
            return (self.t_done - self.t_send) * 1000
        return 0.0


async def run_query(ws, query: str, timeout: float = 30.0) -> TurnResult:
    result = TurnResult(query=query)
    result.t_send = time.perf_counter()
    await ws.send(json.dumps({"type": "text", "text": query}))

    deadline = result.t_send + timeout
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            result.errors.append("timeout waiting for done")
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5.0))
        except asyncio.TimeoutError:
            result.errors.append("recv timeout")
            break

        now = time.perf_counter()
        try:
            msg = json.loads(raw)
        except Exception:
            continue

        mtype = msg.get("type")

        if mtype == "response_start":
            if not result.t_response_start:
                result.t_response_start = now

        elif mtype == "response_chunk":
            text = msg.get("text", "")
            if text:
                result.chat_chunks.append(text)
                if not result.t_first_chunk:
                    result.t_first_chunk = now

        elif mtype == "audio_ready":
            result.audio_chunks += 1
            if not result.t_first_audio:
                result.t_first_audio = now

        elif mtype == "frame":
            result.frame_count += 1
            if not result.t_first_frame:
                result.t_first_frame = now

        elif mtype == "answer_payload":
            result.t_answer_payload = now
            result.spoken = msg.get("spoken", "")
            result.chat = msg.get("chat", "")
            result.winner_source = str(msg.get("winner_source", ""))
            result.winner_confidence = str(msg.get("winner_confidence", ""))
            result.winner_score = float(msg.get("winner_score") or 0.0)

        elif mtype == "done":
            result.t_done = now
            result.server_latency = msg.get("latency_ms", {})
            break

        elif mtype == "error":
            result.errors.append(f"server error: {msg.get('text')}")

    return result


def print_result(r: TurnResult, idx: int) -> None:
    sep = "─" * 72
    print(f"\n{sep}")
    print(f"  Query {idx}: {r.query!r}")
    print(sep)

    # Latency
    print("  LATENCY (client-side, ms from send)")
    print(f"    response_start  : {r.ttfb_ms:>7.0f} ms")
    print(f"    first chat chunk: {r.first_chunk_ms:>7.0f} ms")
    print(f"    first TTS audio : {r.first_audio_ms:>7.0f} ms  ({r.audio_chunks} chunks total)")
    print(f"    first frame     : {r.first_frame_ms:>7.0f} ms  ({r.frame_count} frames total)")
    print(f"    total (done)    : {r.total_ms:>7.0f} ms")

    # Server-reported latency breakdown
    if r.server_latency:
        print("  SERVER LATENCY BREAKDOWN (ms)")
        for k, v in r.server_latency.items():
            if isinstance(v, (int, float)):
                print(f"    {k:<22}: {int(v):>6} ms")
            elif v:
                print(f"    {k:<22}: {v}")

    # Answer source
    print(f"  ANSWER SOURCE: {r.winner_source}  confidence={r.winner_confidence}  score={r.winner_score:.3f}")

    # Quality — spoken
    print(f"  SPOKEN ({len(r.spoken)} chars):")
    print(f"    {r.spoken[:300]}")

    # Quality — chat
    chat_preview = (r.chat or "").strip()[:500]
    print(f"  CHAT ({len(r.chat)} chars):")
    for line in chat_preview.splitlines():
        print(f"    {line}")

    # SyncTalk frames
    if r.frame_count == 0:
        print("  FRAMES: ⚠ none received")
    else:
        print(f"  FRAMES: {r.frame_count} received — first at {r.first_frame_ms:.0f} ms")

    if r.errors:
        print(f"  ERRORS: {r.errors}")


async def main() -> None:
    print(f"Connecting to {WS_URL} …")
    async with websockets.connect(WS_URL, max_size=20 * 1024 * 1024) as ws:
        # Wait for session_state
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        sess = json.loads(raw)
        print(f"Connected — session_id={sess.get('session_id')}\n")

        results: list[TurnResult] = []
        for i, query in enumerate(QUERIES, 1):
            print(f"Sending query {i}/{len(QUERIES)}: {query!r} …")
            r = await run_query(ws, query)
            results.append(r)
            print_result(r, i)
            if i < len(QUERIES):
                await asyncio.sleep(1.0)

    # Summary table
    print("\n" + "=" * 72)
    print("  SUMMARY")
    print("=" * 72)
    print(f"  {'Query':<38} {'Src':<18} {'1stAudio':>8} {'Frames':>6} {'Total':>7}")
    print(f"  {'-'*38} {'-'*18} {'-'*8} {'-'*6} {'-'*7}")
    for r in results:
        q = r.query[:37]
        print(f"  {q:<38} {r.winner_source:<18} {r.first_audio_ms:>7.0f}ms {r.frame_count:>6} {r.total_ms:>6.0f}ms")

    ok = all(r.spoken and r.chat and not r.errors for r in results)
    print(f"\n  Overall: {'✓ PASS' if ok else '✗ FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
