"""Per-night sleep metrics, a transparent quality index and period summaries.

Phases come from the cloud per-segment totals (deep/light/rem/awake minutes): the FDS hypnogram is
not decoded yet, so only the `sleep_nights` view in warehouse.py has to change once it is.
Clock times are kept as "minutes from noon" so that nights crossing midnight average correctly.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import polars as pl

from .config import AnalyticsConfig

# Subscore weights of the quality index; missing subscores are dropped and the rest renormalised.
QUALITY_WEIGHTS = {"duration": 0.30, "eff": 0.20, "deep": 0.15, "rem": 0.15, "continuity": 0.10, "regularity": 0.10}


def ramp(x: pl.Expr, bad: float, good: float) -> pl.Expr:
    """0 at `bad`, 100 at `good`, linear in between (works in either direction)."""
    return ((x - bad) / (good - bad)).clip(0, 1) * 100


def band(x: pl.Expr, lo0: float, lo1: float, hi1: float, hi0: float) -> pl.Expr:
    """100 inside [lo1, hi1], falling linearly to 0 at lo0 and hi0."""
    return pl.min_horizontal(ramp(x, lo0, lo1), ramp(x, hi0, hi1))


def minutes_from_noon(col: str) -> pl.Expr:
    t = pl.col(col)
    return (t.dt.hour().cast(pl.Int32) * 60 + t.dt.minute() - 720) % 1440


def clock(minutes_from_noon_value: float | None) -> str | None:
    if minutes_from_noon_value is None:
        return None
    m = int(round(minutes_from_noon_value + 720)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _sleep_hr(nights: pl.DataFrame, hr: pl.DataFrame) -> pl.DataFrame:
    """Heart rate during each segment: min, mean, RHR (lowest 30-min rolling mean), first-vs-second half dip."""
    if hr.is_empty() or nights.is_empty():
        return pl.DataFrame(schema={"sleep_end": pl.Int64, "hr_min": pl.Int64, "hr_avg": pl.Float64,
                                    "sleep_rhr": pl.Float64, "hr_dip": pl.Float64, "hr_n": pl.UInt32})
    joined = (
        hr.filter(pl.col("is_sleep")).select("ts", "bpm").sort("ts")
        .join_where(nights.select("sleep_start", "sleep_end"),
                    pl.col("ts") >= pl.col("sleep_start"), pl.col("ts") < pl.col("sleep_end"))
        .sort("sleep_end", "ts")
    )
    mid = pl.col("sleep_start") + (pl.col("sleep_end") - pl.col("sleep_start")) // 2
    return joined.group_by("sleep_end").agg(
        pl.col("bpm").min().cast(pl.Int64).alias("hr_min"),
        pl.col("bpm").mean().alias("hr_avg"),
        pl.col("bpm").cast(pl.Float64).rolling_mean(30, min_samples=20).min().alias("sleep_rhr"),
        (pl.col("bpm").filter(pl.col("ts") < mid).mean() - pl.col("bpm").filter(pl.col("ts") >= mid).mean())
        .alias("hr_dip"),
        pl.len().alias("hr_n"),
    )


def night_metrics(nights: pl.DataFrame, hr: pl.DataFrame, cfg: AnalyticsConfig) -> pl.DataFrame:
    """One row per sleep segment (main night and naps) with derived metrics and the quality index."""
    if nights.is_empty():
        return nights
    tst = pl.col("duration").cast(pl.Float64)
    df = nights.sort("sleep_end").with_columns(
        tst.alias("tst"),
        pl.coalesce(
            (pl.col("leave_bed_time") - pl.col("go_bed_time")) / 60,
            pl.col("lin_bed_duration") / 60,
            tst + pl.col("awake").fill_null(0),
        ).alias("tib"),
        (pl.col("latency_s") / 60).alias("latency"),
        (pl.col("deep") / tst * 100).alias("deep_pct"),
        (pl.col("light") / tst * 100).alias("light_pct"),
        (pl.col("rem") / tst * 100).alias("rem_pct"),
        pl.col("awake").alias("waso"),
        minutes_from_noon("bed_local").alias("bed_min"),
        minutes_from_noon("wake_local").alias("wake_min"),
        (pl.col("bed_local") + (pl.col("wake_local") - pl.col("bed_local")) / 2).alias("_mid"),
    ).with_columns(
        (pl.col("tst") / pl.col("tib") * 100).clip(0, 100).alias("eff"),
        minutes_from_noon("_mid").alias("midpoint_min"),
    ).drop("_mid")
    df = df.join(_sleep_hr(nights, hr), on="sleep_end", how="left")

    # Regularity: distance of bedtime from the median of the previous 7 main nights (needs >= 3 of them).
    main = df.filter(pl.col("is_main")).select("date", "bed_min")
    prior = main.rolling(index_column="date", period="7d", closed="left").agg(
        pl.col("bed_min").median().alias("_bed_ref"), pl.len().alias("_bed_n"))
    df = df.join(prior, on="date", how="left").with_columns(
        pl.when(pl.col("is_main") & (pl.col("_bed_n") >= 3))
        .then((pl.col("bed_min") - pl.col("_bed_ref")).abs()).alias("bed_shift")
    ).drop("_bed_ref", "_bed_n")

    hours = pl.col("tst") / 60
    subs = {
        "duration": pl.when(hours <= 9).then(ramp(hours, 4, 7)).otherwise(ramp(hours, 12, 9) * 0.4 + 60),
        "eff": ramp(pl.col("eff"), 65, 85),
        "deep": band(pl.col("deep_pct"), 5, 13, 23, 35),
        "rem": band(pl.col("rem_pct"), 8, 20, 25, 35),
        "continuity": pl.min_horizontal(ramp(pl.col("waso"), 60, 20), ramp(pl.col("awake_count"), 8, 2)),
        "regularity": ramp(pl.col("bed_shift"), 120, 30),
    }
    df = df.with_columns(**{f"q_{k}": v for k, v in subs.items()})
    num = pl.sum_horizontal(pl.col(f"q_{k}") * w for k, w in QUALITY_WEIGHTS.items())
    den = pl.sum_horizontal(pl.when(pl.col(f"q_{k}").is_not_null()).then(w).otherwise(0.0)
                            for k, w in QUALITY_WEIGHTS.items())
    return df.with_columns((num / den).alias("quality_index"))


def _r(x: Any, nd: int = 0) -> int | float | None:
    if x is None:
        return None
    return int(round(x)) if nd == 0 else round(float(x), nd)


def night_summary(row: dict[str, Any]) -> dict[str, Any]:
    phases = {p: [_r(row[p]), _r(row[f"{p}_pct"])] for p in ("deep", "light", "rem") if row.get(p) is not None}
    hr = {"min": _r(row.get("hr_min")), "avg": _r(row.get("hr_avg")), "rhr": _r(row.get("sleep_rhr")),
          "dip": _r(row.get("hr_dip"), 1)}
    return {
        "bed": clock(row["bed_min"]), "wake": clock(row["wake_min"]), "tst": _r(row["tst"]), "tib": _r(row["tib"]),
        "eff": _r(row["eff"]), "latency": _r(row.get("latency")), "phases": phases, "waso": _r(row.get("waso")),
        "awakenings": row.get("awake_count"), "hr": hr, "breath": row.get("avg_breath") or None,
        "score_device": row.get("sleep_score"), "quality_index": _r(row.get("quality_index")),
        "subscores": {k: _r(row.get(f"q_{k}")) for k in QUALITY_WEIGHTS},
    }


def period_summary(nights: pl.DataFrame, end: date, days: int, cfg: AnalyticsConfig) -> dict[str, Any] | None:
    """Aggregates the window (end - days, end]; None with fewer than 3 main nights."""
    if nights.is_empty():
        return None
    window = nights.filter(pl.col("date").is_between(end - timedelta(days=days - 1), end))
    main = window.filter(pl.col("is_main"))
    if main.height < 3:
        return None
    # Wake-ups on Saturday/Sunday follow free evenings.
    weekend = main.filter(pl.col("date").dt.weekday() >= 6)["midpoint_min"]
    weekday = main.filter(pl.col("date").dt.weekday() < 6)["midpoint_min"]
    jetlag = abs(weekend.median() - weekday.median()) if len(weekend) and len(weekday) else None
    return {
        "nights": main.height,
        "tst_avg": _r(main["tst"].mean()),
        "debt": _r((main["tst"] - cfg.sleep_need_min).sum()),
        "bed_avg": clock(main["bed_min"].mean()),
        "bed_sd": _r(main["bed_min"].std()),
        "midpoint_sd": _r(main["midpoint_min"].std()),
        "social_jetlag": _r(jetlag),
        "deep_pct_avg": _r(main["deep_pct"].mean()),
        "rem_pct_avg": _r(main["rem_pct"].mean()),
        "quality_avg": _r(main["quality_index"].mean()),
        "naps": window.height - main.height,
    }
