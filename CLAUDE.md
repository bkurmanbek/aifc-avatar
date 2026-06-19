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
- `SYNCTALK_PIPELINE=1` / `SYNCTALK_FAST_COMPOSITE=1` — SyncTalk render throughput optimizations (default on; see "SyncTalk throughput optimization" below). Other flags `SYNCTALK_DTYPE` / `SYNCTALK_PROFILE` / `SYNCTALK_GPU_JPEG` exist but default off (measured no-ops/net-losses — do NOT enable without re-benchmarking).
- `FAQ_VIDEO_ENABLED=1` — serve prebuilt FAQ answer MP4s on a confident FAQ fast-path win instead of rendering live (see "FAQ answer video cache" below). Build the cache offline with `scripts/build_faq_videos.py`; cache miss → normal live render.

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

### FAQ answer video cache
An FAQ answer is a static, identical-every-time utterance, so — exactly like the intro — it can be rendered through SyncTalk **once** and served as a hardware-decoded MP4, skipping TTS→SyncTalk (and all GPU) at serve time. Measured: a cached FAQ turn completes in **~720 ms vs ~7.4 s** for the same answer rendered live.

- **Module:** `backend/faq_video.py` (mirrors `intro.py`). The MP4 is keyed by the **exact final `spoken` string** the live turn path would render — i.e. the output of the same `candidate_from_answer → extract_json_any → coerce_spoken_chat_payload → normalize_spoken_for_tts(trim_for_latency=False)` chain that `session.py` runs — plus avatar + voice + language, sha256→24 hex chars. Because build-time and serve-time key off that identical string, a hit is guaranteed to say exactly what we'd otherwise render. Cached at `cache/faq/video/<avatar>/<key>.mp4`, served at `/faq-video/<avatar>/<key>.mp4` (`api/routes.py`, key validated `[0-9a-f]{24}`).
- **Encoder is shared with the intro:** `intro.py encode_frames_to_mp4(frames, wav_bytes, out_path)` (base64 JPEG frames + WAV → H.264/AAC via ffmpeg stdin, stderr to a sidecar log to avoid pipe deadlock). Both `_build_intro_video_locked` and `build_faq_video` call it — do NOT fork it.
- **Offline build only** (`scripts/build_faq_videos.py`): renders every `faq_cacheable_entries()` answer (the parsed `_FAQ_ENTRIES`, ~531 across en/ru/kk) once. Run it manually (SyncTalk must be up); there is **no startup prebuild** (would burst GPU and contend with the live pipeline). Flags: `--limit N`, `--langs`, `--force`, `--dry-run`. It strips `scripts/` from `sys.path` (the `chunk.py`/`wave` shadow, same as `bench_synctalk.py`).
- **Serve-time integration** (`session.py`, after `spoken` is computed): on `race_result.winner.source == "faq"` and `not winner_already_streamed`, `lookup_faq_video(spoken, language)`; on a hit, send `{type:'faq_video',url}`, skip `stream.emit_spoken_text` (chat text still flows), `done` with 0 chunks. **Cache miss → normal live render (zero regression).** Gated by `FAQ_VIDEO_ENABLED`.
- **Non-obvious caveat:** `faq_candidate` first tries `aifc_overview_candidate` (a synthesized overview/capability answer NOT in `_FAQ_ENTRIES`) and only then `_faq_fast_path_lookup`. The build script enumerates the **fast-path entries only**, so overview/capability queries ("what is aifc", "what can you do") are a deliberate cache miss → live render. That's fine; if you want them cached too, enumerate the overview answers as well.
- **Frontend** (`App.tsx`): reuses the intro `<video id="introVid">` via `playCachedVideo(url)` — fetches a fresh per-URL blob (the intro reuses one cached blob), plays in the shared element. `faqVideoActiveRef` makes the `done` handler **skip `playback.onAllDone()`** (a 0-chunk turn would otherwise fire `onAllChunksDone → idle` and cut the clip short); the clip's own `onended` drives idle/busy. FAQ cleanup (revoke blob, clear flag) is folded into `stopIntroVideo`, so every existing interrupt/stop/new-turn path tears the clip down. Barge-in works (mic listens during the clip via `ensureActiveListening`).
- **FAQ speaks the FULL answer:** `faq_candidate` calls `candidate_from_answer(..., trim_spoken=False)` so FAQ answers are spoken in full (other candidates still trim the spoken field to ~4 sentences / 75 words for streaming latency via `_trim_for_first_spoken`). The cached video reproduces that full spoken text. If you re-introduce trimming for FAQ, the videos will be cut and the keys will change.
- **Pronunciation/number normalization is shared** (`backend/utils/spoken_text.py` + `tts_pronunciation.py`) by the live path AND the FAQ video build. It handles: spelled-out numbers/decimals/percent/time per language; **natural years** (`2026`→"twenty twenty-six" / «две тысячи двадцать шестого года» with proper RU genitive / KK ordinal when followed by года/жыл); **numeric ranges** (`9–10`→"9 to 10"/«9 по 10», any unicode dash); **emails/URLs** (`a@b.kz`→"a at b.kz", strip `https://`); and it keeps commas as pauses (only `;`/`:` promote a long clause to a new sentence — comma-splitting produced "…business. Attract…"). **Changing this changes the spoken text → changes FAQ video keys → requires a full rebuild.** Verify with a probe (feed sample text through `prepare_tts_text`) before rebuilding.
- **Deployment coupling — frontend and backend versions are coupled here.** Once the backend sends `faq_video` (any cached FAQ), a deployed **frontend without the `faq_video` handler will silently degrade those turns** (chat text appears, but no avatar audio/video, since `emit_spoken_text` was skipped). So: deploy the frontend to Vercel (`cd frontend && vercel deploy --prod`, see `DEPLOY.md`) **before** the cache is relied on in production, or keep `FAQ_VIDEO_ENABLED=0` until it's deployed. The FAQ data file (`/home/admin-aifc/data/faq/aifc_faq_cache.txt`) lives outside the repo and is not in git; rebuild videos after editing it.

