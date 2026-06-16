# Handoff — Avatar Stutter / Latency Work

_Session date: 2026-06-16. Read this first, then `CLAUDE.md`. This documents an end-to-end
effort to kill avatar stuttering and chunk-transition gaps across the intro and answer paths._

---

## TL;DR — current state

Avatar stutter/latency reworked end-to-end. **User-confirmed working: the intro (MP4), and the
answer-path is "much better"** — transition gaps went from ~1100–1700ms down to 15–36ms. Latest
round fixed the start-of-answer stutter and the boundary frame stutters; user's last note was
"much better, need a bit more smoothness at frame switching / sentence-finishing frames," which the
final round (cross-chunk preload + removing per-chunk React re-renders) targets. **That last round
is type-clean and live via HMR but awaits the user's in-browser confirmation.**

History of this work is in Phases 1–5 below. Phases 1–4 = the architecture; Phase 5 = the tuning
that made it actually smooth (segment sizing, prebuffer lead, boundary preloading, de-React-ing the
render loop).

Services (all restartable; SyncTalk is persistent but **was deliberately restarted this session**
because its code changed):

| Service | Port | Notes |
|---|---|---|
| SyncTalk inference | 8005 | **Code changed this session** — continuous head-pose indexing |
| Backend (uvicorn) | 8080 | sub-segmentation + binary frames |
| Frontend (Vite) | 5173 | gapless audio, intro `<video>`, binary frames |

Last verified: all three healthy; intro MP4 serves `HTTP 200` through the Vite proxy.

---

## The original problem

User reported the avatar "stuttering in every frame, from the intro to every video frame," and later
"chunk transition gap 1101ms (chunk 1 → 2)" plus "bitmap not ready" warnings on the answer path.

Pipeline recap (see CLAUDE.md): `STT → LLM → TTS → SyncTalk frames → browser`. Frames are 540×960
JPEG @ quality 82 (confirmed in `SyncTalk_2D/synctalk_server.py:266-267`) — exactly canvas size, so
oversized-decode was **not** the cause.

---

## Fixes, in the order we did them

### Phase 1 — frame decode/render (frontend, `frontend/src/hooks/useChunkPlayback.ts`)
**Problem:** A prior commit switched to `createImageBitmap(img)` where `img` was a freshly-created
`new Image()` whose `src` (a data URL) hadn't loaded. `createImageBitmap` on an unloaded
`HTMLImageElement` **rejects with InvalidStateError**, so `bitmapCache` stayed empty → every frame hit
the `BITMAP_NOT_READY` branch → stutter.

**Fixes (all live, verified by user — "intro is fixed"):**
- `preloadBitmap` now decodes base64 → `Blob` → `createImageBitmap(blob)` (no DOM, no load race).
- **Bitmap eviction**: after drawing, `evictPlayed()` closes/drops bitmaps >4 frames behind the
  playhead; `releaseAllBitmaps()` closes all on reset. Was a multi-hundred-MB leak (548-frame intro)
  causing GC pauses.
- `showSpeak()` guarded with `speakShownRef` so it writes DOM once (hidden→shown), not 25×/sec — the
  per-frame `c.style.opacity`/classList writes were re-triggering the `:has(#speakCvs.show)` recalc.

### Phase 2 — binary WS frames (answer path)
**Problem:** every live frame was `{"type":"frame","data":"<~50KB base64>"}`. The browser paid a
`JSON.parse` of a 50KB string **plus** `atob` per frame, 25×/sec, on the main thread, competing with
the render loop.

**Fix (live, verified — smoke test received 715 binary frames):**
- Frames now ship as **binary WS messages**. Header (little-endian):
  `byte0 = 0xF1 magic | bytes1-2 chunk u16 | byte3 turn_id len u8 | turn_id ascii | JPEG bytes`.
- Backend: `WsWriter.send_frame_binary()` (`backend/api/ws_writer.py`); `response_stream.py`
  `base64.b64decode`s SyncTalk's b64 and sends bytes.
- Frontend: `ws.binaryType='arraybuffer'`, parsed in `useWebSocket.ts` → `onBinaryFrame` →
  `useChunkPlayback.onFrameBinary` stores a `Blob`; `preloadBitmap` handles `Blob | string`.
- `ChunkState.frames` is now `(string | Blob)[]`.

### Phase 3 — intro as MP4 (CONFIRMED FIXED by user)
**Problem:** the intro fetched ~548 base64 JPEGs as a single ~20MB JSON and decoded each at 25fps on
the main thread → stutter. The intro is static, so it's the textbook `<video>` case.

