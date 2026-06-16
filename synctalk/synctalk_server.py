import argparse
import asyncio
import base64
import json as _json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi.responses import StreamingResponse

import cv2
import numpy as np
import torch
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from torch.utils.data import DataLoader

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib_cache")
os.makedirs(os.environ["NUMBA_CACHE_DIR"], exist_ok=True)
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unet_328 import Model
from utils import AudioEncoder, AudDataset, get_audio_features

# ── CLI args ──────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8005)
args, _ = parser.parse_known_args()

SYNCTALK_DIR = os.path.dirname(os.path.abspath(__file__))
AVATAR_NAME  = os.getenv("SYNCTALK_AVATAR", "aifc-avatar-5-exp-3")
MODE         = "ave"
BATCH_SIZE   = int(os.getenv("SYNCTALK_BATCH_SIZE", "32"))

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# ── AudioEncoder ──────────────────────────────────────────────
print("Loading AudioEncoder...")
audio_enc = AudioEncoder().to(device).eval()
ckpt = torch.load(
    os.path.join(SYNCTALK_DIR, "model/checkpoints/audio_visual_encoder.pth"),
    map_location=device,
    weights_only=False,
)
audio_enc.load_state_dict({f"audio_encoder.{k}": v for k, v in ckpt.items()})
print("AudioEncoder ready")

# ── UNet ──────────────────────────────────────────────────────
checkpoint_dir = os.path.join(SYNCTALK_DIR, "checkpoint", AVATAR_NAME)
checkpoint_path = os.path.join(
    checkpoint_dir,
    sorted(os.listdir(checkpoint_dir), key=lambda x: int(x.split(".")[0]))[-1],
)
print(f"Loading UNet from {checkpoint_path}...")
net = Model(6, MODE).to(device)
net.load_state_dict(torch.load(checkpoint_path, map_location=device, weights_only=False))
net.eval()
print("UNet ready")

# ── Pre-compute per-frame image tensors ───────────────────────
img_dir  = os.path.join(SYNCTALK_DIR, "dataset", AVATAR_NAME, "full_body_img/")
lms_dir  = os.path.join(SYNCTALK_DIR, "dataset", AVATAR_NAME, "landmarks/")
len_img  = len(os.listdir(img_dir)) - 1

print(f"Pre-loading {len_img+1} avatar images...")
# img_tensors[i]: [6, 320, 320] float32 CPU tensor (img_real_ex || img_masked)
# img_meta[i]:    (full_img_np, h, w, xmin, ymin, ymax) for compositing
img_tensors = {}
img_meta    = {}

def _load_lms(i: int) -> np.ndarray:
    """Load landmarks for index i, falling back to nearest neighbour if missing."""
    path = os.path.join(lms_dir, f"{i}.lms")
    if not os.path.exists(path):
        # try neighbours
        for delta in range(1, 10):
            for nb in (i + delta, i - delta):
                nb_path = os.path.join(lms_dir, f"{nb}.lms")
                if os.path.exists(nb_path):
                    path = nb_path
                    break
            else:
                continue
            break
    lms_list = []
    with open(path) as f:
        for line in f.read().splitlines():
            lms_list.append(np.array(line.split(" "), dtype=np.float32))
    return np.array(lms_list, dtype=np.int32)


