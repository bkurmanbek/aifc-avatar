"""
scripts/chunk.py — AIFC voice agent RAG chunker.

Strategies per document type:
  faq        → one chunk per Q&A pair (## N. Question pattern)
  table_faq  → one chunk per table row (Category | Question | Answer)
  policy     → PART / numbered-section boundaries; merge tiny, split large
  report     → ## heading sections; tables as separate chunks; overflow by paragraph
  guidebook  → # SERVICE TYPE → numbered service → sub-section / fee table
  web        → URL-page block → [H1]/[H2] heading → paragraph
  single     → entire document as one chunk

Output:
    data/chunks/chunks.json          — all chunks
    data/chunks/chunks_{domain}.json — per-domain splits

Usage (from project root):
  python scripts/chunk.py              # chunk all files
  python scripts/chunk.py --stats      # print stats only (no write)
  python scripts/chunk.py --file <md>  # chunk a single markdown file
"""

import re
import json
import argparse
import os
import unicodedata
from pathlib import Path
from typing import Any

# ── Paths ─────────────────────────────────────────────────────────────────────

# Project root is 1 level up from scripts/
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR     = Path(os.getenv("AIFC_DATA_DIR", os.getenv("DATA_DIR", str(PROJECT_ROOT.parent / "data")))).expanduser().resolve()

MARKDOWN_DIR = DATA_DIR / "parsed"
OUTPUT_DIR   = DATA_DIR / "chunks"

# ── Token helpers ─────────────────────────────────────────────────────────────

def tok(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def split_paragraphs(text: str, max_tok: int, overlap_tok: int = 80) -> list[str]:
    """
    Split text into chunks at paragraph → line → sentence boundaries,
    respecting max_tok.  Adds a small overlap to each new chunk.
    """
    # Try paragraph splits first; fall back to line splits for dense text
    paras = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    if len(paras) == 1 and tok(text) > max_tok:
        # No paragraph breaks — split on single newlines
        paras = [l.strip() for l in text.splitlines() if l.strip()]
    if len(paras) == 1 and tok(text) > max_tok:
        # Still one block — split on sentence boundaries
        paras = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]

    chunks, current, overlap_buf = [], [], ""

    for para in paras:
        # If single para is still too big, hard-split by character
        if tok(para) > max_tok:
            # flush current buffer first
            if current:
                chunks.append((overlap_buf + "\n\n" + "\n\n".join(current)).strip())
                current = []
                overlap_buf = ""
            step = max_tok * 4
            for i in range(0, len(para), step):
                chunks.append(para[i: i + step])
            continue

        candidate = (overlap_buf + "\n\n" + "\n\n".join(current + [para])).strip()
        if tok(candidate) <= max_tok:
            current.append(para)
        else:
            if current:
                chunk_text = (overlap_buf + "\n\n" + "\n\n".join(current)).strip()
                chunks.append(chunk_text)
                # Build overlap from end of current chunk
                end_text  = "\n\n".join(current)
                sentences = re.split(r'(?<=[.!?])\s+', end_text)
                buf = ""
                for s in reversed(sentences):
                    if tok(buf + s) <= overlap_tok:
                        buf = s + " " + buf
                    else:
                        break
                overlap_buf = buf.strip()
            current = [para]

    if current:
        chunks.append((overlap_buf + "\n\n" + "\n\n".join(current)).strip())

    return [c for c in chunks if c] or [text]


# ── File routing ──────────────────────────────────────────────────────────────

