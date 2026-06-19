from __future__ import annotations

from collections.abc import AsyncIterator
import json
import logging
import os
from time import perf_counter
from typing import Any

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None
    import google.generativeai as legacy_genai
else:
    legacy_genai = None

from ..utils.language import language_name
from ..logging_config import log_event, preview_text
from ..settings import GEMINI_MAX_OUTPUT_TOKENS, GEMINI_MODEL, GEMINI_TEMPERATURE, SYSTEM_PROMPT
from .rag import _ANSWER_SYSTEM, _build_context
from .memory import format_conversation_memory

log = logging.getLogger(__name__)

# Reuse ONE genai client process-wide. Creating a fresh genai.Client() per turn added
# ~240ms/turn, and the FIRST call on any new client pays a ~6s TLS/auth cold start — the
# source of the TTFT spikes. A single warm, reused client keeps the connection hot so that
# cold start happens once (prewarmed at boot via prewarm_gemini), not on a user's turn.
_GENAI_CLIENT = None


def _get_genai_client():
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        _GENAI_CLIENT = genai.Client()
    return _GENAI_CLIENT


async def prewarm_gemini() -> None:
    """Warm the shared genai client connection at startup so the first real turn doesn't
    eat the ~6s cold-start. Cheap (1 output token)."""
    if genai is None or types is None:
        return
    started = perf_counter()
    try:
        client = _get_genai_client()
        cfg = types.GenerateContentConfig(max_output_tokens=1)
        stream = await client.aio.models.generate_content_stream(
            model=GEMINI_MODEL, contents=[{"role": "user", "parts": [{"text": "hi"}]}], config=cfg
        )
        async for _chunk in stream:
            break
        log.info("gemini prewarm complete in %dms", int((perf_counter() - started) * 1000))
    except Exception as exc:
        log.warning("gemini prewarm failed: %s", exc)


def _extract_json_payload(raw: str) -> dict[str, Any] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.startswith("{"):
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _extract_json_from_wrapped(raw: str) -> dict[str, Any] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    start = raw.find("{")
    if start < 0:
        return None
    end = raw.rfind("}")
    if end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except Exception:
        return None


def _build_contract_section() -> str:
    return ""


def _fallback_instruction(language: str) -> str:
    fallback = {
        "ru": "Извините, я не нашел надежный ответ в своей базе знаний. Пожалуйста, посетите aifc.kz.",
        "kk": "Кешіріңіз, мен білім базасынан сенімді жауап таба алмадым. aifc.kz сайтына кіріңіз.",
        "zh": "抱歉，我没有在知识库中找到可靠答案。请访问 aifc.kz。",
        "en": "Sorry, I couldn't find a reliable answer in my knowledge base. Please visit aifc.kz.",
    }.get(language, "Sorry, I couldn't find a reliable answer in my knowledge base. Please visit aifc.kz.")
    return (
        "Fallback rule when the retrieved context does not reliably answer the question:\n"
        f'- spoken must be exactly or very close to: "{fallback}"\n'
        f'- chat must be exactly or very close to: "{fallback}"\n'
    )


# Compact abbreviation -> full-spoken-form hints injected into the spoken-field rules so the
# LLM voices full names, not letters. (Full list: data/abbr.txt; the LLM expands others it knows.)
_ABBR_HINT = (
    "AIFC = Astana International Financial Centre; AFSA = Astana Financial Services Authority; "
    "AIX = Astana International Exchange; IAC = AIFC International Arbitration Centre; "
    "AIFCA = AIFC Authority; AEC = AIFC Energy Centre; AFD = Astana Finance Days; "
    "AML = Anti-Money Laundering; CTF = Counter-Terrorism Financing; KYC = Know Your Customer; "
    "IIN = Individual Identification Number; EDS = Electronic Digital Signature; VAT = Value Added Tax; "
    "JSC = Joint Stock Company; CEO = Chief Executive Officer; GDP = Gross Domestic Product; "
    "ESG = Environmental, Social and Governance; MCI = Monthly Calculation Index; "
    "TRP = Temporary Residence Permit; KZT = Kazakhstani tenge; USD = US dollars; "
    "RK = Republic of Kazakhstan; UAE = United Arab Emirates; EU = European Union; "
    "UK = United Kingdom; USA = United States; UN = United Nations"
)