# Frame 0 is never visited by the ping-pong (step goes 0→1→2…), start from 1
for i in range(1, len_img + 1):
    img_path = os.path.join(img_dir, f"{i}.jpg")

    img = cv2.imread(img_path)
    if img is None:
        continue
    lms = _load_lms(i)

    xmin  = int(lms[1][0])
    ymin  = int(lms[52][1])
    xmax  = int(lms[31][0])
    width = xmax - xmin
    ymax  = int(ymin + width)

    # Guard against degenerate coordinates
    xmin, xmax = max(0, xmin), min(img.shape[1], xmax)
    ymin, ymax = max(0, ymin), min(img.shape[0], ymax)
    if xmax <= xmin or ymax <= ymin:
        continue

    crop   = img[ymin:ymax, xmin:xmax]
    h, w   = crop.shape[:2]
    crop   = cv2.resize(crop, (328, 328), interpolation=cv2.INTER_CUBIC)

    real_ex  = crop[4:324, 4:324].copy()
    masked   = real_ex.copy()
    cv2.rectangle(masked, (5, 5, 310, 305), (0, 0, 0), -1)

    t_real = torch.from_numpy(real_ex.transpose(2, 0, 1).astype(np.float32) / 255.0)
    t_mask = torch.from_numpy(masked.transpose(2, 0, 1).astype(np.float32)  / 255.0)
    img_tensors[i] = torch.cat([t_real, t_mask], dim=0)
    img_meta[i]    = (img, h, w, xmin, ymin, ymax)

print(f"Avatar data ready — SyncTalk server live (batch_size={BATCH_SIZE})")

# ── GPU warmup: compile CUDA kernels before first real request ─
print("Warming up GPU (pre-compiling CUDA kernels)...")
_w_stream = torch.cuda.Stream()
with torch.cuda.stream(_w_stream):
    with torch.no_grad():
        _w_imgs  = torch.zeros(1, 6, 320, 320, device=device)
        _w_audio = torch.zeros(1, 32, 16, 16, device=device)
        net(_w_imgs, _w_audio)
_w_stream.synchronize()
del _w_imgs, _w_audio, _w_stream
print("GPU warmup done")

# ── Module-level helpers ──────────────────────────────────────
valid_set = set(img_tensors.keys())


def nearest_valid(idx: int) -> int:
    """Return idx if in valid_set, otherwise the nearest valid frame index."""
    if idx in valid_set:
        return idx
    for d in range(1, len_img + 1):
        for nb in (idx + d, idx - d):
            if nb in valid_set:
                return nb
    return next(iter(valid_set))


# ── Thread-local CUDA streams (Tier B) ───────────────────────
_tl = threading.local()


def _get_stream() -> torch.cuda.Stream:
    if not hasattr(_tl, "stream"):
        _tl.stream = torch.cuda.Stream()
    return _tl.stream


# ── Thread pools ──────────────────────────────────────────────
_cpu_exec = ThreadPoolExecutor(max_workers=int(os.getenv("SYNCTALK_CPU_WORKERS", "4")))  # CPU-bound work
_gpu_exec  = ThreadPoolExecutor(max_workers=int(os.getenv("SYNCTALK_GPU_WORKERS", "4")))  # GPU-bound forward passes


# ── GPU forward pass (module-level, runs in _gpu_exec) ───────
def _gpu_forward(imgs_cpu: torch.Tensor, audio_cpu: torch.Tensor) -> np.ndarray:
    """Run UNet on CPU tensors, return uint8 numpy [B, H, W, C]."""
    stream = _get_stream()
    with torch.cuda.stream(stream):
        imgs  = imgs_cpu.to(device, non_blocking=True)
        audio = audio_cpu.to(device, non_blocking=True)
        with torch.no_grad():
            preds = net(imgs, audio)
        preds_cpu = preds.cpu()
    stream.synchronize()
    return (preds_cpu.numpy().transpose(0, 2, 3, 1) * 255).astype(np.uint8)


