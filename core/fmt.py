"""Russian formatting of durations, dates and relative times."""

from __future__ import annotations

import time
from datetime import date

MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def dur(minutes: float | None) -> str:
    """437 -> '7 ч 17 мин'."""
    if minutes is None:
        return "—"
    m = int(round(abs(minutes)))
    h, m = divmod(m, 60)
    sign = "−" if minutes < 0 else ""
    if h and m:
        return f"{sign}{h} ч {m} мин"
    return f"{sign}{h} ч" if h else f"{sign}{m} мин"


def day(d: date, weekday: bool = False) -> str:
    s = f"{d.day} {MONTHS[d.month - 1]}"
    return f"{s}, {WEEKDAYS[d.weekday()]}" if weekday else s


def span(start: date, end: date) -> str:
    if start == end:
        return day(end)
    if start.month == end.month:
        return f"{start.day}–{end.day} {MONTHS[end.month - 1]}"
    return f"{day(start)} – {day(end)}"


def ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "никогда"
    s = int((now or time.time()) - ts)
    if s < 90:
        return "только что"
    if s < 3600:
        return f"{s // 60} мин назад"
    if s < 86400 * 2:
        return f"{s // 3600} ч назад"
    return f"{s // 86400} дн назад"


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = abs(n) % 10, abs(n) % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def num(x: float | None, nd: int = 0) -> str:
    if x is None:
        return "—"
    return f"{round(x):d}" if nd == 0 else f"{x:.{nd}f}".replace(".", ",")
