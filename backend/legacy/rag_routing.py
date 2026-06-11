from __future__ import annotations

import re

GEMINI_PUBLIC_RAG_TOOL = "gemini_public_rag"
EXTERNAL_INTERNAL_RAG_TOOL = "external_internal_rag"

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі0-9]+")
_INTERNAL_STRONG_PHRASES = (
    "internal policy",
    "internal document",
    "internal docs",
    "internal memo",
    "staff policy",
    "employee policy",
    "employee benefits",
    "human resources",
    "sick leave",
    "access request",
    "service desk",
    "help desk",
    "внутренний документ",
    "внутренняя политика",
    "кадровая политика",
    "больничный лист",
    "служебная записка",
    "ішкі құжат",
    "ішкі саясат",
    "еңбек демалысы",
    "қызметкер саясаты",
)
_INTERNAL_QUERY_STEMS = {
    "internal", "employee", "staff", "hr", "payroll", "salary", "salaries", "leave",
    "vacation", "timesheet", "intranet", "onboarding", "offboarding", "procurement",
    "confidential", "private", "benefits", "expense", "reimbursement", "invoice",
    "vendor", "nda", "helpdesk", "jira", "confluence", "sharepoint", "vpn",
    "внутрен", "сотрудник", "персонал", "кадр", "зарплат", "оклад", "отпуск",
    "больнич", "табел", "интранет", "закуп", "служеб", "конфиденц",
    "ішкі", "қызметкер", "персонал", "кадр", "жалақы", "демалыс", "құпия",
    "шығын", "өтемақы", "сатып",
}


def _normalized_query(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.casefold())).strip()


def _tokens(text: str) -> set[str]:
    return {match.group(0).casefold() for match in _WORD_RE.finditer(text or "")}


def is_internal_query(query: str) -> bool:
    normalized = _normalized_query(query)
    if not normalized:
        return False
    if any(phrase in normalized for phrase in _INTERNAL_STRONG_PHRASES):
        return True
    tokens = _tokens(query)
    if tokens & _INTERNAL_QUERY_STEMS:
        return True
    return any(stem in normalized for stem in _INTERNAL_QUERY_STEMS if len(stem) >= 5)


def select_rag_tool(query: str) -> str:
    if is_internal_query(query):
        return EXTERNAL_INTERNAL_RAG_TOOL
    return GEMINI_PUBLIC_RAG_TOOL
