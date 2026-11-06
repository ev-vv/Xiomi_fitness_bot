from __future__ import annotations

import polars as pl

from analytics.periods import PeriodView
from analytics.sleep import QUALITY_WEIGHTS
from core.fmt import dur

from . import theme as t

SUBSCORES = {"duration": "Длительность", "eff": "Эффективность", "deep": "Глубокий сон", "rem": "REM-сон",
             "continuity": "Непрерывность", "regularity": "Регулярность"}


def day(view: PeriodView, title: str, subtitle: str, kpis: list[tuple[str, str]]) -> bytes:
    n = view.extra["night"]
    fig, (ax_ph, ax_hr, ax_q) = t.dashboard(title, subtitle, kpis, rows=(0.4, 1.4, 1.5))

    # Phases as one 100 % bar with direct labels.
    total = sum((n.get(k) or 0) for k, *_ in t.PHASES) or 1
    x = 0.0
    for key, label, color in t.PHASES:
        v = n.get(key) or 0
        if not v:
            continue
        ax_ph.barh(0, v / total, left=x, color=color, height=0.6, edgecolor=t.SURFACE, linewidth=2)
        if v / total > 0.09:
            ax_ph.text(x + v / total / 2, 0, f"{label}\n{dur(v)} · {v / total * 100:.0f} %", ha="center", va="center",
                       fontsize=10.5, color="white" if key in ("deep", "rem") else t.TEXT)
        x += v / total
    ax_ph.set_xlim(0, 1)
    ax_ph.set_axis_off()
    ax_ph.set_title("Фазы сна", loc="left")

    hr = view.series
    if hr.is_empty():
        t.empty(ax_hr, "Пульса во сне нет")
    else:
        ax_hr.plot(hr["ts_local"].to_list(), hr["bpm"].to_list(), color=t.BLUE, linewidth=1.6, label="Пульс")
        if n.get("sleep_rhr"):
            ax_hr.axhline(n["sleep_rhr"], color=t.TEXT2, linestyle="--", linewidth=1.2,
                          label=f"Пульс покоя во сне {n['sleep_rhr']:.0f}")
        ax_hr.set_title("Пульс во сне, уд/мин", loc="left")
        t.time_axis(ax_hr)
        t.legend(ax_hr)

    subs = [(SUBSCORES[k], n.get(f"q_{k}")) for k in QUALITY_WEIGHTS if n.get(f"q_{k}") is not None]
    if subs:
        names, vals = zip(*subs)
        t.hbars(ax_q, names, vals, t.BLUE, [f"{v:.0f}" for v in vals], track=100)
        ax_q.set_title(f"Индекс качества {n.get('quality_index', 0):.0f}/100 по составляющим", loc="left")
    else:
        t.empty(ax_q, "Нет данных для индекса качества")
    return t.to_png(fig)


def period(view: PeriodView, title: str, subtitle: str, kpis: list[tuple[str, str]], need: int) -> bytes:
    nights = view.nights.sort("date")
    fig, (ax_ph, ax_bed, ax_q) = t.dashboard(title, subtitle, kpis, rows=(1.5, 1.1, 1.0))
    dates = nights["date"].to_list()
    x = list(range(len(dates)))
    bottom = [0.0] * len(x)
    for key, label, color in t.PHASES:
        vals = [(v or 0) / 60 for v in nights[key].to_list()]
        ax_ph.bar(x, vals, bottom=bottom, color=color, label=label, width=0.72, edgecolor=t.SURFACE, linewidth=1.5)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax_ph.axhline(need / 60, color=t.TEXT2, linestyle="--", linewidth=1.2)
    ax_ph.text(-0.5, need / 60 + 0.12, f"цель {dur(need)}", ha="left", va="bottom", fontsize=10, color=t.TEXT2)
    if len(x) <= 10:
        for xi, total in zip(x, bottom):
            ax_ph.text(xi, total + 0.12, f"{total:.1f}".replace(".", ","), ha="center", fontsize=10, color=t.TEXT,
                       bbox={"facecolor": t.SURFACE, "edgecolor": "none", "pad": 1})
    ax_ph.set_ylabel("часы")
    ax_ph.set_title("Сон по ночам, ч", loc="left")
    ax_ph.set_ylabel("")
    pos, labels = t.date_labels(dates)
    ax_ph.set_xticks(pos, labels)
    t.legend(ax_ph)

    # Bed-to-wake floating bars on a clock axis (hours from noon so midnight is continuous).
    bed = [b / 60 for b in nights["bed_min"].to_list()]
    wake = [w / 60 for w in nights["wake_min"].to_list()]
    ax_bed.bar(x, [w - b for b, w in zip(bed, wake)], bottom=bed, color=t.VIOLET, width=0.5)
    lo, hi = int(min(bed)) - 1, int(max(wake)) + 2
    ticks = list(range(lo, hi + 1, 2))
    ax_bed.set_yticks(ticks, [t.clock_label(h) for h in ticks])
    ax_bed.set_ylim(hi, lo)  # evening on top
    ax_bed.set_xticks(pos, labels)
    ax_bed.set_title("Отбой и подъём", loc="left")

    q = nights.select("quality_index", "sleep_score")
    ax_q.plot(x, q["quality_index"].to_list(), color=t.BLUE, marker="o", markersize=6, label="Индекс качества")
    if q["sleep_score"].drop_nulls().len():
        ax_q.plot(x, q["sleep_score"].to_list(), color=t.ORANGE, marker="o", markersize=6, label="Оценка Xiaomi")
    ax_q.set_ylim(max(0, min(pl.concat([q["quality_index"], q["sleep_score"].cast(pl.Float64)]).drop_nulls()) - 10), 100)
    ax_q.set_xticks(pos, labels)
    ax_q.set_title("Качество, из 100", loc="left")
    t.legend(ax_q)
    return t.to_png(fig)
