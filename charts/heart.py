from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl

from analytics.periods import PeriodView

from . import theme as t

DAY_ZONES = (("warm_up", "Разминка"), ("fat_burn", "Жиросжигание"), ("aerobic", "Аэробная"),
             ("anaerobic", "Анаэробная"), ("extreme", "Максимальная"))
DAILY_ZONE_COLS = (("zone_fat_burn", "Жиросжигание"), ("zone_aerobic", "Аэробная"), ("zone_anaerobic", "Анаэробная"),
                   ("zone_extreme", "Максимальная"))


def _segments(df: pl.DataFrame, max_gap_s: int = 1200) -> list[pl.DataFrame]:
    """Splits a sparse series where samples are missing, so lines don't bridge gaps."""
    if df.is_empty():
        return []
    run = (df["ts"].diff().fill_null(0) > max_gap_s).cum_sum()
    return [g for _, g in df.with_columns(run.alias("_r")).group_by("_r", maintain_order=True)]


def _spans(mask: pl.Series, times: list[datetime], gap: timedelta = timedelta(minutes=20)) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for flag, ts in zip(mask.to_list(), times):
        if not flag:
            continue
        if out and ts - out[-1][1] <= gap:
            out[-1] = (out[-1][0], ts)
        else:
            out.append((ts, ts))
    return out


def day(view: PeriodView, title: str, subtitle: str, kpis: list[tuple[str, str]]) -> bytes:
    zones = view.extra.get("zones") or {}
    rows = (2.0, 0.35 * len(zones) + 0.3) if zones else (2.0,)
    fig, axes = t.dashboard(title, subtitle, kpis, rows=rows)
    ax = axes[0]
    hr = view.series
    if hr.is_empty():
        t.empty(ax, "Пульса за этот день нет")
    else:
        times = hr["ts_local"].to_list()
        for a, b in _spans(hr["is_sleep"], times):
            ax.axvspan(a, b, color=t.TINT, zorder=0)
        for w in view.extra.get("workouts", pl.DataFrame()).iter_rows(named=True):
            ax.axvspan(w["start_local"], w["start_local"] + timedelta(seconds=w["duration_sec"]), color=t.ORANGE,
                       alpha=0.15, zorder=0)
        prof = view.extra.get("profile")
        if prof is not None and not prof.is_empty() and prof["hr_days"].max() >= 3:
            day0 = datetime.combine(view.end, datetime.min.time())
            p = prof.sort("hour")
            xs = [day0 + timedelta(hours=h + 0.5) for h in p["hour"].to_list()]
            band = (p["hr_mad"] * 1.4826).to_list()
            med = p["hr_med"].to_list()
            ax.fill_between(xs, [m - b for m, b in zip(med, band)], [m + b for m, b in zip(med, band)],
                            color=t.BLUE, alpha=0.12, linewidth=0, label="Норма")
        for i, seg in enumerate(_segments(hr)):
            ax.plot(seg["ts_local"].to_list(), seg["bpm"].to_list(), color=t.BLUE, linewidth=1.5,
                    marker="o" if seg.height < 4 else None, markersize=3, label="Пульс" if i == 0 else None)
        spikes = [a for a in view.extra.get("anomalies", {}).get(view.end, []) if a["type"] == "hr_spike"]
        for k, a in enumerate(spikes):
            hh, mm = map(int, a["at"].split(":"))
            at = datetime.combine(view.end, datetime.min.time()) + timedelta(hours=hh, minutes=mm)
            ax.scatter([at], [a["peak"]], s=70, color=t.CRITICAL, zorder=5, edgecolor=t.SURFACE, linewidth=2,
                       label="Выше нормы в покое" if k == 0 else None)
            ax.annotate(f"{a['peak']}", (at, a["peak"]), textcoords="offset points", xytext=(0, 8), ha="center",
                        fontsize=10, color=t.TEXT)
        ax.set_title("Пульс, уд/мин", loc="left")
        ax.text(0, -0.13, "серый фон — сон, оранжевый — тренировка, голубая полоса — ваша обычная норма для этого часа",
                transform=ax.transAxes, fontsize=10, color=t.TEXT2, va="top")
        t.time_axis(ax)
        t.int_y(ax)
        t.legend(ax)
    if zones:
        ax_z = axes[1]
        items = [(name, zones.get(key, 0)) for key, name in DAY_ZONES if zones.get(key)]
        colors = {name: t.HR_ZONES[i][2] for i, (_, name) in enumerate(DAY_ZONES)}
        t.hbars(ax_z, [n for n, _ in items], [v for _, v in items], [colors[n] for n, _ in items],
                [f"{v} мин" for _, v in items])
        ax_z.set_title("Минуты в пульсовых зонах", loc="left")
    return t.to_png(fig)


