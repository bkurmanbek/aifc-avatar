#!/usr/bin/env python3
"""
Benchmark: full TTS generation time + SyncTalk first-frame latency.
Sends the complete TTS output as a single SyncTalk inference per query.
"""
import asyncio
import base64
import json
import os
import sys
import time
import urllib.request

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if sys.path and sys.path[0] == _scripts_dir:
    sys.path.pop(0)
sys.path.insert(0, os.path.dirname(_scripts_dir))

SYNCTALK_URL = "http://127.0.0.1:8005/infer_stream"

QUERIES = [
    "Welcome to AIFC. How can I help you today?",
    "AIFC is the Astana International Financial Centre, a leading financial hub in Central Asia.",
    "You can register your company at AIFC through our streamlined online portal in just a few steps.",
    "AIFC offers a wide range of financial services including banking, insurance, and capital markets.",
    "Our FinTech Lab supports innovative startups with mentorship, funding, and regulatory guidance.",
]


async def tts_synthesize(text: str) -> tuple[bytes, float]:
    """Returns (wav_bytes, elapsed_ms)."""
    from backend.media.tts import SonioxRealtimeTTS
    tts = SonioxRealtimeTTS(session_id="benchmark")
    t0 = time.perf_counter()
    wav = await tts.synthesize(text, language="en")
    elapsed = (time.perf_counter() - t0) * 1000
    return wav, elapsed


def synctalk_first_frame(wav_bytes: bytes) -> tuple[float, int]:
    """Returns (ms_to_first_frame, total_frames)."""
    audio_b64 = base64.b64encode(wav_bytes).decode()
    payload = json.dumps({"audio_b64": audio_b64}).encode()
    req = urllib.request.Request(
        SYNCTALK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    first_frame_ms = None
    total_frames = 0
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw_line in resp:
            line = raw_line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "frame" in obj:
                total_frames += 1
                if first_frame_ms is None:
                    first_frame_ms = (time.perf_counter() - t0) * 1000
    return first_frame_ms or 0.0, total_frames


async def run_query(idx: int, text: str) -> dict:
    print(f"\n[{idx+1}/5] \"{text[:60]}...\"" if len(text) > 60 else f"\n[{idx+1}/5] \"{text}\"")

    wav, tts_ms = await tts_synthesize(text)
    wav_kb = len(wav) / 1024
    audio_s = (len(wav) - 44) / (24000 * 2)
    print(f"  TTS done:        {tts_ms:.0f}ms  ({wav_kb:.1f}KB WAV, {audio_s:.2f}s audio)")

    first_frame_ms, total_frames = synctalk_first_frame(wav)
    print(f"  First frame:     {first_frame_ms:.0f}ms after SyncTalk call")
    print(f"  Total frames:    {total_frames}  ({total_frames/25:.2f}s @ 25fps)")

    return {
        "query": text,
        "tts_ms": tts_ms,
        "audio_s": audio_s,
        "wav_kb": wav_kb,
        "first_frame_ms": first_frame_ms,
        "total_frames": total_frames,
    }


def tts_subprocess(idx: int, text: str, out_path: str) -> dict:
    """Run a single TTS call in a subprocess, write WAV to out_path, return timing."""
    import subprocess, tempfile
    script = f"""
import asyncio, sys, os, time
_scripts_dir = os.path.dirname(os.path.abspath('{__file__}'))
if sys.path and sys.path[0] == _scripts_dir:
    sys.path.pop(0)
sys.path.insert(0, os.path.dirname(_scripts_dir))
from backend.media.tts import SonioxRealtimeTTS
async def run():
    tts = SonioxRealtimeTTS(session_id='p{idx}')
    t0 = time.perf_counter()
    wav = await tts.synthesize({text!r}, language='en')
    ms = (time.perf_counter() - t0) * 1000
    with open({out_path!r}, 'wb') as f:
        f.write(wav)
    print(f'{{ms:.0f}}')
asyncio.run(run())
"""
    result = subprocess.run(
        ["/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python", "-c", script],
        capture_output=True, text=True, timeout=60
    )
    ms = float(result.stdout.strip()) if result.stdout.strip() else 0.0
    return {"idx": idx, "tts_ms": ms}


def run_parallel_tts() -> tuple[float, list[dict]]:
    """Run all 5 TTS calls in parallel subprocesses. Returns (wall_ms, results)."""
    import concurrent.futures, tempfile
    tmp_dir = tempfile.mkdtemp(prefix="bm_tts_")
    out_paths = [os.path.join(tmp_dir, f"q{i}.wav") for i in range(len(QUERIES))]

    wall_t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(QUERIES)) as ex:
        futures = [ex.submit(tts_subprocess, i, q, out_paths[i]) for i, q in enumerate(QUERIES)]
        results = [f.result() for f in futures]
    wall_ms = (time.perf_counter() - wall_t0) * 1000

    for r, path in zip(results, out_paths):
        if os.path.exists(path):
            size = os.path.getsize(path)
            audio_s = max(0, (size - 44)) / (24000 * 2)
            r["wav_path"] = path
            r["wav_kb"] = size / 1024
            r["audio_s"] = audio_s
        else:
            r["wav_path"] = None
            r["wav_kb"] = 0.0
            r["audio_s"] = 0.0
    return wall_ms, results


