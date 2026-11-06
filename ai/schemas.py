"""Structured output of the analysis call."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Item(BaseModel):
    text: str = Field(description="одно-два предложения по-русски, простым языком")
    fact_ids: list[str] = Field(description="id фактов или советов (F1, A2...), на которых основан пункт")


class AnalysisOut(BaseModel):
    summary: str = Field(description="2–3 предложения: главное за период")
    insights: list[Item] = Field(description="закономерности, выбросы и аномалии, понятные в жизни")
    advice: list[Item] = Field(description="конкретные действия")
    caveat: str = Field(default="", description="одна короткая оговорка о данных, если важна; иначе пусто")


# (min, max) insights and advice per analysis mode.
LIMITS = {
    "day": ((1, 3), (2, 4)),
    "period": ((2, 5), (3, 5)),
    "workout": ((1, 3), (2, 4)),
    "brief": ((0, 2), (1, 2)),
}
