from __future__ import annotations

import re


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.。])(?:\s+|(?=[A-ZА-ЯЁӘҒҚҢӨҰҮҺІ\u4e00-\u9fff])|$)|\n+")
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(
    r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9\u4e00-\u9fff]+"
    r"(?:['’\-][A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9\u4e00-\u9fff]+)?",
    re.UNICODE,
)
_WORD_CHAR_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9\u4e00-\u9fff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TERMINAL_PUNCT = ".!?。！？"
_MAX_WORD_COUNT = 7


class LowLatencyVoiceChunker:
    """SentenceSplitter-compatible chunker tuned for smooth avatar playback.

    It emits TTS chunks at a period boundary or after seven complete words.
    The seven-word limit is based on human words, not model tokens or characters.
    """

    def __init__(
        self,
        min_chars: int,
        first_chars: int,
        max_chars: int,
        short_chars: int,
    ):
        self._min_chars = min_chars
        self._first_chars = first_chars
        self._max_chars = max_chars
        self._short_chars = short_chars
        self.buffer = ""
        self.chunk_idx = 0
        self.sentences: list[str] = []

    def feed(self, delta: str) -> list[tuple[str, int]]:
        ready: list[tuple[str, int]] = []
        self.buffer += delta or ""

        while True:
            cut = self._next_cut(final=False)
            if cut is None:
                break
            segment = self.buffer[:cut].strip()
            self.buffer = self.buffer[cut:].lstrip()
            if not segment:
                continue
            emitted = self._dispatch(segment)
            if emitted:
                ready.append(emitted)

        return ready

    def flush(self) -> list[tuple[str, int]]:
        ready: list[tuple[str, int]] = []
        if self.buffer.strip():
            emitted = self._dispatch(self.buffer.strip())
            if emitted:
                ready.append(emitted)
        self.buffer = ""
        return ready

    @property
    def total_chunks(self) -> int:
        return self.chunk_idx

    def _next_cut(self, final: bool) -> int | None:
        text = self.buffer
        stripped_len = len(text.strip())
        if not stripped_len:
            return len(text) if final else None

        sentence_cut = None
        for match in _SENTENCE_BOUNDARY_RE.finditer(text):
            sentence_cut = match.end()
            break
        word_cut = self._word_count_cut_index(text)
        first_cut = self._threshold_cut_index(text, self._first_chars) if self.chunk_idx == 0 else None
        max_cut = self._max_cut_index(text) if stripped_len >= self._max_chars else None
        candidates = [cut for cut in (sentence_cut, word_cut, first_cut, max_cut) if cut is not None]
        if candidates:
            return min(candidates)

        return len(text) if final else None

    def _threshold_cut_index(self, text: str, min_chars: int) -> int | None:
        if len(text.strip()) < min_chars:
            return None
        if _CJK_RE.search(text):
            return min(len(text), max(1, min_chars))
        for match in _WORD_RE.finditer(text):
            if match.end() >= min_chars and _has_complete_boundary(text, match.end()):
                return match.end()
        return None

    def _max_cut_index(self, text: str) -> int | None:
        if _CJK_RE.search(text):
            return min(len(text), max(1, self._max_chars))
        cut = None
        for match in _WORD_RE.finditer(text):
            if match.end() <= self._max_chars:
                cut = match.end()
                continue
            break
        return cut or min(len(text), self._max_chars)

    def _word_count_cut_index(self, text: str) -> int | None:
        words = list(_WORD_RE.finditer(text))
        if len(words) < _MAX_WORD_COUNT:
            return None
        end = words[_MAX_WORD_COUNT - 1].end()
        if not _has_complete_boundary(text, end):
            return None
        lookahead = end
        while lookahead < len(text) and text[lookahead].isspace():
            lookahead += 1
        if lookahead < len(text) and text[lookahead] in _TERMINAL_PUNCT:
            return lookahead + 1
        return end

    def _dispatch(self, text: str) -> tuple[str, int] | None:
        cleaned = _SPACE_RE.sub(" ", text).strip()
        cleaned = re.sub(r"\s+([.!?。！？])", r"\1", cleaned)
        if not cleaned or not _WORD_RE.search(cleaned):
            return None
        result = (cleaned, self.chunk_idx)
        self.sentences.append(cleaned)
        self.chunk_idx += 1
        return result


def _has_complete_boundary(text: str, end: int) -> bool:
    if end < len(text):
        return not _WORD_CHAR_RE.match(text[end])
    return False
