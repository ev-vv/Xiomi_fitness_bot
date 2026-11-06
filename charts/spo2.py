from __future__ import annotations

from analytics.periods import PeriodView

from . import theme as t

HINT = "Похоже, ночной мониторинг SpO2 выключен.\nВключите его в Mi Fitness в настройках браслета\n(раздел про SpO2 / кислород во сне)."


def chart(view: PeriodView, title: str, subtitle: str, kpis: list[tuple[str, str]]) -> bytes:
    if not view.has_data:
        fig, (ax,) = t.dashboard(title, subtitle, [], rows=(1.2,))
        t.empty(ax, "Нет ночных замеров SpO2\n\n" + HINT)
        return t.to_png(fig)
    if view.period == "day":
        fig, (ax,) = t.dashboard(title, subtitle, kpis, rows=(1.8,))
        s = view.series
        ax.plot(s["ts_local"].to_list(), s["spo2"].to_list(), color=t.BLUE, linewidth=1.6, label="SpO2")
        ax.axhline(90, color=t.CRITICAL, linestyle="--", linewidth=1.2, label="Порог 90 %")
        low = s.filter(s["spo2"] < 90)
        if not low.is_empty():
            ax.scatter(low["ts_local"].to_list(), low["spo2"].to_list(), color=t.CRITICAL, s=30, zorder=5,
                       label="Ниже 90 %")
        ax.set_ylim(min(85, s["spo2"].min() - 2), 100.5)
        ax.set_title("SpO2 во сне, %", loc="left")
        t.time_axis(ax)
        t.legend(ax)
        return t.to_png(fig)
    rows = view.extra.get("per_night", [])
    fig, (ax_a, ax_o) = t.dashboard(title, subtitle, kpis, rows=(1.3, 1.0))
    dates = [r["date"] for r in rows]
    x = list(range(len(rows)))
    pos, labels = t.date_labels(dates)
    ax_a.plot(x, [r["avg"] for r in rows], color=t.BLUE, marker="o", markersize=6, label="Средний")
    ax_a.plot(x, [r["min"] for r in rows], color=t.ORANGE, marker="o", markersize=6, label="Минимум")
    ax_a.axhline(90, color=t.CRITICAL, linestyle="--", linewidth=1.2)
    ax_a.set_xticks(pos, labels)
    ax_a.set_title("SpO2 по ночам, %", loc="left")
    t.legend(ax_a)
    ax_o.bar(x, [r.get("odi") or 0 for r in rows], color=t.BLUE, width=0.6)
    ax_o.axhline(5, color=t.TEXT2, linestyle="--", linewidth=1.2)
    ax_o.set_xticks(pos, labels)
    ax_o.set_title("Просадки кислорода в час сна (ODI); выше 5 — повод присмотреться", loc="left")
    return t.to_png(fig)
