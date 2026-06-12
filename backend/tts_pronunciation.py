from __future__ import annotations

from .spoken_text import (
    normalize_spoken_numbers,
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
    """Normalize text for speech synthesis.

    Keep acronyms in the text by default. Soniox receives the domain context on
    the STT side and the TTS voice is faster when it speaks short acronyms
    instead of expanded legal names.
    """
    del expand_context_terms
    speech_lang = _speech_lang(language)
    prepared = text or ""
    prepared = sanitize_spoken_text(prepared, keep_digits=True)
    prepared = normalize_spoken_numbers(prepared, speech_lang)
    prepared = sentenceize_spoken_text(prepared, speech_lang)
    prepared = sanitize_spoken_text(prepared)
    prepared = remove_repeated_sentences(prepared)
    return sanitize_spoken_text(prepared)


def _speech_lang(language: str | None) -> str:
    if language in {"en", "ru", "kk", "zh"}:
        return language
    return "en"