async def main():
    print("=" * 65)
    print("Benchmark: TTS full generation + SyncTalk first-frame latency")
    print("=" * 65)

    # --- Sequential ---
    print("\n>>> SEQUENTIAL (one query at a time)")
    results = []
    for i, q in enumerate(QUERIES):
        r = await run_query(i, q)
        results.append(r)

    tts_vals = [r["tts_ms"] for r in results]
    ff_vals  = [r["first_frame_ms"] for r in results]
    tot_vals = [r["tts_ms"] + r["first_frame_ms"] for r in results]

    print("\n" + "=" * 65)
    print("SUMMARY — SEQUENTIAL")
    print("=" * 65)
    print(f"{'#':<3} {'TTS ms':>8} {'Audio s':>8} {'1st frame ms':>14} {'Total ms':>10}")
    print("-" * 65)
    for i, r in enumerate(results):
        print(f"{i+1:<3} {r['tts_ms']:>8.0f} {r['audio_s']:>8.2f} {r['first_frame_ms']:>14.0f} {r['tts_ms']+r['first_frame_ms']:>10.0f}")
    print("-" * 65)
    print(f"{'avg':<3} {sum(tts_vals)/len(tts_vals):>8.0f} {'':>8} {sum(ff_vals)/len(ff_vals):>14.0f} {sum(tot_vals)/len(tot_vals):>10.0f}")
    print(f"{'min':<3} {min(tts_vals):>8.0f} {'':>8} {min(ff_vals):>14.0f} {min(tot_vals):>10.0f}")
    print(f"{'max':<3} {max(tts_vals):>8.0f} {'':>8} {max(ff_vals):>14.0f} {max(tot_vals):>10.0f}")

    # --- Parallel TTS ---
    print("\n>>> PARALLEL TTS (all 5 fired simultaneously via subprocesses)")
    wall_tts_ms, par_tts = run_parallel_tts()
    print(f"  Wall-clock for all 5 TTS: {wall_tts_ms:.0f}ms  (sequential avg: {sum(tts_vals)/len(tts_vals):.0f}ms)")
    for r in par_tts:
        print(f"  [{r['idx']+1}] TTS: {r['tts_ms']:.0f}ms  ({r['wav_kb']:.1f}KB, {r['audio_s']:.2f}s audio)")

    print("\n  SyncTalk after parallel TTS (sequential infer, one at a time):")
    parallel_results = []
    for r in par_tts:
        if r.get("wav_path") and os.path.exists(r["wav_path"]):
            with open(r["wav_path"], "rb") as f:
                wav_bytes = f.read()
            ff_ms, total_frames = synctalk_first_frame(wav_bytes)
        else:
            print(f"  [{r['idx']+1}] TTS failed — skipping SyncTalk")
            ff_ms, total_frames = 0.0, 0
        print(f"  [{r['idx']+1}] First frame: {ff_ms:.0f}ms  total frames: {total_frames}")
        parallel_results.append({**r, "first_frame_ms": ff_ms, "total_frames": total_frames})

    print("\n" + "=" * 65)
    print("SUMMARY — PARALLEL TTS")
    print("=" * 65)
    print(f"  Wall-clock (all 5 TTS in parallel): {wall_tts_ms:.0f}ms  vs  sequential avg: {sum(tts_vals)/len(tts_vals):.0f}ms")
    p_tts = [r["tts_ms"] for r in parallel_results]
    p_ff  = [r["first_frame_ms"] for r in parallel_results]
    p_tot = [r["tts_ms"] + r["first_frame_ms"] for r in parallel_results]
    print(f"{'#':<3} {'TTS ms':>8} {'Audio s':>8} {'1st frame ms':>14} {'Total ms':>10}")
    print("-" * 65)
    for r in parallel_results:
        print(f"{r['idx']+1:<3} {r['tts_ms']:>8.0f} {r['audio_s']:>8.2f} {r['first_frame_ms']:>14.0f} {r['tts_ms']+r['first_frame_ms']:>10.0f}")
    if p_tts:
        print("-" * 65)
        print(f"{'avg':<3} {sum(p_tts)/len(p_tts):>8.0f} {'':>8} {sum(p_ff)/len(p_ff):>14.0f} {sum(p_tot)/len(p_tot):>10.0f}")
        print(f"{'min':<3} {min(p_tts):>8.0f} {'':>8} {min(p_ff):>14.0f} {min(p_tot):>10.0f}")
        print(f"{'max':<3} {max(p_tts):>8.0f} {'':>8} {max(p_ff):>14.0f} {max(p_tot):>10.0f}")


if __name__ == "__main__":
    asyncio.run(main())
