# CLAUDE.md — Avatar System 2

## Project Overview

Realtime conversational avatar demo for AIFC. Full pipeline:

```
User speech → STT (Soniox) → transcript → answer race → Gemini LLM → TTS (Soniox) → SyncTalk avatar frames → browser
```

Working directory: `/home/admin-aifc/avatar-system-2`

---

## Stack & Ports

| Service | Port | Process |
|---|---|---|
| Backend (FastAPI/uvicorn) | 8080 | Python, restartable |
| Frontend (Vite) | 5173 | npm, restartable |
| SyncTalk inference server | 8005 | **Persistent** — do NOT restart with backend |

SyncTalk is persistent. Only restart it when the checkpoint or server code changes. Backend and frontend restarts must leave SyncTalk untouched.

---

## Start / Stop

```bash
# Start SyncTalk ONCE (idempotent — safe to run again, does nothing if already up)
bash scripts/start_synctalk.sh

# Start full stack (backend + frontend; assumes SyncTalk already running)
bash scripts/run_single_avatar.sh

# Stop backend + frontend only
bash scripts/stop_single_avatar.sh

# Backend only
bash scripts/run_ws_backend.sh

# Frontend only
cd frontend && npm run dev
```

Python binary: `/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python`
Override via `WS_BACKEND_PYTHON`.

---

## Configuration

- `config.env` — non-secret defaults, committed to git
- `.env` — secrets and local overrides, gitignored, takes precedence

Key settings to know:
- `SYNCTALK_AVATAR` — checkpoint name under `/home/admin-aifc/SyncTalk_2D/checkpoint/`
- `TTS_PROVIDER=soniox` / `SONIOX_TTS_VOICE=Maya`
- `EXTERNAL_RAG_URL` / `EXTERNAL_RAG_API_KEY` — external (internal) document RAG endpoint
- `GEMINI_MODEL=gemini-3.1-flash-lite`
- `MAX_TTS_CHARS=220` — do NOT lower this; it caps TTS sentences, not the full answer
- `_AVATAR_WORKER_COUNT = 2` in `response_stream.py` — two parallel SyncTalk workers eliminate gap between chunks

---

## Backend Architecture

```
backend/
  main.py              — FastAPI app, lifespan, WebSocket route
  settings.py          — all env-var settings
  external_rag.py      — HTTP client for external RAG service
  startup.py           — prewarm RAG / TTS on startup
  api/
    ws_writer.py       — WebSocket send wrapper
  knowledge/
    llm.py             — build_prompt(), stream_answer() (Gemini)
    rag.py             — local FAISS/Qdrant retrieval
    cache.py           — semantic answer cache
    faq.py             — FAQ fast-path
  pipeline/
    answer_race.py     — run_answer_race(): orchestrates all candidates
    answer_sources.py  — individual candidate coroutines
    answer_common.py   — RaceCandidate, shared helpers
    answer_format.py   — parse Gemini JSON output → spoken/chat
    rag_routing.py     — select_rag_tool(): decides local vs external RAG
    response_stream.py — ResponseStream: TTS → SyncTalk → WebSocket frames
  session/
    session.py         — per-connection state machine, STT loop, turn logic
  utils/
    voice_chunker.py   — sentence splitter for TTS chunking
    tts_pronunciation.py — text normalization per language
    spoken_text.py     — sanitize_spoken_text()
  media/
    audio_utils.py     — PCM ↔ WAV conversion
```

---

## Answer Race Architecture

`run_answer_race()` selects a RAG path based on `select_rag_tool(query)`:

**Public path** (`GEMINI_PUBLIC_RAG_TOOL`):
- Races: FAQ fast-path, semantic cache, local RAG retrieval
- Winner goes to `gemini_local_rag_candidate()` for Gemini generation
- Streaming: `_SpokenFieldExtractor` parses Gemini's `"spoken"` field during generation → `on_spoken_delta` → TTS starts while LLM is still generating

**Internal/external path** (`EXTERNAL_INTERNAL_RAG_TOOL`):
- Triggered by keyword match in `select_rag_tool()` (internal policy, employee, HR terms, etc.)
- `gemini_external_rag_candidate()`: fetches chunks from external RAG API, then passes them to Gemini for generation — same pattern as local RAG
- Also uses `on_spoken_delta` streaming and `_SpokenFieldExtractor`
- Timeout: `EXTERNAL_RAG_TIMEOUT_S + GEMINI_RAG_MAX_WAIT_MS / 1000` (20s total)

**External RAG response format** (the service only returns chunks, never a generated answer):
```json
{"results": [{"content": "...", "similarity": -0.005, "documentName": "...", "assistantName": "..."}]}
```

---

## Key Implementation Details

### Spoken text streaming (latency bottleneck #1 fix)
`_SpokenFieldExtractor` in `answer_sources.py` is a streaming JSON state machine that parses the `"spoken"` field out of Gemini's raw JSON output character-by-character as it arrives. This lets TTS start during LLM generation rather than waiting for the full response. It handles all JSON escape sequences including `\uXXXX`.

### Parallel SyncTalk workers
`_AVATAR_WORKER_COUNT = 2` in `response_stream.py`. Two workers run concurrently so SyncTalk chunk N+1 starts rendering while chunk N frames are being played. Without this, there is a ~900ms idle gap between spoken sentences.

### SyncTalk checkpoint
Current: `aifc-avatar-5-3min_exp_6` — 5747 frames (229s head cycle), better visual quality than the previous 27s cycle checkpoint. Located at `/home/admin-aifc/SyncTalk_2D/checkpoint/aifc-avatar-5-3min_exp_6/`.

### winner_already_streamed guard
In `session.py`: after `run_answer_race()` completes, spoken text was already streamed via `on_spoken_delta` for `gemini_local_rag` and `external_internal_rag` sources. The guard skips `stream.emit_spoken_text(spoken)` to avoid double-emitting.

---

## Smoke Tests

```bash
python scripts/smoke_ws_text.py
python scripts/smoke_ws_interrupt.py
python scripts/capture_ws_tts.py --text "Hello" --out rec_1.wav
python scripts/capture_ws_mp4.py --query "What is AIFC?" --output /tmp/turn.mp4
```

---

## Runtime Artifacts (gitignored)

```
var/backend.log       — backend stdout/stderr
var/synctalk.log      — SyncTalk stdout/stderr
var/synctalk.pid      — SyncTalk PID file
var/debug/responses/  — full Gemini JSON responses per turn
var/debug/tts_chunks/ — per-sentence TTS PCM debug dumps
cache/                — semantic answer cache
```

---

## What NOT to do

- Do not restart SyncTalk when restarting the backend
- Do not lower `MAX_TTS_CHARS` — it only caps individual TTS sentences, not the full spoken answer
- Do not add comma-based sentence splits to `voice_chunker.py` — causes unnatural TTS pauses
- Do not mock the external RAG or Gemini responses in integration paths — use real endpoints
- Do not import from `/home/admin-aifc/avatar_system` (production stack) — keep stacks separate
