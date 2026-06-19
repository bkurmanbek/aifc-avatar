#!/usr/bin/env python
"""Offline builder for the FAQ answer video cache (cache/faq/video/<avatar>/<key>.mp4).

Renders each FAQ answer through TTS -> SyncTalk -> ffmpeg ONCE and stores a hardware-
decodable MP4, keyed by the exact final ``spoken`` string the live turn path would render.
At runtime, a confident FAQ fast-path win serves the matching MP4 instead of re-rendering
live (zero GPU at serve time). A cache miss simply falls back to the live render.

Prerequisites (same as a normal turn):
  - SyncTalk server running on :8005   (bash scripts/start_synctalk.sh)
  - SONIOX_API_KEY etc. in .env        (loaded automatically via backend.settings)

Run with the backend's interpreter:
  /home/admin-aifc/miniforge3/envs/synctalk2d/bin/python scripts/build_faq_videos.py
  ... --limit 20        # only the first N entries (smoke / partial build)
  ... --langs en,ru     # languages to render for language-less fallback entries
  ... --force           # rebuild even if the MP4 already exists
  ... --dry-run         # list what WOULD be built, render nothing
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Drop the scripts/ dir (and cwd) from sys.path: scripts/chunk.py would otherwise shadow
# the stdlib `chunk` module that `wave` (pulled in via backend.intro) imports.
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _SCRIPT_DIR)]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.faq_video import build_faq_video, faq_video_key, faq_video_path  # noqa: E402
from backend.knowledge.faq import faq_cacheable_entries  # noqa: E402
from backend.media.synctalk import SyncTalkClient  # noqa: E402
from backend.media.tts import SonioxRealtimeTTS  # noqa: E402
from backend.pipeline.answer_common import candidate_from_answer  # noqa: E402
from backend.pipeline.answer_format import (  # noqa: E402
    coerce_spoken_chat_payload,
    extract_json_any,
    normalize_spoken_for_tts,
)
from backend.settings import SONIOX_TTS_VOICE  # noqa: E402

# Conversation languages a language-less ("") fallback FAQ entry can be asked in. Such an
# entry matches any conversation language, and normalize_spoken_for_tts is language-aware,
# so we render one MP4 per language to guarantee a runtime key match.
DEFAULT_LANGS = ["en", "ru", "kk", "zh"]


async def compute_spoken(answer: str, language: str) -> str:
    """Reproduce the exact ``spoken`` string session.py renders for an FAQ winner.

    Mirrors session.py: candidate_from_answer -> extract_json_any -> coerce_spoken_chat_
    payload -> normalize_spoken_for_tts(..., trim_for_latency=False). Keeping this identical
    is what makes the build-time cache key equal the serve-time lookup key.
    """
    cand = candidate_from_answer(
        source="faq",
        answer=answer,
        language=language,
        confidence="high",
        score=1.0,
        cacheable=True,
        trim_spoken=False,  # must match faq_candidate so build key == serve key
    )
    payload = extract_json_any(cand.raw_answer)
    if isinstance(payload, dict):
        ap = coerce_spoken_chat_payload(payload, language)
    else:
        ap = coerce_spoken_chat_payload({"spoken": answer, "chat": answer}, language)
    return await normalize_spoken_for_tts(ap.get("spoken", ""), language, trim_for_latency=False)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="only build the first N entries (0 = all)")
    parser.add_argument("--langs", default=",".join(DEFAULT_LANGS), help="langs for language-less entries")
    parser.add_argument("--force", action="store_true", help="rebuild even if the MP4 exists")
    parser.add_argument("--dry-run", action="store_true", help="list planned builds, render nothing")
    args = parser.parse_args()

    fallback_langs = [s.strip() for s in args.langs.split(",") if s.strip()]
    entries = faq_cacheable_entries()
    if args.limit > 0:
        entries = entries[: args.limit]

    # Expand to (spoken, language) units and dedupe by cache key.
    plan: dict[str, tuple[str, str]] = {}
    for entry in entries:
        answer = entry["answer"]
        langs = [entry["language"]] if entry["language"] else fallback_langs
        for lang in langs:
            spoken = await compute_spoken(answer, lang)
            if not spoken.strip():
                continue
            key = faq_video_key(spoken, lang)
            plan.setdefault(key, (spoken, lang))

    print(f"[faq-video] {len(entries)} entries -> {len(plan)} unique (spoken, lang) videos")
    if args.dry_run:
        for key, (spoken, lang) in plan.items():
            exists = faq_video_path(key).exists()
            print(f"  {key} [{lang}] {'CACHED' if exists else 'TODO  '}  {spoken[:70]!r}")
        return

    tts = SonioxRealtimeTTS()
    synctalk = SyncTalkClient()
    built = skipped = failed = 0
    try:
        for i, (key, (spoken, lang)) in enumerate(plan.items(), 1):
            if not args.force and faq_video_path(key).exists():
                skipped += 1
                continue
            print(f"[faq-video] ({i}/{len(plan)}) building {key} [{lang}] {spoken[:60]!r}")
            try:
                path = await build_faq_video(
                    spoken, lang, tts=tts, synctalk=synctalk, force=args.force
                )
            except Exception as exc:  # one bad entry must not abort the whole build
                failed += 1
                print(f"  ERROR {key}: {exc!r}")
                continue
            if path is None:
                failed += 1
                print(f"  FAILED {key} (no output)")
            else:
                built += 1
    finally:
        with __import__("contextlib").suppress(Exception):
            await tts.close()
        with __import__("contextlib").suppress(Exception):
            await synctalk.close()

    print(f"[faq-video] done: built={built} skipped={skipped} failed={failed} total={len(plan)}")


if __name__ == "__main__":
    asyncio.run(main())
