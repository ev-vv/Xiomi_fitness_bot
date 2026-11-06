"""Steps, workouts, training load (ACWR) and heart-rate zones."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from .config import AnalyticsConfig

ZONES = {"zone_warm_up": "warm_up", "zone_fat_burn": "fat_burn", "zone_aerobic": "aerobic",
         "zone_anaerobic": "anaerobic", "zone_extreme": "extreme"}


def steps_block(daily: pl.DataFrame, target: date) -> dict[str, Any]:
    today = daily.filter(pl.col("date") == target)["steps"]
    steps = today.item() if len(today) else None
    prior = daily.filter(pl.col("date").is_between(target - timedelta(days=28), target - timedelta(days=1)))["steps"]
    med = prior.drop_nulls().median() if prior.drop_nulls().len() >= 3 else None
    return {"steps": steps,
            "steps_vs_28d_pct": round((steps - med) / med * 100) if steps is not None and med else None}


def workouts_block(workouts: pl.DataFrame, target: date, now: datetime, cfg: AnalyticsConfig) -> dict[str, Any]:
    """Acute (7 d) vs chronic (28 d, weekly average) training load; ACWR only with 28 days of history."""
    if workouts.is_empty():
        return {"workouts_7d": 0}
    upto = workouts.filter(pl.col("date") <= target)
    w7 = upto.filter(pl.col("date") > target - timedelta(days=7))
    w28 = upto.filter(pl.col("date") > target - timedelta(days=28))
    load7, load28 = w7["train_load"].fill_null(0).sum(), w28["train_load"].fill_null(0).sum()
    has_history = not upto.is_empty() and upto["date"].min() <= target - timedelta(days=27)
    acwr = round(load7 / (load28 / 4), 2) if has_history and load28 else None
    out: dict[str, Any] = {
        "workouts_7d": w7.height, "load_7d": int(load7), "load_28d": int(load28), "acwr": acwr,
        "acwr_risk": None if acwr is None else acwr > cfg.acwr_risk,
        "types_28d": dict(w28.group_by("sport_type").len().sort("len", descending=True).iter_rows()) or None,
        "zones_7d_min": {k: round(w7[f"{k}_s"].fill_null(0).sum() / 60)
                         for k in ("fat_burning", "aerobic", "anaerobic", "extreme")} if w7.height else None,
    }
    if not upto.is_empty():
        last = upto.sort("ts").row(-1, named=True)
        ended = datetime.fromtimestamp(last["end_time"], tz=now.tzinfo)
        left = (last["recover_h"] or 0) - (now - ended).total_seconds() / 3600
        out["last"] = {"type": last["sport_type"], "days_ago": (target - last["date"]).days,
                       "load": last["train_load"], "effect": last["train_effect"],
                       "recovery_left_h": max(0, round(left))}
    return out


def zones_block(daily_hr: pl.DataFrame, target: date) -> dict[str, int] | None:
    row = daily_hr.filter(pl.col("date") == target)
    if row.is_empty():
        return None
    r = row.row(0, named=True)
    return {name: r[col] for col, name in ZONES.items() if r.get(col)} or None
