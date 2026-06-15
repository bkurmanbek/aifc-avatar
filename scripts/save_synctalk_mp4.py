#!/usr/bin/env python3
"""
Send audio to the running SyncTalk server and save the output frames as MP4.
Usage: python scripts/save_synctalk_mp4.py [wav_file] [output.mp4]
Defaults: /tmp/query_en.wav  ->  /tmp/synctalk_output.mp4
"""
import argparse
import base64
import io
import json
import sys
import urllib.request

import cv2
import numpy as np

SYNCTALK_URL = "http://127.0.0.1:8005/infer"
FPS = 25


def infer(wav_path: str) -> list[np.ndarray]:
    with open(wav_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()

    payload = json.dumps({"audio_b64": audio_b64}).encode()
    req = urllib.request.Request(
        SYNCTALK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print(f"Sending {wav_path} to SyncTalk...")
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


def save_mp4(frames: list[np.ndarray], out_path: str) -> None:
    if not frames:
        print("No frames to save.", file=sys.stderr)
        sys.exit(1)

    h, w = frames[0].shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(out_path, fourcc, FPS, (w, h))
    for frame in frames:
        writer.write(frame)
    writer.release()
    print(f"Saved {len(frames)} frames ({len(frames)/FPS:.2f}s) → {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", nargs="?", default="/tmp/query_en.wav")
    parser.add_argument("out", nargs="?", default="/tmp/synctalk_output.mp4")
    args = parser.parse_args()

    frames = infer(args.wav)
    save_mp4(frames, args.out)


if __name__ == "__main__":
    main()
