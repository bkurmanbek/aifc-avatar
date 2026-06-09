from __future__ import annotations

import re


_SPOKEN_RE = re.compile(r"\[\[spoken\]\](.*?)\[\[/spoken\]\]", re.IGNORECASE | re.DOTALL)
_DETAILS_RE = re.compile(r"\[\[details\]\](.*?)\[\[/details\]\]", re.IGNORECASE | re.DOTALL)
_FOLLOWUPS_RE = re.compile(r"\[\[followups\]\](.*?)\[\[/followups\]\]", re.IGNORECASE | re.DOTALL)
_CONTROL_TAG_RE = re.compile(r"\[\[/?[a-z_]+\]\]|\[\[[^\]]+\]\]|<[^>]+>")
_STAGE_DIR_RE = re.compile(r"\[(?:[A-Za-z][^\]]{0,40}|[А-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі][^\]]{0,40}|[\u4e00-\u9fff][^\]]{0,20})\]")
_PARENS_DIR_RE = re.compile(r"\((?:laughs|giggles|whispers|sighs|pause|sarcastically|smiles?|breathes?)\)", re.IGNORECASE)
_NON_SPEECH_RE = re.compile(r"[^A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі\u4e00-\u9fff\s.,;:!?%/'’-]")
_NON_SPEECH_RE_KEEP_DIGITS = re.compile(r"[^0-9A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі\u4e00-\u9fff\s.,;:!?%/'’-]")
_MULTISPACE_RE = re.compile(r"\s+")
_MULTIPUNCT_RE = re.compile(r"([.,;:!?]){2,}")
_DIGIT_RE = re.compile(r"\d")
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})(?=\b|\s|[.,!?])")
_PERCENT_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*%")
_NUM_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9\u4e00-\u9fff])[+-]?"  # leading sign must not be part of a larger token
    r"(?:\d{1,3}(?:[.,]\d{3})*|\d+)(?:[.,]\d+)?"
    r"(?![A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9\u4e00-\u9fff])"
)

_EN_ONES = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
    12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen", 16: "sixteen",
    17: "seventeen", 18: "eighteen", 19: "nineteen",
}
_EN_TENS = {
    2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty",
    7: "seventy", 8: "eighty", 9: "ninety",
}
_RU_ONES = {
    0: "ноль", 1: "один", 2: "два", 3: "три", 4: "четыре", 5: "пять",
    6: "шесть", 7: "семь", 8: "восемь", 9: "девять", 10: "десять", 11: "одиннадцать",
    12: "двенадцать", 13: "тринадцать", 14: "четырнадцать", 15: "пятнадцать",
    16: "шестнадцать", 17: "семнадцать", 18: "восемнадцать", 19: "девятнадцать",
}
_RU_TENS = {
    2: "двадцать", 3: "тридцать", 4: "сорок", 5: "пятьдесят", 6: "шестьдесят",
    7: "семьдесят", 8: "восемьдесят", 9: "девяносто",
}
_RU_HUNDREDS = {
    1: "сто", 2: "двести", 3: "триста", 4: "четыреста", 5: "пятьсот",
    6: "шестьсот", 7: "семьсот", 8: "восемьсот", 9: "девятьсот",
}
_KK_ONES = {
    0: "нөл", 1: "бір", 2: "екі", 3: "үш", 4: "төрт", 5: "бес",
    6: "алты", 7: "жеті", 8: "сегіз", 9: "тоғыз",
}
_KK_TENS = {
    2: "жиырма", 3: "отыз", 4: "қырық", 5: "елу", 6: "алпыс",
    7: "жетпіс", 8: "сексен", 9: "тоқсан",
}
_ZH_DIGITS = "零一二三四五六七八九"
_DIGIT_WORDS_BY_LANG = {
    "en": {i: str(_EN_ONES[i]) for i in range(10)},
    "ru": {i: str(_RU_ONES[i]) for i in range(10)},
    "kk": {i: str(_KK_ONES[i]) for i in range(10)},
    "zh": {i: _ZH_DIGITS[i] for i in range(10)},
}
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")
_LATIN_CYRILLIC_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі]+")
_CLAUSE_PUNCT_RE = re.compile(r"([,;:，；、])")


