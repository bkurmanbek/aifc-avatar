#!/usr/bin/env python
"""Build the FAQ spoken sidecar: an LLM rewrite of every FAQ answer into an already-
pronounceable spoken form (numbers/dates/ordinals spelled out, abbreviations expanded),
in the answer's own language. Output: data/faq/aifc_faq_spoken.json = {sha256(answer): {spoken, lang}}.

The original FAQ source (aifc_faq_cache.txt) is NOT modified — the chat panel still shows the
written answer; only the avatar VOICE uses this sidecar. This lets us drop the regex number
normalizer. Resumable + incremental save. Run with the backend's base python (GOOGLE_API_KEY in .env):
  /home/admin-aifc/miniforge3/bin/python scripts/rewrite_faq_spoken.py [--limit N] [--force] [--concurrency 5]
"""
from __future__ import annotations
import argparse, asyncio, hashlib, json, sys
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _SCRIPT_DIR)]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.knowledge.faq import faq_cacheable_entries  # noqa: E402
from backend.knowledge.llm import _ABBR_HINT, _get_genai_client  # noqa: E402
from backend.settings import GEMINI_MODEL, DATA_DIR  # noqa: E402
from google.genai import types  # noqa: E402

OUT = DATA_DIR / "faq" / "aifc_faq_spoken.json"
LANG_NAME = {"en": "English", "ru": "Russian", "kk": "Kazakh", "zh": "Chinese"}


def ahash(a: str) -> str:
    return hashlib.sha256((a or "").strip().encode("utf-8")).hexdigest()


def prompt_for(answer: str, lang: str) -> str:
    lname = LANG_NAME.get(lang, "the same language as the answer")
    return (
        f"Rewrite the following AIFC answer so it can be read aloud by a text-to-speech voice, in {lname}. "
        "Output ONLY the rewritten text — no preamble, no quotes, no markdown, no notes.\n"
        "Rules:\n"
        "- Keep ALL facts, names, values, dates and meaning EXACTLY. Do not add, drop, or change information.\n"
        f"- Write EVERY number, date, ordinal, money amount, percentage and range as spoken WORDS in {lname} "
        "(never digits or symbols). English examples: \"9-10 September 2026\" -> \"the ninth to the tenth of "
        "September, twenty twenty-six\"; \"$5M\" -> \"five million dollars\"; \"50%\" -> \"fifty percent\"; "
        "\"3.5\" -> \"three point five\". In Chinese use Chinese number words.\n"
        f"- Expand EVERY abbreviation/acronym to its full spoken form in {lname} (do not speak the letters). "
        f"Known AIFC expansions (English canonical — translate to {lname}): {_ABBR_HINT}. Expand others you know.\n"
        "- Say emails and URLs naturally (e.g. \"property at aifc dot k z\").\n"
        "- Plain speakable prose only; keep it natural and the same length/detail as the original.\n\n"
        f"Answer to rewrite:\n{answer}"
    )


async def rewrite(client, answer: str, lang: str) -> str | None:
    cfg = types.GenerateContentConfig(temperature=0.1, max_output_tokens=1200)
    try:
        resp = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[{"role": "user", "parts": [{"text": prompt_for(answer, lang)}]}],
            config=cfg,
        )
        text = (resp.text or "").strip()
        return text or None
    except Exception as exc:
        print(f"  ERR {ahash(answer)[:8]}: {exc!r}"[:120])
        return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--concurrency", type=int, default=5)
    args = ap.parse_args()

    # unique answers (variants share answers) -> {hash: (answer, lang)}
    uniq: dict[str, tuple[str, str]] = {}
    for e in faq_cacheable_entries():
        a = (e["answer"] or "").strip()
        if a:
            uniq.setdefault(ahash(a), (a, e["language"] or "en"))
    items = list(uniq.items())
    if args.limit > 0:
        items = items[: args.limit]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    done: dict[str, dict] = {}
    if OUT.exists() and not args.force:
        try:
            done = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            done = {}
    todo = [(h, a, lang) for h, (a, lang) in items if h not in done]
    print(f"[faq-spoken] {len(uniq)} unique answers; {len(done)} already done; {len(todo)} to rewrite (concurrency={args.concurrency})")

    client = _get_genai_client()
    sem = asyncio.Semaphore(args.concurrency)
    n_ok = n_fail = 0
    lock = asyncio.Lock()

    async def one(h, a, lang):
        nonlocal n_ok, n_fail
        async with sem:
            spoken = await rewrite(client, a, lang)
        async with lock:
            if spoken:
                done[h] = {"spoken": spoken, "lang": lang}
                n_ok += 1
            else:
                n_fail += 1
            if (n_ok + n_fail) % 20 == 0:
                OUT.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"  progress: {n_ok} ok, {n_fail} fail, saved {len(done)}")

    # process in chunks to bound memory of pending tasks
    B = max(args.concurrency * 4, 20)
    for i in range(0, len(todo), B):
        await asyncio.gather(*(one(h, a, lang) for h, a, lang in todo[i:i + B]))
    OUT.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[faq-spoken] done: {n_ok} ok, {n_fail} fail, total {len(done)} -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
