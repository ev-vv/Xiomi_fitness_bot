from __future__ import annotations

from analytics.periods import STRESS_ZONES, PeriodView

from . import theme as t

BOUNDS = [(lo, hi) for _, lo, hi in STRESS_ZONES]


def _zone_bar(ax, totals: dict[str, float], title: str) -> None:
    total = sum(totals.values()) or 1
    x = 0.0
    for key, label, color in t.STRESS:
        v = totals.get(key, 0) / total
        if v <= 0:
            continue
        ax.barh(0, v, left=x, color=color, height=0.6, edgecolor=t.SURFACE, linewidth=2)
        if v > 0.08:
            ax.text(x + v / 2, 0, f"{label}\n{v * 100:.0f} %", ha="center", va="center", fontsize=10.5,
                    color="white" if key in ("relaxed", "high") else t.TEXT)
        x += v
    ax.set_xlim(0, 1)
    ax.set_axis_off()
    ax.set_title(title, loc="left")


def chart(view: PeriodView, title: str, subtitle: str, kpis: list[tuple[str, str]]) -> bytes:
    zones = view.extra.get("zones")
    totals = {k: float(zones[k].sum()) for k, *_ in t.STRESS} if zones is not None and not zones.is_empty() else {}
    if view.period == "day":
        fig, (ax, ax_z) = t.dashboard(title, subtitle, kpis, rows=(1.8, 0.5))
        s = view.series
        for (_, label, color), (lo, hi) in zip(t.STRESS, BOUNDS):
            ax.axhspan(lo, hi + 1, color=color, alpha=0.07, zorder=0)
            ax.text(0.005, (lo + hi) / 2, label.lower(), transform=ax.get_yaxis_transform(), va="center",
                    fontsize=10, color=t.TEXT2)
        ax.plot(s["ts_local"].to_list(), s["stress"].to_list(), color=t.TEXT2, linewidth=1.4, label="Стресс")
        high = s.filter(s["stress"] >= 80)
        if not high.is_empty():
            ax.scatter(high["ts_local"].to_list(), high["stress"].to_list(), color=t.RED, s=25, zorder=5,
                       label="Высокий стресс")
        ax.set_ylim(0, 100)
        ax.set_yticks([0, 40, 60, 80, 100])
        ax.set_title("Стресс, шкала 1–100", loc="left")
        t.time_axis(ax)
        t.legend(ax)
        _zone_bar(ax_z, totals, "Доля времени по зонам")
        return t.to_png(fig)
    d = view.days.sort("date").filter(view.days.sort("date")["stress_avg"].is_not_null())
    fig, (ax_a, ax_z) = t.dashboard(title, subtitle, kpis, rows=(1.4, 1.2))
    dates = d["date"].to_list()
    x = list(range(len(dates)))
    pos, labels = t.date_labels(dates)
    ax_a.bar(x, d["stress_avg"].to_list(), color=t.BLUE, width=0.6, label="Средний")
    ax_a.scatter(x, d["stress_max"].to_list(), color=t.ORANGE, s=40, zorder=4, label="Максимум")
    for lvl in (40, 60, 80):
        ax_a.axhline(lvl, color=t.GRID, linewidth=1.2, linestyle="--")
    ax_a.set_ylim(0, 100)
    ax_a.set_xticks(pos, labels)
    ax_a.set_title("Стресс по дням", loc="left")
    t.legend(ax_a)
    if zones is not None and not zones.is_empty():
        z = zones.sort("date")
        zx = list(range(z.height))
        zpos, zlabels = t.date_labels(z["date"].to_list())
        bottom = [0.0] * z.height
        tot = [sum(r) or 1 for r in zip(*(z[k].to_list() for k, *_ in t.STRESS))]
        for key, label, color in t.STRESS:
            vals = [v / s * 100 for v, s in zip(z[key].to_list(), tot)]
            ax_z.bar(zx, vals, bottom=bottom, color=color, label=label, width=0.6, edgecolor=t.SURFACE, linewidth=1.5)
            bottom = [b + v for b, v in zip(bottom, vals)]
        ax_z.set_ylim(0, 100)
        ax_z.set_xticks(zpos, zlabels)
        ax_z.set_title("Зоны стресса, % времени", loc="left")
        t.legend(ax_z)
    else:
        t.empty(ax_z, "Нет данных о зонах")
    return t.to_png(fig)
