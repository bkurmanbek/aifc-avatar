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
- `_AVATAR_WORKER_COUNT = 2` in `response_stream.py` — two parallel SyncTalk workers
- `AVATAR_TTS_FIRST_SEGMENT_MS` / `AVATAR_TTS_SEGMENT_MS` — PCM sub-segment sizes fed to SyncTalk as TTS streams (default 1000 / 1500). Do NOT shrink the first segment to a few-hundred ms — a tiny first chunk renders choppily and forces an instant transition. See "Streaming media pipeline" below.

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
`_AVATAR_WORKER_COUNT = 2` in `response_stream.py`. Two workers run concurrently so SyncTalk chunk N+1 starts rendering while chunk N frames are being played.

### Streaming media pipeline (anti-stutter — the big one)
The realtime answer path was reworked to eliminate stutter and chunk-transition gaps. Four coupled pieces — change them together, not in isolation:

1. **Binary WS frames.** Avatar frames ship as binary WebSocket messages (magic `0xF1` header: `chunk u16 | turn_id_len u8 | turn_id | JPEG`), NOT base64-in-JSON. Backend: `WsWriter.send_frame_binary()`. Frontend: `ws.binaryType='arraybuffer'` → `onBinaryFrame` → `onFrameBinary` stores a `Blob`; `preloadBitmap` does `createImageBitmap(blob)` (no JSON.parse / atob per frame). `ChunkState.frames` is `(string | Blob)[]`.

2. **PCM sub-segmentation.** `_run_streaming_sentence_batch` flushes ~1s (first) / ~1.5s PCM segments to SyncTalk *as TTS streams in*, instead of waiting for the whole sentence — first frame ~9s→~3s. A long answer becomes ~20 small chunks.

3. **SyncTalk continuous head-pose indexing.** `/infer_stream` takes `start_frame`; `_encode_audio` resumes the head-pose ping-pong from that offset instead of restarting at frame 1. `_queue_wav_segment` assigns `start_frame = self._turn_frame_offset` (advanced per segment). Without this, every segment boundary snapped the head back to pose 1. **Requires a SyncTalk restart when `synctalk_server.py` changes** (separate git repo at `/home/admin-aifc/SyncTalk_2D`).

4. **Frontend gapless audio + lead** (`useChunkPlayback.ts`). Each segment's `AudioBufferSource` is scheduled back-to-back on the AudioContext timeline (`audioCursorRef`, `scheduleChunkAudio`/`prescheduleAhead`) so mid-word seams are gapless. `prebufferReady()` builds a `LIVE_PREBUFFER_S` (~2.2s) lead before the first chunk so TTS-realtime production never starves playback (do NOT short-circuit on chunk-0 `frameDone` — that was the recurring-gap bug). Cross-chunk bitmap preload (`CROSS_CHUNK_PRELOAD`) decodes the next chunk's opening frames before the handoff. The canvas render loop is fully decoupled from React — do NOT add per-chunk React dispatches (they re-render at boundaries and cause frame hitches).

### Intro is a prebuilt MP4
The static intro is a combined H.264+AAC MP4 (`backend/intro.py` `build_intro_video`/`ensure_intro_video`, cached at `cache/intro/video/<avatar>/intro.mp4`), served at `/intro-video/...` and played in a `<video id="introVid">` — hardware-decoded, no per-frame JS. `session.py run_intro` sends `{type:'intro_video',url}` when a valid MP4 exists; falls back to the canvas frame-cache path otherwise. ffmpeg resolves via `_ffmpeg_bin()` (synctalk2d conda env).

**Frontend playback (`App.tsx`):** the intro auto-presents on every page load behind a full-screen **"Tap to start" `<button>`** (`awaitingIntroTap` → `AvatarStage` overlay). Its onClick (`startIntro`) is a real user gesture, which is required to play with sound. There is **no dock intro button** — stop the intro via the primary interrupt control. Two non-obvious constraints, do NOT regress them:
- **Fetch the MP4 as a blob, never as a bare `<video src>`.** `loadIntroBlob()` does `fetch(...)` → `URL.createObjectURL(blob)` → `v.src`. This was originally required because the ngrok-free backend served an **interstitial HTML** page to bare `<video>` requests (browser UA, no skip header) → `MEDIA_ERR_SRC_NOT_SUPPORTED` (code=4). The backend is now behind **Cloudflare Tunnel** which has no interstitial, so a bare `<video src>` would work too — but the blob fetch is kept (harmless, and portable if the transport changes). The fetch still sends `ngrok-skip-browser-warning: true`, which Cloudflare ignores.
- **Start the intro from a user-gesture handler** (the overlay button), not from the WS `intro_video` message handler — autoplay-with-sound is blocked otherwise. `preloadIntro` warms the blob on `intro_video`; sticky activation from the tap permits sound across the async fetch.

### Public deployment (Vercel + Cloudflare Tunnel) — see `DEPLOY.md`
Frontend on **Vercel** (`frontend-five-lemon-98.vercel.app`), backend stays on the H200 box exposed via a **named Cloudflare Tunnel** at `https://avatar.bk-project.org`. Single env knob `VITE_BACKEND_ORIGIN` drives the WS URL and all backend asset URLs (`frontend/src/utils.ts`). Backend + tunnel run as **user systemd units** (`deploy/systemd/avatar-backend.service`, `avatar-cloudflared.service`, linger-enabled); the backend unit must set `LD_LIBRARY_PATH=/home/admin-aifc/miniforge3/lib`. `DEPLOY.md` is the runbook.

**Transport history (do NOT regress):** originally ngrok-free, but it couldn't sustain the ~10 Mbps binary avatar-frame stream (~50 KB JPEG × 25 fps) — frames backed up in the tunnel and playback stuttered (client render lag grew to ~5s; `client_first_render` − `first_frame` in the `pipeline_done` metrics is the tell). The box uplink is ~88 Mbps, so the box was never the bottleneck. Cloudflare Tunnel (no throttle, Almaty PoP) dropped the lag to ~2.2s (the designed `LIVE_PREBUFFER_S`) at full frame quality. ngrok is retired/disabled. If frames stutter again over the network, check the tunnel/bandwidth FIRST — the LAN-tuned client buffer can't paper over a transport throughput deficit.

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
- Do not switch the heavy binary frame transport back to ngrok-free — it throttles the ~10 Mbps frame stream and reintroduces stutter. The backend is on a Cloudflare Tunnel; see deployment section
- Keep loading the intro MP4 via the header-bearing blob fetch (`loadIntroBlob`), not a bare `<video src>` — portable across transports (see intro section)
- Do not start the intro from the WS `intro_video` handler — autoplay-with-sound needs a user gesture; start it from the "Tap to start" button's onClick
