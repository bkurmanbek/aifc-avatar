#!/usr/bin/env python3
"""
Phase 0 benchmark harness for OPTIMIZATION_PLAN.md.

Measures SyncTalk render throughput + first-frame latency against a FIXED
reference WAV, samples peak VRAM, and (optionally) saves "golden" frames or
compares a run against the golden set with SSIM/PSNR.

This is a pure HTTP client — it does NOT import or modify the SyncTalk server,
and it does NOT restart anything. Safe to run while SyncTalk is up (it will
contend for the GPU with any live session, so run it when the box is idle).

Usage
-----
  # 1) establish the FP32 baseline + save golden frames:
  python scripts/bench_synctalk.py --tag fp32_baseline --save-golden

  # 2) after an optimization (e.g. BF16), compare against golden:
  python scripts/bench_synctalk.py --tag bf16 --compare-golden

Results are written to var/bench/results_<tag>_<ts>.json.
"""
import os
import sys

# Drop the scripts/ dir from sys.path so stdlib `wave` doesn't import the local
# scripts/chunk.py instead of the stdlib `chunk` module (name collision).
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0]) == _scripts_dir:
    sys.path.pop(0)

import argparse
import base64
import json
import math
import threading
import time
import urllib.request
import wave
from glob import glob

import cv2
import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH_DIR = os.path.join(REPO, "var", "bench")
DEFAULT_TTS_CHUNKS = os.path.join(REPO, "var", "debug", "tts_chunks")


# ── reference WAV ─────────────────────────────────────────────
def build_reference_wav(out_path: str, tts_chunks_root: str) -> str:
    """Concatenate the chunk_*.wav of one tts_chunks turn into a single ~10-15s
    reference WAV. Cached: rebuilt only if missing."""
    if os.path.exists(out_path):
        return out_path
    # pick the turn dir with the most chunks (longest answer → steady-state fps)
    best_dir, best_n = None, -1
    for d in glob(os.path.join(tts_chunks_root, "*")):
        if not os.path.isdir(d):
            continue
        n = len(glob(os.path.join(d, "chunk_*.wav")))
        if n > best_n:
            best_dir, best_n = d, n
    if best_dir is None or best_n <= 0:
        raise SystemExit(
            f"No tts_chunks found under {tts_chunks_root}. "
            f"Pass --wav <path> with a 24kHz mono 16-bit WAV instead."
        )
    chunks = sorted(glob(os.path.join(best_dir, "chunk_*.wav")))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    params = None
    frames = []
    for c in chunks:
        with wave.open(c, "rb") as w:
            if params is None:
                params = w.getparams()
            frames.append(w.readframes(w.getnframes()))
    with wave.open(out_path, "wb") as w:
        w.setparams(params)
        w.writeframes(b"".join(frames))
    print(f"[ref] built {out_path} from {best_n} chunks of {os.path.basename(best_dir)}")
    return out_path


def wav_seconds(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


# ── VRAM sampler ──────────────────────────────────────────────
class VramSampler:
    """Polls total GPU memory.used (MiB) in a background thread, tracks peak."""

    def __init__(self, interval_s: float = 0.15):
        self.interval = interval_s
        self._stop = threading.Event()
        self._thread = None
        self.peak_mib = 0
        self.idle_mib = self._read()

    @staticmethod
    def _read() -> int:
        try:
            out = os.popen(
                "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits"
            ).read().strip().splitlines()
            return int(out[0]) if out else 0
        except Exception:
            return 0

    def _loop(self):
        while not self._stop.is_set():
            self.peak_mib = max(self.peak_mib, self._read())
            time.sleep(self.interval)

    def start(self):
        self.peak_mib = self.idle_mib
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)