# ── CPU-only audio encoding ───────────────────────────────────
def _encode_audio(wav_bytes: bytes, start_frame: int = 0):
    """Save WAV, run AudioEncoder (GPU via thread-local stream), return
    (audio_feats np.ndarray, frame_indices list[int], n_frames int).

    start_frame resumes the head-pose ping-pong walk from a turn-wide offset so
    multi-segment turns render continuous head motion instead of resetting to frame 1."""
    tmp = f"/tmp/synctalk_{uuid.uuid4().hex}.wav"
    try:
        with open(tmp, "wb") as f:
            f.write(wav_bytes)

        dataset  = AudDataset(tmp)
        loader   = DataLoader(dataset, batch_size=64, shuffle=False)
        enc_outs = []
        stream   = _get_stream()
        with torch.cuda.stream(stream):
            with torch.no_grad():
                for mel in loader:
                    enc_outs.append(audio_enc(mel.to(device)))
        stream.synchronize()
        enc_outs = torch.cat(enc_outs, dim=0).cpu()

        ff, lf      = enc_outs[:1], enc_outs[-1:]
        audio_feats = torch.cat([ff, enc_outs, lf], dim=0).numpy()
        n_frames    = audio_feats.shape[0]

        # Walk the ping-pong from frame 0 up to (start_frame + n_frames) and keep the
        # tail slice, so a turn's segments resume the head pose where the prior one ended.
        step, idx = 0, 0
        full_indices = []
        for _ in range(start_frame + n_frames):
            if idx > len_img - 1:
                step = -1
            if idx < 1:
                step = 1
            idx += step
            full_indices.append(idx)
        frame_indices = full_indices[start_frame:]

        return audio_feats, frame_indices, n_frames
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── CPU-only tensor prep for one batch ───────────────────────
def _prep_batch(audio_feats, frame_indices, batch_start: int, batch_end: int):
    """Assemble img + audio tensors as CPU tensors (no GPU calls)."""
    b_imgs  = []
    b_audio = []
    target_audio_values = 32 * 16 * 16
    for i in range(batch_start, batch_end):
        b_imgs.append(img_tensors[nearest_valid(frame_indices[i])])
        audio_window = get_audio_features(audio_feats, i).reshape(-1)
        if audio_window.numel() < target_audio_values:
            audio_window = torch.nn.functional.pad(
                audio_window,
                (0, target_audio_values - audio_window.numel()),
            )
        elif audio_window.numel() > target_audio_values:
            audio_window = audio_window[:target_audio_values]
        b_audio.append(audio_window.reshape(32, 16, 16))
    imgs_cpu  = torch.stack(b_imgs)   # [B, 6, 320, 320]
    audio_cpu = torch.stack(b_audio)  # [B, 32, 16, 16]
    return imgs_cpu, audio_cpu


# ── CPU-only JPEG compositing ─────────────────────────────────
def _composite(preds_np: np.ndarray, frame_idx_slice) -> list:
    """Composite UNet predictions onto avatar images, return list of b64 JPEGs."""
    result = []
    for j, fi_raw in enumerate(frame_idx_slice):
        fi = nearest_valid(fi_raw)
        src_img, h, w, xmin, ymin, ymax = img_meta[fi]
        out    = src_img.copy()
        crop   = out[ymin:ymax, xmin:xmin + (ymax - ymin)]
        crop_r = cv2.resize(crop, (328, 328), interpolation=cv2.INTER_CUBIC)
        crop_r[4:324, 4:324] = preds_np[j]
        crop_r = cv2.resize(crop_r, (w, h), interpolation=cv2.INTER_CUBIC)
        out[ymin:ymax, xmin:xmin + (ymax - ymin)] = crop_r
        out = cv2.resize(out, (540, 960), interpolation=cv2.INTER_AREA)
        _, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 82])
        result.append(base64.b64encode(buf).decode())
    return result


