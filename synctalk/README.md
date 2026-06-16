# SyncTalk server — continuous head-pose indexing patch

This folder is a **reference copy + explanation** of a change made to the SyncTalk inference
server. That server lives in a **separate git repository** at `/home/admin-aifc/SyncTalk_2D`,
whose `origin` points at the **upstream** project (`github.com/ZiqiaoPeng/SyncTalk_2D`) — so the
change cannot be pushed there. `synctalk_server.py` is also **untracked** in that repo (it is an
AIFC-specific server file). This folder keeps the change version-controlled and recoverable in the
avatar-system-2 repo.

- `synctalk_server.py` — full copy of the deployed server (source of truth / backup).
- Deployed location: `/home/admin-aifc/SyncTalk_2D/synctalk_server.py`, port **8005**.

## What changed and why

**Problem.** The realtime answer path streams a turn to SyncTalk in many small audio segments
(~1.5s each) so frames flow continuously (see avatar-system-2 `CLAUDE.md` → "Streaming media
pipeline"). But every `/infer_stream` call recomputed the avatar's **head-pose ping-pong walk from
frame 1**, so each segment boundary **snapped the head/body back to the starting pose** — visibly
choppy and inconsistent, and worse the more segments there are.

**Fix.** Thread a per-turn `start_frame` offset into the server so each segment **resumes** the
head-pose walk where the previous one ended, making segment boundaries seamless. The backend
(`backend/pipeline/response_stream.py` → `_queue_wav_segment`) assigns
`start_frame = self._turn_frame_offset` and advances it per segment; the client
(`backend/media/synctalk.py` `infer_stream`) sends it in the JSON body.

## The exact edits (3)

1. **`InferRequest` gains `start_frame`:**
   ```python
   class InferRequest(BaseModel):
       audio_b64: str
       start_frame: int = 0   # continuous head-pose index across a turn's segments
   ```

2. **`_encode_audio(wav_bytes, start_frame=0)`** — walk the ping-pong from frame 0 up to
   `start_frame + n_frames` and keep the tail slice, instead of always starting at frame 1:
   ```python
   step, idx = 0, 0
   full_indices = []
   for _ in range(start_frame + n_frames):
       if idx > len_img - 1: step = -1
       if idx < 1:           step = 1
       idx += step
       full_indices.append(idx)
   frame_indices = full_indices[start_frame:]
   ```

3. **Both endpoints pass `req.start_frame`** to `_encode_audio`:
   ```python
   audio_feats, frame_indices, n_frames = await loop.run_in_executor(
       _cpu_exec, _encode_audio, wav_bytes, req.start_frame)
   ```
   (in `/infer_stream` and `/infer`).

## Deploying / re-applying

If the SyncTalk server is reset or redeployed and loses this change, copy this file back and
restart the server:

```bash
cp synctalk/synctalk_server.py /home/admin-aifc/SyncTalk_2D/synctalk_server.py
pkill -f synctalk_server          # stop (it is the persistent service on :8005)
bash scripts/start_synctalk.sh    # restart; ~60-100s to load model + 5747 avatar images + GPU warmup
```

Only restart SyncTalk when its code (this file) or the checkpoint changes — not on backend restarts.

## Caveat

The backend advances the head-pose offset by an **estimated** frame count (`expected_frames + 2`,
approximating the audio-feature edge padding), so there can be ~2 frames of drift per seam —
visually negligible since the head moves slowly. Making it exact would require the server to report
actual rendered frame counts back to the backend, which is hard with two concurrent avatar workers
and queue-time offset assignment.
