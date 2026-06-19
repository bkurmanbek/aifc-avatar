"""
scripts/ingest_afd2026_docx.py — ADDITIVE ingestion of the
"Astana Finance Days 2026 Info FAQ.docx" into the AIFC RAG corpus + indexes.

What it does (additive only — never overwrites existing source docs):
  1. Extracts text from the .docx via stdlib zipfile + ElementTree (no python-docx).
  2. Chunks it to match the existing corpus schema
     {chunk_id, text, metadata:{chunk_index, doc_type, domain, is_table,
      language, section_title, source_file, token_estimate}}.
  3. Writes data/chunks/chunks_afd-2026.json (new per-domain chunk file).
  4. APPENDS the new chunks to data/chunks/chunks.json (the canonical corpus the
     FAISS builder reads) so a future full rebuild keeps them.
  5. APPENDS the embedded vectors to the LIVE FAISS index (IndexFlatIP is
     appendable — no full re-embed of the 30k existing chunks) and appends the
     matching metadata rows.
  6. Upserts the same chunks into Qdrant (so both backends carry the doc).

Embeddings: the SAME local bge-m3 used by the backend (rag.retriever.embed_texts),
L2-normalized exactly like scripts/build_faiss_index.py.
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

from rag.retriever import embed_texts
from rag.settings import (
    FAISS_INDEX_PATH,
    FAISS_METADATA_PATH,
    QDRANT_VECTOR_SIZE,
    RAG_CHUNKS_PATH,
)

DOCX_PATH = PROJECT_ROOT / "Astana Finance Days 2026 Info FAQ.docx"
SOURCE_FILE = "Astana Finance Days 2026 Info FAQ.docx"
DOMAIN = "afd-2026"
DOC_TYPE = "event"
AFD_CHUNKS_FILE = RAG_CHUNKS_PATH.parent / "chunks_afd-2026.json"

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Rough token estimate consistent with the corpus (~words * 1.3). Existing chunks
# show ~300-400 token_estimate for ~250-300 word blocks.
def _token_estimate(text: str) -> int:
    words = len(re.findall(r"\S+", text))
    return int(round(words * 1.3))


def _para_text(p) -> str:
    parts: list[str] = []
    for node in p.iter():
        tag = node.tag
        if tag == W + "t":
            parts.append(node.text or "")
        elif tag == W + "tab":
            parts.append("\t")
        elif tag in (W + "br", W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _extract_blocks() -> list[dict]:
    """Return ordered blocks: {'kind':'p'|'row', 'cells': [...]} preserving structure."""
    with zipfile.ZipFile(DOCX_PATH) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(W + "body")
    blocks: list[dict] = []
    for el in body:
        if el.tag == W + "p":
            txt = _para_text(el).strip()
            if txt:
                blocks.append({"kind": "p", "text": txt})
        elif el.tag == W + "tbl":
            for row in el.iter(W + "tr"):
                cells = []
                for cell in row.findall(W + "tc"):
                    ctext = " ".join(_para_text(p) for p in cell.findall(W + "p"))
                    cells.append(ctext.strip())
                blocks.append({"kind": "row", "cells": cells})
    return blocks


# Section headings that appear as standalone short paragraphs in the doc.
_SECTION_HEADINGS = {
    "Brief Description",
    "AFD Press Release",
    "Why Attend AFD 2026:",
    "AFD 2026Thematic Tracks",
    "Confirmed Speakers:",
    "AFD 2026 Programme Overview:",
    "AFD 2026 Partners:",
    "AFD Exhibition",
    "AFD 2026 Exhibitors:",
    "FAQ:",
    "Reference:",
}

# Track-section headers inside the Programme Overview (used as section_title).
_TRACK_HEADERS = {
    "Capital Markets & Investment Products",
    "Trust Infrastructure: Regulation, Law & Market Confidence",
    "Innovation at Institutional Scale",
    "Financing the Real Economy",
    "Regional & Cross-Border Capital Cooperation",
}


def _make_chunk(idx: int, text: str, section_title: str, language: str, is_table: bool) -> dict:
    text = text.strip()
    return {
        "chunk_id": f"{DOMAIN}-afd2026-faq-{idx:05d}",
        "text": text,
        "metadata": {
            "chunk_index": idx,
            "doc_type": DOC_TYPE,
            "domain": DOMAIN,
            "is_table": is_table,
            "language": language,
            "section_title": section_title,
            "source_file": SOURCE_FILE,
            "token_estimate": _token_estimate(text),
        },
    }


def build_chunks() -> list[dict]:
    blocks = _extract_blocks()
    chunks: list[dict] = []
    idx = 0

    current_section = "Astana Finance Days 2026"
    # Buffer of prose paragraphs under a section, flushed when section changes
    # or buffer grows large, to keep chunk sizes near the corpus norm.
    buf: list[str] = []

    def flush_prose():
        nonlocal idx, buf
        if not buf:
            return
        text = "\n".join(buf).strip()
        if text:
            chunks.append(_make_chunk(idx, text, current_section, "en", False))
            idx += 1
        buf = []

    in_faq = False
    lang_for_col: dict[int, str] = {}  # column index -> language code
    pending_q: dict[int, str] | None = None  # buffered question row (col->text)

    def emit_faq_pair(q_cells: dict[int, str], a_cells: dict[int, str] | None):
        """Emit one chunk per language combining Q + A (A may be None for dividers)."""
        nonlocal idx
        en_q = q_cells.get(next((ci for ci, l in lang_for_col.items() if l == "en"), 1), "").strip()
        for ci, lang in lang_for_col.items():
            q = q_cells.get(ci, "").strip()
            if not q:
                continue
            a = (a_cells or {}).get(ci, "").strip()
            text = f"{q}\n\n{a}" if a else q
            sect = f"AFD 2026 FAQ — {en_q[:90]}" if en_q else "AFD 2026 FAQ"
            chunks.append(_make_chunk(idx, text, sect, lang, True))
            idx += 1

    def is_question_row(cells_by_col: dict[int, str]) -> bool:
        # The EN column drives the decision; questions end with '?'.
        en_col = next((ci for ci, l in lang_for_col.items() if l == "en"), 1)
        en = cells_by_col.get(en_col, "").strip()
        return en.endswith("?")

    for blk in blocks:
        if blk["kind"] == "p":
            txt = blk["text"]
            if txt in _SECTION_HEADINGS:
                flush_prose()
                if in_faq and pending_q is not None:
                    emit_faq_pair(pending_q, None)
                    pending_q = None
                in_faq = txt == "FAQ:"
                current_section = txt.rstrip(":")
                continue
            if txt in _TRACK_HEADERS:
                flush_prose()
                current_section = f"Programme Track: {txt}"
                buf.append(txt)
                continue
            buf.append(txt)
            # Flush when the accumulated prose gets long (~ corpus chunk size).
            if _token_estimate("\n".join(buf)) >= 320:
                flush_prose()
        else:  # table row
            cells = list(blk["cells"])
            non_empty = [c for c in cells if c]
            # Detect FAQ language header row: non-empty cells are EN/RU/KZ.
            if in_faq and {c.upper() for c in non_empty} & {"EN", "RU", "KZ"} and len(non_empty) >= 2:
                lang_for_col = {}
                for ci, h in enumerate(cells):
                    hu = h.strip().upper()
                    if hu in {"EN", "RU", "KZ"}:
                        lang_for_col[ci] = {"EN": "en", "RU": "ru", "KZ": "kk"}[hu]
                continue
            if in_faq and lang_for_col:
                row = {ci: cells[ci].strip() for ci in lang_for_col if ci < len(cells)}
                if is_question_row(row):
                    # New question; flush any unanswered previous question (divider).
                    if pending_q is not None:
                        emit_faq_pair(pending_q, None)
                    pending_q = row
                else:
                    # Answer (or category-divider) row.
                    if pending_q is not None:
                        emit_faq_pair(pending_q, row)
                        pending_q = None
                    else:
                        # Category divider (e.g. "Forum-Related Questions") — index alone.
                        emit_faq_pair(row, None)
            else:
                # Non-FAQ table (e.g. Thematic Tracks): join cells into one line.
                line = " — ".join(c for c in cells if c)
                if line.strip():
                    buf.append(line.strip())
                    if _token_estimate("\n".join(buf)) >= 320:
                        flush_prose()
    if in_faq and pending_q is not None:
        emit_faq_pair(pending_q, None)
        pending_q = None
    flush_prose()
    return chunks


def append_to_corpus(new_chunks: list[dict]) -> None:
    """Append new chunks to data/chunks/chunks.json without touching existing entries."""
    corpus = json.loads(RAG_CHUNKS_PATH.read_text(encoding="utf-8"))
    existing_ids = {c.get("chunk_id") for c in corpus}
    added = [c for c in new_chunks if c["chunk_id"] not in existing_ids]
    corpus.extend(added)
    RAG_CHUNKS_PATH.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    print(f"[afd] corpus chunks.json: appended {len(added)} (now {len(corpus)})", file=sys.stderr)


def append_to_faiss(new_chunks: list[dict]) -> None:
    import faiss

    texts = [c["text"] for c in new_chunks]
    vecs = np.asarray(embed_texts(texts), dtype=np.float32)
    if vecs.ndim != 2 or vecs.shape[1] != QDRANT_VECTOR_SIZE:
        raise RuntimeError(f"bad embedding shape {vecs.shape}")
    faiss.normalize_L2(vecs)

    index = faiss.read_index(str(FAISS_INDEX_PATH))
    before = index.ntotal
    index.add(vecs)

    metadata = json.loads(FAISS_METADATA_PATH.read_text(encoding="utf-8"))
    for c in new_chunks:
        metadata.append({
            "text": c["text"],
            "chunk_id": c["chunk_id"],
            "metadata": c["metadata"],
        })
    if len(metadata) != index.ntotal:
        raise RuntimeError(f"metadata rows {len(metadata)} != index ntotal {index.ntotal}")

    faiss.write_index(index, str(FAISS_INDEX_PATH))
    FAISS_METADATA_PATH.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    print(f"[afd] FAISS: {before} -> {index.ntotal} vectors (+{len(new_chunks)})", file=sys.stderr)
    return vecs


def upsert_qdrant(new_chunks: list[dict], vecs) -> None:
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import PointStruct
    except Exception as e:  # pragma: no cover
        print(f"[afd] Qdrant client unavailable, skipping Qdrant upsert: {e}", file=sys.stderr)
        return
    import os

    url = os.getenv("QDRANT_URL", "http://localhost:6333")
    collection = os.getenv("QDRANT_COLLECTION", "aifc_chunks")
    try:
        client = QdrantClient(url=url)
        info = client.get_collection(collection)
        next_id = info.points_count  # append after existing ids
        points = []
        for j, (c, v) in enumerate(zip(new_chunks, vecs)):
            meta = c["metadata"]
            payload_meta = {
                "source_file": meta.get("source_file", ""),
                "domain": meta.get("domain", ""),
                "doc_type": meta.get("doc_type", ""),
                "language": meta.get("language", ""),
                "section_title": meta.get("section_title", ""),
                "is_table": meta.get("is_table", False),
                "token_estimate": meta.get("token_estimate", 0),
            }
            points.append(PointStruct(
                id=int(next_id + j),
                vector=[float(x) for x in v],
                payload={
                    "text": c["text"],
                    "chunk_id": c["chunk_id"],
                    "metadata": payload_meta,
                    **payload_meta,
                },
            ))
        client.upsert(collection_name=collection, points=points)
        after = client.get_collection(collection).points_count
        client.close()
        print(f"[afd] Qdrant '{collection}': upserted {len(points)} (now {after})", file=sys.stderr)
    except Exception as e:
        print(f"[afd] Qdrant upsert failed (non-fatal, FAISS is the live backend): {e}", file=sys.stderr)


def main() -> None:
    if not DOCX_PATH.exists():
        raise SystemExit(f"docx not found: {DOCX_PATH}")
    chunks = build_chunks()
    print(f"[afd] built {len(chunks)} chunks from docx", file=sys.stderr)
    AFD_CHUNKS_FILE.write_text(json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[afd] wrote {AFD_CHUNKS_FILE}", file=sys.stderr)

    append_to_corpus(chunks)
    vecs = append_to_faiss(chunks)
    upsert_qdrant(chunks, vecs)
    print("[afd] DONE", file=sys.stderr)


if __name__ == "__main__":
    main()