# ── one inference run ─────────────────────────────────────────
def run_once(url: str, wav_bytes: bytes, keep_frames: bool):
    """Stream /infer_stream once. Returns (first_frame_ms, total_frames, wall_s, frames_or_None)."""
    payload = json.dumps({"audio_b64": base64.b64encode(wav_bytes).decode(),
                          "start_frame": 0}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    frames = [] if keep_frames else None
    first_ms = None
    total = 0
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.strip()
            if not line:
                continue
            obj = json.loads(line)
            b64 = obj.get("frame")
            if b64 is None:
                continue
            total += 1
            if first_ms is None:
                first_ms = (time.perf_counter() - t0) * 1000.0
            if keep_frames:
                buf = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
                frames.append(cv2.imdecode(buf, cv2.IMREAD_COLOR))
    wall = time.perf_counter() - t0
    return (first_ms or 0.0), total, wall, frames


# ── quality metrics (no skimage dependency) ───────────────────
def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    if mse == 0:
        return float("inf")
    return 20.0 * math.log10(255.0 / math.sqrt(mse))


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Gaussian-windowed SSIM (Wang et al.) on grayscale, via cv2."""
    a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.float64)
    b = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.float64)
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    k = (11, 11)
    mu_a = cv2.GaussianBlur(a, k, 1.5)
    mu_b = cv2.GaussianBlur(b, k, 1.5)
    mu_a2, mu_b2, mu_ab = mu_a * mu_a, mu_b * mu_b, mu_a * mu_b
    sa = cv2.GaussianBlur(a * a, k, 1.5) - mu_a2
    sb = cv2.GaussianBlur(b * b, k, 1.5) - mu_b2
    sab = cv2.GaussianBlur(a * b, k, 1.5) - mu_ab
    num = (2 * mu_ab + C1) * (2 * sab + C2)
    den = (mu_a2 + mu_b2 + C1) * (sa + sb + C2)
    return float(np.mean(num / den))


def compare_to_golden(frames, golden_dir):
    golden = sorted(glob(os.path.join(golden_dir, "frame_*.png")))
    if not golden:
        raise SystemExit(f"No golden frames in {golden_dir}. Run with --save-golden first.")
    n = min(len(frames), len(golden))
    if len(frames) != len(golden):
        print(f"[compare] WARNING frame count differs: run={len(frames)} golden={len(golden)} "
              f"(comparing first {n})")
    ssims, psnrs = [], []
    for i in range(n):
        g = cv2.imread(golden[i], cv2.IMREAD_COLOR)
        f = frames[i]
        if g.shape != f.shape:
            f = cv2.resize(f, (g.shape[1], g.shape[0]))
        ssims.append(ssim(g, f))
        psnrs.append(psnr(g, f))
    return {
        "frames_compared": n,
        "ssim_mean": float(np.mean(ssims)), "ssim_min": float(np.min(ssims)),
        "psnr_mean": float(np.mean(psnrs)), "psnr_min": float(np.min(psnrs)),
    }


def save_golden(frames, golden_dir):
    os.makedirs(golden_dir, exist_ok=True)
    for f in glob(os.path.join(golden_dir, "frame_*.png")):
        os.remove(f)
    for i, fr in enumerate(frames):
        cv2.imwrite(os.path.join(golden_dir, f"frame_{i:04d}.png"), fr)
    print(f"[golden] saved {len(frames)} frames to {golden_dir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8005/infer_stream")
    ap.add_argument("--wav", default=os.path.join(BENCH_DIR, "reference.wav"),
                    help="reference WAV (auto-built from tts_chunks if missing)")
    ap.add_argument("--tts-chunks", default=DEFAULT_TTS_CHUNKS)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--warmup", type=int, default=2, help="discarded warmup runs")
    ap.add_argument("--tag", default="run", help="label for this benchmark (e.g. fp32_baseline)")
    ap.add_argument("--golden-dir", default=os.path.join(BENCH_DIR, "golden"))
    ap.add_argument("--save-golden", action="store_true")
    ap.add_argument("--compare-golden", action="store_true")
    args = ap.parse_args()

    os.makedirs(BENCH_DIR, exist_ok=True)
    wav_path = args.wav
    if not os.path.exists(wav_path):
        wav_path = build_reference_wav(wav_path, args.tts_chunks)
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()
    dur = wav_seconds(wav_path)
    print(f"[bench] tag={args.tag} wav={os.path.basename(wav_path)} "
          f"({dur:.2f}s audio) runs={args.runs} warmup={args.warmup}")

    # warmup (not measured) — also pre-compiles any kernels for a cold server
    for _ in range(args.warmup):
        run_once(args.url, wav_bytes, keep_frames=False)

    vram = VramSampler()
    vram.start()
    per_run = []
    first_frames = None
    for i in range(args.runs):
        keep = (i == 0) and (args.save_golden or args.compare_golden)
        first_ms, total, wall, frames = run_once(args.url, wav_bytes, keep_frames=keep)
        fps = total / wall if wall > 0 else 0.0
        per_run.append({"first_frame_ms": first_ms, "frames": total,
                        "wall_s": wall, "fps": fps})
        if keep:
            first_frames = frames
        print(f"  run {i+1:>2}/{args.runs}: first_frame={first_ms:6.0f}ms "
              f"frames={total:>4} wall={wall:5.2f}s fps={fps:5.1f}")
    vram.stop()

    fps_list = sorted(r["fps"] for r in per_run)
    ff_list = sorted(r["first_frame_ms"] for r in per_run)
    median = lambda xs: xs[len(xs) // 2]
    result = {
        "tag": args.tag,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "wav": os.path.basename(wav_path), "audio_s": dur,
        "runs": args.runs, "warmup": args.warmup,
        "fps_median": median(fps_list), "fps_min": fps_list[0], "fps_max": fps_list[-1],
        "first_frame_ms_median": median(ff_list),
        "realtime_factor": median(fps_list) / 25.0,
        "vram_idle_mib": vram.idle_mib, "vram_peak_mib": vram.peak_mib,
        "per_run": per_run,
    }

    if args.save_golden:
        save_golden(first_frames, args.golden_dir)
    if args.compare_golden:
        result["quality_vs_golden"] = compare_to_golden(first_frames, args.golden_dir)

    out = os.path.join(BENCH_DIR, f"results_{args.tag}_{int(time.time())}.json")
    with open(out, "w") as f:
        json.dump(result, f, indent=2)

    print("\n── summary ─────────────────────────────")
    print(f"  fps median      : {result['fps_median']:.1f}  "
          f"(min {result['fps_min']:.1f} / max {result['fps_max']:.1f})")
    print(f"  realtime factor : {result['realtime_factor']:.2f}x  (need >=1.0 per stream)")
    print(f"  est. concurrent : ~{int(result['fps_median'] // 25)} avatar(s) @ 25fps")
    print(f"  first frame     : {result['first_frame_ms_median']:.0f}ms")
    print(f"  VRAM idle/peak  : {result['vram_idle_mib']} / {result['vram_peak_mib']} MiB")
    if "quality_vs_golden" in result:
        q = result["quality_vs_golden"]
        print(f"  vs golden       : SSIM {q['ssim_mean']:.4f} (min {q['ssim_min']:.4f}) "
              f"PSNR {q['psnr_mean']:.1f}dB (min {q['psnr_min']:.1f}dB) "
              f"over {q['frames_compared']} frames")
        ok = q['ssim_mean'] >= 0.98 and q['psnr_mean'] >= 38.0
        print(f"  quality gate    : {'PASS' if ok else 'FAIL'} (SSIM>=0.98, PSNR>=38dB)")
    print(f"  results json    : {out}")


if __name__ == "__main__":
    main()
