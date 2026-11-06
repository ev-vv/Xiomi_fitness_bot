"""Chat tools for a small model: few, high-level, enum parameters, and FactSheet-style text output."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from analytics.periods import latest_date, period_view, workout_list
from core import data
from core.fmt import day, dur, span

from .facts import (SPORTS, activity_facts, anomaly_metric, describe_anomaly, overview_facts, period_facts,
                    workout_facts)

_DATE = {"type": ["string", "null"], "description": "дата ГГГГ-ММ-ДД; null — последняя доступная"}
_METRIC = {"type": "string", "enum": ["sleep", "heart", "spo2", "stress", "activity"],
           "description": "sleep — сон, heart — пульс, spo2 — кислород, stress — стресс, activity — шаги и тренировки"}
_PERIOD = {"type": "string", "enum": ["day", "week", "month"],
           "description": "day — один день (для сна — ночь, закончившаяся в этот день), week — 7 дней, month — 30 дней, "
                          "заканчивающихся указанной датой"}


def _schema(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


TOOLS: list[dict[str, Any]] = [
    {"name": "get_overview", "strict": True,
     "description": "Сводка за день по всем метрикам сразу: сон прошедшей ночи, пульс, стресс, активность.",
     "input_schema": _schema({"date": _DATE})},
    {"name": "get_metric", "strict": True,
     "description": "Подробные факты по одной метрике за день, неделю или месяц: средние, нормы, тренды, выбросы, "
                    "сравнение с прошлым периодом.",
     "input_schema": _schema({"metric": _METRIC, "period": _PERIOD, "end_date": _DATE})},
    {"name": "compare_periods", "strict": True,
     "description": "Сравнить одну метрику в двух периодах одинаковой длины (например, эту и прошлую неделю).",
     "input_schema": _schema({"metric": _METRIC, "period": _PERIOD,
                              "end_date_a": {"type": "string", "description": "конец первого периода, ГГГГ-ММ-ДД"},
                              "end_date_b": {"type": "string", "description": "конец второго периода, ГГГГ-ММ-ДД"}})},
    {"name": "find_anomalies", "strict": True,
     "description": "Список необычных событий за диапазон дат: пики пульса в покое, повышенный пульс во сне, "
                    "высокий стресс, сильные отклонения от нормы.",
     "input_schema": _schema({"start_date": {"type": "string"}, "end_date": {"type": "string"},
                              "metric": {"anyOf": [{"type": "string", "enum": ["sleep", "heart", "stress", "activity"]},
                                                   {"type": "null"}],
                                         "description": "фильтр по метрике или null — все"}})},
    {"name": "list_workouts", "strict": True,
     "description": "Список тренировок (новые сверху) с id, датой, типом, длительностью, пульсом и нагрузкой.",
     "input_schema": _schema({"start_date": _DATE, "end_date": _DATE,
                              "sport_type": {"type": ["string", "null"],
                                             "description": "тип, например strength_training, outdoor_walking; null — все"}})},
    {"name": "get_workout", "strict": True,
     "description": "Подробный разбор одной тренировки по её id из list_workouts: зоны, эффект, сравнение с обычными.",
     "input_schema": _schema({"workout_id": {"type": "integer"}})},
]

# Server-side tool (runs at Anthropic, only in chat). The basic variant: dynamic filtering needs Opus/Sonnet.
WEB_SEARCH: dict[str, Any] = {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}
CHAT_TOOLS = [*TOOLS, WEB_SEARCH]

METRIC_RU = {"sleep": "сон", "heart": "пульс", "spo2": "кислород", "stress": "стресс", "activity": "активность"}
PERIOD_RU = {"day": "день", "week": "неделю", "month": "месяц"}


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def label(name: str, args: dict[str, Any]) -> str:
    """Status line shown while a tool runs (args are empty while the call is still streaming)."""
    if not args:
        return {"get_overview": "Смотрю сводку", "compare_periods": "Сравниваю периоды", "find_anomalies":
                "Ищу необычные события", "list_workouts": "Смотрю тренировки", "get_workout": "Смотрю тренировку",
                "web_search": "Ищу в интернете"}.get(name, "Смотрю данные")
    if name == "get_overview":
        return "Смотрю сводку" + (f" за {day(_d(args['date']))}" if args.get("date") else "")
    if name == "get_metric":
        return f"Смотрю {METRIC_RU.get(args.get('metric'), '')} за {PERIOD_RU.get(args.get('period'), 'период')}"
    if name == "compare_periods":
        return f"Сравниваю {METRIC_RU.get(args.get('metric'), '')} в двух периодах"
    if name == "find_anomalies":
        return "Ищу необычные события"
    if name in ("list_workouts", "get_workout"):
        return "Смотрю тренировки"
    if name == "web_search":
        return "Ищу в интернете" + (f": «{args['query']}»" if args.get("query") else "")
    return "Смотрю данные"


def _metric_text(metric: str, period: str, end: date | None) -> str:
    proc, today = data.processor(), data_today()
    if metric == "activity":
        target = end or latest_date(proc, "heart") or today
        if period == "day":
            return activity_facts(proc, target).render()
        lines = []
        for k in range({"week": 7, "month": 30}[period]):
            s = activity_facts(proc, target - timedelta(days=k))
            if s.has_data:
                lines.append(f"{day(target - timedelta(days=k), True)}: " + "; ".join(f.text for f in s.facts))
        return f"Активность за {PERIOD_RU[period]} до {day(target)}:\n" + "\n".join(lines or ["нет данных"])
    view = period_view(proc, metric, period, end, today)
    if view is None:
        return f"Нет данных по метрике {METRIC_RU[metric]}."
    return period_facts(proc, view).render()


def data_today() -> date:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(data.analytics_config().tz)).date()


def run_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """(text, is_error). Never raises: errors go back to the model as tool errors."""
    try:
        return _run(name, args), False
    except (ValueError, KeyError, TypeError) as exc:
        return f"Ошибка в параметрах: {exc}", True


def _run(name: str, a: dict[str, Any]) -> str:
    proc = data.processor()
    if name == "get_overview":
        target = _d(a.get("date")) or latest_date(proc, "heart") or data_today()
        return overview_facts(proc, target, data_today()).render()
    if name == "get_metric":
        return _metric_text(a["metric"], a["period"], _d(a.get("end_date")))
    if name == "compare_periods":
        parts = []
        for key in ("end_date_a", "end_date_b"):
            parts.append(_metric_text(a["metric"], a["period"], _d(a[key])))
        return "ПЕРИОД A\n" + parts[0] + "\n\nПЕРИОД B\n" + parts[1]
    if name == "find_anomalies":
        start, end = _d(a["start_date"]), _d(a["end_date"])
        if start is None or end is None or end < start:
            raise ValueError("нужны start_date <= end_date")
        start = max(start, end - timedelta(days=60))
        lines = []
        d = start
        while d <= end:
            for x in proc.anomalies(d):
                if a.get("metric") in (None, anomaly_metric(x)):
                    lines.append(f"{day(d, True)}: {describe_anomaly(x)}")
            d += timedelta(days=1)
        return f"Необычные события за {span(start, end)}:\n" + ("\n".join(lines) if lines else "не найдено")
    if name == "list_workouts":
        w = workout_list(proc)
        start, end = _d(a.get("start_date")), _d(a.get("end_date"))
        rows = [r for r in w.iter_rows(named=True)
                if (start is None or r["date"] >= start) and (end is None or r["date"] <= end)
                and (a.get("sport_type") in (None, r["sport_type"]))][:30]
        if not rows:
            return "Тренировок не найдено."
        return "\n".join(
            f"id={r['ts']} · {day(r['date'], True)} · {SPORTS.get(r['sport_type'], ('', r['sport_type']))[1]} · "
            f"{dur(r['duration_sec'] / 60)} · пульс {r['avg_hr']}/{r['max_hr']} · нагрузка {r['train_load']}"
            for r in rows)
    if name == "get_workout":
        sheet = workout_facts(proc, int(a["workout_id"]))
        return sheet.render() if sheet else "Тренировка с таким id не найдена."
    raise ValueError(f"неизвестный инструмент {name}")
