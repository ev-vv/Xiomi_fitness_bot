"""Trends (OLS slope per week, 7-vs-7 delta, EWMA) and cautious cross-metric correlations."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import polars as pl

from .config import AnalyticsConfig

# (x on day d, y on day d + lag): "next night" metrics live on the wake-up date, i.e. lag 1.
CORRELATION_PAIRS = (
    ("workout_load", "deep_pct", 1),
    ("workout_load", "sleep_rhr", 1),
    ("steps", "tst", 1),
    ("steps", "quality_index", 1),
    ("stress_avg", "quality_index", 1),
    ("tst", "stress_avg", 0),
)


def _window(daily: pl.DataFrame, col: str, start: date, end: date) -> pl.DataFrame:
    return daily.filter(pl.col("date").is_between(start, end) & pl.col(col).is_not_null()).select("date", col)


def slope_per_week(daily: pl.DataFrame, col: str, end: date, days: int = 28, min_points: int = 5) -> float | None:
    w = _window(daily, col, end - timedelta(days=days - 1), end)
    if w.height < min_points:
        return None
    x = (w["date"] - w["date"].min()).dt.total_days().cast(pl.Float64)
    y = w[col].cast(pl.Float64)
    var = ((x - x.mean()) ** 2).sum()
    return round(((x - x.mean()) * (y - y.mean())).sum() / var * 7, 2) if var else None


def delta_7d(daily: pl.DataFrame, col: str, end: date, min_points: int = 3) -> float | None:
    cur = _window(daily, col, end - timedelta(days=6), end)[col]
    prev = _window(daily, col, end - timedelta(days=13), end - timedelta(days=7))[col]
    if cur.len() < min_points or prev.len() < min_points:
        return None
    return round(cur.mean() - prev.mean(), 1)


def ewma(daily: pl.DataFrame, col: str, end: date, span: int = 7) -> float | None:
    s = daily.filter(pl.col("date") <= end).sort("date")[col].drop_nulls()
    return round(s.ewm_mean(span=span).item(-1), 1) if s.len() else None


def trend_block(daily: pl.DataFrame, cols: tuple[str, ...], end: date) -> dict[str, dict[str, float]]:
    out = {}
    for c in cols:
        t = {"per_wk": slope_per_week(daily, c, end), "d7": delta_7d(daily, c, end), "ewma": ewma(daily, c, end)}
        if any(v is not None for v in t.values()):
            out[c] = {k: v for k, v in t.items() if v is not None}
    return out


def correlations(daily: pl.DataFrame, end: date, cfg: AnalyticsConfig, days: int = 90) -> list[dict[str, Any]]:
    """Spearman rho over the last `days`, only with >= `corr_min_pairs` complete pairs. Not causal."""
    if daily.is_empty():
        return []
    start = end - timedelta(days=days - 1)
    grid = pl.DataFrame({"date": pl.date_range(start, end, "1d", eager=True)}).join(daily, on="date", how="left")
    out = []
    for x, y, lag in CORRELATION_PAIRS:
        pairs = grid.select(pl.col(x), pl.col(y).shift(-lag).alias("_y")).drop_nulls()
        if pairs.height < cfg.corr_min_pairs:
            continue
        rho = pairs.select(pl.corr(x, "_y", method="spearman")).item()
        if rho is not None and rho == rho:
            out.append({"x": x, "y": y, "lag": lag, "rho": round(rho, 2), "n": pairs.height})
    return out
