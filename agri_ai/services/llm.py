"""Claude integration for photo diagnosis and conversational crop advice.

The model is always grounded on the platform's own knowledge base, which is sent
as a stable, cached system prompt. Every caller has a rule-based fallback, so an
outage, a missing API key or a refusal degrades the service instead of breaking it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from functools import lru_cache
from typing import Protocol

import anthropic

from ..config import get_settings
from ..knowledge import get_kb

log = logging.getLogger("agri_ai.llm")

LANGUAGE_NAMES = {"en": "English", "sw": "Kiswahili", "fr": "French"}


class LLMUnavailable(Exception):
    """The model could not produce a usable answer; callers fall back to rules."""


class LLM(Protocol):
    def diagnose_image(self, image: bytes, media_type: str, *, crop: str | None, context: str, lang: str) -> dict: ...

    def answer(self, question: str, *, context: str, lang: str, max_chars: int) -> str: ...


DIAGNOSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "image_usable": {"type": "boolean", "description": "False if the photo is too blurry, dark, distant or not of a plant to judge."},
        "crop_detected": {"type": "string", "description": "Crop id from the reference list, or 'unknown'."},
        "healthy": {"type": "boolean"},
        "pest_id": {
            "type": "string",
            "description": "Pest/disease id from the reference list; 'other' for a problem not in the list (e.g. nutrient deficiency); 'none' if healthy.",
        },
        "label": {"type": "string", "description": "Common name of the problem, in the farmer's language."},
        "confidence": {"type": "number", "description": "Probability 0-1 that pest_id is correct."},
        "severity": {"type": "string", "enum": ["none", "low", "medium", "high", "critical"]},
        "visible_evidence": {"type": "string", "description": "The symptoms you can actually see in the photo."},
        "alternatives": {"type": "array", "items": {"type": "string"}, "description": "Other plausible pest ids."},
        "actions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-4 concrete actions, most important first, in the farmer's language.",
        },
        "needs_expert": {"type": "boolean", "description": "True if an extension officer should confirm (uncertain, notifiable, or high stakes)."},
        "farmer_message": {
            "type": "string",
            "description": "One SMS (max 300 characters) in the farmer's language summarising the problem and first action.",
        },
    },
    "required": [
        "image_usable",
        "crop_detected",
        "healthy",
        "pest_id",
        "label",
        "confidence",
        "severity",
        "visible_evidence",
        "alternatives",
        "actions",
        "needs_expert",
        "farmer_message",
    ],
}

BASE_SYSTEM_PROMPT = """You are the agronomy assistant of a service that supports smallholder farmers in Africa and South Asia. \
Partners such as mobile operators, farmer co-operatives and NGOs deliver your answers by SMS, USSD, WhatsApp and a basic \
smartphone app, so most farmers read your words on a small screen, often in a second language, and pay for data.

How to help:
- Be practical and specific to the farmer's crop, growth stage, location and weather given in the context.
- Prefer integrated pest management: prevention and cultural control first, then biological options, then chemicals.
- Name chemical options by active ingredient, never by brand, and only ones listed in the reference below or widely registered \
for that use. Always remind the farmer to follow the label, wear protection and respect the pre-harvest interval. Never suggest \
banned or highly hazardous pesticides, mixing products, or unsafe doses.
- Favour low-cost actions a farmer with one hectare and little cash can take this week.
- Say plainly when you are unsure, and recommend the farmer's extension officer for notifiable diseases (marked NOTIFIABLE) or \
whenever a wrong answer could cost the harvest.
- Write in the farmer's language using simple words. No markdown, no bullet symbols, no emojis.

The reference knowledge base below is curated by the platform's agronomists. Use its ids when you classify a problem."""


