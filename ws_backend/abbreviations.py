from __future__ import annotations

from difflib import SequenceMatcher
import os
import re
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("AIFC_DATA_DIR", os.getenv("DATA_DIR", str(ROOT.parent / "data")))).expanduser().resolve()
ABBR_FILE = DATA_DIR / "abbr.txt"
_PIPE_RE = re.compile(r"\s*\|\s*")
_PAREN_RE = re.compile(r"\([^)]*\)")
_PAREN_CONTENT_RE = re.compile(r"\(([^)]*)\)")
_WORD_RE = re.compile(r"\b[\wӘәҒғҚқҢңӨөҰұҮүҺһІіЁё-]{2,16}\b", re.UNICODE)
_TOKEN_BOUNDARY = r"(?<![\wӘәҒғҚқҢңӨөҰұҮүҺһІіЁё'’\-]){}(?![\wӘәҒғҚқҢңӨөҰұҮүҺһІіЁё'’\-])"
_FUZZY_THRESHOLD = 0.85
_STT_CONTEXT_EXCLUDED_ABBRS = {
    # Event/internal/generic macro, geography, ambiguous vendor, or unverified
    # terms that are not useful as public AIFC assistant recognition hints.
    "TAF",
    "UAE",
    "EU",
    "ЕС",
    "UK",
    "USA",
    "США",
    "RK",
    "ҚР",
    "UN",
    "ООН",
    "КЗТ",
    "HR",
    "SDU",
    "ВВП",
    "GDP",
    "GLP",
    "MSCI",
    "ICE",
}
_STT_CONTEXT_ENTRY_OVERRIDES: dict[str, dict[str, str]] = {
    "AEC": {
        "en": "AIFC Expat Centre",
        "ru": "Экспат центр МФЦА",
        "kk": "АХҚО Экспат орталығы",
    },
    "AFSA": {
        "ru": "Комитет МФЦА по регулированию финансовых услуг",
        "kk": "АХҚО Қаржылық қызметтер көрсетуді реттеу жөніндегі комитеті",
    },
    "AIFCA": {
        "en": "AIFC Authority",
        "ru": "Администрация МФЦА",
        "kk": "АХҚО әкімшілігі",
    },
    "GFC": {
        "en": "AIFC Green Finance Centre",
        "ru": "Центр зеленых финансов МФЦА",
        "kk": "АХҚО Жасыл қаржы орталығы",
        "category": "carbon markets / climate",
    },
    "REC": {
        "en": "Renewable Energy Certificate / International Renewable Energy Certificate",
        "ru": "Сертификат возобновляемой энергии",
        "kk": "Жаңартылатын энергия сертификаты",
    },
    "СВЭ": {
        "en": "Renewable Energy Certificate / International Renewable Energy Certificate",
        "ru": "Сертификат возобновляемой энергии",
        "kk": "Жаңартылатын энергия сертификаты",
    },
    "CCP": {
        "en": "Core Carbon Principles",
        "ru": "Основные углеродные принципы",
        "kk": "Негізгі көміртегі қағидаттары",
    },
    "CIBAFI": {
        "en": "General Council for Islamic Banks and Financial Institutions",
        "ru": "Генеральный совет исламских банков и финансовых институтов",
        "kk": "Ислам банктері мен қаржы институттарының бас кеңесі",
    },
}
_STT_CONTEXT_VERIFIED_ALIASES: dict[str, dict[str, list[str]]] = {
    "AIFC": {"ru": ["МФЦА"], "kk": ["АХҚО"]},
    "МФЦА": {"en": ["AIFC"], "kk": ["АХҚО"]},
    "АХҚО": {"en": ["AIFC"], "ru": ["МФЦА"]},
    "AFSA": {"ru": ["АФСА"]},
    "GFC": {"ru": ["ЦЗФ"], "kk": ["ЖҚО"]},
    "AML": {"ru": ["ПОД"]},
    "CTF": {"ru": ["ПФТ"]},
    "IIN": {"ru": ["ИИН"], "kk": ["ЖСН"]},
    "ИИН": {"en": ["IIN"], "kk": ["ЖСН"]},
    "EDS": {"ru": ["ЭЦП"], "kk": ["ЭЦҚ"]},
    "JSC": {"ru": ["АО"], "kk": ["АҚ"]},
    "TRP": {"ru": ["РВП"]},
    "VAT": {"ru": ["НДС"], "kk": ["ҚҚС"]},
    "CFC": {"ru": ["КИК"]},
    "MCI": {"ru": ["МРП"], "kk": ["АЕК"]},
    "ETS": {"ru": ["СТВ"], "kk": ["ШСЖ"]},
    "СТВ": {"en": ["ETS"], "kk": ["ШСЖ"]},
    "GHG": {"en": ["GHGs"], "ru": ["ПГ"]},
    "ПГ": {"en": ["GHG", "GHGs"]},
    "CO2": {"en": ["CO₂"]},
    "REC": {"en": ["I-REC", "I-RECs", "RECs"], "ru": ["СВЭ"]},
    "СВЭ": {"en": ["REC", "I-REC", "I-RECs", "RECs"]},
    "REDD": {"en": ["REDD+"]},
    "ICAO": {"ru": ["ИКАО"], "kk": ["ИКАО"]},
    "ВИЭ": {"en": ["RES"], "kk": ["ЖЭК"]},
}
_STT_CONTEXT_CATEGORY_ORDER = (
    "core aifc entities",
    "financial / legal",
    "carbon markets / climate",
    "capital markets / exchanges",
    "geography / international organisations",
    "hr / internal",
)
_STT_CONTEXT_CATEGORY_LABELS = {
    "core aifc entities": "Core AIFC entities",
    "financial / legal": "Financial and legal terms",
    "carbon markets / climate": "Carbon markets and climate terms",
    "capital markets / exchanges": "Capital markets and exchange terms",
    "geography / international organisations": "Currency and energy terms",
    "hr / internal": "Role terms",
}
_STT_CONTEXT_EXTRA_TERMS = (
    "FinTech Lab",
    "Expat Centre",
    "AIFC Portal",
    "Public Register",
    "Carbon Platform",
    "Astana International Financial Centre",
    "Международный финансовый центр «Астана»",
    "Астана Халықаралық Қаржы Орталығы",
    "Astana Financial Services Authority",
    "Комитет МФЦА по регулированию финансовых услуг",
    "АХҚО Қаржылық қызметтер көрсетуді реттеу жөніндегі комитеті",
    "AIFC Authority",
    "Администрация МФЦА",
    "АХҚО әкімшілігі",
    "Astana International Exchange",
    "Астанинская международная биржа",
    "Астана Халықаралық Биржасы",
    "AIFC Expat Centre",
    "Экспат центр МФЦА",
    "АХҚО Экспат орталығы",
    "International Arbitration Centre",
    "AIFC Green Finance Centre",
    "Центр зеленых финансов МФЦА",
    "АХҚО Жасыл қаржы орталығы",
    "Anti-Money Laundering",
    "Counter-Terrorism Financing",
    "Know Your Customer",
    "Individual Identification Number",
    "Electronic Digital Signature",
    "Temporary Residence Permit",
    "Value Added Tax",
    "Controlled Foreign Companies",
    "Monthly Calculation Index",
    "Voluntary Carbon Market",
    "Добровольный углеродный рынок",
    "Ерікті көміртегі нарығы",
    "Emissions Trading System",
    "Система торговли выбросами",
    "Шығарындылармен сауда жүйесі",
    "Renewable Energy Certificate",
    "International Renewable Energy Certificate",
    "Core Carbon Principles",
    "Environmental, Social, and Governance",
    "Federation of Euro-Asian Stock Exchanges",
    "General Council for Islamic Banks and Financial Institutions",
)
_DOMAIN_HOMOPHONES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bаққу\b", re.IGNORECASE), "АХҚО"),
    (re.compile(r"\bахко\b", re.IGNORECASE), "АХҚО"),
    (re.compile(r"\bмфса\b", re.IGNORECASE), "МФЦА"),
)
_KZ_FOLD = str.maketrans({
    "ә": "а",
    "ғ": "г",
    "қ": "к",
    "ң": "н",
    "ө": "о",
    "ұ": "у",
    "ү": "у",
    "һ": "х",
    "і": "и",
    "Ә": "а",
    "Ғ": "г",
    "Қ": "к",
    "Ң": "н",
    "Ө": "о",
    "Ұ": "у",
    "Ү": "у",
    "Һ": "х",
    "І": "и",
})