# ── FrameAccumulator (Tier C) ─────────────────────────────────
class FrameAccumulator:
    """Batches GPU forward calls across concurrent streaming requests.

    Fires a merged _gpu_forward() when MAX_FRAMES tensors have accumulated
    OR after MAX_WAIT_S seconds, whichever comes first.
    """
    MAX_FRAMES = int(os.getenv("SYNCTALK_MAX_FRAMES", "128"))
    MAX_WAIT_S = float(os.getenv("SYNCTALK_MAX_WAIT_S", "0.030"))

    def __init__(self):
        self._pending = []           # list of (imgs_cpu, audio_cpu, future)
        self._lock: asyncio.Lock | None = None
        self._flush_task: asyncio.Task | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def infer(self, imgs_cpu: torch.Tensor, audio_cpu: torch.Tensor) -> np.ndarray:
        loop = asyncio.get_running_loop()
        fut  = loop.create_future()
        async with self._get_lock():
            self._pending.append((imgs_cpu, audio_cpu, fut))
            total = sum(p[0].shape[0] for p in self._pending)
            if total >= self.MAX_FRAMES:
                if self._flush_task and not self._flush_task.done():
                    self._flush_task.cancel()
                    self._flush_task = None
                await self._do_flush(loop)
            elif self._flush_task is None or self._flush_task.done():
                self._flush_task = loop.create_task(self._timed_flush())
        return await fut

    async def _timed_flush(self):
        await asyncio.sleep(self.MAX_WAIT_S)
        async with self._get_lock():
            self._flush_task = None
            if self._pending:
                await self._do_flush(asyncio.get_running_loop())

    async def _do_flush(self, loop: asyncio.AbstractEventLoop):
        if not self._pending:
            return
        batch, self._pending = self._pending[:], []
        all_imgs  = torch.cat([p[0] for p in batch], dim=0)
        all_audio = torch.cat([p[1] for p in batch], dim=0)
        preds_np  = await loop.run_in_executor(_gpu_exec, _gpu_forward, all_imgs, all_audio)
        offset = 0
        for imgs, audio, f in batch:
            n = imgs.shape[0]
            if not f.done():
                f.set_result(preds_np[offset:offset + n])
            offset += n


_accum = FrameAccumulator()

app = FastAPI()


class InferRequest(BaseModel):
    audio_b64: str
    # Continuous head-pose index across a turn's segments. The head-pose ping-pong
    # walk resumes from this offset instead of restarting at frame 1, so splitting a
    # turn into multiple segments keeps the head/body motion seamless across calls.
    start_frame: int = 0


# ── /infer_stream — streaming NDJSON (priority-aware pool entry) ──
@app.post("/infer_stream")
async def infer_stream(req: InferRequest):
    """Stream frames as NDJSON lines.  Tensor prep is CPU-async; GPU batches
    are merged across concurrent requests via FrameAccumulator."""
    wav_bytes = base64.b64decode(req.audio_b64)
    loop = asyncio.get_running_loop()

    audio_feats, frame_indices, n_frames = await loop.run_in_executor(
        _cpu_exec, _encode_audio, wav_bytes, req.start_frame
    )

    async def generate():
        for batch_start in range(0, n_frames, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, n_frames)
            imgs_cpu, audio_cpu = await loop.run_in_executor(
                _cpu_exec, _prep_batch,
                audio_feats, frame_indices, batch_start, batch_end,
            )
            preds_np = await _accum.infer(imgs_cpu, audio_cpu)
            frames   = await loop.run_in_executor(
                _cpu_exec, _composite,
                preds_np, frame_indices[batch_start:batch_end],
            )
            for frame in frames:
                yield _json.dumps({"frame": frame}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ── /infer — non-streaming fallback ──────────────────────────
@app.post("/infer")
async def infer(req: InferRequest):
    wav_bytes = base64.b64decode(req.audio_b64)
    loop = asyncio.get_running_loop()

    audio_feats, frame_indices, n_frames = await loop.run_in_executor(
        _cpu_exec, _encode_audio, wav_bytes, req.start_frame
    )

    result_frames = []
    for batch_start in range(0, n_frames, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, n_frames)
        imgs_cpu, audio_cpu = await loop.run_in_executor(
            _cpu_exec, _prep_batch,
            audio_feats, frame_indices, batch_start, batch_end,
        )
        preds_np = await _accum.infer(imgs_cpu, audio_cpu)
        frames   = await loop.run_in_executor(
            _cpu_exec, _composite,
            preds_np, frame_indices[batch_start:batch_end],
        )
        result_frames.extend(frames)

    return {"frames": result_frames}


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=args.port)