### Interrupt / barge-in must not block the receive loop
`session.interrupt()` cancels the pipeline task but **must not `await` full teardown inline** — it's called from the WS receive loop (stop button, new query) and the STT loop (barge-in). A wedged SyncTalk call would otherwise block those loops, starving heartbeat pings (→ client watchdog force-closes the WS → disconnect + reconnect→`busy` race) and freezing mic capture. It waits only `_INTERRUPT_TEARDOWN_TIMEOUT_S` (1.5s) via `asyncio.wait`; if teardown is still pending it drains in the background (`_drain_cancelled`). Verified: a ping after a mid-turn interrupt gets a pong in ~3ms. Relatedly, `SYNCTALK_FRAME_TIMEOUT_S` is 3s (not 8s) so a GPU-contention stall fails the segment fast instead of an ~8s chunk-transition gap. On cancel, `ResponseStream.cancel_all()` `.cancel()`s the TTS + both avatar workers (it does NOT `wait_all`), and the SyncTalk `/infer_stream` endpoint polls `request.is_disconnected()` before each GPU batch and bails — so an interrupted turn leaves at most one in-flight batch of orphaned GPU work. **That abort lives in the separate SyncTalk repo (`synctalk_server.py`) and needs a SyncTalk restart to take effect.**

### Barge-in / VAD tuning
Frontend VAD (`activeListeningConfig.ts`): `positiveSpeechThreshold` 0.40 (noise rejection), `minSpeechMs` 500 (don't drop short queries), `negativeSpeechThreshold` 0.28 (wide hysteresis), `redemptionMs` 800, `preRollMs` 800 (onset buffer flushed on speech start — see the mic effect). Backend barge-in gate is `is_interrupt_candidate()` in `utils/language.py`: meaningful-partial only (never raw VAD energy); `_MIN_INTERRUPTING_ALPHA_WORDS=3` (noise guard) + `_MIN_INTERRUPTING_ALPHA_CHARS=7` (lets short queries interrupt). All VAD knobs are `VITE_*`-overridable.

### Single-pipeline session guard
`backend/session/session_gate.py` (`GATE`) admits at most `MAX_CONCURRENT_SESSIONS` (default **1**) live WebSocket sessions — there's ONE SyncTalk GPU pipeline, so concurrent users would contend for it and reintroduce stutter. Extra connections get `{type:'busy'}` + close `1013` (rejected *before* intro/prewarm, so they never touch the pipeline); the frontend shows a "please wait" overlay (`busyWaiting`) and its reconnect loop retries until a slot frees. The slot releases on disconnect (`GATE.release` in the `finally` of `api/websocket.py`) and when the socket dies (gate's dead-socket check). **Heartbeat pings DO count as activity** (`GATE.touch` on `ping`), so a connected client — including a silent user just reading/thinking — is **never idle-disconnected**; `SESSION_IDLE_EVICT_S` (default 120s) now only catches the rare open-but-non-pinging socket. (This intentionally reverses the earlier "pings don't count" eviction — a silent present user must not be dropped; a closed tab still frees the slot via dead-socket detection.) Raise the limit only if pipeline capacity grows.

### Public deployment (Vercel + Cloudflare Tunnel) — see `DEPLOY.md`
Frontend on **Vercel** (`frontend-five-lemon-98.vercel.app`), backend stays on the H200 box exposed via a **named Cloudflare Tunnel** at `https://avatar.bk-project.org`. Single env knob `VITE_BACKEND_ORIGIN` drives the WS URL and all backend asset URLs (`frontend/src/utils.ts`). Backend + tunnel run as **user systemd units** (`deploy/systemd/avatar-backend.service`, `avatar-cloudflared.service`, linger-enabled); the backend unit must set `LD_LIBRARY_PATH=/home/admin-aifc/miniforge3/lib`. `DEPLOY.md` is the runbook.