**Fix (live, user confirmed "intro is fixed!"):**
- Backend builds a combined H.264+AAC MP4 from cached frames+audio (`backend/intro.py`:
  `build_intro_video`, `ensure_intro_video`, `intro_video_*`). Cached at
  `cache/intro/video/<avatar>/intro.mp4` + `.json` signature sidecar. Built once in
  `prebuild_intro_cache`.
- Route `GET /intro-video/{avatar}/intro.mp4` (`backend/api/routes.py`, range-enabled FileResponse).
  Added to the **Vite proxy** (`frontend/vite.config.ts`).
- `session.py run_intro` sends `{type:'intro_video',url}` when a valid MP4 exists; **falls back** to
  the old canvas frame-cache path otherwise.
- Frontend plays it in a dedicated `<video id="introVid">` (`AvatarStage.tsx`); `App.tsx` handles
  `intro_video` (play, on-ended cleanup) with `stopIntroVideo()` wired into interrupt / stop_confirmed
  / interrupted / error / response_start.

**ffmpeg gotchas we hit and solved (important for future builds):**
- ffmpeg lives in the **synctalk2d conda env** (`/home/admin-aifc/miniforge3/envs/synctalk2d/bin/ffmpeg`),
  not on the base PATH. `_ffmpeg_bin()` probes `FFMPEG_BIN` env, PATH, then that env path.
- **Never** use `stderr=subprocess.PIPE` while streaming ~250MB of JPEG bytes to stdin — the 64KB
  stderr buffer fills, ffmpeg stalls, and stdin pipe breaks (`BrokenPipeError`). We write stderr to a
  temp file instead.
- The temp output is `intro.mp4.tmp`; ffmpeg can't infer the muxer from `.tmp`, so we pass `-f mp4`.

### Phase 4 — chunk-transition gaps (answer path) — THE BIG ONE, NEEDS BROWSER VERIFICATION
**Root cause (found via backend timing logs):** Soniox TTS streams a sentence at ~1× realtime
(8–12s for a 10–14s sentence), and SyncTalk for a chunk **could not start until that chunk's entire
TTS stream finished** (`_run_streaming_sentence_batch` buffered the whole sentence, queued one WAV).
The 2 avatar workers idled during the long TTS; gaps appeared when the next sentence's produce time
exceeded the current sentence's playback. `AVATAR_TTS_SEGMENT_MS`/`FIRST_SEGMENT_MS`/`MAX_SEGMENT_MS`
existed in settings but were **dead config** — sub-segmentation had been lost.

**Critical constraint the user correctly intuited:** more/shorter SyncTalk calls would normally look
choppy — because `_encode_audio` **restarted the head-pose ping-pong walk at frame 1 on every call**
(`SyncTalk_2D/synctalk_server.py`, `step,idx=0,0`). So every chunk boundary snapped the head/body
pose back to frame 1. This already affected sentence boundaries; finer segments would expose it badly.

**Fix (implemented, type-clean, backend smoke OK — needs in-browser confirm):**

1. **SyncTalk continuous head-pose indexing** (`SyncTalk_2D/synctalk_server.py`):
   - `InferRequest` gained `start_frame: int = 0`.
   - `_encode_audio(wav, start_frame)` walks the ping-pong from 0 up to `start_frame + n_frames` and
     keeps the tail slice, so segments **resume** the head pose instead of resetting. Threaded into
     both `/infer_stream` and `/infer`.
   - **This is why the SyncTalk restart was needed and authorized.**

