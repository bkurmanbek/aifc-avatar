from __future__ import annotations

from .spoken_text import (
    clean_links_and_ranges,
    remove_repeated_sentences,
    sanitize_spoken_text,
    sentenceize_spoken_text,
)


def prepare_tts_text(
    text: str,
    language: str | None,
    *,
    expand_context_terms: bool = False,
) -> str:
    # The number/ordinal/abbreviation normalizer was REMOVED: spoken text is now produced
    # already-pronounceable upstream — the live LLM `spoken` field (build_prompt rules) and the
    # FAQ spoken sidecar (scripts/rewrite_faq_spoken.py) spell out numbers/dates/ordinals and
    # expand abbreviations. The old regex normalizer mangled ordinals (e.g. "9th"->"nineth",
    # "10th"->"onezeroth"). We keep only sanitization + light link/range cleanup as a safety net.
    del expand_context_terms
    speech_lang = _speech_lang(language)
    prepared = text or ""
    prepared = clean_links_and_ranges(prepared, speech_lang)
    prepared = sanitize_spoken_text(prepared, keep_digits=True)
    prepared = sentenceize_spoken_text(prepared, speech_lang)
    prepared = sanitize_spoken_text(prepared)
    prepared = remove_repeated_sentences(prepared)
    return sanitize_spoken_text(prepared)


def _speech_lang(language: str | None) -> str:
    if language in {"en", "ru", "kk", "zh"}:
        return language
    return "en"
