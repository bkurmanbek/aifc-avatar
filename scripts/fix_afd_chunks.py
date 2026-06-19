#!/usr/bin/env python
"""Post-process the ingested Astana Finance Days 2026 (AFD) chunks for retrievability.

The raw docx ingestion (scripts/ingest_afd2026_docx.py) stored some sections in a way that
did not retrieve well: list sections (Confirmed Speakers, Partners, Exhibitors) were single
name-dominated chunks whose embedding drowned out the section context, so queries like
"confirmed speakers at AFD" never surfaced them; and contact details were buried in prose.

This script makes the AFD knowledge reachable (idempotent — safe to re-run):
  1. Prefix every AFD chunk's text with "Astana Finance Days 2026 (AFD) - <section>." so the
     embedding carries forum + section context.
  2. Split the list sections into small, naturally-framed sub-chunks that LEAD with the
     matchable phrase, e.g. "Confirmed speakers at Astana Finance Days 2026 (AFD) include: ...".
  3. Add a dedicated contacts/registration chunk (afd@aifc.kz / partnership@aifc.kz /
     pr@aifc.kz / astanafindays.org).

Run, then rebuild the FAISS index:
  PYTHONPATH=. /home/admin-aifc/miniforge3/bin/python scripts/fix_afd_chunks.py
  PYTHONPATH=. /home/admin-aifc/miniforge3/bin/python scripts/build_faiss_index.py
"""
from __future__ import annotations
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.settings import RAG_CHUNKS_PATH  # noqa: E402

PFX = "Astana Finance Days 2026 (AFD)"
LIST_LEAD = {
    "Confirmed Speakers": "Confirmed speakers at Astana Finance Days 2026 (AFD) include",
    "AFD 2026 Partners": "Astana Finance Days 2026 (AFD) partners and sponsors include",
    "AFD 2026 Exhibitors": "Astana Finance Days 2026 (AFD) exhibitors include",
}
GROUP = 7
CONTACTS = (
    "Astana Finance Days 2026 (AFD) contacts and registration. "
    "For general enquiries, contact afd@aifc.kz. "
    "For partnership, sponsorship, or exhibition enquiries, contact partnership@aifc.kz. "
    "For press and media enquiries, contact the AIFC Press Office at pr@aifc.kz (Ainur Issabayeva, Press Secretary). "
    "Registration and full forum details are available on the official website astanafindays.org."
)


def is_afd(c: dict) -> bool:
    return (c.get("metadata") or {}).get("domain") == "afd-2026"


def main() -> None:
    path = Path(RAG_CHUNKS_PATH)
    chunks = json.loads(path.read_text(encoding="utf-8"))
    out = []
    n_pfx = n_split = 0
    has_contacts = any(c.get("chunk_id") == "afd-2026-contacts" for c in chunks)

    for c in chunks:
        if not is_afd(c):
            out.append(c)
            continue
        md = c.get("metadata") or {}
        sec = (md.get("section_title") or "").strip()
        # already split sub-chunks pass through unchanged
        if str(c.get("chunk_id", "")).endswith(tuple(f"-p{i}" for i in range(20))):
            out.append(c)
            continue
        # 1) context prefix (idempotent)
        if not c["text"].startswith(PFX):
            c["text"] = (f"{PFX} - {sec}.\n" if sec else f"{PFX}.\n") + c["text"]
            n_pfx += 1
        # 2) split list sections into framed sub-chunks
        if sec in LIST_LEAD:
            lines = [l.strip() for l in c["text"].split("\n") if l.strip()]
            items = lines[1:] if lines and lines[0].startswith(PFX) else lines
            for i in range(0, len(items), GROUP):
                grp = items[i:i + GROUP]
                out.append({
                    "chunk_id": f"{c.get('chunk_id', sec)}-p{i // GROUP}",
                    "text": f"{LIST_LEAD[sec]}: " + "; ".join(grp) + ".",
                    "metadata": dict(md),
                })
                n_split += 1
        else:
            out.append(c)

    # 3) dedicated contacts chunk
    if not has_contacts:
        md0 = next((c["metadata"] for c in out if is_afd(c)), {"domain": "afd-2026"})
        out.append({
            "chunk_id": "afd-2026-contacts",
            "text": CONTACTS,
            "metadata": {**md0, "section_title": "AFD 2026 Contacts and Registration"},
        })

    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"AFD chunk fix: prefixed={n_pfx} split_subchunks={n_split} "
          f"contacts_added={not has_contacts} total_chunks={len(out)}")
    print("Now rebuild: PYTHONPATH=. python scripts/build_faiss_index.py")


if __name__ == "__main__":
    main()