FILE_CONFIG: dict[str, dict] = {
    # carbon-platform ─────────────────────────────────────────────────────────
    "ETS Report eng.md":  {"domain": "carbon-platform", "doc_type": "report",   "language": "en", "strategy": "report"},
    "ETS Rus.md":         {"domain": "carbon-platform", "doc_type": "report",   "language": "ru", "strategy": "report"},
    "VCM Eng.md":         {"domain": "carbon-platform", "doc_type": "report",   "language": "en", "strategy": "report"},
    "VCM Ru.md":          {"domain": "carbon-platform", "doc_type": "report",   "language": "ru", "strategy": "report"},
    "FAQ \u0434\u043b\u044f \u0441\u0430\u0439\u0442\u0430 - \u0447\u0438\u0441\u0442\u0430\u044f - \u0430\u043d\u0433\u043b-\u0440\u0443\u0441.md": {"domain": "carbon-platform", "doc_type": "faq",      "language": "en", "strategy": "faq"},
    "FAQ_for_website_Carbon_Platform_KZ_checked.md":                                                                                               {"domain": "carbon-platform", "doc_type": "faq",      "language": "kk", "strategy": "faq"},
    "\u0414\u043b\u044f \u0421\u0430\u0439\u0442\u0430 updated.md":                                                                               {"domain": "carbon-platform", "doc_type": "overview", "language": "ru", "strategy": "single"},
    # hr-policy ───────────────────────────────────────────────────────────────
    "1 - AIFC Ethics Code 2025.md":                               {"domain": "hr-policy", "doc_type": "policy", "language": "en", "strategy": "policy"},
    "2 - AIFCA Speak Up Policy 2021.md":                          {"domain": "hr-policy", "doc_type": "policy", "language": "en", "strategy": "policy"},
    "3 - AIFCA Internal Employment Policy and Procedures 2024.md":{"domain": "hr-policy", "doc_type": "policy", "language": "en", "strategy": "policy"},
    "4 - AIFCA Business trips policy 2025.md":                    {"domain": "hr-policy", "doc_type": "policy", "language": "en", "strategy": "policy"},
    "5 - AIFCA Performance Management Policy 2025.md":            {"domain": "hr-policy", "doc_type": "policy", "language": "en", "strategy": "policy"},
    "6 - AIFC Training and Development Policy and Procedures.md": {"domain": "hr-policy", "doc_type": "policy", "language": "en", "strategy": "policy"},
    # expat-centre ────────────────────────────────────────────────────────────
    "Guidebook AIFC Participants 2026 Expat Centre services (Astana).md": {"domain": "expat-centre", "doc_type": "guidebook", "language": "en", "strategy": "guidebook"},
    "Guidebook Non-AIFC 2026 Expat Centre services (Astana).md":          {"domain": "expat-centre", "doc_type": "guidebook", "language": "en", "strategy": "guidebook"},
    "Guidebook AIFC Participants 2026 Expat Centre services (Almaty).md": {"domain": "expat-centre", "doc_type": "guidebook", "language": "en", "strategy": "guidebook"},
    "Expat Centre_Front office AI_03022026.md":                           {"domain": "expat-centre", "doc_type": "faq",      "language": "en", "strategy": "table_faq"},
    # policy ──────────────────────────────────────────────────────────────────
    "New Version_4_Contract Policy_clean.md":  {"domain": "policy", "doc_type": "policy", "language": "en", "strategy": "policy"},
    "IP-PC-01 Policy Framework.md":            {"domain": "policy", "doc_type": "policy", "language": "en", "strategy": "policy"},
    "150125_Guidelines on the procedure for issuing, registering and monitoring the use of PoAs of AIFCA.md":
                                               {"domain": "policy", "doc_type": "policy", "language": "en", "strategy": "policy"},
    "Confidentiality Policy (4).md":           {"domain": "policy", "doc_type": "policy", "language": "en", "strategy": "policy"},
    # public-website ──────────────────────────────────────────────────────────
    "web_content.md": {"domain": "public-website", "doc_type": "web", "language": "en", "strategy": "web"},
    # afsa-web (AFSA – Astana Financial Services Authority, scraped) ───────────
    "afsa-web/afsa_raw_data/data.md": {"domain": "afsa-web", "doc_type": "web", "language": "en", "strategy": "web", "source_file": "afsa-web.md"},
    # aix-web (AIX – Astana International Exchange, scraped) ──────────────────
    "aix-web/aix_raw_data/data.md":   {"domain": "aix-web",  "doc_type": "web", "language": "en", "strategy": "web", "source_file": "aix-web.md"},
    # canonical ingestion corpus ───────────────────────────────────────────────
    "site_corpus.md":                {"domain": "site-corpus", "doc_type": "web", "language": "multi", "strategy": "web", "source_file": "site-corpus.md"},
}