def extract_blocks(tagged_text: str) -> tuple[str, str, str]:
    spoken = _match_block(_SPOKEN_RE, tagged_text)
    details = _match_block(_DETAILS_RE, tagged_text)
    followups = _match_block(_FOLLOWUPS_RE, tagged_text)
    return spoken, details, followups


def rebuild_blocks(spoken: str, details: str, followups: str) -> str:
    return (
        f"[[spoken]]{spoken.strip()}[[/spoken]]"
        f"[[details]]{details.strip()}[[/details]]"
        f"[[followups]]{followups.strip()}[[/followups]]"
    )


def sanitize_spoken_text(text: str, *, keep_digits: bool = False) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = _CONTROL_TAG_RE.sub(" ", text)
    text = _STAGE_DIR_RE.sub(" ", text)
    text = _PARENS_DIR_RE.sub(" ", text)
    text = (_NON_SPEECH_RE_KEEP_DIGITS if keep_digits else _NON_SPEECH_RE).sub(" ", text)
    text = _MULTIPUNCT_RE.sub(r"\1", text)
    text = _MULTISPACE_RE.sub(" ", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"(?<!\d)([.,;:!?])(?=[^\s.,;:!?\d])", r"\1 ", text)
    return text.strip(" \n\t-")


def sentenceize_spoken_text(text: str, language: str | None = None) -> str:
    cleaned = sanitize_spoken_text(text)
    if not cleaned:
        return ""
    if language == "zh":
        return _sentenceize_zh(cleaned)
    return _sentenceize_word_language(cleaned)


def _sentenceize_word_language(text: str) -> str:
    parts = _CLAUSE_PUNCT_RE.split(text)
    if len(parts) <= 1:
        return text

    output: list[str] = []
    current = ""
    for idx in range(0, len(parts), 2):
        segment = parts[idx].strip()
        punct = parts[idx + 1] if idx + 1 < len(parts) else ""
        if segment:
            current = f"{current} {segment}".strip() if current else segment
        if not punct:
            continue

        if punct in ",;:" and _clause_should_be_sentence(current):
            output.append(current.rstrip(" ,;:") + ".")
            current = ""
        else:
            current = (current.rstrip() + punct).strip()

    if current.strip():
        output.append(current.strip())
    return " ".join(_capitalize_after_sentence(part) for part in output if part.strip()).strip()


def _sentenceize_zh(text: str) -> str:
    parts = _CLAUSE_PUNCT_RE.split(text)
    if len(parts) <= 1:
        return text
    output: list[str] = []
    current = ""
    for idx in range(0, len(parts), 2):
        segment = parts[idx].strip()
        punct = parts[idx + 1] if idx + 1 < len(parts) else ""
        current += segment
        if not punct:
            continue
        if punct in "，；、" and len(current) >= 28:
            output.append(current.rstrip("，；、") + "。")
            current = ""
        else:
            current += punct
    if current.strip():
        output.append(current.strip())
    return "".join(output).strip()


def _clause_should_be_sentence(text: str) -> bool:
    words = _LATIN_CYRILLIC_WORD_RE.findall(text or "")
    return len(words) >= 8 or len(text) >= 90


