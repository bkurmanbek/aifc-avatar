#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover - dependency is environment-specific
    PdfReader = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv("AIFC_DATA_DIR", os.getenv("DATA_DIR", str(ROOT.parent / "data")))).expanduser().resolve()
RAW = DATA / "raw"
PARSED = DATA / "parsed"
PREVIEW = DATA / "cleaned_preview"

BLOCK_SEP = "=" * 80
KEEP_QUERY_KEYS = {"id", "p", "page", "search", "s"}

NAV_EXACT = {
    "home",
    "about",
    "contacts",
    "contact",
    "apply now",
    "calculate fee",
    "general enquiries",
    "visit the aifc authority website",
    "visit the afsa website",
    "visit the aifc court website",
    "visit the iac website",
    "visit the aix website",
    "privacy policy",
    "procurement",
    "careers",
    "management",
    "news",
    "alerts",
    "annual reports",
    "history management ecosystem annual reports",
    "rankings recognition membership",
    "book an appointment",
    "join the association",
}

NAV_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"^\.breadcrumbs$",
        r"^filter\b",
        r"^share\b",
        r"^print\b",
        r"^subscribe\b",
        r"^stay current\b",
        r"^by submitting this form\b",
        r"^submit a request\b",
        r"^not sure which course suits you best\b",
        r"^our partners:?$",
        r"^trusted by:?$",
        r"^we will answer all questions:?$",
        r"^read more$",
        r"^learn more$",
        r"^download$",
        r"^previous$",
        r"^next$",
        r"^all rights reserved",
        r"^cookie",
    ]
]

META_KEYS = {
    "SITE",
    "LANGUAGE",
    "SOURCE_TYPE",
    "URL",
    "TITLE",
    "SECTION",
    "BREADCRUMB",
    "HEADINGS",
    "CRAWLED_AT",
    "SCRAPED_AT",
    "CONTENT_HASH",
}


@dataclass
class PageRecord:
    site: str = ""
    language: str = ""
    source_type: str = ""
    url: str = ""
    title: str = ""
    section: str = ""
    scraped_at: str = ""
    content_hash: str = ""
    body: str = ""


def flatten_node_text(node: object) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(part for item in node if (part := flatten_node_text(item)))
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("text"), str):
        return node["text"]
    if "items" in node:
        return flatten_node_text(node["items"])
    if "data" in node:
        return flatten_node_text(node["data"])
    return ""


def render_table(data: object) -> list[str]:
    lines: list[str] = []
    if not isinstance(data, list):
        return lines
    for row in data:
        if not isinstance(row, list):
            continue
        cells = [
            re.sub(r"\s+", " ", flatten_node_text(cell)).strip()
            for cell in row
        ]
        cells = [
            cell
            for cell in cells
            if cell and line_key(cell) not in {"download", "data", "(data)", "form available"}
        ]
        if len(cells) < 2 and useful_word_count(" ".join(cells)) < 4:
            continue
        lines.append(" | ".join(cells))
    return lines


def render_json_content(nodes: object) -> str:
    if not isinstance(nodes, list):
        return ""
    lines: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type == "header":
            tag = str(node.get("tag", "h2")).lower()
            level = tag[1:] if re.fullmatch(r"h[1-6]", tag) else "2"
            text = flatten_node_text(node).strip()
            if text:
                lines.append(f"[H{level}] {text}")
            continue
        if node_type == "text":
            text = flatten_node_text(node).strip()
            if text:
                lines.append(text)
            continue
        if node_type == "list":
            for item in node.get("items", []):
                text = flatten_node_text(item).strip()
                if text:
                    lines.append(f"- {text}")
            continue
        if node_type == "file_attachment":
            text = flatten_node_text(node).strip()
            url = str(node.get("url", "")).strip()
            if text and line_key(text) not in {"download", "pdf", "image"}:
                lines.append(f"[PDF: {text} | {url}]")
            continue
        if node_type == "table":
            table_lines = render_table(node.get("data"))
            if table_lines:
                lines.extend(table_lines)
            continue
        if node_type == "faq":
            question = str(node.get("question", "")).strip()
            answer = str(node.get("answer", "")).strip()
            if question and answer:
                lines.extend([f"Q: {question}", f"A: {answer}"])
            continue
    return "\n".join(lines)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\ufb01", "fi").replace("\ufb02", "fl")
    text = text.replace("\ufffd", ".")
    text = text.replace("\u00a0", " ")
    text = text.replace("•", "-").replace("●", "-").replace("▪", "-")
    text = re.sub(r"(?m)^\s*o\s+", "- ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in {"http", "https"} else parts.scheme
    netloc = parts.netloc.lower()
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k in KEEP_QUERY_KEYS
    ]
    return urlunsplit((scheme, netloc, path, urlencode(query_pairs), ""))