def build_prompt(
    query: str,
    language: str,
    chunks: list[dict],
    history: list[dict[str, str]],
    conversation_memory: dict | None = None,
    faq_seed: str = "",
    expert_mode: bool = False,
    needs_widget: bool = False,
) -> tuple[list[dict[str, str]], str]:
    history_msgs = [
        {"role": "user" if item["role"] == "user" else "assistant", "content": item["content"]}
        for item in history[-6:]
    ]
    context_chunks = list(chunks)
    if faq_seed:
        context_chunks = [
            {
                "text": f"[Pre-verified FAQ answer — treat as confirmed]: {faq_seed}",
                "source_file": "FAQ",
                "domain": "faq",
                "rerank_score": 1.0,
            },
            *context_chunks,
        ]
    context = _build_context(context_chunks)
    memory_text = format_conversation_memory(conversation_memory)
    lang_line = f"Answer language: {language_name(language)}\n"
    mode_line = (
        "Expert mode: use specialist AIFC or finance terms directly.\n"
        if expert_mode
        else "Default mode: use clear professional wording.\n"
    )
    widget_line = "Widget mode: keep chat markdown compact and easy to display.\n" if needs_widget else ""
    contract_section = _build_contract_section()
    fallback_rule = _fallback_instruction(language)
    prompt_user = (
        f"{lang_line}"
        f"{mode_line}"
        f"{widget_line}"
        f"{contract_section}"
        "Core answer policy from the production AIFC retrieval pipeline:\n"
        f"{_ANSWER_SYSTEM}\n\n"
        + "Use only the retrieved context below.\n"
        "Do not use outside knowledge.\n"
        "Do not infer facts that are not clearly supported by the retrieved context.\n"
        "If the retrieved context is missing the answer or is unclear, use the fallback rule below.\n\n"
        "Critical relevance rules:\n"
        "- Select only context blocks that directly answer the user's exact question.\n"
        "- Do not combine unrelated services, departments, or contact blocks just because they were retrieved together.\n"
        "- If different context blocks discuss different departments or topics, use only the blocks matching the user's question.\n"
        "- Include emails, phone numbers, named contact persons, physical addresses, schedules, office hours, or department contact blocks ONLY if the user explicitly asks for contact details, email, phone, address, schedule, office hours, or how to contact someone.\n"
        "- If the user asks broadly what you can help with, answer with service/topic categories only and omit all contact details.\n\n"
        f"{fallback_rule}\n"
        "Return one valid JSON object only with exactly these top-level keys:\n"
        "- spoken (string; required): compact voice answer for TTS/avatar.\n"
        "- chat (string; required): complete chat answer for the transcript UI.\n\n"
        "Rules for spoken:\n"
        "- Provide a complete spoken summary (3–5 sentences) suitable for TTS/avatar playback.\n"
        "- First sentence: one direct sentence that answers the user's exact question — state what, how, who, or when.\n"
        "- Following sentences: cover the key steps, requirements, facts, fees, dates, or relevant details from the context.\n"
        "- Use plain speakable text only: no markdown, bullets, lists, JSON, citations, or source labels.\n"
        "- Do not start with phrases like \"according to the context\" or \"based on the provided information\".\n"
        "- Include the exact AIFC body, department, service, fee, threshold, date, or timeframe when it directly answers the question.\n"
        "- spoken is read by TEXT-TO-SPEECH, so it MUST be already pronounceable — there is NO\n"
        "  number/abbreviation normalizer downstream. Therefore in spoken you MUST:\n"
        "  * Write EVERY number, date, ordinal, money amount, percentage and range as spoken\n"
        "    WORDS in the answer's language — never digits or symbols. Examples (English):\n"
        "    \"9-10\" -> \"the ninth to the tenth\"; \"2026\" -> \"twenty twenty-six\"; \"$5M\" ->\n"
        "    \"five million dollars\"; \"50%\" -> \"fifty percent\"; \"3.5\" -> \"three point five\";\n"
        "    \"14:30\" -> \"two thirty PM\". In Chinese use Chinese number words.\n"
        "  * Expand EVERY abbreviation/acronym to its full spoken form in the answer's language\n"
        "    (do NOT speak the letters). Known AIFC expansions: " + _ABBR_HINT + ". Expand any\n"
        "    other acronym you recognise; if unsure, use the natural full name.\n"
        "  * Say emails/URLs naturally (for example, \"aifc dot k z\").\n\n"
        "Rules for chat:\n"
        "- chat keeps NORMAL WRITTEN form: digits, %, $, dates, and standard abbreviations\n"
        "  (AIFC, AFSA, AIX) are fine and preferred for readability.\n"
        "- Provide the full detailed answer for the chat panel.\n"
        "- Markdown is allowed: short paragraphs, bullets, and small headings are fine.\n"
        "- Keep it focused on the user's exact question and retrieved facts.\n"
        "- Do not repeat the same idea just to fill space.\n"
        "- Include only supported facts from the retrieved context.\n"
        "- If useful, include relevant names, requirements, steps, fees, thresholds, dates, contacts, and citations from the retrieved context.\n"
        "- If the utterance is noisy, empty, or malformed, set both spoken and chat to the clear-question fallback in the user's language.\n\n"
        "Rules for multilingual website context:\n"
        "- Retrieved website or PDF context may be in English, Russian, Kazakh, or mixed languages.\n"
        "- Answer in the user's language and translate supported facts from retrieved context when needed.\n"
        "- Preserve official names, legal terms, and standard abbreviations accurately.\n"
        "- Do not generate follow-up questions.\n\n"
        "If no reliable answer is available from retrieved context, do not guess. Use the fallback rule.\n\n"
        f"Additional system guidance:\n{SYSTEM_PROMPT}\n\n"
        f"Persistent conversation memory:\n{memory_text}\n\n"
        f"User question:\n{query}\n\n"
        f"Retrieved context:\n{context}"
    )
    return history_msgs, prompt_user


