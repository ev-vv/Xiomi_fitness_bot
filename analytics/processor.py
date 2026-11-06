"""Data Processor: DuckDB views -> Polars metrics -> gold Parquet + compact JSON report.

All math lives in pure functions of the sibling modules; this class only loads frames once,
wires them together and writes outputs, so a Celery task can wrap `report()` / `build()` as is.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import polars as pl

from . import activity, anomalies, sleep, trends
from .baseline import METRICS, add_baselines, hourly_profile
from .config import AnalyticsConfig
from .report import Report
from .warehouse import connect

VIEWS = ("hr", "spo2", "stress", "sleep_nights", "workouts", "daily_hr", "daily_steps", "daily_spo2")
# Streams expected every day; reported as missing when absent over the last 7 days.
EXPECTED = {"heart_rate": "hr", "spo2": "spo2", "stress": "stress", "sleep": "sleep_nights",
            "steps": "daily_steps", "hr_daily": "daily_hr"}
SLEEP_Z = ("tst", "deep_pct", "rem_pct", "quality_index", "sleep_rhr")
HEART_TRENDS = ("rhr_device", "sleep_rhr", "awake_hr_avg")
ANOMALY_ORDER = ("sleep_hr_elevated", "hr_spike", "bradycardia", "day_deviation", "out_of_reference", "stress_high")
MAX_ANOMALIES = 6


@dataclass
class Frames:
    hr: pl.DataFrame
    spo2: pl.DataFrame
    stress: pl.DataFrame
    sleep_nights: pl.DataFrame
    workouts: pl.DataFrame
    daily_hr: pl.DataFrame
    daily_steps: pl.DataFrame
    daily_spo2: pl.DataFrame


def load_frames(con: duckdb.DuckDBPyConnection) -> Frames:
    return Frames(**{v: con.sql(f"SELECT * FROM {v}").pl() for v in VIEWS})


def _local_date(col: str = "ts_local") -> pl.Expr:
    return pl.col(col).dt.date().alias("date")


def build_daily(f: Frames, nights: pl.DataFrame, cfg: AnalyticsConfig) -> pl.DataFrame:
    """One row per local date with every daily metric, plus personal baselines and z-scores."""
    awake = ~pl.col("is_sleep") & ~pl.col("in_workout")
    parts = [
        f.hr.group_by(_local_date()).agg(
            pl.col("bpm").filter(awake).mean().alias("awake_hr_avg"),
            pl.col("bpm").max().alias("hr_max"),
            (pl.col("ts") // 60).n_unique().alias("hr_minutes"),
        ),
        f.stress.group_by(_local_date()).agg(
            pl.col("stress").filter(~pl.col("is_sleep")).mean().alias("stress_avg"),
            pl.col("stress").max().alias("stress_max"),
            ((pl.col("stress") >= cfg.stress_high).sum() * 5).alias("stress_high_min"),
        ),
        f.spo2.group_by(_local_date()).agg(pl.col("spo2").mean().alias("spo2_avg")),
        nights.filter(pl.col("is_main")).select(
            "date", "tst", "deep_pct", "rem_pct", "eff", "latency", "waso", "quality_index", "sleep_score",
            "sleep_rhr", pl.col("hr_avg").alias("sleep_hr_avg"), "bed_min", "midpoint_min",
        ) if not nights.is_empty() else None,
        f.daily_hr.select("date", "rhr_device", pl.col("avg_hr").alias("avg_hr_device"), "abnormal_hr_count",
                          *activity.ZONES),
        f.daily_steps.select("date", "steps", "distance"),
        f.daily_spo2.select("date", pl.col("avg_spo2").alias("spo2_device")),
        f.workouts.group_by("date").agg(pl.len().alias("workouts_n"),
                                        pl.col("train_load").fill_null(0).sum().alias("workout_load")),
    ]
    parts = [p for p in parts if p is not None and not p.is_empty()]
    if not parts:
        return pl.DataFrame({"date": []}, schema={"date": pl.Date})
    dates = pl.concat([p.select("date") for p in parts]).drop_nulls()["date"]
    # Workout history reaches far back; the daily grid starts with the first non-workout data.
    sensor = [p for p in parts if "workouts_n" not in p.columns]
    start = min(p["date"].min() for p in sensor) if sensor else dates.min()
    grid = pl.DataFrame({"date": pl.date_range(start, dates.max(), "1d", eager=True)})
    for p in parts:
        grid = grid.join(p, on="date", how="left")
    grid = grid.with_columns(
        *(pl.col(c).fill_null(0) for c in ("workouts_n", "workout_load") if c in grid.columns),
        (pl.col("hr_minutes") / 1440 * 100 if "hr_minutes" in grid.columns else pl.lit(None, pl.Float64))
        .alias("hr_coverage_pct"),
        *(pl.lit(None, pl.Float64).alias(m) for m in METRICS if m not in grid.columns),
    )
    return add_baselines(grid, cfg)


def _ordered(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {t: i for i, t in enumerate(ANOMALY_ORDER)}
    return sorted(items, key=lambda a: (rank.get(a["type"], 99), -abs(a.get("z") or a.get("dur") or 0)))


def _z(row: dict[str, Any], cols: tuple[str, ...]) -> dict[str, float]:
    return {c: round(row[f"{c}_z"], 1) for c in cols if row.get(f"{c}_z") is not None}


def _int(x: Any) -> int | None:
    return None if x is None else int(round(x))


def recovery(row: dict[str, Any], ready: bool, recovery_left_h: int) -> dict[str, Any]:
    """Heuristic readiness 0-100: personal z-scores when the baseline is ready, absolute values otherwise."""
    signs = {"sleep_rhr": -1, "rhr_device": -1, "quality_index": 1, "stress_avg": -1}
    # Each component is capped at ±3 so one unusual metric cannot pin the index to 0 or 100.
    comps = {k: max(-3.0, min(3.0, s * row[f"{k}_z"])) for k, s in signs.items() if row.get(f"{k}_z") is not None}
    if ready and comps:
        index = 50 + 15 * sum(comps.values()) / len(comps)
        drivers = [f"{k}{'+' if v > 0 else '-'}" for k, v in sorted(comps.items(), key=lambda kv: -abs(kv[1]))
                   if abs(v) >= 0.5]
        method = "baseline_z"
    else:
        q, s = row.get("quality_index"), row.get("stress_avg")
        weighted = [(q, 0.6), (None if s is None else 100 - s, 0.4)]
        weighted = [(v, w) for v, w in weighted if v is not None]
        index = sum(v * w for v, w in weighted) / sum(w for _, w in weighted) if weighted else None
        drivers = ([f"quality_index{'+' if q >= 75 else '-'}"] if q is not None and (q >= 75 or q < 60) else []) \
            + (["stress_avg-"] if s is not None and s >= 50 else [])
        method = "absolute"
    if index is not None and recovery_left_h:
        index -= min(10, recovery_left_h / 3)
        drivers.append("workout_recovery_pending")
    return {"index": None if index is None else int(round(min(100, max(0, index)))), "method": method,
            "drivers": drivers}


class Processor:
    def __init__(self, cfg: AnalyticsConfig | None = None, con: duckdb.DuckDBPyConnection | None = None) -> None:
        self.cfg = cfg or AnalyticsConfig()
        own = con is None
        con = con or connect(self.cfg)
        try:
            self.f = load_frames(con)
        finally:
            if own:
                con.close()
        self.nights = sleep.night_metrics(self.f.sleep_nights, self.f.hr, self.cfg)
        self.daily = build_daily(self.f, self.nights, self.cfg)

    # ---- per-day pieces ------------------------------------------------------

    def _row(self, target: date) -> dict[str, Any]:
        rows = self.daily.filter(pl.col("date") == target)
        return rows.row(0, named=True) if rows.height else {}

    def anomalies(self, target: date) -> list[dict[str, Any]]:
        cfg, row = self.cfg, self._row(target)
        hr_day = self.f.hr.filter(pl.col("ts_local").dt.date() == target)
        stress_day = self.f.stress.filter(pl.col("ts_local").dt.date() == target)
        return _ordered(
            anomalies.sleep_hr_elevated(row, cfg)
            + anomalies.hr_spikes(hr_day, hourly_profile(self.f.hr, target, cfg), cfg)
            + anomalies.bradycardia(hr_day, cfg)
            + anomalies.day_deviations(row, METRICS, cfg)
            + anomalies.stress_episodes(stress_day, cfg)
        )

    def _night(self, target: date) -> dict[str, Any] | None:
        main = self.nights.filter((pl.col("date") == target) & pl.col("is_main")) if not self.nights.is_empty() else None
        return main.row(0, named=True) if main is not None and main.height else None

    def _missing_streams(self, target: date) -> list[str]:
        start = target - timedelta(days=6)
        missing = []
        for name, view in EXPECTED.items():
            df = getattr(self.f, view)
            col = pl.col("date") if "date" in df.columns else pl.col("ts_local").dt.date()
            if df.filter(col.is_between(start, target)).is_empty():
                missing.append(name)
        return missing

    def report(self, target: date, now: datetime | None = None) -> Report:
        cfg = self.cfg
        tz = ZoneInfo(cfg.tz)
        now = now or datetime.now(tz)
        complete = target < now.date()
        moment = datetime.combine(target, time(23, 59), tz) if complete else now
        row = self._row(target)
        baseline_days = max((row.get(f"{m}_n") or 0 for m in ("rhr_device", "sleep_rhr", "tst")), default=0)
        ready = baseline_days >= cfg.baseline_min_days

        week = sleep.period_summary(self.nights, target, 7, cfg)
        month = sleep.period_summary(self.nights, target, 28, cfg)
        if week and month and month["nights"] == week["nights"]:
            month = None  # no history beyond the week yet
        night = self._night(target)
        last_night = None
        if night:
            last_night = sleep.night_summary(night) | {"z": _z(row, SLEEP_Z)}
        spo2_night = self.f.spo2.filter(
            pl.col("is_sleep") & pl.col("ts").is_between(night["sleep_start"], night["sleep_end"])
        ) if night else self.f.spo2.clear()

        found = self.anomalies(target)
        stress_eps = [a for a in found if a["type"] == "stress_high"]
        acts = activity.workouts_block(self.f.workouts, target, moment, cfg)
        left = (acts.get("last") or {}).get("recovery_left_h") or 0

        return Report(
            date=target.isoformat(), day_complete=complete, tz=cfg.tz,
            data_quality={"hr_coverage_pct": _int(row.get("hr_coverage_pct")),
                          "streams_missing": self._missing_streams(target),
                          "baseline_days": baseline_days, "baseline_status": "ready" if ready else "warming_up"},
            sleep={"need": cfg.sleep_need_min, "last_night": last_night,
                   "week": week, "month": month},
            heart={"rhr_device": row.get("rhr_device"), "sleep_rhr": _int(row.get("sleep_rhr")),
                   "awake_avg": _int(row.get("awake_hr_avg")), "max": row.get("hr_max"),
                   "abnormal_count": row.get("abnormal_hr_count") or None,
                   "z": _z(row, ("rhr_device", "sleep_rhr", "awake_hr_avg")),
                   "trends": trends.trend_block(self.daily, HEART_TRENDS, target)},
            anomalies=found[:MAX_ANOMALIES], anomalies_total=len(found),
            spo2=anomalies.spo2_dips(spo2_night, (night or {}).get("tst", 0) / 60 or None, cfg),
            stress={"avg": _int(row.get("stress_avg")), "max": row.get("stress_max"),
                    "high_episodes": len(stress_eps), "high_min": sum(a["dur"] for a in stress_eps),
                    "trend": trends.trend_block(self.daily, ("stress_avg",), target).get("stress_avg", {})},
            activity=activity.steps_block(self.daily, target) | acts
            | {"hr_zones_min": activity.zones_block(self.f.daily_hr, target)},
            recovery=recovery(row, ready, left),
            correlations=trends.correlations(self.daily, target, cfg),
        )

    # ---- outputs -------------------------------------------------------------

    def write_report(self, target: date, now: datetime | None = None) -> tuple[Path, Report]:
        report = self.report(target, now)
        path = self.cfg.reports_dir / f"{target.isoformat()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(report.compact(), ensure_ascii=False, indent=1))
        os.replace(tmp, path)
        return path, report

    def build(self) -> dict[str, int]:
        """Recomputes gold tables from scratch: thresholds may change, so stale rows must not survive."""
        found = [
            {"date": d, "type": a["type"], "at_local": a.get("at"), "detail": json.dumps(a, ensure_ascii=False)}
            for d in self.daily["date"] for a in self.anomalies(d)
        ]
        tables = {
            "daily_metrics": self.daily,
            "sleep_nights": self.nights,
            "anomalies": pl.DataFrame(found, schema={"date": pl.Date, "type": pl.String, "at_local": pl.String,
                                                     "detail": pl.String}),
        }
        for name, df in tables.items():
            write_partitioned(df, self.cfg.gold_dir / name)
        return {name: df.height for name, df in tables.items()}


def write_partitioned(df: pl.DataFrame, root: Path) -> None:
    """Writes `root/date=YYYY-MM-DD/data.parquet` into a temp dir and swaps it in."""
    root.parent.mkdir(parents=True, exist_ok=True)
    tmp = root.with_name(f".{root.name}.{uuid.uuid4().hex}.tmp")
    old = root.with_name(f".{root.name}.{uuid.uuid4().hex}.old")
    try:
        for (d,), part in df.group_by("date"):
            out = tmp / f"date={d.isoformat()}" / "data.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            part.sort(part.columns[0]).write_parquet(out, compression="zstd")
        tmp.mkdir(parents=True, exist_ok=True)
        if root.exists():
            os.replace(root, old)
        os.replace(tmp, root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