MAX_TOKENS = {
    "faq":       400,
    "table_faq": 400,
    "policy":    600,
    "report":    800,
    "guidebook": 600,
    "web":       500,
    "single":    1200,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_toc_line(line: str) -> bool:
    """Lines like 'PART 1: INTRODUCTION ................. 3' are TOC entries."""
    return bool(re.search(r'\.{4,}\s*\d+\s*$', line))


_ONLY_NUMBERS_RE = re.compile(r'^[\d\s.\n]+$')


def is_toc_block(lines: list[str]) -> bool:
    """Returns True if the majority of non-empty lines are TOC entries."""
    non_empty = [l for l in lines if l.strip()]
    if len(non_empty) < 3:
        return False
    return sum(1 for l in non_empty if is_toc_line(l)) / len(non_empty) > 0.4


def is_junk_chunk(text: str) -> bool:
    """True for chunks that are purely section-number lists with no real content."""
    if tok(text) > 50:
        return False
    # Mostly just decimal numbers and whitespace (e.g. TOC number run-ons)
    stripped = re.sub(r'\b(PART|Schedule)\s+\d+[.:]?\s*', '', text)
    if _ONLY_NUMBERS_RE.match(stripped.strip()):
        return True
    # Numbered-list TOC pages: majority of lines are just "N." or "N.\n"
    non_empty_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(non_empty_lines) >= 3:
        num_lines = sum(1 for l in non_empty_lines if re.match(r'^\d+\.?$', l))
        if num_lines / len(non_empty_lines) > 0.5:
            return True
    # Very short with no real words (< 3 alphabetic tokens)
    words = re.findall(r'[a-zA-Zа-яА-Я]{3,}', text)
    return len(words) < 3


def clean_heading(text: str) -> str:
    """Strip # markers and trailing dots/whitespace from heading text."""
    text = re.sub(r'^#+\s*', '', text).strip()
    text = re.sub(r'\.{3,}.*$', '', text).strip()
    return text


def clean_chunk_text(text: str) -> str:
    """Remove stray page-number lines and PDF running-header artefacts."""
    text = text.strip()
    text = text.replace("\ufffd", ".")
    # Strip a lone integer on the very first line (PDF page artefact)
    text = re.sub(r'^\d{1,3}\n', '', text)
    text = re.sub(r'(?is)<(?:form|script|style|input|div|a)\b[^>]*>.*?</(?:form|script|style|div|a)>', ' ', text)
    text = re.sub(r'(?is)<(?:form|script|style|input|div|a)\b[^>]*>', ' ', text)
    text = re.sub(r'(?m)^.*?/https?:/.*$', '', text)
    text = re.sub(r'(?m)^.*\bDownload\b.*\|\s*\bDownload\b.*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'(?m)^.*(?:\(data\).*){2,}$', '', text)
    text = re.sub(r'\s*\(data\)\s*', ' ', text)
    # Strip standalone all-caps running header lines (PDF page headers).
    # A running header is a line of 8-60 all-uppercase letters/spaces only.
    text = re.sub(r'(?m)^[A-Z][A-Z\s]{7,59}$', '', text)
    # Strip header+page-number prefix at start of line
    # e.g. "AIFC ETHICS CODE 5 or only if..." → "or only if..."
    text = re.sub(r'(?m)^[A-Z][A-Z\s]{7,59}\s+\d+\s+', '', text)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if is_low_value_line(stripped):
            continue
        lines.append(stripped)
    return re.sub(r'\n{3,}', '\n\n', "\n".join(lines)).strip()


def useful_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі]{3,}", text or ""))


def is_low_value_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    key = re.sub(r"\s+", " ", stripped).casefold()
    if key in {
        "home",
        ".breadcrumbs",
        "download",
        "read more",
        "learn more",
        "apply now",
        "calculate fee",
        "book an appointment",
    }:
        return True
    if stripped.startswith((
        "SITE:",
        "SITE_GROUP:",
        "LANGUAGE:",
        "SOURCE_TYPE:",
        "BREADCRUMB:",
        "HEADINGS:",
        "ATTACHMENTS:",
        "DOWNLOADED_FILES:",
        "CONTENT_HASH:",
        "CRAWLED_AT:",
    )):
        return True
    if stripped.startswith("<") and re.search(r"\b(form|script|style|input|div|a)\b", stripped, re.I):
        return True
    if re.fullmatch(r"[-_=]{5,}", stripped):
        return True
    if re.search(r"\.{4,}", stripped) and useful_word_count(stripped) < 12:
        return True
    if stripped.count("(data)") >= 2:
        return True
    if stripped.count("(data)") >= 1 and useful_word_count(stripped) <= 6:
        return True
    if stripped.lower().count("download") >= 3:
        return True
    if "/http:/" in stripped or "/https:/" in stripped:
        return True
    return False


def _source_slug(source_file: str) -> str:
    """Short filesystem-safe slug from source filename, guaranteed unique via hash suffix."""
    import hashlib
    stem = re.sub(r'\.md$', '', source_file)
    slug = re.sub(r'[^a-zA-Z0-9а-яА-ЯёЁ]+', '-', stem).strip('-')
    h4  = hashlib.sha1(source_file.encode()).hexdigest()[:4]
    return f"{slug[:35]}-{h4}"


def make_chunk(text: str, cfg: dict, section_title: str,
               idx: int, is_table: bool = False) -> dict:
    """Build one chunk record with metadata for downstream retrieval."""
    text = clean_chunk_text(text)
    slug = _source_slug(cfg["source_file"])
    return {
        "chunk_id":  f"{cfg['domain']}-{slug}-{idx:05d}",
        "text":      text,
        "metadata": {
            "source_file":   cfg["source_file"],
            "domain":        cfg["domain"],
            "doc_type":      cfg["doc_type"],
            "language":      cfg["language"],
            "section_title": section_title[:120],
            "chunk_index":   idx,
            "is_table":      is_table,
            "token_estimate": tok(text),
        },
    }


# ── Strategy: FAQ ─────────────────────────────────────────────────────────────

def chunk_faq(text: str, cfg: dict) -> list[dict]:
    """Split on '## N. Question' headings — one chunk per Q&A pair."""
    max_tok = MAX_TOKENS["faq"]
    # Split at every ## N. heading
    sections = re.split(r'(?m)(?=^##\s+\d+[\.\)])', text)
    chunks, idx = [], 0

    for sec in sections:
        sec = sec.strip()
        if not sec or tok(sec) < 20:
            continue
        # Extract heading line as section title
        lines = sec.splitlines()
        title = clean_heading(lines[0]) if lines else "Q&A"
        # If answer is very long, split by paragraph
        if tok(sec) > max_tok:
            for part in split_paragraphs(sec, max_tok):
                chunks.append(make_chunk(part, cfg, title, idx))
                idx += 1
        else:
            chunks.append(make_chunk(sec, cfg, title, idx))
            idx += 1

    return chunks


# ── Strategy: Table FAQ ───────────────────────────────────────────────────────

def chunk_table_faq(text: str, cfg: dict) -> list[dict]:
    """Extract each markdown table row as Category | Question | Answer chunk."""
    chunks, idx = [], 0
    header = None
    category_carry = ""   # forward-fill empty category cells

    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        if re.match(r'^\|\s*[-:]+', line):   # separator row
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # Pad to 3 cols
        while len(cells) < 3:
            cells.append("")
        category, question, answer = cells[0], cells[1], cells[2]

        if not header:
            header = cells
            continue

        # Skip if question or answer empty
        if not question or not answer:
            continue

        # Forward-fill category
        if category:
            category_carry = category

        chunk_text = (
            f"Category: {category_carry}\n"
            f"Question: {question}\n"
            f"Answer: {answer}"
        )
        chunks.append(make_chunk(chunk_text, cfg, question[:100], idx))
        idx += 1

    return chunks


# ── Strategy: Policy ─────────────────────────────────────────────────────────

# Matches section starters in body text:
#   "PART 1: INTRODUCTION", "PART 2. ...", "1.1.", "3.2.3.", "10.", "Schedule 1."
_SECTION_RE = re.compile(
    r'^(?:'
    r'PART\s+\d+[.:]\s+'          # PART 1: / PART 2.
    r'|Schedule\s+\d+'            # Schedule 1
    r'|\d+(?:\.\d+)+\.?\s+'       # 1.1. / 3.2.3.
    r'|\d+\.\s+[A-Z]'             # 10. Ethics / 5. Scope
    r')',
    re.MULTILINE,
)


def _policy_sections(text: str) -> list[tuple[str, str]]:
    """
    Return list of (section_title, section_body) tuples.
    Splits on PART headers, numbered sections, and # headings.
    Skips TOC blocks.
    """
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []   # (title, body_lines)
    current_title = "Introduction"
    current_body: list[str] = []

    def flush():
        body = "\n".join(current_body).strip()
        if body and not is_toc_block(current_body):
            sections.append((current_title, body))

    for line in lines:
        stripped = line.strip()

        # Skip pure TOC lines anywhere
        if is_toc_line(stripped):
            continue

        # Markdown heading → new section
        if line.startswith("#"):
            heading_text = clean_heading(line)
            if heading_text and len(heading_text) > 2:
                flush()
                current_title = heading_text
                current_body = []
            continue

        # Body-level section marker
        m = _SECTION_RE.match(stripped)
        if m and len(stripped) > len(m.group(0)):
            flush()
            current_title = stripped[:120]
            current_body = [line]
            continue

        current_body.append(line)

    flush()
    return sections


def chunk_policy(text: str, cfg: dict) -> list[dict]:
    """Chunk policy documents by section boundaries and size limits."""
    max_tok = MAX_TOKENS["policy"]
    sections = _policy_sections(text)
    chunks, idx = [], 0
    pending_title = ""
    pending_body  = ""

    def emit(title: str, body: str):
        nonlocal idx
        # Pre-clean the body (removes running headers) before junk check
        body = clean_chunk_text(body)
        if not body or is_junk_chunk(body):
            return
        if tok(body) > max_tok:
            for part in split_paragraphs(body, max_tok):
                if not is_junk_chunk(part):
                    chunks.append(make_chunk(part, cfg, title, idx))
                    idx += 1
        else:
            chunks.append(make_chunk(body, cfg, title, idx))
            idx += 1

    for title, body in sections:
        # Merge tiny sections into previous pending body
        combined = (pending_body + "\n\n" + body).strip() if pending_body else body
        if tok(combined) < 80 and pending_title:
            pending_body = combined
            continue

        if pending_body:
            emit(pending_title, pending_body)

        pending_title = title
        pending_body  = body

    if pending_body:
        emit(pending_title, pending_body)

    return chunks


# ── Strategy: Report ─────────────────────────────────────────────────────────

def chunk_report(text: str, cfg: dict) -> list[dict]:
    """
    Split at ## headings.  Tables become separate chunks.
    Long sections split by paragraph with overlap.
    """
    max_tok = MAX_TOKENS["report"]
    # Split at every # heading
    raw_sections = re.split(r'(?m)(?=^#{1,3}\s)', text)
    chunks, idx = [], 0

    for sec in raw_sections:
        sec = sec.strip()
        if not sec or tok(sec) < 20:
            continue

        lines = sec.splitlines()
        title = clean_heading(lines[0]) if lines[0].startswith("#") else "Section"

        # Skip Table of Contents sections entirely
        if re.search(r'table\s+of\s+contents', title, re.IGNORECASE):
            continue
        # Skip if the whole section body is mostly TOC lines
        if is_toc_block(lines[1:]):
            continue

        # Separate table blocks from prose
        table_buf: list[str] = []
        prose_buf: list[str] = []
        in_table = False

        for line in lines[1:]:
            if line.startswith("|"):
                if not in_table and prose_buf:
                    # Flush prose accumulated so far
                    prose_text = "\n".join(prose_buf).strip()
                    if tok(prose_text) > 20:
                        if tok(prose_text) > max_tok:
                            for part in split_paragraphs(prose_text, max_tok):
                                chunks.append(make_chunk(part, cfg, title, idx))
                                idx += 1
                        else:
                            chunks.append(make_chunk(prose_text, cfg, title, idx))
                            idx += 1
                    prose_buf = []
                in_table = True
                table_buf.append(line)
            else:
                if in_table:
                    # Flush table
                    table_text = f"**{title}**\n\n" + "\n".join(table_buf)
                    chunks.append(make_chunk(table_text, cfg, title, idx, is_table=True))
                    idx += 1
                    table_buf = []
                    in_table  = False
                prose_buf.append(line)

        # Flush remaining table
        if table_buf:
            table_text = f"**{title}**\n\n" + "\n".join(table_buf)
            chunks.append(make_chunk(table_text, cfg, title, idx, is_table=True))
            idx += 1

        # Flush remaining prose
        prose_text = "\n".join(prose_buf).strip()
        if tok(prose_text) > 20:
            if tok(prose_text) > max_tok:
                for part in split_paragraphs(prose_text, max_tok):
                    chunks.append(make_chunk(part, cfg, title, idx))
                    idx += 1
            else:
                chunks.append(make_chunk(prose_text, cfg, title, idx))
                idx += 1

    return chunks


# ── Strategy: Guidebook ───────────────────────────────────────────────────────

# Service-type heading: # ALL CAPS (e.g., "# EMPLOYMENT VISA")
_SERVICE_RE = re.compile(r'^#\s+([A-Z][A-Z\s/&–-]{4,})$')

# Sub-section markers within a service block
_SUBSEC_RE = re.compile(
    r'^(?:'
    r'\d+\.\s+.{10,}'
    r'|Step\s+\d+'
    r'|Application Process'
    r'|Required documents?'
    r'|Note[:\s]'
    r'|CONTACT'
    r'|SERVICE\s+TYPE'
    r')',
    re.IGNORECASE,
)


def _guidebook_subsections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Split a service block into labelled sub-sections."""
    subs: list[tuple[str, list[str]]] = []
    cur_title = "Overview"
    cur_body:  list[str] = []

    for line in lines:
        stripped = line.strip()
        # New numbered service item
        m = re.match(r'^(\d+)\.\s+(.+)', stripped)
        if m:
            if cur_body:
                subs.append((cur_title, cur_body))
            cur_title = stripped[:120]
            cur_body  = [line]
            continue
        # Subsection keyword markers
        if _SUBSEC_RE.match(stripped) and len(stripped) > 5:
            if cur_body:
                subs.append((cur_title, cur_body))
            cur_title = stripped[:80]
            cur_body  = [line]
            continue
        cur_body.append(line)

    if cur_body:
        subs.append((cur_title, cur_body))
    return subs


def chunk_guidebook(text: str, cfg: dict) -> list[dict]:
    """Chunk guidebooks around service sections, tables, and fee blocks."""
    max_tok = MAX_TOKENS["guidebook"]
    lines   = text.splitlines()
    chunks, idx = [], 0

    # Identify service-type boundaries via "# ALL CAPS" headings
    service_starts = [
        i for i, l in enumerate(lines) if _SERVICE_RE.match(l.strip())
    ]

    if not service_starts:
        # Fallback: treat as policy
        return chunk_policy(text, cfg)

    service_starts.append(len(lines))   # sentinel

    for si in range(len(service_starts) - 1):
        s_start = service_starts[si]
        s_end   = service_starts[si + 1]
        service_name = clean_heading(lines[s_start])
        service_lines = lines[s_start + 1: s_end]

        # Split service block into sub-sections
        subsecs = _guidebook_subsections(service_lines)

        for sub_title, sub_lines in subsecs:
            sub_text = "\n".join(sub_lines).strip()
            if not sub_text or tok(sub_text) < 20:
                continue

            full_title = f"{service_name} – {sub_title}"

            # Tables in sub-sections become separate chunks
            table_buf: list[str] = []
            prose_buf: list[str] = []
            in_table = False

            for line in sub_lines:
                if line.startswith("|"):
                    if not in_table and prose_buf:
                        prose = "\n".join(prose_buf).strip()
                        if tok(prose) > 20:
                            for part in split_paragraphs(prose, max_tok):
                                chunks.append(make_chunk(part, cfg, full_title, idx))
                                idx += 1
                        prose_buf = []
                    in_table = True
                    table_buf.append(line)
                else:
                    if in_table:
                        table_text = f"**{full_title}**\n\n" + "\n".join(table_buf)
                        chunks.append(make_chunk(table_text, cfg, full_title, idx, is_table=True))
                        idx += 1
                        table_buf = []
                        in_table  = False
                    prose_buf.append(line)

            if table_buf:
                table_text = f"**{full_title}**\n\n" + "\n".join(table_buf)
                chunks.append(make_chunk(table_text, cfg, full_title, idx, is_table=True))
                idx += 1

            prose = "\n".join(prose_buf).strip()
            if tok(prose) > 20:
                for part in split_paragraphs(prose, max_tok):
                    chunks.append(make_chunk(part, cfg, full_title, idx))
                    idx += 1

    return chunks


# ── Strategy: Web ─────────────────────────────────────────────────────────────

_PAGE_SEP_RE = re.compile(r'^(?:##\s*)?={10,}')   # matches both "## ===" and plain "==="
_HEADING_RE  = re.compile(r'^\[(H[1-6])\]\s*(.*)')

# Short / noisy blocks to filter out
_NOISE_RE = re.compile(
    r'^(?:\[IMAGE:|PDF LINK:|Home\b|Filter\b|SITE:|SITE_GROUP:|LANGUAGE:|SOURCE_TYPE:|SCRAPED_AT:|CRAWLED_AT:|\d+\s*$)',
    re.IGNORECASE,
)


def _parse_web_page(block: str) -> dict[str, Any] | None:
    """Extract metadata and content from a scraped page block."""
    meta = {"url": "", "title": "", "section": ""}
    content_lines = []
    in_meta = False

    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("URL:"):
            meta["url"]     = stripped[4:].strip()
            in_meta = True
        elif stripped.startswith("TITLE:"):
            meta["title"]   = stripped[6:].strip()
        elif stripped.startswith("SECTION:"):
            meta["section"] = stripped[8:].strip()
        elif _PAGE_SEP_RE.match(stripped) or stripped.startswith(("SCRAPED_AT", "SITE:", "LANGUAGE:", "SITE_GROUP:", "SOURCE_TYPE:", "CRAWLED_AT:")):
            continue
        elif in_meta and stripped:
            content_lines.append(line)

    return {**meta, "lines": content_lines} if meta["url"] else None


def chunk_web(text: str, cfg: dict) -> list[dict]:
    """
    Parse web_content.md line-by-line.
    Page structure:
      ## ===...  (separator)
      URL: ...
      TITLE: ...
      SECTION: ...
      ## SCRAPED_AT: ...
      ## ===...  (separator)
      [content lines]
      ## ===...  (separator, start of next page)
    """
    max_tok  = MAX_TOKENS["web"]
    chunks, idx = [], 0
    lines = text.splitlines()

    # State
    url = title = section = ""
    cur_heading = ""
    cur_lines: list[str] = []
    in_page = False

    def flush_section(heading: str, body_lines: list[str]):
        nonlocal idx
        # Filter noise lines
        clean = [
            l for l in body_lines
            if l.strip()
            and not _NOISE_RE.match(l.strip())
            and not is_low_value_line(l.strip())
            and len(l.strip()) > 20
        ]
        body = "\n".join(clean).strip()
        if tok(body) < 30:
            return
        page_ctx  = f"URL: {url}\nTitle: {title}\nSection: {section}"
        full_text = f"{page_ctx}\n\n## {heading}\n\n{body}"
        ctx_tok   = tok(page_ctx) + 20
        if tok(full_text) > max_tok:
            for part in split_paragraphs(body, max_tok - ctx_tok):
                ct = f"{page_ctx}\n\n## {heading}\n\n{part}"
                chunks.append(make_chunk(ct, cfg, heading[:100], idx))
                idx += 1
        else:
            chunks.append(make_chunk(full_text, cfg, heading[:100], idx))
            idx += 1

    for line in lines:
        stripped = line.strip()

        # Separator line
        if _PAGE_SEP_RE.match(stripped):
            if in_page and cur_lines:
                flush_section(cur_heading or title or "Page", cur_lines)
                cur_lines = []
                cur_heading = title or "Page"
            continue

        # Metadata lines
        if stripped.startswith("URL:"):
            # Starting a new page — flush any pending section from previous page
            if in_page and cur_lines:
                flush_section(cur_heading or title or "Page", cur_lines)
            url         = stripped[4:].strip()
            title       = ""
            section     = ""
            cur_heading = ""
            cur_lines   = []
            in_page     = True
            continue
        if stripped.startswith("TITLE:"):
            title = stripped[6:].strip()
            cur_heading = title
            continue
        if stripped.startswith("SECTION:"):
            section = stripped[8:].strip()
            continue
        if stripped.startswith(("SITE:", "LANGUAGE:", "SITE_GROUP:", "SOURCE_TYPE:", "CRAWLED_AT:")):
            continue
        if stripped.startswith("SCRAPED_AT"):   # handles both "SCRAPED_AT:" and "## SCRAPED_AT:"
            continue

        if not in_page:
            continue

        # Heading lines → section boundary
        m = _HEADING_RE.match(stripped)
        if m:
            level, heading_text = m.group(1), m.group(2).strip()
            if level in ("H1", "H2") and heading_text and heading_text != cur_heading:
                if cur_lines:
                    flush_section(cur_heading or title or "Page", cur_lines)
                cur_heading = heading_text
                cur_lines   = []
            # Lower headings treated as text
            elif heading_text:
                cur_lines.append(heading_text)
        else:
            cur_lines.append(line)

    # Flush final section
    if in_page and cur_lines:
        flush_section(cur_heading or title or "Page", cur_lines)

    return chunks


# ── Strategy: Single ─────────────────────────────────────────────────────────

def chunk_single(text: str, cfg: dict) -> list[dict]:
    """Return the full document as a single chunk for short or flat content."""
    text = text.strip()
    if not text:
        return []
    return [make_chunk(text, cfg, cfg["source_file"], 0)]


# ── Dispatcher ────────────────────────────────────────────────────────────────

STRATEGY_FN = {
    "faq":       chunk_faq,
    "table_faq": chunk_table_faq,
    "policy":    chunk_policy,
    "report":    chunk_report,
    "guidebook": chunk_guidebook,
    "web":       chunk_web,
    "single":    chunk_single,
}


def chunk_file(md_path: Path) -> list[dict]:
    """Chunk one markdown file using the strategy defined in FILE_CONFIG."""
    # Normalise to NFC so Cyrillic filenames match dict keys on macOS (NFD fs)
    name = unicodedata.normalize("NFC", md_path.name)
    # Also try the relative path key (handles same-name files in sub-directories,
    # e.g. afsa-web/afsa_raw_data/data.md vs aix-web/aix_raw_data/data.md)
    try:
        rel_key = unicodedata.normalize("NFC", str(md_path.relative_to(MARKDOWN_DIR)))
    except ValueError:
        rel_key = name

    if rel_key in FILE_CONFIG:
        lookup_key = rel_key
    elif name in FILE_CONFIG:
        lookup_key = name
    else:
        print(f"  [SKIP] {name} — not in FILE_CONFIG")
        return []

    entry = FILE_CONFIG[lookup_key]
    # Allow FILE_CONFIG entries to override source_file (e.g. for pretty names)
    source_file = entry.get("source_file", name)
    cfg = {**entry, "source_file": source_file}
    text = md_path.read_text(encoding="utf-8")
    strategy = cfg["strategy"]
    fn = STRATEGY_FN.get(strategy)
    if not fn:
        print(f"  [WARN] Unknown strategy '{strategy}' for {name}")
        return []

    chunks = fn(text, cfg)
    # Drop any chunks that ended up empty after text cleaning
    chunks = [c for c in chunks if c["text"].strip()]
    print(f"  [{strategy:10}] {name[:55]:<55} → {len(chunks):4} chunks")
    return chunks


# ── Main ──────────────────────────────────────────────────────────────────────

def collect_md_files() -> list[Path]:
    """Collect all markdown files under the parsed tree."""
    files = sorted(MARKDOWN_DIR.rglob("*.md"))
    canonical_md = DATA_DIR / "site_corpus.md"
    if canonical_md.exists():
        files.append(canonical_md)
    return files


def main():
    """CLI entry point for chunking the current parsed markdown corpus."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--stats", action="store_true", help="Print stats only, no write")
    parser.add_argument("--file",  help="Chunk a single markdown file by name (stem or full path)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.file:
        p = Path(args.file)
        if not p.exists():
            # search by name
            matches = list(MARKDOWN_DIR.rglob(f"*{args.file}*"))
            p = matches[0] if matches else None
        if not p or not p.exists():
            print(f"File not found: {args.file}")
            return
        files = [p]
    else:
        files = collect_md_files()

    print(f"\nChunking {len(files)} markdown files...\n")
    all_chunks: list[dict] = []
    by_domain:  dict[str, list[dict]] = {}

    for md_path in files:
        chunks = chunk_file(md_path)
        all_chunks.extend(chunks)
        for c in chunks:
            d = c["metadata"]["domain"]
            by_domain.setdefault(d, []).append(c)

    # ── Stats ──────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"{'Domain':<22} {'Chunks':>7} {'Avg tok':>8} {'Min':>5} {'Max':>6}")
    print(f"{'─'*60}")
    for domain, dc in sorted(by_domain.items()):
        toks = [c["metadata"]["token_estimate"] for c in dc]
        print(f"{domain:<22} {len(dc):>7} {sum(toks)//len(toks):>8} {min(toks):>5} {max(toks):>6}")
    print(f"{'─'*60}")
    toks_all = [c["metadata"]["token_estimate"] for c in all_chunks]
    print(f"{'TOTAL':<22} {len(all_chunks):>7} {sum(toks_all)//max(1,len(toks_all)):>8} {min(toks_all):>5} {max(toks_all):>6}")
    print(f"{'─'*60}")

    if args.stats:
        return

    # ── Write ──────────────────────────────────────────────────────────────
    out = OUTPUT_DIR / "chunks.json"
    out.write_text(json.dumps(all_chunks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ Saved {len(all_chunks)} chunks → {out}")
    root_out = PROJECT_ROOT.parent / "chunks.json"
    root_out.write_text(json.dumps(all_chunks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ mirrored canonical chunks → {root_out}")

    for domain, dc in by_domain.items():
        domain_out = OUTPUT_DIR / f"chunks_{domain}.json"
        domain_out.write_text(json.dumps(dc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✓ {domain_out.name}  ({len(dc)} chunks)")

    print("\n✅ Done.")


if __name__ == "__main__":
    main()
