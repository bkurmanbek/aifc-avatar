# Realtime Avatar Architecture

The canonical source layout is now:

- `backend/app`: FastAPI entrypoint.
- `backend/api`: HTTP API surface.
- `backend/core`: logging and process-level infrastructure.
- `backend/config`: settings adapters.
- `backend/stt`: STT providers.
- `backend/llm`: LLM providers.
- `backend/tts`: TTS providers.
- `backend/avatar`: SyncTalk/avatar providers.
- `backend/websocket`: WebSocket transport helpers.
- `backend/pipeline`: session orchestration and streaming response pipeline.
- `backend/legacy`: moved production implementation kept behind canonical adapters.
- `frontend/src/components`: visual components.
- `frontend/src/hooks`: microphone, websocket, playback, and idle hooks.
- `frontend/src/services`: browser-side services such as client logging.
- `frontend/src/state`: conversation reducer.
- `frontend/src/utils.ts`: browser utility functions.

The compatibility package `ws_backend` remains so older smoke scripts and local tools can still import `ws_backend.settings`, `ws_backend.tts`, and related modules.

## Pipeline

1. Browser captures microphone audio at sixteen kilohertz and sends low-latency PCM chunks over WebSocket.
2. Backend opens a realtime Soniox STT websocket per browser session and keeps it open across utterances until browser disconnect or backend reset.
3. Partial transcripts are sent to the frontend immediately as `partial`.
4. Final transcripts are normalized, de-duplicated, checked for language/support, then emitted as `transcript`.
5. A cancellable pipeline turn is created with a `turn_id`.
6. The answer race gathers direct replies, RAG, and Gemini streaming output.
7. Text deltas feed `ResponseStream`, which chunks spoken text at sentence or low-latency word boundaries.
8. Backend opens a Soniox realtime TTS websocket per browser session and keeps it open across TTS chunks/turns until browser disconnect or backend reset.
9. TTS PCM is segmented into short WAV chunks and sent to the browser as `audio_ready`.
10. The same WAV chunks are streamed to SyncTalk, which returns avatar frames as `frame`.
11. Browser playback waits for a small frame headroom, then renders audio and frames in order.
12. Interrupts cancel the active pipeline task, clear the active turn, and stale `turn_id` events are ignored.

## Cancellation

Each response turn uses `turn_id` as the request ID. The backend writer drops stale turn-bound messages after interruption, and the frontend rejects stale turn-bound events before they mutate state or playback.

## Latency Defaults

The first avatar segment defaults to three hundred twenty milliseconds. Frontend playback starts after four live frames. The voice chunker now cuts at the configured first/min/max thresholds instead of waiting for very long punctuation-free text.
