"""Anomaly detectors over one local day. Each returns plain dicts ready for the report."""

from __future__ import annotations

from typing import Any

import polars as pl

from .baseline import REFERENCE
from .config import AnalyticsConfig


# Awake heart rate is sampled every 10 min (1 min during activity): allow one missed sample.
HR_MAX_GAP_S = 660


def runs(df: pl.DataFrame, flag: pl.Expr, max_gap_s: int) -> pl.DataFrame:
    """Flagged rows labelled with `run`: consecutive flagged samples without a gap over `max_gap_s`."""
    if df.is_empty():
        return df.with_columns(pl.lit(0, pl.Int64).alias("run"))
    d = df.sort("ts").with_columns(flag.fill_null(False).alias("_f"))
    brk = (pl.col("_f") != pl.col("_f").shift(1)) | (pl.col("ts").diff() > max_gap_s)
    return d.with_columns(brk.fill_null(True).cum_sum().alias("run")).filter(pl.col("_f")).drop("_f")


def _episodes(flagged: pl.DataFrame, peak: pl.Expr, step_s: int) -> pl.DataFrame:
    return (
        flagged.group_by("run").agg(
            pl.col("ts_local").min().alias("start"),
            ((pl.col("ts").max() - pl.col("ts").min() + step_s) / 60).alias("dur"),
            peak.alias("peak"),
            pl.len().alias("n"),
            *([pl.col("hr_med").median().alias("base")] if "hr_med" in flagged.columns else []),
        ).sort("start")
    )


def _sustained(cfg: AnalyticsConfig) -> pl.Expr:
    """A single sample is never an episode, whatever the sampling rate."""
    return (pl.col("dur") >= cfg.hr_spike_min_minutes) & (pl.col("n") >= 2)


def hr_spikes(hr_day: pl.DataFrame, profile: pl.DataFrame, cfg: AnalyticsConfig) -> list[dict[str, Any]]:
    """Sustained high awake heart rate outside workouts, against the hour-of-day baseline."""
    awake = hr_day.filter(~pl.col("is_sleep") & ~pl.col("in_workout"))
    if awake.is_empty():
        return []
    d = awake.with_columns(pl.col("ts_local").dt.hour().alias("hour")).join(profile, on="hour", how="left")
    ready = pl.col("hr_days").fill_null(0) >= cfg.baseline_min_days
    z = 0.6745 * (pl.col("bpm") - pl.col("hr_med")) / pl.max_horizontal(pl.col("hr_mad"), pl.lit(cfg.hr_mad_floor))
    flag = pl.when(ready).then((z > cfg.hr_spike_z) & (pl.col("bpm") > cfg.hr_spike_min_bpm)) \
        .otherwise(pl.col("bpm") > cfg.hr_spike_fallback_bpm)
    ep = _episodes(runs(d, flag, HR_MAX_GAP_S), pl.col("bpm").max(), 60).filter(_sustained(cfg))
    return [
        {"type": "hr_spike", "at": r["start"].strftime("%H:%M"), "dur": round(r["dur"]), "peak": r["peak"],
         "over_baseline": round(r["peak"] - r["base"]) if r["base"] is not None else None,
         "context": "no_workout"}
        for r in ep.iter_rows(named=True)
    ]


def bradycardia(hr_day: pl.DataFrame, cfg: AnalyticsConfig) -> list[dict[str, Any]]:
    awake = hr_day.filter(~pl.col("is_sleep") & ~pl.col("in_workout"))
    ep = _episodes(runs(awake, pl.col("bpm") < cfg.brady_bpm, HR_MAX_GAP_S), pl.col("bpm").min(), 60) \
        .filter(_sustained(cfg))
    return [{"type": "bradycardia", "at": r["start"].strftime("%H:%M"), "dur": round(r["dur"]), "peak": r["peak"]}
            for r in ep.iter_rows(named=True)]


def stress_episodes(stress_day: pl.DataFrame, cfg: AnalyticsConfig) -> list[dict[str, Any]]:
    """Runs of high stress (5-minute samples) lasting at least `stress_min_samples` samples."""
    ep = _episodes(runs(stress_day, pl.col("stress") >= cfg.stress_high, 600), pl.col("stress").max(), 300)
    return [{"type": "stress_high", "at": r["start"].strftime("%H:%M"), "dur": round(r["dur"]), "peak": r["peak"]}
            for r in ep.filter(pl.col("n") >= cfg.stress_min_samples).iter_rows(named=True)]


def spo2_dips(spo2_night: pl.DataFrame, sleep_hours: float | None, cfg: AnalyticsConfig) -> dict[str, Any]:
    """ODI-like desaturation index: drops of >= `spo2_drop_pct` below the rolling 10-minute median."""
    if spo2_night.is_empty():
        return {"status": "no_data"}
    d = spo2_night.sort("ts").with_columns(
        pl.col("spo2").cast(pl.Float64).rolling_median_by("ts_local", window_size=cfg.spo2_median_window).alias("_med"))
    events = runs(d, pl.col("spo2") <= pl.col("_med") - cfg.spo2_drop_pct, 300)["run"].n_unique()
    low = _episodes(runs(d, pl.col("spo2") < cfg.spo2_low_pct, 300), pl.col("spo2").min(), 60)
    hours = sleep_hours or (d["ts"].max() - d["ts"].min()) / 3600 or None
    return {
        "status": "ok", "avg": round(d["spo2"].mean(), 1), "min": int(d["spo2"].min()), "events": events,
        "odi": round(events / hours, 1) if hours else None, "low90_min": round(low["dur"].sum()),
    }


def day_deviations(row: dict[str, Any], metrics: tuple[str, ...], cfg: AnalyticsConfig) -> list[dict[str, Any]]:
    out = []
    for m in metrics:
        z = row.get(f"{m}_z")
        if z is not None and abs(z) >= cfg.z_day_threshold:
            out.append({"type": "day_deviation", "metric": m, "value": round(row[m], 1), "z": round(z, 1)})
        elif z is None and row.get(m) is not None and m in REFERENCE and row.get(f"{m}_n", 0) < cfg.baseline_min_days:
            lo, hi = REFERENCE[m]
            if (lo is not None and row[m] < lo) or (hi is not None and row[m] > hi):
                out.append({"type": "out_of_reference", "metric": m, "value": round(row[m], 1), "ref": [lo, hi]})
    return out


def sleep_hr_elevated(row: dict[str, Any], cfg: AnalyticsConfig) -> list[dict[str, Any]]:
    """Night RHR well above the personal baseline: poor recovery, illness, alcohol, late meals."""
    x, med, n = row.get("sleep_rhr"), row.get("sleep_rhr_med"), row.get("sleep_rhr_n") or 0
    if x is None or med is None or n < cfg.baseline_min_days or x - med < cfg.sleep_hr_elevated_bpm:
        return []
    return [{"type": "sleep_hr_elevated", "value": round(x), "delta": round(x - med, 1)}]
