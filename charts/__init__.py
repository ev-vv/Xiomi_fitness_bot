"""Dashboard PNG renderers (matplotlib, Agg)."""

from __future__ import annotations

from typing import Any

from analytics.periods import PeriodView
from core.fmt import span

from . import heart, sleep, spo2, stress, workout

TITLES = {"sleep": "😴 Сон", "heart": "❤️ Пульс", "spo2": "🩸 Кислород (SpO2)", "stress": "🧠 Стресс"}
PERIODS = {"day": "День", "week": "Неделя", "month": "Месяц"}


def _strip_emoji(s: str) -> str:
    # The bundled fonts have no color emoji; keep titles plain text in images.
    return s.split(" ", 1)[1] if s[:1] and not s[:1].isalnum() else s


def render_period(view: PeriodView, kpis: list[tuple[str, str]], sleep_need: int) -> bytes:
    title = _strip_emoji(TITLES[view.metric])
    subtitle = f"{PERIODS[view.period]} · {span(view.start, view.end)}" + ("" if view.complete else " · день ещё идёт")
    if view.metric == "spo2":
        return spo2.chart(view, title, subtitle, kpis)
    if view.metric == "stress":
        return stress.chart(view, title, subtitle, kpis)
    mod = {"sleep": sleep, "heart": heart}[view.metric]
    if view.period == "day":
        return mod.day(view, title, subtitle, kpis)
    return mod.period(view, title, subtitle, kpis, sleep_need) if view.metric == "sleep" else mod.period(
        view, title, subtitle, kpis)


def render_workout(v: dict[str, Any], title: str, subtitle: str, kpis: list[tuple[str, str]]) -> bytes:
    return workout.chart(v, title, subtitle, kpis)
