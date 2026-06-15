#!/usr/bin/env python3
"""
Full pipeline: text → Soniox TTS → SyncTalk → MP4.
Usage: python scripts/query_to_mp4.py "Your text here" [output.mp4]
"""
import argparse
import asyncio
import base64
import io
import json
import os
import sys
import urllib.request

# Remove scripts/ from path to avoid shadowing built-in 'chunk' module
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if sys.path and sys.path[0] == _scripts_dir:
    sys.path.pop(0)

_root = os.path.dirname(_scripts_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)

import cv2
import numpy as np

SYNCTALK_URL = "http://127.0.0.1:8005/infer"
FPS = 25


async def tts_to_wav(text: str) -> bytes:
    from backend.media.tts import SonioxRealtimeTTS
    tts = SonioxRealtimeTTS(session_id="query_to_mp4")
    print(f"Synthesizing TTS: \"{text}\"")
    wav = await tts.synthesize(text, language="en")
    print(f"TTS done: {len(wav)} bytes WAV")
    return wav


def synctalk_infer(wav_bytes: bytes) -> list[np.ndarray]:
    audio_b64 = base64.b64encode(wav_bytes).decode()
    payload = json.dumps({"audio_b64": audio_b64}).encode()
    req = urllib.request.Request(
        SYNCTALK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print("Sending to SyncTalk...")
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())

    frames_b64 = result.get("frames", [])
    print(f"Received {len(frames_b64)} frames from SyncTalk")

    frames = []
    for fb64 in frames_b64:
        img_bytes = base64.b64decode(fb64)
        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            frames.append(img)
    return frames


FFMPEG = "/home/admin-aifc/miniforge3/envs/synctalk2d/bin/ffmpeg"


def save_mp4(frames: list[np.ndarray], out_path: str) -> None:
    if not frames:
        print("No frames to save.", file=sys.stderr)
        sys.exit(1)
    h, w = frames[0].shape[:2]
    tmp_path = out_path + ".raw.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(tmp_path, fourcc, FPS, (w, h))
    for frame in frames:
        writer.write(frame)
    writer.release()
    # Re-encode to H.264 for universal compatibility
    import subprocess
    subprocess.run(
        [FFMPEG, "-y", "-i", tmp_path,
         "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
         out_path],
        check=True, capture_output=True,
    )
    os.remove(tmp_path)
    print(f"Saved {len(frames)} frames ({len(frames)/FPS:.2f}s) → {out_path}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="?", default="Welcome to AIFC. How can I help you today?")
    parser.add_argument("out", nargs="?", default="/home/admin-aifc/avatar-system-2/synctalk_output.mp4")
    args = parser.parse_args()

    wav = await tts_to_wav(args.text)
    frames = synctalk_infer(wav)
    save_mp4(frames, args.out)


if __name__ == "__main__":
    asyncio.run(main())