def _clean_meaning(value: str) -> str:
    cleaned = _PAREN_RE.sub("", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned


def _extract_aliases(*values: str) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        for content in _PAREN_CONTENT_RE.findall(value or ""):
            for alias in re.split(r"[,/;]", content):
                cleaned = re.sub(r"\s+", " ", alias).strip(" .")
                if not cleaned or len(cleaned) > 18:
                    continue
                key = _abbr_key(cleaned)
                if not key or key in seen:
                    continue
                seen.add(key)
                aliases.append(cleaned)
    return aliases


def _verified_alias_fields(abbr: str) -> dict[str, str]:
    by_lang = _STT_CONTEXT_VERIFIED_ALIASES.get(abbr, {})
    aliases: list[str] = [abbr]
    seen: set[str] = {_abbr_key(abbr)}
    fields: dict[str, str] = {}
    for lang in ("en", "ru", "kk"):
        lang_aliases: list[str] = []
        for alias in by_lang.get(lang, []):
            alias = re.sub(r"\s+", " ", alias or "").strip(" .")
            key = _abbr_key(alias)
            if not alias or not key:
                continue
            lang_aliases.append(alias)
            if key not in seen:
                seen.add(key)
                aliases.append(alias)
        fields[f"{lang}_aliases"] = "\t".join(lang_aliases)
    fields["aliases"] = "\t".join(aliases)
    return fields


def _apply_entry_overrides(entry: dict[str, str]) -> dict[str, str]:
    override = _STT_CONTEXT_ENTRY_OVERRIDES.get(entry["abbr"], {})
    for key in ("en", "ru", "kk", "category"):
        if override.get(key):
            entry[key] = override[key]
    entry.update(_verified_alias_fields(entry["abbr"]))
    return entry


@lru_cache(maxsize=1)
def abbreviation_entries() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    category = "uncategorized"
    try:
        lines = ABBR_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") and "──" in stripped:
            category = stripped.strip("# ─").strip().lower()
            continue
        if not stripped or stripped.startswith("#") or "|" not in stripped:
            continue
        parts = _PIPE_RE.split(stripped, maxsplit=3)
        if len(parts) < 4:
            continue
        abbr, en_raw, ru_raw, kk_raw = [part.strip() for part in parts[:4]]
        if not abbr:
            continue
        entry = {
            "abbr": abbr,
            "en": _clean_meaning(en_raw),
            "ru": _clean_meaning(ru_raw),
            "kk": _clean_meaning(kk_raw),
            "category": category,
        }
        entries.append(_apply_entry_overrides(entry))
    return entries


def stt_context_entries() -> list[dict[str, str]]:
    return [
        entry
        for entry in abbreviation_entries()
        if entry["abbr"] not in _STT_CONTEXT_EXCLUDED_ABBRS
    ]


def excluded_stt_context_entries() -> list[dict[str, str]]:
    return [
        entry
        for entry in abbreviation_entries()
        if entry["abbr"] in _STT_CONTEXT_EXCLUDED_ABBRS
    ]


def stt_context_groups() -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {
        _STT_CONTEXT_CATEGORY_LABELS[category]: []
        for category in _STT_CONTEXT_CATEGORY_ORDER
    }
    for entry in stt_context_entries():
        label = _STT_CONTEXT_CATEGORY_LABELS.get(entry["category"], entry["category"])
        grouped.setdefault(label, []).append(entry)
    return {label: entries for label, entries in grouped.items() if entries}


def _entry_aliases(entry: dict[str, str]) -> list[str]:
    aliases: list[str] = []
    seen = {_abbr_key(entry["abbr"])}
    for alias in entry.get("aliases", "").split("\t"):
        alias = alias.strip()
        key = _abbr_key(alias)
        if alias and key and key not in seen:
            seen.add(key)
            aliases.append(alias)
    return aliases


def stt_previous_text(max_chars: int = 900) -> str:
    priority = [
        "AIFC", "AFSA", "AIFCA", "AIX", "IAC", "FinTech Lab", "Expat Centre",
        "Astana International Financial Centre", "Astana Financial Services Authority",
        "Astana International Exchange", "International Arbitration Centre",
        "МФЦА", "АХҚО", "FinTech", "KYC", "AML", "CTF", "IIN", "EDS", "VAT",
        "VCM", "ETS", "GHG", "ESG", "KZT", "USD",
    ]
    seen: set[str] = set()
    hints: list[str] = []
    for item in priority:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            hints.append(item)
    for entry in stt_context_entries():
        for value in (entry["abbr"], entry["en"], entry["ru"], entry["kk"]):
            if not value:
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            hints.append(value)
            if len(", ".join(hints)) >= max_chars:
                return ", ".join(hints)[:max_chars]
    return ", ".join(hints)[:max_chars]


def merge_stt_context_text(custom_text: str = "", max_chars: int | None = None) -> str:
    blocks: list[str] = []
    custom_text = re.sub(r"\s+", " ", custom_text or "").strip()
    known_terms = {
        item.casefold()
        for item in stt_keyterms(max_terms=1000, max_words=20, max_chars=120)
    }
    custom_terms = [
        item.strip()
        for item in re.split(r"[,;\n]", custom_text)
        if item.strip()
    ]
    extra_terms = [
        item
        for item in custom_terms
        if item.casefold() not in known_terms
    ]
    if extra_terms:
        blocks.append("Additional terms: " + ", ".join(extra_terms))

    for label, entries in stt_context_groups().items():
        lines: list[str] = []
        for entry in entries:
            aliases = _entry_aliases(entry)
            alias_text = f" | aliases: {', '.join(aliases)}" if aliases else ""
            lines.append(
                f"{entry['abbr']} = {entry['en']} | {entry['ru']} | {entry['kk']}{alias_text}"
            )
        if lines:
            blocks.append(f"{label}:\n" + "\n".join(lines))

    text = "\n\n".join(blocks)
    if max_chars is not None:
        return text[:max_chars]
    return text


def stt_keyterms(max_terms: int = 100, max_words: int = 5, max_chars: int = 50) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    entries = stt_context_entries()
    candidate_groups: list[list[str]] = [
        [entry["abbr"] for entry in entries],
        [alias for entry in entries for alias in _entry_aliases(entry)],
        list(_STT_CONTEXT_EXTRA_TERMS),
    ]
    for values in candidate_groups:
        for value in values:
            value = re.sub(r"\s+", " ", value or "").strip(" .")
            if not value:
                continue
            if len(value) > max_chars or len(value.split()) > max_words:
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            terms.append(value)
            if len(terms) >= max_terms:
                return terms
    return terms


def _letter_pattern(abbr: str) -> re.Pattern[str] | None:
    if not re.fullmatch(r"[A-ZА-ЯЁӘҒҚҢӨҰҮҺІ]{2,10}", abbr):
        return None
    pieces = [re.escape(char) for char in abbr]
    return re.compile(r"\b" + r"[\s.\-]*".join(pieces) + r"\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def _transcript_patterns() -> list[tuple[re.Pattern[str], str]]:
    patterns: list[tuple[re.Pattern[str], str]] = []
    for entry in abbreviation_entries():
        abbr = entry["abbr"]
        pattern = _letter_pattern(abbr)
        if pattern is not None:
            patterns.append((pattern, abbr))
    return patterns


def _aliases_for_entry(entry: dict[str, str]) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in entry.get("aliases", "").split("\t"):
        value = value.strip()
        key = _abbr_key(value)
        if value and key and key not in seen:
            seen.add(key)
            aliases.append(value)
    return aliases


def _canonical_for_language(entry: dict[str, str], language: str | None) -> str:
    lang = (language or "").lower().strip()
    if lang not in {"en", "ru", "kk"}:
        return entry["abbr"]
    aliases = [alias for alias in entry.get(f"{lang}_aliases", "").split("\t") if alias]
    if lang == "kk":
        for alias in aliases:
            if re.search(r"[ӘәҒғҚқҢңӨөҰұҮүҺһІі]", alias):
                return alias
    if lang == "ru":
        for alias in aliases:
            if re.search(r"[А-Яа-яЁё]", alias) and not re.search(r"[ӘәҒғҚқҢңӨөҰұҮүҺһІі]", alias):
                return alias
    if lang == "en" and re.fullmatch(r"[A-Za-z0-9-]{2,12}", entry["abbr"]):
        return entry["abbr"]
    if aliases:
        return aliases[0]
    return entry["abbr"]


@lru_cache(maxsize=8)
def _alias_replacements(language: str | None = None) -> list[tuple[re.Pattern[str], str]]:
    replacements: list[tuple[re.Pattern[str], str]] = []
    seen_patterns: set[str] = set()
    for entry in abbreviation_entries():
        canonical = _canonical_for_language(entry, language)
        for alias in _aliases_for_entry(entry):
            if not alias or _abbr_key(alias) == _abbr_key(canonical):
                continue
            pattern_src = _TOKEN_BOUNDARY.format(r"[\s.\-]*".join(re.escape(char) for char in alias))
            if pattern_src in seen_patterns:
                continue
            seen_patterns.add(pattern_src)
            replacements.append((re.compile(pattern_src, re.IGNORECASE), canonical))
    replacements.sort(key=lambda item: len(item[0].pattern), reverse=True)
    return replacements


@lru_cache(maxsize=1)
def _fuzzy_abbreviation_aliases() -> list[tuple[str, str, str]]:
    abbreviations: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for entry in abbreviation_entries():
        for alias in _aliases_for_entry(entry):
            if not 2 <= len(_abbr_key(alias)) <= 10:
                continue
            key = _abbr_key(alias)
            folded = _abbr_fold_key(alias)
            if not key or key in seen:
                continue
            seen.add(key)
            abbreviations.append((alias, key, folded))
    return abbreviations


def _abbr_key(value: str) -> str:
    return re.sub(r"[^\wӘәҒғҚқҢңӨөҰұҮүҺһІіЁё]+", "", value or "").casefold()


def _abbr_fold_key(value: str) -> str:
    return _abbr_key(value).translate(_KZ_FOLD)


def _canonical_for_alias(alias: str, language: str | None) -> str:
    alias_key = _abbr_key(alias)
    for entry in abbreviation_entries():
        if any(_abbr_key(candidate) == alias_key for candidate in _aliases_for_entry(entry)):
            return _canonical_for_language(entry, language)
    return alias


def _maybe_fuzzy_abbreviation(token: str, language: str | None = None) -> str:
    token_key = _abbr_key(token)
    if len(token_key) < 2 or len(token_key) > 12:
        return token
    if token_key.isdigit():
        return token
    folded_token_key = _abbr_fold_key(token)

    best_abbr = ""
    best_score = 0.0
    for abbr, abbr_key, folded_abbr_key in _fuzzy_abbreviation_aliases():
        if not abbr_key:
            continue
        if abs(len(token_key) - len(abbr_key)) > 2:
            continue
        score = max(
            SequenceMatcher(None, token_key, abbr_key).ratio(),
            SequenceMatcher(None, folded_token_key, folded_abbr_key).ratio(),
        )
        if score > best_score:
            best_score = score
            best_abbr = abbr

    if best_score < _FUZZY_THRESHOLD or not best_abbr:
        return token
    if token_key == _abbr_key(best_abbr):
        return _canonical_for_alias(best_abbr, language)
    # Near-miss replacements are limited to acronym-like words to avoid changing
    # normal short words that happen to resemble a short abbreviation.
    if token.isupper() or any(char.isdigit() for char in token) or len(token_key) >= 4:
        return _canonical_for_alias(best_abbr, language)
    return token


def normalize_transcript_abbreviations(text: str, language: str | None = None) -> str:
    normalized = text or ""
    if not normalized.strip():
        return ""
    for pattern, replacement in _DOMAIN_HOMOPHONES:
        normalized = pattern.sub(_canonical_for_alias(replacement, language), normalized)
    for pattern, replacement in _alias_replacements(language):
        normalized = pattern.sub(replacement, normalized)
    for pattern, abbr in _transcript_patterns():
        normalized = pattern.sub(_canonical_for_alias(abbr, language), normalized)
    normalized = re.sub(r"\bA\s+IFC\b", _canonical_for_alias("AIFC", language), normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bA\s+FSA\b", _canonical_for_alias("AFSA", language), normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bA\s+IX\b", _canonical_for_alias("AIX", language), normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bI\s+AC\b", _canonical_for_alias("IAC", language), normalized, flags=re.IGNORECASE)
    normalized = _WORD_RE.sub(lambda match: _maybe_fuzzy_abbreviation(match.group(0), language), normalized)
    return re.sub(r"\s+", " ", normalized).strip()


@lru_cache(maxsize=8)
def _spoken_replacements(language: str) -> list[tuple[re.Pattern[str], str]]:
    lang_key = "ru" if language == "ru" else "kk" if language == "kk" else "en"
    replacements: list[tuple[re.Pattern[str], str]] = []
    seen_patterns: set[str] = set()
    for entry in abbreviation_entries():
        meaning = entry.get(lang_key) or entry.get("en") or ""
        meaning = meaning.strip()
        if not meaning:
            continue
        for alias in _aliases_for_entry(entry):
            pattern_src = _TOKEN_BOUNDARY.format(re.escape(alias))
            if pattern_src in seen_patterns:
                continue
            seen_patterns.add(pattern_src)
            replacements.append((re.compile(pattern_src, re.IGNORECASE), meaning))
    replacements.sort(key=lambda item: len(item[0].pattern), reverse=True)
    return replacements


def expand_spoken_abbreviations(text: str, language: str | None = None) -> str:
    expanded = text or ""
    if not expanded.strip():
        return ""
    lang = "ru" if language == "ru" else "kk" if language == "kk" else "en"
    for pattern, replacement in _spoken_replacements(lang):
        expanded = pattern.sub(replacement, expanded)
    return re.sub(r"\s+", " ", expanded).strip()


def spoken_abbreviation_rules(language: str, max_items: int = 32) -> str:
    lang_key = "ru" if language == "ru" else "kk" if language == "kk" else "en"
    rows: list[str] = []
    for entry in abbreviation_entries()[:max_items]:
        meaning = entry.get(lang_key) or entry.get("en") or ""
        if not meaning:
            continue
        rows.append(f"- {entry['abbr']}: write as \"{meaning}\" in spoken.")
    if not rows:
        return ""
    return (
        "Spoken abbreviation expansion map. In the spoken field, do not write the bare abbreviation; "
        "write the expansion so TTS pronounces it naturally. In details, keep the standard abbreviation.\n"
        + "\n".join(rows)
    )