def _kb_reference() -> str:
    kb = get_kb()
    lines = ["CROPS:"]
    for c in kb.crops.values():
        stages = "; ".join(f"{s.name} (day {s.start}-{s.end}): {' / '.join(s.tasks)}" for s in c.stages)
        lines.append(
            f"- {c.id}: spacing {c.spacing}. Fertiliser: {c.fertilizer} Water-critical stages: "
            f"{', '.join(c.critical_water_stages)}. Stages: {stages}. Main pests: {', '.join(c.pests)}."
        )
    lines.append("\nPESTS AND DISEASES:")
    for p in kb.pests.values():
        flag = " NOTIFIABLE" if p.notifiable else ""
        lines.append(
            f"- {p.id} [{p.type}, crops: {', '.join(p.crops)}, severity {p.severity}{flag}] {p.names['en']}. "
            f"Symptoms: {p.symptoms} Cultural: {' / '.join(p.cultural)}. Biological: {' / '.join(p.biological) or 'none'}. "
            f"Chemical: {' / '.join(p.chemical) or 'none'}."
        )
    return "\n".join(lines)


@lru_cache
def system_blocks() -> tuple[dict, ...]:
    # One stable block: identical bytes on every request so the prompt cache hits.
    return (
        {
            "type": "text",
            "text": BASE_SYSTEM_PROMPT + "\n\n" + _kb_reference(),
            "cache_control": {"type": "ephemeral"},
        },
    )


class ClaudeLLM:
    def __init__(self, api_key: str, model: str, effort: str = "medium") -> None:
        self.client = anthropic.Anthropic(api_key=api_key, timeout=90.0, max_retries=2)
        self.model = model
        self.effort = effort

    def _create(self, messages: list[dict], *, schema: dict | None = None, max_tokens: int = 16000) -> str:
        output_config: dict = {"effort": self.effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=list(system_blocks()),
                messages=messages,
                output_config=output_config,
                # Re-run a policy decline on Anthropic's recommended fallback model server-side.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable(f"rate limited: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"connection error: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMUnavailable("model declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMUnavailable("response truncated")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise LLMUnavailable("empty response")
        usage = response.usage
        log.info(
            "llm ok model=%s in=%s out=%s cache_read=%s",
            response.model,
            usage.input_tokens,
            usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", None),
        )
        return text

    def diagnose_image(self, image: bytes, media_type: str, *, crop: str | None, context: str, lang: str) -> dict:
        language = LANGUAGE_NAMES.get(lang, "English")
        prompt = (
            f"A farmer sent this photo asking what is wrong with their {crop or 'crop'}.\n"
            f"Farmer context:\n{context}\n\n"
            f"Diagnose the main problem visible in the photo. Write label, actions and farmer_message in {language}."
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(image).decode()}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        text = self._create(messages, schema=DIAGNOSIS_SCHEMA)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable("invalid JSON from model") from exc

    def answer(self, question: str, *, context: str, lang: str, max_chars: int) -> str:
        language = LANGUAGE_NAMES.get(lang, "English")
        prompt = (
            f"Farmer context:\n{context}\n\nFarmer's question: {question}\n\n"
            f"Answer in {language} in at most {max_chars} characters of plain text."
        )
        return self._create([{"role": "user", "content": prompt}], max_tokens=16000)


_llm: LLM | None = None
_llm_overridden = False


def get_llm() -> LLM | None:
    """The configured model client, or ``None`` when AI is disabled (rules only)."""
    global _llm
    if _llm_overridden:
        return _llm
    if _llm is None:
        s = get_settings()
        if s.llm_available:
            _llm = ClaudeLLM(s.anthropic_api_key, s.llm_model, os.environ.get("AGRI_LLM_EFFORT", "medium"))
    return _llm


def set_llm(llm: LLM | None) -> None:
    """Override the model client (tests, or to force rules-only mode)."""
    global _llm, _llm_overridden
    _llm, _llm_overridden = llm, True


def reset_llm() -> None:
    global _llm, _llm_overridden
    _llm, _llm_overridden = None, False