def site_label(record: PageRecord) -> str:
    if record.site.strip():
        return record.site.strip()
    host = urlsplit(record.url).netloc.lower().removeprefix("www.")
    if not host:
        return "general"
    if host.startswith("afsa."):
        return "afsa"
    if "aix" in host:
        return "aix"
    return host.split(".", 1)[0] or "general"


def is_bad_url(url: str) -> bool:
    if not url:
        return True
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return True
    return bool(re.search(r"/https?:/", parts.path))


def line_key(line: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^\s*[-*]\s+", "", line.strip())).casefold()


def strip_placeholder_cells(line: str) -> str:
    """Remove scraper placeholder table cells while keeping useful row text."""
    if "(data)" not in line:
        return line
    if "|" in line:
        cells = [
            re.sub(r"\s*\(data\)\s*", " ", cell).strip()
            for cell in line.split("|")
        ]
        cells = [
            cell
            for cell in cells
            if cell and line_key(cell) not in {"data", "(data)"}
        ]
        return " | ".join(cells).strip()
    return re.sub(r"\s*\(data\)\s*", " ", line).strip()


def is_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    key = line_key(stripped)
    if key in NAV_EXACT:
        return True
    if any(pattern.search(stripped) for pattern in NAV_PATTERNS):
        return True
    if re.search(r"\.{4,}", stripped) and useful_word_count(stripped) < 12:
        return True
    if re.fullmatch(r"[-_=]{5,}", stripped):
        return True
    if re.fullmatch(r"\d{1,4}", stripped):
        return True
    if re.fullmatch(r"\[\s*image:.*\]", stripped, re.I):
        return True
    if stripped.startswith("[IMAGE:"):
        return True
    if stripped.startswith("<") and re.search(r"\b(form|input|div|script|style)\b", stripped, re.I):
        return True
    if stripped.startswith("ATTACHMENTS:") or stripped.startswith("DOWNLOADED_FILES:"):
        return True
    if stripped.count("(data)") >= 2 or stripped.lower().count("download") >= 3:
        return True
    if stripped.count("(data)") >= 1 and useful_word_count(stripped) <= 6:
        return True
    return False


def is_pre_content_nav_line(line: str, heading_key: str = "") -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    key = line_key(stripped)
    if heading_key and key == heading_key:
        return True
    if is_noise_line(stripped):
        return True
    word_count = useful_word_count(stripped)
    has_sentence_end = bool(re.search(r"[.!?;:。！？]$", stripped))
    if stripped.startswith("- ") and word_count <= 12 and not has_sentence_end:
        return True
    if word_count <= 3 and not has_sentence_end:
        return True
    return False


def is_orphan_fragment(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(("[H", "-", "|")):
        return False
    key = line_key(stripped)
    if key in {"and", "or", "to", "of", "for", "from", "in", "on", "with"}:
        return True
    return useful_word_count(stripped) <= 1 and len(stripped) <= 8 and not re.search(r"\d", stripped)


def looks_like_toc_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if re.search(r"\.{4,}\s*\d+\s*$", stripped):
        return True
    if re.match(r"^(PART|Schedule|Chapter)\s+\d+.+\s+\d{1,3}$", stripped, re.I):
        return True
    return False


def useful_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі]{3,}", text))


def collapse_repeated_phrase(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    if len(words) < 2 or len(words) % 2:
        return text
    half = len(words) // 2
    left = " ".join(words[:half])
    right = " ".join(words[half:])
    if line_key(left) == line_key(right):
        return left
    return text


def clean_heading_line(line: str) -> str:
    line = re.sub(r"^\[H[1-6]\]\s*", "", line.strip())
    line = re.sub(r"^#{1,6}\s+", "", line)
    line = re.sub(r"\s+", " ", line).strip(" -")
    line = collapse_repeated_phrase(line)
    return line


def is_probable_heading(line: str) -> bool:
    stripped = clean_heading_line(line)
    if not stripped or len(stripped) > 140:
        return False
    if re.match(r"^(PART|Schedule|Chapter)\s+\d+", stripped, re.I):
        return True
    if re.match(r"^\d+(?:\.\d+)*\.?\s+[A-ZА-ЯӘҒҚҢӨҰҮІ]", stripped):
        return True
    letters = re.sub(r"[^A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі]", "", stripped)
    if len(letters) >= 4 and letters.upper() == letters:
        return True
    return False


def repair_line_wraps(lines: list[str]) -> list[str]:
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        if not out or out[-1] == "":
            out.append(line)
            continue
        prev = out[-1]
        if prev.startswith("|") or line.startswith("|"):
            out.append(line)
            continue
        if prev.startswith("[H") or line.startswith("[H"):
            out.append(line)
            continue
        if re.match(r"^[,.;:!?)]", line):
            out[-1] = f"{prev}{line}"
            continue
        if re.match(r"^[-*]\s+", line) or re.match(r"^\d+[.)]\s+", line):
            out.append(line)
            continue
        if is_probable_heading(prev) or is_probable_heading(line):
            out.append(line)
            continue
        if re.search(r"[.!?:;。！？)]$", prev):
            out.append(line)
            continue
        if re.match(r"^[a-zа-яәғқңөұүһі,;)]", line):
            out[-1] = f"{prev} {line}"
            continue
        if len(prev) < 95 and len(line) < 95:
            out[-1] = f"{prev} {line}"
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return out


def merge_lonely_markers(lines: list[str]) -> list[str]:
    merged: list[str] = []
    i = 0
    marker_re = re.compile(r"^(?:\d+(?:\.\d+)+\.?|\([a-z]\))$", re.I)
    while i < len(lines):
        line = lines[i].strip()
        if i + 1 < len(lines) and marker_re.fullmatch(line):
            nxt = lines[i + 1].strip()
            if nxt and not nxt.startswith("|"):
                merged.append(f"{line.rstrip('.')} {nxt}")
                i += 2
                continue
        merged.append(lines[i])
        i += 1
    return merged


def strip_repeated_page_lines(page_lines: list[list[str]]) -> list[list[str]]:
    candidates: Counter[str] = Counter()
    original_by_key: dict[str, str] = {}
    for lines in page_lines:
        edge_lines = [*lines[:4], *lines[-4:]]
        for line in edge_lines:
            key = line_key(line)
            if len(key) < 5 or len(key) > 90:
                continue
            candidates[key] += 1
            original_by_key[key] = line
    repeated = {
        key
        for key, count in candidates.items()
        if count >= 3 and count >= max(2, len(page_lines) // 4)
    }
    cleaned_pages: list[list[str]] = []
    for lines in page_lines:
        cleaned_pages.append([line for line in lines if line_key(line) not in repeated])
    return cleaned_pages


def clean_pdf_page(text: str) -> list[str]:
    text = normalize_text(text)
    text = re.sub(r"(?m)^\s*Page\s+\d+\s*(?:of\s+\d+)?\s*$", "", text, flags=re.I)
    raw_lines = [line.strip() for line in text.splitlines()]
    lines: list[str] = []
    for line in raw_lines:
        if is_noise_line(line):
            continue
        if looks_like_toc_line(line):
            continue
        line = re.sub(r"\s{2,}", " ", line).strip()
        if line:
            lines.append(line)
    if not lines:
        return []
    toc_like = sum(1 for line in lines if looks_like_toc_line(line))
    if len(lines) >= 5 and toc_like / len(lines) > 0.35:
        return []
    return repair_line_wraps(merge_lonely_markers(lines))


def split_existing_pdf_pages(text: str) -> list[list[str]]:
    text = normalize_text(text)
    pages: list[list[str]] = []
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if re.match(r"^\[H2\]\s+Page\s+\d+", line, re.I):
            if current:
                pages.append(current)
            current = []
            continue
        if re.fullmatch(r"\d{1,3}", line):
            if current and useful_word_count("\n".join(current)) > 20:
                pages.append(current)
                current = []
            continue
        current.append(line)
    if current:
        pages.append(current)
    return pages


def clean_existing_pdf_markdown(text: str) -> str:
    pages = split_existing_pdf_pages(text)
    cleaned_pages: list[list[str]] = []
    for page in pages:
        lines: list[str] = []
        for raw in page:
            line = raw.strip()
            if re.search(r"\bTable of Contents\b", line, re.I):
                break
            if not line or is_noise_line(line) or looks_like_toc_line(line):
                continue
            line = re.sub(r"^#{1,6}\s+", "", line).strip()
            line = re.sub(r"\bEnd-\s+if-?\s*Service\b", "End-of-Service", line, flags=re.I)
            line = re.sub(r"\bEnd-\s+of-\s+Service\b", "End-of-Service", line, flags=re.I)
            line = re.sub(r"\b([A-Za-z])-\s+([a-z]{2,})\b", r"\1\2", line)
            line = re.sub(r"\s{2,}", " ", line).strip()
            if line:
                lines.append(line)
        lines = merge_lonely_markers(lines)
        if not lines:
            continue
        joined = "\n".join(lines)
        toc_markers = sum(1 for line in lines if looks_like_toc_line(line) or re.fullmatch(r"\d{1,3}", line))
        if re.search(r"\b(CONTENT|Table of Contents)\b", joined, re.I) and toc_markers + 3 >= len(lines) * 0.25:
            continue
        heading_count = sum(1 for line in lines if is_probable_heading(line))
        punct_count = sum(1 for line in lines if re.search(r"[.!?;:]$", line))
        if len(lines) >= 4 and heading_count / len(lines) > 0.8 and punct_count <= 1:
            continue
        first_numbered = next((i for i, line in enumerate(lines) if re.match(r"^\d+\.\s+", line)), None)
        if first_numbered and first_numbered > 1:
            keep_prefix: list[str] = []
            previous = lines[first_numbered - 1]
            if is_probable_heading(previous) and not re.search(r"\d+\s*$", previous):
                keep_prefix = [previous]
            lines = keep_prefix + lines[first_numbered:]
        if useful_word_count(joined) < 15:
            continue
        cleaned_pages.append(repair_line_wraps(lines))
    cleaned_pages = strip_repeated_page_lines(cleaned_pages)

    output: list[str] = []
    for page_index, lines in enumerate(cleaned_pages, start=1):
        output.append(f"[H2] Page {page_index}")
        for line in lines:
            if line.startswith("|"):
                output.append(line)
                continue
            if is_probable_heading(line):
                heading = clean_heading_line(line)
                if re.match(r"^(PART|Schedule|Chapter)\s+\d+", heading, re.I):
                    output.append(f"# {heading}")
                elif re.match(r"^\d+(?:\.\d+)*\.?\s+", heading):
                    output.append(f"## {heading}")
                else:
                    output.append(f"# {heading}")
            else:
                output.append(line)
        output.append("")
    cleaned = "\n".join(output).strip()
    return (cleaned or text.strip()) + "\n"


def format_pdf_markdown(pdf_path: Path) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed in this Python environment")
    reader = PdfReader(str(pdf_path))
    page_lines: list[list[str]] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
        avg_line_len = sum(len(line) for line in raw_lines) / max(1, len(raw_lines))
        needs_layout = useful_word_count(text) < 20 or (len(raw_lines) > 30 and avg_line_len < 24)
        if needs_layout:
            try:
                text = page.extract_text(extraction_mode="layout") or text
            except Exception:
                pass
        cleaned = clean_pdf_page(text)
        if cleaned:
            page_lines.append(cleaned)
    page_lines = strip_repeated_page_lines(page_lines)

    output: list[str] = []
    for page_index, lines in enumerate(page_lines, start=1):
        if not lines:
            continue
        output.append(f"[H2] Page {page_index}")
        for line in lines:
            if is_probable_heading(line):
                heading = clean_heading_line(line)
                if re.match(r"^(PART|Schedule|Chapter)\s+\d+", heading, re.I):
                    output.append(f"# {heading}")
                elif re.match(r"^\d+(?:\.\d+)*\.?\s+", heading):
                    output.append(f"## {heading}")
                else:
                    output.append(f"# {heading}")
            else:
                output.append(line)
        output.append("")
    text = "\n".join(output).strip()
    if not text:
        text = "[H2] Extraction failed\nNo extractable text found in PDF."
    return text + "\n"


def parse_block_file(path: Path) -> list[PageRecord]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    records: list[PageRecord] = []
    current_site_group = ""
    i = 0
    while i < len(lines):
        if not re.fullmatch(r"={20,}", lines[i].strip()):
            i += 1
            continue
        i += 1
        meta_lines: list[str] = []
        while i < len(lines):
            stripped = lines[i].strip()
            if re.fullmatch(r"={20,}", stripped) or not stripped:
                break
            meta_lines.append(lines[i])
            i += 1
        if not meta_lines:
            continue
        first = meta_lines[0].strip()
        if first.startswith("SITE_GROUP:") or first.startswith("SITE GROUP:"):
            current_site_group = first.split(":", 1)[1].strip()
            continue

        if i < len(lines) and not lines[i].strip():
            i += 1
        if i < len(lines) and re.fullmatch(r"={20,}", lines[i].strip()):
            i += 1
            if i < len(lines) and not lines[i].strip():
                i += 1

        body_lines: list[str] = []
        while i < len(lines) and not re.fullmatch(r"={20,}", lines[i].strip()):
            body_lines.append(lines[i])
            i += 1

        meta: dict[str, str] = {}
        skip_attachment = False
        for line in meta_lines:
            stripped = line.strip()
            if stripped in {"ATTACHMENTS:", "DOWNLOADED_FILES:"}:
                skip_attachment = True
                continue
            if skip_attachment and stripped.startswith("- "):
                continue
            skip_attachment = False
            m = re.match(r"^([A-Z_]+):\s*(.*)$", stripped)
            if m and m.group(1) in META_KEYS:
                meta[m.group(1)] = m.group(2).strip()
                continue
        body = "\n".join(body_lines).strip()
        url = meta.get("URL", "")
        if not url:
            continue
        records.append(
            PageRecord(
                site=meta.get("SITE", current_site_group),
                language=meta.get("LANGUAGE", ""),
                source_type=meta.get("SOURCE_TYPE", ""),
                url=url,
                title=meta.get("TITLE", url),
                section=meta.get("SECTION", ""),
                scraped_at=meta.get("SCRAPED_AT") or meta.get("CRAWLED_AT", ""),
                content_hash=meta.get("CONTENT_HASH", ""),
                body=body,
            )
        )
    return records


def parse_jsonl_file(path: Path, site: str) -> list[PageRecord]:
    records: list[PageRecord] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[jsonl] skipped {path}:{line_number}: {exc}", file=sys.stderr)
            continue
        body = render_json_content(data.get("content", []))
        faq_pairs = data.get("faq_pairs")
        if isinstance(faq_pairs, list):
            faq_lines: list[str] = []
            for pair in faq_pairs:
                if not isinstance(pair, dict):
                    continue
                question = str(pair.get("question", "")).strip()
                answer = str(pair.get("answer", "")).strip()
                if question and answer:
                    faq_lines.extend([f"Q: {question}", f"A: {answer}"])
            if faq_lines:
                body = f"{body}\n" + "\n".join(faq_lines)
        records.append(
            PageRecord(
                site=site,
                source_type="structured_jsonl",
                url=str(data.get("url", "")).strip(),
                title=str(data.get("title", "")).strip(),
                section=str(data.get("section", "")).strip(),
                scraped_at=str(data.get("scraped_at", "")).strip(),
                body=body,
            )
        )
    return records


def clean_web_body(body: str) -> str:
    body = normalize_text(body)
    body = re.sub(r"\s+(\[H[1-6]\])", r"\n\1", body)
    raw_lines = [line.strip() for line in body.splitlines()]
    output: list[str] = []
    seen_counts: Counter[str] = Counter()
    for line in raw_lines:
        line = strip_placeholder_cells(line)
        if is_noise_line(line):
            continue
        line = re.sub(r"^\s*[-*]\s+", "- ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            if output and output[-1] != "":
                output.append("")
            continue
        heading_match = re.match(r"^(\[H[1-6]\])\s+(.+)$", line)
        if heading_match:
            heading = clean_heading_line(line)
            if heading and not is_noise_line(heading):
                line = f"{heading_match.group(1)} {heading}"
            else:
                continue
        key = line_key(line)
        if key and seen_counts[key] >= 1 and len(key) > 3:
            continue
        seen_counts[key] += 1
        output.append(line)
    first_heading = next((i for i, line in enumerate(output) if re.match(r"^\[H[1-3]\]", line)), None)
    if first_heading is not None and first_heading > 0:
        output = output[first_heading:]
    if output and re.match(r"^\[H[1-3]\]", output[0]):
        heading_key = line_key(clean_heading_line(output[0]))
        filtered = [output[0]]
        skipping_nav = True
        for line in output[1:]:
            if skipping_nav and is_pre_content_nav_line(line, heading_key):
                continue
            skipping_nav = False
            filtered.append(line)
        output = filtered
    output = repair_line_wraps(output)
    output = [
        line
        for line in output
        if not is_orphan_fragment(line) and not is_noise_line(line) and not looks_like_toc_line(line)
    ]
    text = "\n".join(output)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def dedupe_records(records: list[PageRecord]) -> list[PageRecord]:
    best: dict[str, PageRecord] = {}
    for record in records:
        record.url = canonical_url(record.url)
        if is_bad_url(record.url):
            continue
        record.body = clean_web_body(record.body)
        if useful_word_count(record.body) < 20:
            continue
        key = record.content_hash or record.url or hashlib.sha1(record.body.encode()).hexdigest()
        score = useful_word_count(record.body)
        if record.source_type == "structured_jsonl":
            score += 1000
        if record.url.startswith("https://"):
            score += 20
        if record.title and record.title != record.url:
            score += 10
        existing = best.get(key)
        if existing is None or score > useful_word_count(existing.body):
            best[key] = record
    url_best: dict[str, PageRecord] = {}
    for record in best.values():
        existing = url_best.get(record.url)
        if existing is None or useful_word_count(record.body) > useful_word_count(existing.body):
            url_best[record.url] = record
    return sorted(url_best.values(), key=lambda r: (r.site, r.language, r.section, r.url))


def format_web_records(records: list[PageRecord], include_site_group: bool = False) -> str:
    out: list[str] = []
    current_site = None
    for record in records:
        label = site_label(record)
        if include_site_group and label != current_site:
            current_site = label
            out.extend([BLOCK_SEP, f"SITE_GROUP: {label}", BLOCK_SEP, ""])
        out.extend(
            [
                BLOCK_SEP,
                f"URL: {record.url}",
                f"TITLE: {record.title}",
                f"SECTION: {record.section or record.site or 'General'}",
            ]
        )
        if record.language:
            out.append(f"LANGUAGE: {record.language}")
        out.append(f"SITE: {label}")
        out.append(f"SCRAPED_AT: {record.scraped_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()}")
        out.extend([BLOCK_SEP, "", record.body.strip(), ""])
    return "\n".join(out).strip() + "\n"


def write_pdf_previews(out_root: Path) -> tuple[int, int]:
    written = 0
    failures = 0
    pdfs = sorted(RAW.glob("**/*.pdf"))
    for pdf_path in pdfs:
        rel = pdf_path.relative_to(RAW).with_suffix(".md")
        out_path = out_root / "parsed" / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = PARSED / rel
            if existing.exists():
                text = clean_existing_pdf_markdown(existing.read_text(encoding="utf-8", errors="replace"))
            else:
                text = format_pdf_markdown(pdf_path)
            out_path.write_text(text, encoding="utf-8")
            written += 1
        except Exception as exc:
            failures += 1
            print(f"[pdf] failed {pdf_path}: {exc}", file=sys.stderr)
    return written, failures


def copy_non_pdf_parsed(out_root: Path) -> int:
    copied = 0
    parsed_out = out_root / "parsed"
    for path in PARSED.glob("**/*"):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        target = parsed_out / path.relative_to(PARSED)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def clean_block_sources(out_root: Path) -> dict[str, int]:
    stats: dict[str, int] = {}
    public_src = RAW / "public-website-data" / "web_content.txt"
    if not public_src.exists():
        public_src = PARSED / "public-website-data" / "web_content.md"

    block_sources: list[tuple[str, list[PageRecord], Path, bool]] = []
    if public_src.exists():
        block_sources.append(
            (
                str(public_src.relative_to(ROOT)),
                parse_block_file(public_src),
                out_root / "parsed" / "public-website-data" / "web_content.md",
                False,
            )
        )

    afsa_jsonl = RAW / "afsa-web" / "afsa_raw_data" / "data.jsonl"
    afsa_fallback = PARSED / "afsa-web" / "afsa_raw_data" / "data.md"
    if afsa_jsonl.exists():
        block_sources.append(
            (
                str(afsa_jsonl.relative_to(ROOT)),
                parse_jsonl_file(afsa_jsonl, "afsa"),
                out_root / "parsed" / "afsa-web" / "afsa_raw_data" / "data.md",
                False,
            )
        )
    elif afsa_fallback.exists():
        block_sources.append(
            (
                str(afsa_fallback.relative_to(ROOT)),
                parse_block_file(afsa_fallback),
                out_root / "parsed" / "afsa-web" / "afsa_raw_data" / "data.md",
                False,
            )
        )

    aix_jsonl = RAW / "aix-web" / "aix_raw_data" / "data.jsonl"
    aix_fallback = PARSED / "aix-web" / "aix_raw_data" / "data.md"
    if aix_jsonl.exists():
        block_sources.append(
            (
                str(aix_jsonl.relative_to(ROOT)),
                parse_jsonl_file(aix_jsonl, "aix"),
                out_root / "parsed" / "aix-web" / "aix_raw_data" / "data.md",
                False,
            )
        )
    elif aix_fallback.exists():
        block_sources.append(
            (
                str(aix_fallback.relative_to(ROOT)),
                parse_block_file(aix_fallback),
                out_root / "parsed" / "aix-web" / "aix_raw_data" / "data.md",
                False,
            )
        )

    for source_name, records, dst, grouped in block_sources:
        cleaned = dedupe_records(records)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(format_web_records(cleaned, include_site_group=grouped), encoding="utf-8")
        stats[source_name] = len(cleaned)

    aggregate_source = DATA / "site_corpus.txt"
    if not aggregate_source.exists():
        aggregate_source = DATA / "site_corpus.md"
    if aggregate_source.exists():
        aggregate_records = parse_block_file(aggregate_source)
        for _, records, _, _ in block_sources:
            aggregate_records.extend(records)
        cleaned = dedupe_records(aggregate_records)
        formatted = format_web_records(cleaned, include_site_group=True)
        (out_root / "site_corpus.txt").write_text(formatted, encoding="utf-8")
        (out_root / "site_corpus.md").write_text(formatted, encoding="utf-8")
        stats[str(aggregate_source.relative_to(ROOT))] = len(cleaned)
    return stats


def apply_preview(preview_root: Path) -> None:
    backup_root = DATA / f"backup_before_clean_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    backup_root.mkdir(parents=True, exist_ok=False)
    if PARSED.exists():
        shutil.copytree(PARSED, backup_root / "parsed")
    for filename in ("site_corpus.md", "site_corpus.txt"):
        src = DATA / filename
        if src.exists():
            shutil.copy2(src, backup_root / filename)

    preview_parsed = preview_root / "parsed"
    if preview_parsed.exists():
        shutil.rmtree(PARSED)
        shutil.copytree(preview_parsed, PARSED)
    for filename in ("site_corpus.md", "site_corpus.txt"):
        src = preview_root / filename
        if src.exists():
            shutil.copy2(src, DATA / filename)
    print(f"[apply] backup saved to {backup_root.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean AIFC parsed PDFs and web corpora.")
    parser.add_argument("--preview-dir", default=str(PREVIEW))
    parser.add_argument("--apply", action="store_true", help="Replace data/parsed and site_corpus.* after generating preview.")
    args = parser.parse_args()

    out_root = Path(args.preview_dir)
    if not out_root.is_absolute():
        out_root = ROOT / out_root
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    pdf_written, pdf_failures = write_pdf_previews(out_root)
    copied = copy_non_pdf_parsed(out_root)
    web_stats = clean_block_sources(out_root)

    print(f"[clean] preview={out_root.relative_to(ROOT)}")
    print(f"[clean] pdfs_written={pdf_written} pdf_failures={pdf_failures}")
    print(f"[clean] non_pdf_parsed_copied={copied}")
    for source, count in sorted(web_stats.items()):
        print(f"[clean] web_records {source} -> {count}")

    if args.apply:
        apply_preview(out_root)


if __name__ == "__main__":
    main()