def period(view: PeriodView, title: str, subtitle: str, kpis: list[tuple[str, str]]) -> bytes:
    d = view.days.sort("date")
    fig, (ax_r, ax_a, ax_z) = t.dashboard(title, subtitle, kpis, rows=(1.4, 1.1, 1.0))
    dates = d["date"].to_list()
    x = list(range(len(dates)))
    pos, labels = t.date_labels(dates)
    for col, label, color in (("rhr_device", "Пульс покоя (браслет)", t.BLUE), ("sleep_rhr", "Пульс покоя во сне", t.VIOLET)):
        vals = d[col].to_list()
        if any(v is not None for v in vals):
            ax_r.plot(x, vals, color=color, marker="o", markersize=6, label=label)
    elevated = {dd for dd, items in view.extra.get("anomalies", {}).items()
                for a in items if a["type"] == "sleep_hr_elevated"}
    for xi, dd, v in zip(x, dates, d["sleep_rhr"].to_list()):
        if dd in elevated and v is not None:
            ax_r.scatter([xi], [v], s=110, facecolor="none", edgecolor=t.CRITICAL, linewidth=2, zorder=5)
    ax_r.set_xticks(pos, labels)
    t.int_y(ax_r)
    ax_r.set_title("Пульс покоя, уд/мин", loc="left")
    t.legend(ax_r)

    awake, mx = d["awake_hr_avg"].to_list(), d["hr_max"].to_list()
    ax_a.bar(x, [v or 0 for v in awake], color=t.BLUE, width=0.6, label="Средний днём")
    ax_a.scatter(x, mx, color=t.ORANGE, s=40, zorder=4, label="Максимум")
    spikes: dict = {}
    for dd, items in view.extra.get("anomalies", {}).items():
        spikes[dd] = sum(1 for a in items if a["type"] == "hr_spike")
    top = max([v for v in mx if v is not None] or [0])
    for xi, dd in zip(x, dates):
        if spikes.get(dd):
            ax_a.text(xi, top * 1.06, str(spikes[dd]), ha="center", fontsize=10.5, color=t.CRITICAL, fontweight="bold")
    ax_a.set_ylim(0, top * 1.15 or 1)
    ax_a.set_xticks(pos, labels)
    ax_a.set_title("Днём, уд/мин", loc="left")
    ax_a.text(0, -0.16, "красные числа — эпизоды пульса выше нормы в покое", transform=ax_a.transAxes, fontsize=10,
              color=t.TEXT2, va="top")
    t.legend(ax_a)

    bottom = [0.0] * len(x)
    for (col, label), (_, _, color) in zip(DAILY_ZONE_COLS, t.HR_ZONES[1:]):
        if col not in d.columns:
            continue
        vals = [v or 0 for v in d[col].to_list()]
        if any(vals):
            ax_z.bar(x, vals, bottom=bottom, color=color, label=label, width=0.6, edgecolor=t.SURFACE, linewidth=1.5)
            bottom = [b + v for b, v in zip(bottom, vals)]
    if not any(bottom):
        t.empty(ax_z, "Нагрузки в пульсовых зонах не было")
    else:
        ax_z.set_xticks(pos, labels)
        ax_z.set_title("Пульсовые зоны, мин", loc="left")
        t.legend(ax_z)
    return t.to_png(fig)
