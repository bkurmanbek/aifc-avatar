from __future__ import annotations

from collections.abc import AsyncIterator
import json
import os
from pathlib import Path
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

from .abbreviations import spoken_abbreviation_rules
from .language import language_name
from .original_backend import _ANSWER_SYSTEM, _build_context, format_conversation_memory
from .settings import GEMINI_MAX_OUTPUT_TOKENS, GEMINI_MODEL, GEMINI_TEMPERATURE, ROOT, SYSTEM_PROMPT


_PROMPT_MD_CANDIDATES = (
    ROOT.parent / "prompt.md",
    ROOT / "prompt.md",
)
_PIPELINE_CONTRACT_CACHE: str | None = None


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


def _load_pipeline_contract() -> str:
    global _PIPELINE_CONTRACT_CACHE
    if _PIPELINE_CONTRACT_CACHE is not None:
        return _PIPELINE_CONTRACT_CACHE

    for path in _PROMPT_MD_CANDIDATES:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            _PIPELINE_CONTRACT_CACHE = text
            return text

    _PIPELINE_CONTRACT_CACHE = ""
    return ""


def _build_contract_section() -> str:
    return ""


def _fallback_instruction(language: str) -> str:
    spoken = {
        "ru": "Извините, я не нашел надежный ответ в своей базе знаний. Пожалуйста, посетите aifc.kz.",
        "kk": "Кешіріңіз, мен білім базасынан сенімді жауап таба алмадым. aifc.kz сайтына кіріңіз.",
        "zh": "抱歉，我没有在知识库中找到可靠答案。请访问 aifc.kz。",
        "en": "Sorry, I couldn't find a reliable answer in my knowledge base. Please visit aifc.kz.",
    }.get(language, "Sorry, I couldn't find a reliable answer in my knowledge base. Please visit aifc.kz.")
    return (
        "Fallback rule when the retrieved context does not reliably answer the question:\n"
        "- spoken must be an empty string.\n"
        f'- details.summary must be exactly or very close to: "{spoken}"\n'
        f'- details.points must include: "{spoken}"\n'
        "- details.answer_kind must be \"fallback\" and follow_up_questions must be an empty array.\n"
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
) -> str:
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
    widget_line = "Widget mode: keep the structured details easy to display.\n" if needs_widget else ""
    contract_section = _build_contract_section()
    abbreviation_rules = spoken_abbreviation_rules(language)
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
        "Return one valid JSON object only.\n"
        "Allowed top-level keys:\n"
        "- details (object; required, this is the full structured answer and the backend will voice it)\n"
        "- control (object)\n"
        "- followups (optional array, fallback alias)\n"
        "- follow_up_questions (optional array)\n"
        "Do not add a separate spoken summary.\n\n"
        "Details generation is required.\n"
        "- Put the useful answer in details.\n"
        "- Use details.summary for the direct answer.\n"
        "- Use details.points for the most important facts, requirements, fees, thresholds, dates, contacts, and next steps that directly answer the user.\n"
        "- Use details.sections for procedures, comparisons, multi-part answers, or long answers.\n"
        "- Keep details bounded: at most five points and two short sections unless the user explicitly asks for exhaustive detail.\n"
        "- Do not repeat the same idea across summary, points, and sections.\n"
        "- Include only relevant supported details needed to answer the user's exact question.\n\n"
        "Expected control schema:\n"
        "{\n"
        '  "interrupt_ack": false,\n'
        '  "handoff_greeting": false\n'
        "}\n\n"
        "Rules for voice output:\n"
        "- The backend uses details for voice generation.\n"
        "- Do not create a separate short spoken summary or a spoken field.\n"
        "- The first sentence is mandatory: compact, direct, standalone, and it must answer the user's exact question immediately.\n"
        "- The first sentence must not start with background framing, caveats, or phrases like \"according to the context\".\n"
        "- Write all numbers as words in details. Never use digits in details.\n"
        "- In Chinese, use Chinese number characters for number words.\n"
        "- Details must be concise, complete enough, and speakable.\n"
        "- The first sentence must name the exact AIFC body, department, or service when the context provides it.\n"
        "- The first sentence must include exact numbers, fees, thresholds, timeframes, or statistics that directly answer the question, written as words.\n"
        "- For abbreviations in details, write the expansion or pronunciation text, not the bare abbreviation.\n"
        "- For website domains, write the normal domain form such as aifc.kz. Never spell domains letter-by-letter.\n"
        "- If the utterance is a noisy, empty, or malformed one, set details to {}.\n\n"
        f"{abbreviation_rules}\n\n"
        "Rules for multilingual website context:\n"
        "- Retrieved website or PDF context may be in English, Russian, Kazakh, or mixed languages.\n"
        "- Answer in the user's language and translate supported facts from retrieved context when needed.\n"
        "- Preserve official names, legal terms, and standard abbreviations accurately.\n"
        "- Use follow_up_questions only when they are genuinely useful; use an empty array for fallback.\n\n"
        "Rules for control:\n"
        "- interrupt_ack=true only when the input is a genuine new/interrupting query while answering.\n"
        "- handoff_greeting may be true for first turn greetings.\n\n"
        "If no reliable answer is available from retrieved context, do not guess. Use the fallback rule.\n\n"
        f"Additional system guidance:\n{SYSTEM_PROMPT}\n\n"
        f"Persistent conversation memory:\n{memory_text}\n\n"
        f"User question:\n{query}\n\n"
        f"Retrieved context:\n{context}"
    )
    return history_msgs, prompt_user


async def stream_answer(history_msgs: list[dict[str, str]], prompt: str) -> AsyncIterator[str]:
    if genai is not None and types is not None:
        client = genai.Client()
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
                yield chunk.text
        return

    async for text in _stream_answer_legacy(history_msgs, prompt):
        yield text


async def rewrite_spoken_text(text: str, language: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    cache_key = f"{language}|{text}"
    if cache_key in _REWRITE_CACHE:
        return _REWRITE_CACHE[cache_key]

    prompt = (
        f"Rewrite the following spoken answer in {language_name(language)}.\n"
        "Rules:\n"
        "- Return plain text only.\n"
        "- Keep the meaning and factual content.\n"
        "- Remove any markup, tags, stage directions, or non-speech annotations.\n"
        "- Convert every number, date, percentage, currency amount, and time into spoken words.\n"
        "- In Chinese, use Chinese number characters.\n"
        "- Keep it concise and natural for TTS.\n\n"
        f"Text:\n{text}"
    )
    if genai is not None and types is not None:
        client = genai.Client()
        config = types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=300,
        )
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            config=config,
        )
        rewritten = (response.text or "").strip()
    else:
        rewritten = await _generate_legacy_text(prompt, temperature=0.1, max_output_tokens=300)
    _REWRITE_CACHE[cache_key] = rewritten
    return rewritten


_REWRITE_CACHE: dict[str, str] = {}


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
