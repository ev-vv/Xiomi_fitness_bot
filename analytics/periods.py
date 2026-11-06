"""Metric x period slices of the processed data: what a dashboard draws and what facts are built from.

A period is identified by its last day `end`: day = [end], week = [end-6, end], month = [end-29, end].
For sleep the day is the night that ended on `end` (wake-up date).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import polars as pl

from . import anomalies
from .activity import zones_block
from .baseline import hourly_profile
from .processor import Processor

METRICS = ("sleep", "heart", "spo2", "stress")
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}
# Xiaomi stress scale.
STRESS_ZONES = (("relaxed", 1, 39), ("mild", 40, 59), ("moderate", 60, 79), ("high", 80, 100))


@dataclass
class PeriodView:
    metric: str
    period: str
    start: date
    end: date
    days: pl.DataFrame
    prev_days: pl.DataFrame
    nights: pl.DataFrame
    prev_nights: pl.DataFrame
    series: pl.DataFrame = field(default_factory=pl.DataFrame)
    extra: dict[str, Any] = field(default_factory=dict)
    has_data: bool = True
    complete: bool = True

    @property
    def n_days(self) -> int:
        return PERIOD_DAYS[self.period]


def period_bounds(period: str, end: date) -> tuple[date, date]:
    return end - timedelta(days=PERIOD_DAYS[period] - 1), end


def shift(period: str, end: date, direction: int) -> date:
    return end + timedelta(days=PERIOD_DAYS[period] * direction)


def _dates(df: pl.DataFrame, col: str = "ts_local") -> pl.Series:
    return df[col].dt.date() if not df.is_empty() else pl.Series([], dtype=pl.Date)


def latest_date(proc: Processor, metric: str) -> date | None:
    f = proc.f
    candidates = {
        "sleep": proc.nights["date"] if not proc.nights.is_empty() else pl.Series([], dtype=pl.Date),
        "heart": _dates(f.hr),
        "stress": _dates(f.stress),
        "spo2": _dates(f.spo2),
    }
    s = candidates[metric]
    if s.is_empty() and metric == "spo2":
        s = candidates["sleep"]  # show the "no data" view for the last night
    return s.max() if not s.is_empty() else None


def _main_nights(proc: Processor, start: date, end: date) -> pl.DataFrame:
    if proc.nights.is_empty():
        return proc.nights
    return proc.nights.filter(pl.col("is_main") & pl.col("date").is_between(start, end)).sort("date")


def _days(proc: Processor, start: date, end: date) -> pl.DataFrame:
    return proc.daily.filter(pl.col("date").is_between(start, end)).sort("date")


def _anomalies_in(proc: Processor, start: date, end: date) -> dict[date, list[dict[str, Any]]]:
    out = {}
    d = start
    while d <= end:
        found = proc.anomalies(d)
        if found:
            out[d] = found
        d += timedelta(days=1)
    return out


def _pivot_zones(stress: pl.DataFrame, zone: pl.Expr) -> pl.DataFrame:
    long = (stress.with_columns(pl.col("ts_local").dt.date().alias("date"), zone.alias("zone"))
            .group_by("date", "zone").agg((pl.len() * 5).alias("min")))
    wide = long.pivot(on="zone", index="date", values="min").fill_null(0)
    for name, *_ in STRESS_ZONES:
        if name not in wide.columns:
            wide = wide.with_columns(pl.lit(0, pl.UInt32).alias(name))
    return wide.select("date", *[z[0] for z in STRESS_ZONES]).sort("date")


def period_view(proc: Processor, metric: str, period: str, end: date | None = None,
                today: date | None = None) -> PeriodView | None:
    """None when there is no data at all for the metric."""
    if metric not in METRICS or period not in PERIOD_DAYS:
        raise ValueError(f"unknown metric/period {metric}/{period}")
    end = end or latest_date(proc, metric)
    if end is None:
        return None
    start, _ = period_bounds(period, end)
    pstart, pend = period_bounds(period, start - timedelta(days=1))
    f, cfg = proc.f, proc.cfg
    view = PeriodView(
        metric=metric, period=period, start=start, end=end,
        days=_days(proc, start, end), prev_days=_days(proc, pstart, pend),
        nights=_main_nights(proc, start, end), prev_nights=_main_nights(proc, pstart, pend),
        complete=today is None or end < today,
    )
    in_range = lambda df: df.filter(pl.col("ts_local").dt.date().is_between(start, end))  # noqa: E731
    view.extra["anomalies"] = _anomalies_in(proc, start, end)

    if metric == "sleep":
        view.has_data = not view.nights.is_empty()
        if period == "day" and view.has_data:
            night = view.nights.row(-1, named=True)
            view.extra["night"] = night
            view.series = f.hr.filter(pl.col("is_sleep") & pl.col("ts").is_between(
                night["sleep_start"], night["sleep_end"] - 1)).select("ts", "ts_local", "bpm").sort("ts")
    elif metric == "heart":
        hr = in_range(f.hr)
        view.has_data = not hr.is_empty() or view.days["rhr_device"].drop_nulls().len() > 0
        view.extra["workouts"] = f.workouts.filter(pl.col("date").is_between(start, end)).sort("ts")
        if period == "day":
            view.series = hr.select("ts", "ts_local", "bpm", "is_sleep", "in_workout").sort("ts")
            view.extra["profile"] = hourly_profile(f.hr, end, cfg)
            view.extra["zones"] = zones_block(f.daily_hr, end)
    elif metric == "stress":
        st = in_range(f.stress)
        view.has_data = not st.is_empty()
        view.extra["zones"] = _pivot_zones(st, _zone_expr()) if view.has_data else None
        if period == "day":
            view.series = st.select("ts", "ts_local", "stress", "is_sleep").sort("ts")
            view.extra["episodes"] = anomalies.stress_episodes(st, cfg)
    elif metric == "spo2":
        sp = in_range(f.spo2)
        view.has_data = not sp.is_empty()
        if period == "day":
            night = view.nights.row(-1, named=True) if not view.nights.is_empty() else None
            if night:
                sp = f.spo2.filter(pl.col("ts").is_between(night["sleep_start"], night["sleep_end"] - 1))
                view.has_data = not sp.is_empty()
            view.series = sp.select("ts", "ts_local", "spo2").sort("ts") if view.has_data else pl.DataFrame()
            view.extra["summary"] = anomalies.spo2_dips(sp, (night or {}).get("tst", 0) / 60 or None, cfg)
        elif view.has_data:
            per_night = []
            for n in view.nights.iter_rows(named=True):
                s = f.spo2.filter(pl.col("ts").is_between(n["sleep_start"], n["sleep_end"] - 1))
                per_night.append({"date": n["date"], **anomalies.spo2_dips(s, n["tst"] / 60, cfg)})
            view.extra["per_night"] = [p for p in per_night if p["status"] == "ok"]
    return view


def _zone_expr() -> pl.Expr:
    zone = pl.lit(None, pl.String)
    for name, lo, hi in STRESS_ZONES:
        zone = pl.when(pl.col("stress").is_between(lo, hi)).then(pl.lit(name)).otherwise(zone)
    return zone


# ---- workouts ---------------------------------------------------------------------

WORKOUT_ZONES = ("warm_up", "fat_burning", "aerobic", "anaerobic", "extreme")


def workout_list(proc: Processor) -> pl.DataFrame:
    return proc.f.workouts.sort("ts", descending=True)


def workout_view(proc: Processor, ts: int) -> dict[str, Any] | None:
    """One workout with zones, comparison against the same sport type and the HR track if any."""
    w = proc.f.workouts
    rows = w.filter(pl.col("ts") == ts)
    if rows.is_empty():
        return None
    row = rows.row(0, named=True)
    same = w.filter((pl.col("sport_type") == row["sport_type"]) & (pl.col("ts") != ts))
    before = w.filter(pl.col("ts") < ts).sort("ts")
    track = proc.f.hr.filter(pl.col("ts").is_between(row["ts"], row["end_time"])).select("ts", "ts_local", "bpm")
    zones = {z: (row.get(f"{z}_s") or 0) for z in WORKOUT_ZONES}
    load7 = w.filter(pl.col("date").is_between(row["date"] - timedelta(days=6), row["date"]))["train_load"].fill_null(0).sum()
    load28 = w.filter(pl.col("date").is_between(row["date"] - timedelta(days=27), row["date"]))["train_load"].fill_null(0).sum()
    has_history = not w.is_empty() and w["date"].min() <= row["date"] - timedelta(days=27)
    return {
        "row": row,
        "zones_s": zones,
        "same_type_n": same.height,
        "same_type_median": {c: same[c].median() for c in ("duration_sec", "avg_hr", "max_hr", "train_load", "calories")}
        if same.height >= 2 else None,
        "days_since_prev": (row["date"] - before["date"].max()).days if not before.is_empty() else None,
        "acwr": round(load7 / (load28 / 4), 2) if has_history and load28 else None,
        "track": track.sort("ts"),
    }

