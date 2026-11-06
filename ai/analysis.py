"""Period/workout/brief analysis: FactSheet -> validated AnalysisOut (LLM, cache or template)."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic

from core import user
from core.config import settings

from . import client as llm
from .facts import FactSheet
from .guard import check
from .prompts import ANALYSIS_SYSTEM, PROMPT_VERSION, analysis_user_message
from .schemas import LIMITS, AnalysisOut, Item

log = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    out: AnalysisOut
    source: str  # "llm" | "cache" | "template"
    note: str | None = None  # why the template was used

    def to_dict(self) -> dict[str, Any]:
        return {"out": self.out.model_dump(), "source": self.source, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnalysisResult:
        return cls(AnalysisOut.model_validate(d["out"]), d["source"], d.get("note"))


def mode_for(sheet: FactSheet) -> str:
    if sheet.metric == "workout":
        return "workout"
    if sheet.metric == "overview":
        return "brief"
    return "day" if sheet.period == "day" else "period"


def cache_key(sheet: FactSheet, model: str, profile: str = "") -> str:
    h = hashlib.sha256(f"{PROMPT_VERSION}|{model}|{profile}|{sheet.render()}".encode()).hexdigest()[:32]
    return user.key(f"{sheet.metric}:{sheet.period}:{h}")


def template(sheet: FactSheet, mode: str) -> AnalysisOut:
    """Deterministic analysis from facts: used without data, over budget, or when the LLM fails."""
    ranked = sheet.ranked()
    (_, ihi), (_, ahi) = LIMITS[mode]
    head = [f.text for f in ranked[:2]]
    return AnalysisOut(
        summary=". ".join(t.rstrip(".") for t in head) + "." if head else "Данных за период нет.",
        insights=[Item(text=f.text + ".", fact_ids=[f.id]) for f in ranked[2:2 + ihi] if f.level != "low"][:ihi],
        advice=[Item(text=a.text, fact_ids=[a.id]) for a in sheet.advice[:ahi]],
        caveat=sheet.caveats[0] if sheet.caveats else "",
    )


def _call(client: anthropic.Anthropic, model: str, effort: str, messages: list[dict[str, Any]],
          route: str) -> AnalysisOut:
    resp = client.messages.parse(
        model=model,
        max_tokens=6000,
        system=[{"type": "text", "text": ANALYSIS_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        output_format=AnalysisOut,
        output_config={"effort": effort},
    )
    llm.record(route, model, resp.usage)
    if resp.stop_reason == "refusal":
        raise llm.LLMRefused()
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise ValueError(f"incomplete structured output (stop_reason={resp.stop_reason})")
    return resp.parsed_output


def analyze(sheet: FactSheet, *, force: bool = False, client: anthropic.Anthropic | None = None,
            route: str | None = None) -> AnalysisResult:
    from core.profile import render_current
    from storage.db import get_analysis, put_analysis

    cfg = settings()
    mode = mode_for(sheet)
    if not sheet.has_data:
        return AnalysisResult(template(sheet, mode), "template", "нет данных")
    model = cfg.llm_model_analysis
    effort = cfg.llm_effort_brief if mode == "brief" else cfg.llm_effort_analysis
    profile = render_current()
    key = cache_key(sheet, model, profile)
    if not force and (cached := get_analysis(key)):
        return AnalysisResult(AnalysisOut.model_validate(cached), "cache")
    try:
        llm.check_budget()
        client = client or llm.sync_client()
        content = analysis_user_message(sheet.render(), mode, profile)
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        out = _call(client, model, effort, messages, route or mode)
        errors = check(out, sheet, mode, profile)
        if errors:
            log.info("analysis rejected by guard: %s", errors)
            messages += [{"role": "assistant", "content": out.model_dump_json()},
                         {"role": "user", "content": "В ответе есть ошибки, исправь их и верни JSON заново:\n- "
                                                     + "\n- ".join(errors)}]
            out = _call(client, model, effort, messages, route or mode)
            errors = check(out, sheet, mode, profile)
            if errors:
                log.warning("analysis rejected twice, using template: %s", errors)
                return AnalysisResult(template(sheet, mode), "template", "ответ модели не прошёл проверку")
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the template
        log.warning("analysis failed: %s", exc)
        return AnalysisResult(template(sheet, mode), "template", llm.describe_error(exc))
    put_analysis(key, json.loads(out.model_dump_json()))
    return AnalysisResult(out, "llm")