**Transport history (do NOT regress):** originally ngrok-free, but it couldn't sustain the ~10 Mbps binary avatar-frame stream (~50 KB JPEG × 25 fps) — frames backed up in the tunnel and playback stuttered (client render lag grew to ~5s; `client_first_render` − `first_frame` in the `pipeline_done` metrics is the tell). The box uplink is ~88 Mbps, so the box was never the bottleneck. Cloudflare Tunnel (no throttle, Almaty PoP) dropped the lag to ~2.2s (the designed `LIVE_PREBUFFER_S`) at full frame quality. ngrok is retired/disabled. If frames stutter again over the network, check the tunnel/bandwidth FIRST — the LAN-tuned client buffer can't paper over a transport throughput deficit.

### SyncTalk throughput optimization (stage pipelining + fast composite)
Lives in the **separate SyncTalk repo** `synctalk_server.py` (`/infer_stream`); needs a SyncTalk restart to take effect. Flag-gated via `config.env`, default on. Took SyncTalk render throughput **~65 → ~128 fps (≈2×), ~2 → ~5 concurrent avatars per H200**, quality SSIM 0.999 (visually identical). Full write-up + measured dead-ends in `OPTIMIZATION_PLAN.md`.
- **`SYNCTALK_PIPELINE=1`** — `/infer_stream` runs prep/gpu/composite as three concurrent coroutines linked by bounded queues (each a single in-order consumer → frame order preserved). Throughput becomes gated by the slowest stage (composite), not their sum. Abort-on-disconnect is preserved (gpu stage polls `is_disconnected`; `finally` cancels workers synchronously). `=0` → original sequential path.
- **`SYNCTALK_FAST_COMPOSITE=1`** — composite directly in 540×960 output space (precomputed downscaled bg + 328 border crop per frame) instead of copying+compositing the full 1920×1080 frame then downscaling. SSIM 0.999 vs full-res (NOT byte-identical). `=0` → byte-perfect full-res.
- **Bench harness:** `python scripts/bench_synctalk.py --tag X --compare-golden` measures fps + SSIM/PSNR vs golden frames (`var/bench/`). Use it before/after any SyncTalk render change — quality gate SSIM≥0.98.
- **Measured dead-ends (do NOT retry without re-benchmarking):** BF16/TensorRT (GPU forward already 15× faster than the pipeline), NVENC H.264 (no encoder on H200), software libx264 (~15 ms/f, and JPEG bandwidth fits the uplink at 5 streams), GPU nvJPEG (net loss in-pipeline). The remaining bottleneck is CPU JPEG `imencode` (~1.83 ms/f); beyond ~5 avatars, scale via more GPUs + a router.
- **`_AVATAR_WORKER_COUNT = 2`** (`response_stream.py`) pipelines *across* chunks; the above pipelines *within* one `/infer_stream` call. Independent.

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
python scripts/bench_synctalk.py --tag X --compare-golden   # SyncTalk fps + SSIM vs golden
python scripts/build_faq_videos.py --dry-run                # plan the FAQ video cache (no render)
python scripts/build_faq_videos.py                          # build FAQ answer MP4s (SyncTalk must be up)
```
Note: `smoke_ws_interrupt.py` sends a client `{type:'interrupt'}` and waits for `interrupted`, but the manual-interrupt path cancels silently (`session.py` → `interrupt(send_event=False)`); only barge-in emits `interrupted`. So that script hangs by design — not a regression.

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
- Do not make `interrupt()` `await` full pipeline teardown inline — it blocks the WS/STT loops, starves heartbeat pings (→ WS disconnect) and freezes mic capture. Keep the bounded `asyncio.wait` + background drain
- Keep loading the intro MP4 via the header-bearing blob fetch (`loadIntroBlob`), not a bare `<video src>` — portable across transports (see intro section)
- Do not start the intro from the WS `intro_video` handler — autoplay-with-sound needs a user gesture; start it from the "Tap to start" button's onClick
- Do not change the SyncTalk render path (pipelining / composite / encode in `synctalk_server.py`) without re-running `scripts/bench_synctalk.py --compare-golden` — quality gate SSIM≥0.98, and several "obvious" speedups (BF16, NVENC, GPU nvJPEG) measured as no-ops/net-losses (see `OPTIMIZATION_PLAN.md`)
- Do not `git push` the SyncTalk repo to `origin` — its `origin` is the **upstream** `ZiqiaoPeng/SyncTalk_2D` (no write access). SyncTalk commits are local; push only to your own fork remote if you add one
- Do not enable the FAQ video cache (`FAQ_VIDEO_ENABLED=1`) on the live backend without first deploying a frontend that handles `faq_video` — an old Vercel frontend degrades cached-FAQ turns (text shows, no avatar A/V). See "FAQ answer video cache"
- Do not add a startup prebuild for FAQ videos — it would burst heavy GPU work at boot and contend with SyncTalk; build them offline via `scripts/build_faq_videos.py`
