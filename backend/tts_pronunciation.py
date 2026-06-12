from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .spoken_text import (
    normalize_spoken_numbers,
    remove_repeated_sentences,
    sanitize_spoken_text,
    sentenceize_spoken_text,
)

_TOKEN_BOUNDARY = r"(?<![\wӘәҒғҚқҢңӨөҰұҮүҺһІіЁё'’\-]){}(?![\wӘәҒғҚқҢңӨөҰұҮүҺһІіЁё'’\-])"
_DOMAIN_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z]{2,})+\b")


def prepare_tts_text(
    text: str,
    language: str | None,
    context_file: Path,
    *,
    expand_context_terms: bool = True,
) -> str:
    """Normalize text for speech synthesis.

    Soniox TTS does not accept an STT-style context object, so terms from
    soniox-context-audio.json must be applied to the text before synthesis.
    """
    speech_lang = _speech_lang(language)
    context_lang = _context_lang(language)
    prepared = expand_audio_context_terms(text or "", context_lang, context_file) if expand_context_terms else (text or "")
    prepared = sanitize_spoken_text(prepared, keep_digits=True)
    prepared = normalize_spoken_numbers(prepared, speech_lang)
    prepared = sentenceize_spoken_text(prepared, speech_lang)
    prepared = sanitize_spoken_text(prepared)
    prepared = remove_repeated_sentences(prepared)
    return sanitize_spoken_text(prepared)


def expand_audio_context_terms(text: str, language: str | None, context_file: Path) -> str:
    if not text or not text.strip():
        return ""
    replacements = _compiled_replacements(str(context_file), _context_lang(language))
    expanded = text
    placeholders: list[str] = []

    def protect_text(value: str) -> str:
        marker = f"\uE000{len(placeholders)}\uE001"
        placeholders.append(value)
        return marker

    expanded = _DOMAIN_RE.sub(lambda match: protect_text(match.group(0)), expanded)

    for phrase in _known_spoken_phrases(str(context_file), _context_lang(language)):
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)

        def protect(match: re.Match[str]) -> str:
            return protect_text(match.group(0))

        expanded = pattern.sub(protect, expanded)

    for pattern, replacement in replacements:
        def repl(_: re.Match[str]) -> str:
            return protect_text(replacement)

        expanded = pattern.sub(repl, expanded)

    for idx, replacement in enumerate(placeholders):
        expanded = expanded.replace(f"\uE000{idx}\uE001", replacement)
    return re.sub(r"\s+", " ", expanded).strip()


@lru_cache(maxsize=16)
def _compiled_replacements(context_file: str, language: str) -> tuple[tuple[re.Pattern[str], str], ...]:
    entries = _load_context_entries(context_file)
    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        replacement = _spoken_value(entry.get(language) or entry.get("en") or "")
        if not replacement:
            continue
        aliases = [entry.get("abbr", "")]
        aliases.extend(entry.get("aliases") or [])
        for alias in aliases:
            alias = str(alias).strip()
            if not alias:
                continue
            key = (alias.casefold(), replacement.casefold())
            if key in seen:
                continue
            seen.add(key)
            rows.append((alias, replacement))

    rows.sort(key=lambda item: len(item[0]), reverse=True)
    compiled = [
        (re.compile(_TOKEN_BOUNDARY.format(re.escape(alias)), re.IGNORECASE), replacement)
        for alias, replacement in rows
    ]
    return tuple(compiled)


@lru_cache(maxsize=16)
def _known_spoken_phrases(context_file: str, language: str) -> tuple[str, ...]:
    phrases = {
        _spoken_value(entry.get(language) or entry.get("en") or "")
        for entry in _load_context_entries(context_file)
    }
    phrases = {phrase for phrase in phrases if len(phrase) >= 12}
    return tuple(sorted(phrases, key=len, reverse=True))


@lru_cache(maxsize=8)
def _load_context_entries(context_file: str) -> tuple[dict, ...]:
    try:
        payload = json.loads(Path(context_file).read_text(encoding="utf-8"))
    except OSError:
        return ()
    except json.JSONDecodeError:
        return ()

    term_groups = payload.get("term_groups")
    if not isinstance(term_groups, dict):
        return ()

    entries: list[dict] = []
    for group_entries in term_groups.values():
        if not isinstance(group_entries, list):
            continue
        for item in group_entries:
            if isinstance(item, dict):
                entries.append(item)
    return tuple(entries)


def _spoken_value(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    if " / " in cleaned:
        cleaned = cleaned.split(" / ", 1)[0].strip()
    return cleaned.strip(" .")


def _context_lang(language: str | None) -> str:
    if language == "ru":
        return "ru"
    if language == "kk":
        return "kk"
    return "en"


def _speech_lang(language: str | None) -> str:
    if language in {"en", "ru", "kk", "zh"}:
        return language
    return "en"
