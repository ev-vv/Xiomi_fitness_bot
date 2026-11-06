"""Individual baselines: robust median/MAD over the window *before* the target day.

z = 0.6745 * (x - median) / MAD (Iglewicz-Hoaglin modified z-score). With fewer than
`baseline_min_days` valid days the baseline is "warming_up": z stays null and the report
falls back to population reference ranges.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from .config import AnalyticsConfig

# Daily metrics that get a personal baseline.
METRICS = ("rhr_device", "sleep_rhr", "awake_hr_avg", "tst", "deep_pct", "rem_pct", "sleep_score",
           "quality_index", "stress_avg", "steps")

# Population reference ranges for adults, used while the personal baseline is warming up.
# None = unbounded: a lot of deep sleep or steps is not an anomaly.
REFERENCE: dict[str, tuple[float | None, float | None]] = {
    "rhr_device": (50, 80), "sleep_rhr": (45, 70), "tst": (420, 600), "deep_pct": (13, None),
    "rem_pct": (15, None), "stress_avg": (None, 59), "steps": (5000, None),
}


# Smallest meaningful spread per metric: a flat history must not turn tiny changes into huge z-scores.
MAD_FLOOR = {
    "rhr_device": 1.5, "sleep_rhr": 1.5, "awake_hr_avg": 3.0, "tst": 15.0, "deep_pct": 2.0, "rem_pct": 2.0,
    "sleep_score": 3.0, "quality_index": 3.0, "stress_avg": 3.0, "steps": 800.0,
}


def mad(col: str) -> pl.Expr:
    return (pl.col(col) - pl.col(col).median()).abs().median()


def robust_z(x: pl.Expr, med: pl.Expr, mad_: pl.Expr, floor: float = 0.0) -> pl.Expr:
    """MAD is floored (per-metric minimum, else 1 % of the median) so a flat history keeps z finite and sane."""
    scale = pl.max_horizontal(mad_, med.abs() * 0.01, pl.lit(floor))
    return pl.when(scale > 0).then(0.6745 * (x - med) / scale)


def add_baselines(daily: pl.DataFrame, cfg: AnalyticsConfig, metrics: tuple[str, ...] = METRICS) -> pl.DataFrame:
    """Adds `{m}_med`, `{m}_mad`, `{m}_n` (valid days in the window) and `{m}_z` for each metric."""
    daily = daily.sort("date")
    stats = daily.rolling(index_column="date", period=f"{cfg.baseline_window_days}d", closed="left").agg(
        *(e for m in metrics for e in (
            pl.col(m).median().alias(f"{m}_med"), mad(m).alias(f"{m}_mad"), pl.col(m).count().alias(f"{m}_n"))),
    )
    out = daily.join(stats, on="date", how="left")
    return out.with_columns(
        pl.when(pl.col(f"{m}_n") >= cfg.baseline_min_days)
        .then(robust_z(pl.col(m), pl.col(f"{m}_med"), pl.col(f"{m}_mad"), MAD_FLOOR.get(m, 0.0))).alias(f"{m}_z")
        for m in metrics
    )


def hourly_profile(hr: pl.DataFrame, target: date, cfg: AnalyticsConfig) -> pl.DataFrame:
    """Awake, non-workout heart rate by hour of day over the baseline window before `target`."""
    start = target - timedelta(days=cfg.baseline_window_days)
    awake = hr.filter(~pl.col("is_sleep") & ~pl.col("in_workout")).with_columns(
        pl.col("ts_local").dt.date().alias("_d"), pl.col("ts_local").dt.hour().alias("hour"))
    return (
        awake.filter((pl.col("_d") >= start) & (pl.col("_d") < target))
        .group_by("hour")
        .agg(pl.col("bpm").median().alias("hr_med"), mad("bpm").alias("hr_mad"), pl.col("_d").n_unique().alias("hr_days"))
    )