async def stream_answer(history_msgs: list[dict[str, str]], prompt: str) -> AsyncIterator[str]:
    started = perf_counter()
    chunk_count = 0
    output_chars = 0
    provider = "google_genai" if genai is not None and types is not None else "google_generativeai_legacy"
    log_event(
        log,
        "llm_stream_start",
        provider=provider,
        model=GEMINI_MODEL,
        temperature=GEMINI_TEMPERATURE,
        max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
        history_items=len(history_msgs),
        prompt_chars=len(prompt),
        prompt_preview=preview_text(prompt, 500),
    )
    try:
        if genai is not None and types is not None:
            client = _get_genai_client()
            config = types.GenerateContentConfig(
                temperature=GEMINI_TEMPERATURE,
                max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
            )
            async for chunk in await client.aio.models.generate_content_stream(
                model=GEMINI_MODEL,
                contents=[
                    {"role": "model", "parts": [{"text": _ANSWER_SYSTEM}]},
                    *[
                        {"role": "user" if item["role"] == "user" else "model", "parts": [{"text": item["content"]}]}
                        for item in history_msgs
                    ],
                    {"role": "user", "parts": [{"text": prompt}]},
                ],
                config=config,
            ):
                if chunk.text:
                    chunk_count += 1
                    output_chars += len(chunk.text)
                    yield chunk.text
            return

        async for text in _stream_answer_legacy(history_msgs, prompt):
            chunk_count += 1
            output_chars += len(text)
            yield text
    except Exception as exc:
        log_event(log, "llm_stream_failed", latency_ms=(perf_counter() - started) * 1000, error=exc, level=logging.ERROR)
        raise
    finally:
        log_event(
            log,
            "llm_stream_done",
            latency_ms=(perf_counter() - started) * 1000,
            provider=provider,
            model=GEMINI_MODEL,
            chunks=chunk_count,
            output_chars=output_chars,
        )


def _legacy_configure() -> None:
    if legacy_genai is None:
        raise RuntimeError("Gemini client is not installed")
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if api_key:
        legacy_genai.configure(api_key=api_key)


def _legacy_prompt(history_msgs: list[dict[str, str]], prompt: str) -> str:
    parts = [f"SYSTEM:\n{_ANSWER_SYSTEM}"]
    for item in history_msgs:
        role = "USER" if item["role"] == "user" else "ASSISTANT"
        parts.append(f"{role}:\n{item['content']}")
    parts.append(f"USER:\n{prompt}")
    return "\n\n".join(parts)


async def _stream_answer_legacy(history_msgs: list[dict[str, str]], prompt: str) -> AsyncIterator[str]:
    text = await _generate_legacy_text(
        _legacy_prompt(history_msgs, prompt),
        temperature=GEMINI_TEMPERATURE,
        max_output_tokens=min(GEMINI_MAX_OUTPUT_TOKENS, 900),
    )
    if text:
        yield text


async def _generate_legacy_text(prompt: str, *, temperature: float, max_output_tokens: int) -> str:
    _legacy_configure()
    model = legacy_genai.GenerativeModel(GEMINI_MODEL)
    response = await model.generate_content_async(
        prompt,
        generation_config={
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        },
        stream=False,
    )
    return (getattr(response, "text", "") or "").strip()