def _capitalize_after_sentence(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped[0].upper() + stripped[1:]


def needs_spoken_rewrite(text: str) -> bool:
    cleaned = sanitize_spoken_text(text)
    if not cleaned:
        return False
    if _DIGIT_RE.search(cleaned):
        return True
    return cleaned != (text or "").strip()


def has_numeric_content(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


def is_speakable_text(text: str) -> bool:
    cleaned = sanitize_spoken_text(text)
    return bool(re.search(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі\u4e00-\u9fff]", cleaned))


def remove_repeated_sentences(text: str) -> str:
    """Remove immediate duplicate sentence echoes produced by noisy model output."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    raw_sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(cleaned) if part.strip()]
    if not raw_sentences:
        return ""
    deduped: list[str] = []
    seen: list[str] = []
    for sentence in raw_sentences:
        normalized = re.sub(r"\s+", " ", sentence).strip().casefold()
        normalized = normalized.strip(".!?。！？")
        if seen and normalized == seen[-1]:
            continue
        deduped.append(sentence)
        seen.append(normalized)
    return " ".join(deduped).strip()


def _spell_en_number(value: int) -> str:
    if value < 0:
        return f"minus {_spell_en_number(-value)}"
    if value < 20:
        return _EN_ONES[value]
    if value < 100:
        tens, rem = divmod(value, 10)
        if rem:
            return f"{_EN_TENS[tens]} {_EN_ONES[rem]}"
        return _EN_TENS[tens]
    if value < 1000:
        hundreds, rem = divmod(value, 100)
        if rem:
            return f"{_EN_ONES[hundreds]} hundred {_spell_en_number(rem)}"
        return f"{_EN_ONES[hundreds]} hundred"
    if value < 1_000_000:
        thousands, rem = divmod(value, 1000)
        if rem:
            return f"{_spell_en_number(thousands)} thousand {_spell_en_number(rem)}"
        return f"{_spell_en_number(thousands)} thousand"
    return str(value)


def _spell_ru_number(value: int) -> str:
    if value < 0:
        return f"минус {_spell_ru_number(-value)}"
    if value < 20:
        return _RU_ONES[value]
    if value < 100:
        tens, rem = divmod(value, 10)
        if rem:
            return f"{_RU_TENS[tens]} {_RU_ONES[rem]}"
        return _RU_TENS[tens]
    if value < 1000:
        hundreds, rem = divmod(value, 100)
        if rem:
            return f"{_RU_HUNDREDS[hundreds]} {_spell_ru_number(rem)}"
        return _RU_HUNDREDS[hundreds]
    if value < 1_000_000:
        thousands, rem = divmod(value, 1000)
        thousands_text = _spell_ru_number_feminine(thousands)
        thousands_unit = _ru_plural(thousands, "тысяча", "тысячи", "тысяч")
        if rem:
            return f"{thousands_text} {thousands_unit} {_spell_ru_number(rem)}"
        return f"{thousands_text} {thousands_unit}"
    return str(value)


def _spell_ru_number_feminine(value: int) -> str:
    text = _spell_ru_number(value)
    if 10 < value % 100 < 20:
        return text
    if value % 10 == 1:
        return re.sub(r"\bодин$", "одна", text)
    if value % 10 == 2:
        return re.sub(r"\bдва$", "две", text)
    return text


def _ru_plural(value: int, one: str, few: str, many: str) -> str:
    tail = abs(value) % 100
    if 10 < tail < 20:
        return many
    last = tail % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _spell_kk_number(value: int) -> str:
    if value < 0:
        return f"теріс {_spell_kk_number(-value)}"
    if value < 10:
        return _KK_ONES[value]
    if value < 20:
        return f"он {_KK_ONES[value - 10]}"
    if value < 100:
        tens, rem = divmod(value, 10)
        if rem:
            return f"{_KK_TENS[tens]} {_KK_ONES[rem]}"
        return _KK_TENS[tens]
    if value < 1000:
        hundreds, rem = divmod(value, 100)
        if rem:
            return f"{_spell_kk_number(hundreds)} жүз {_spell_kk_number(rem)}"
        return f"{_spell_kk_number(hundreds)} жүз"
    if value < 1_000_000:
        thousands, rem = divmod(value, 1000)
        if rem:
            return f"{_spell_kk_number(thousands)} мың {_spell_kk_number(rem)}"
        return f"{_spell_kk_number(thousands)} мың"
    return str(value)


def _spell_zh_digits(value: str) -> str:
    if not value:
        return value
    if not value.isdigit():
        return value
    number = int(value)
    if number < 10:
        return _ZH_DIGITS[number]
    if number < 100:
        tens, ones = divmod(number, 10)
        if tens == 1:
            return f"十{_ZH_DIGITS[ones]}" if ones else "十"
        return f"{_ZH_DIGITS[tens]}十{_ZH_DIGITS[ones]}" if ones else f"{_ZH_DIGITS[tens]}十"
    if number < 1000:
        hundreds, rest = divmod(number, 100)
        if rest == 0:
            return f"{_ZH_DIGITS[hundreds]}百"
        if rest < 10:
            return f"{_ZH_DIGITS[hundreds]}百零{_ZH_DIGITS[rest]}"
        return f"{_ZH_DIGITS[hundreds]}百{_spell_zh_digits(str(rest))}"
    return "".join(_ZH_DIGITS[int(ch)] for ch in value)


def _spell_zh_number(value: int) -> str:
    return _spell_zh_digits(str(value))


def _convert_decimal(value: str, lang: str) -> str:
    if "." not in value:
        return value
    integer, frac = value.split(".", 1)
    try:
        integer_text = _convert_integer(integer or "0", lang)
    except Exception:
        integer_text = integer
    frac_words = " ".join(_convert_integer(ch, lang) for ch in frac if ch.isdigit())
    if not frac_words:
        return integer_text
    point_word = {
        "ru": "запятая",
        "kk": "үтір",
        "zh": "点",
    }.get(lang, "point")
    return f"{integer_text} {point_word} {frac_words}"


def _convert_integer(raw: str, lang: str) -> str:
    value = int(raw)
    if lang == "en":
        return _spell_en_number(value)
    if lang == "ru":
        return _spell_ru_number(value)
    if lang == "kk":
        return _spell_kk_number(value)
    if lang == "zh":
        return _spell_zh_number(value)
    return str(value)


def _convert_single_token(token: str, lang: str) -> str:
    if ":" in token and all(part.isdigit() for part in token.split(":")):
        left, right = token.split(":", 1)
        if lang == "zh":
            return f"{_convert_decimal(left, lang)} {_convert_decimal(right, lang)}"
        return f"{_convert_integer(left, lang)} {_convert_integer(right, lang)}"
    if "." in token or "," in token:
        norm = _normalize_number_token(token)
        if "." in norm:
            return _convert_decimal(norm, lang)
    if any(ch.isdigit() for ch in token):
        raw = token.replace(",", "")
        try:
            return _convert_integer(str(int(raw)), lang)
        except ValueError:
            pass
    return token


def _normalize_number_token(token: str) -> str:
    if "." in token and "," in token:
        return token.replace(",", "")
    if "," in token and "." not in token:
        left, right = token.rsplit(",", 1)
        if right.isdigit() and len(right) != 3:
            return f"{left.replace(',', '')}.{right}"
        return token.replace(",", "")
    return token


def _replace_numbers(text: str, lang: str) -> str:
    normalized = text

    def repl_digit(match: re.Match[str]) -> str:
        digit = match.group(0)
        return _DIGIT_WORDS_BY_LANG.get(lang, _DIGIT_WORDS_BY_LANG["en"]).get(int(digit), digit)

    def repl_percent(match: re.Match[str]) -> str:
        value = match.group(1)
        try:
            spoken = _convert_decimal(value, lang)
        except Exception:
            return value
        if lang == "ru":
            return f"{spoken} процентов"
        if lang == "zh":
            return f"百分之 {spoken}"
        if lang == "kk":
            return f"{spoken} пайыз"
        return f"{spoken} percent"

    normalized = _PERCENT_RE.sub(repl_percent, normalized)

    def repl_time(match: re.Match[str]) -> str:
        hours, mins = match.group(1).split(":", 1)
        if lang == "ru":
            return f"{_convert_integer(hours, lang)} {minutes_word(hours, mins)}"
        if lang == "kk":
            return f"{_convert_integer(hours, lang)} {minutes_word(hours, mins, kk=True)}"
        if lang == "zh":
            return f"{_convert_integer(hours, lang)} {minutes_word(hours, mins, zh=True)}"
        return f"{_convert_integer(hours, lang)} {_convert_integer(mins, lang)}"

    normalized = _TIME_RE.sub(repl_time, normalized)

    def repl_number(match: re.Match[str]) -> str:
        token = match.group(0).strip()
        try:
            return _convert_single_token(token, lang)
        except Exception:
            return token

    normalized = _NUM_RE.sub(repl_number, normalized)
    normalized = re.sub(r"\d", repl_digit, normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def minutes_word(h: str, m: str, kk: bool = False, zh: bool = False) -> str:
    if kk:
        return f"минут {_convert_integer(m.lstrip('0') or '0', 'kk')}"
    if zh:
        return f"点 {_convert_integer(m.lstrip('0') or '0', 'zh')}"
    return _convert_integer(m.lstrip("0") or "0", "ru") + " минут"


def normalize_spoken_numbers(text: str, lang: str) -> str:
    if not text:
        return ""
    return _replace_numbers(text, lang)


def _match_block(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text or "")
    return (match.group(1) if match else "").strip()