2. **Backend sub-segmentation** (`backend/pipeline/response_stream.py`):
   - `_run_streaming_sentence_batch` flushes ~1s first / ~2s subsequent PCM segments to SyncTalk **as
     audio streams in**, instead of buffering the whole sentence.
   - `_queue_wav_segment` assigns each segment `start_frame = self._turn_frame_offset`, then advances
     the offset by `expected_frames + 2` (the +2 approximates SyncTalk's audio-feature edge padding).
   - `avatar_queue` items are now `(media_idx, audio_wav, start_frame)`; the avatar worker passes
     `start_frame` to `infer_stream`.
   - `synctalk.py infer_stream` gained `start_frame` (sent in the JSON body).
   - Settings defaults bumped: `AVATAR_TTS_FIRST_SEGMENT_MS=1000`, `AVATAR_TTS_SEGMENT_MS=2000`.

3. **Frontend gapless audio** (`frontend/src/hooks/useChunkPlayback.ts`):
   - Segments are mid-word, so audio must be sample-accurate gapless. Decoupled **audio scheduling**
     from **frame rendering**: `scheduleChunkAudio` / `prescheduleAhead` queue each chunk's
     `AudioBufferSource` at `audioCursorRef` (prev chunk's end) ahead of the playhead; the per-chunk
     render loop uses that scheduled `t0` and ends at `elapsed >= chunkDuration` (no +0.2 overshoot).
   - **Startup prebuffer** (`LIVE_PREBUFFER_FRAMES=55`): the first live chunk waits for enough lead so
     later segments never starve. `prebufferReady()` gates only the first live chunk.
   - `LIVE_READY_FRAME_HEADROOM` raised 4→10 (kills "bitmap not ready" at chunk starts).
   - `stopAllScheduledAudio()` cancels every queued source on interrupt/new turn (wired into
     `stopPlayback` and `startStream`).
   - New `ChunkState` fields: `decoding`, `scheduledSource`, `scheduledT0`, `scheduledDuration`.
   - `prescheduleAhead()` is triggered from `onAudioReady`, `onFrame`, `onFrameBinary`, `onChunkDone`
     so upcoming chunks' audio queues onto the timeline while the current chunk renders.

**Backend smoke result after Phase 4:** first frame **~9s → 3.1s**, first audio 9.5s → 1.8s, a single
answer split into ~14 small segments (was 3–4), no media_error.

### Phase 5 — tuning to actually-smooth (the iterations that mattered)
Phase 4's architecture worked but the browser still stuttered. Three concrete bugs/dials, each found
from the user's console screenshots:

1. **Tiny first segment → choppy start.** `config.env` had a stale `AVATAR_TTS_FIRST_SEGMENT_MS=220`
   (overriding the settings default), so chunk 0 was a **4-frame / ~160ms micro-clip** that stuttered
   and instantly transitioned. Fixed in `config.env`: `AVATAR_TTS_FIRST_SEGMENT_MS=1000`,
   `AVATAR_TTS_SEGMENT_MS=2500→1500`. Now chunk 0 = ~24 frames, others ~36 frames.
   **config.env overrides settings.py defaults — always set values there, not just in settings.py.**

2. **Near-zero playback lead → recurring 600–1700ms gaps.** `prebufferReady()` had
   `if (first.frameDone) return true`; chunk 0 (a ~1s segment) reaches `frameDone` instantly, so
   playback started with chunk 1 at zero frames and no lead. Since TTS streams at ~realtime, playback
   then raced production and starved on any jitter. Fixed: removed the `frameDone` short-circuit;
   `prebufferReady` now requires `LIVE_PREBUFFER_S` (~2.2s) of contiguous buffered audio before the
   first chunk plays (with a "whole turn already delivered" escape hatch for short answers). Result:
   transition gaps dropped to **15–36ms**.

3. **Boundary frame stutters (start ~182ms, end ~60–70ms).** Three causes:
   - **No cross-chunk bitmap preload** — the render loop only decoded the *current* chunk's frames, so
     the next chunk's opening bitmaps weren't ready at the handoff. Added `CROSS_CHUNK_PRELOAD` (decode
     the next chunk's first 12 frames during the current chunk's last ~0.5s; kept narrow so it doesn't
     starve the tail).
   - **Cold-start `await` desync** — awaiting the first bitmaps *after* scheduling audio let the audio
     advance and the loop skipped the just-decoded frames. Now the await runs **only for the cold first
     chunk, before scheduling its audio**; gapless chunks don't block.
   - **42 React re-renders at boundaries** — `onChunkPlaybackStart/End` dispatched `active_spoken_chunk`
     on every chunk start+end (~42×/answer), forcing a top-level App re-render exactly at each frame
     boundary. The state was **never read** (dead). Removed the dispatches entirely → render loop fully
     decoupled from React.

---

## What works (verified)
- Intro plays smoothly via MP4 (**user-confirmed**).
- Answer path "much better" (**user-confirmed**); transition gaps ~1100–1700ms → **15–36ms**.
- `BITMAP_NOT_READY` flood gone after the `createImageBitmap(blob)` fix.
- Binary frames flow end-to-end (smoke: all chunks `chunk_done`, `done`).
- Sub-segmentation: chunk 0 ~24 frames, others ~36 (~1.5s); first-frame latency ~9s → ~3s.
- Intro MP4 builds (~34s one-time) and serves with HTTP range support through Vite.
- Frontend `tsc --noEmit` clean after every change.

## What is NOT yet confirmed (do this first next session)
Phase 5's boundary-smoothness round (cross-chunk preload + de-React-ing the render loop) is live but
unconfirmed. Open `http://localhost:5173`, run a Q&A, and check the console:
1. Start-of-chunk (`stutter … at frame 1-3/NN`) and end-of-chunk stutters should be largely **gone**.
   `window.__avatarPerf` should show far fewer `STUTTER`/`BITMAP_NOT_READY` events.
2. **Interrupt / barge-in** mid-answer cleanly stops audio + video.
3. If mild ~60ms blips remain: likely inherent rAF quantization (25fps on 60Hz alternates 33/50ms) or
   JPEG-decode jitter. Next lever would be a Web Worker decode pool or hardware decode (`chrome://gpu`),
   or streaming answer segments as short MP4s like the intro — all bigger changes.

## Known caveats / tuning dials
- Head-pose offset uses **estimated** frame counts (`expected_frames + 2`), so ~2-frame drift can
  accumulate per seam. Visually negligible. To make exact, SyncTalk would need to report actual frame
  counts back (hard with 2 concurrent workers + queue-time offset assignment).
- SyncTalk pads audio features at each segment's edges → a minor lip-sync imperfection possible at
  seams. Far less visible than the old full pose reset.
- **Autoplay:** the intro `<video>` plays unmuted; if the browser blocks autoplay-with-sound it falls
  back to muted. Intro normally follows a user interaction so audio works.
- **Latency vs gap-free tradeoff:** the ~2.2s prebuffer adds startup delay before the avatar speaks. If
  too slow, lower `LIVE_PREBUFFER_S` toward ~1.5s — but gaps return if SyncTalk jitter exceeds the lead.
- Tuning dials: `LIVE_PREBUFFER_S`, `CROSS_CHUNK_PRELOAD`, `LIVE_READY_FRAME_HEADROOM`,
  `AUDIO_SCHEDULE_LOOKAHEAD` (frontend constants in `useChunkPlayback.ts`);
  `AVATAR_TTS_SEGMENT_MS` / `AVATAR_TTS_FIRST_SEGMENT_MS` (**config.env**).

## Environment notes / operational gotchas
- **Backend runs under base conda python** (`/home/admin-aifc/miniforge3/bin/python`), not synctalk2d,
  despite `run_ws_backend.sh` defaulting to synctalk2d — something in `.env`/`config.env` pins
  `WS_BACKEND_PYTHON`. It works (deps present; ffmpeg resolved via the robust path probe). If you want
  it under synctalk2d, check what sets that var.
- **`bash` tool quirk this session:** `pkill -f "node.*vite"` (and similar) repeatedly returned the
  shell with exit code 143/144 (the pkill caught a process the shell depended on). The kills still
  worked; just re-check `ss -ltn` afterward and relaunch.
- Restart commands used:
  - SyncTalk: `pkill -f synctalk_server` then `bash scripts/start_synctalk.sh` (waits ~60–100s:
    loads model + preloads 5747 avatar images + GPU warmup).
  - Backend: `pkill -f "uvicorn backend.main:app"` then `bash scripts/run_ws_backend.sh`.
  - Frontend: `pkill -f "node.*vite"` then `cd frontend && npm run dev`.
- **Smoke test** (`scripts/smoke_ws_text.py`) was updated to count **binary** frames (it used to
  `json.loads` every message and would now choke on binary). Other capture scripts
  (`capture_ws_mp4.py`, `capture_ws_tts.py`) likely still assume JSON frames — update them similarly
  if you use them.

## Files touched this session
**SyncTalk (SEPARATE git repo at `/home/admin-aifc/SyncTalk_2D`):** `synctalk_server.py` (continuous
head-pose indexing). ⚠️ **This change is NOT in the avatar-system-2 commit** — it lives in its own repo
and is currently uncommitted there. If you redeploy/reset SyncTalk, re-apply or commit it separately.
**Backend:** `backend/api/ws_writer.py`, `backend/pipeline/response_stream.py`, `backend/media/synctalk.py`,
`backend/intro.py`, `backend/api/routes.py`, `backend/session/session.py`, `backend/settings.py`,
`config.env`, `scripts/smoke_ws_text.py`.
**Frontend:** `frontend/src/hooks/useChunkPlayback.ts` (largest change), `frontend/src/hooks/useWebSocket.ts`,
`frontend/src/components/AvatarStage.tsx`, `frontend/src/App.tsx`, `frontend/src/types.ts`,
`frontend/src/styles.css`, `frontend/vite.config.ts`.

The avatar-system-2 changes above were committed and pushed to `main` at the end of this session.
The SyncTalk repo change was **not** committed (separate repo per CLAUDE.md). Related memory:
`.claude/.../memory/frame_delivery_binary_and_intro_mp4.md` and
`.claude/.../memory/streaming_pipeline_subsegment_gapless.md`.
