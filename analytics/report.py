"""Compact JSON report for the LLM layer: pydantic schema + serialisation without empty blocks.

Only aggregates leave this module: no raw series, device ids (`did`) or workout ids (`sid`).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Units(_M):
    hr: str = "bpm"
    dur: str = "min"
    pct: str = "%"
    clock: str = "HH:MM local"


class DataQuality(_M):
    hr_coverage_pct: int | None = None
    streams_missing: list[str] = Field(default_factory=list)
    baseline_days: int = 0
    baseline_status: Literal["ready", "warming_up"] = "warming_up"


class SleepHR(_M):
    min: int | None = None
    avg: int | None = None
    rhr: int | None = Field(None, description="lowest 30-min rolling mean during sleep")
    dip: float | None = Field(None, description="avg HR first half minus second half of the night")


class Night(_M):
    bed: str | None = None
    wake: str | None = None
    tst: int | None = Field(None, description="total sleep time, min")
    tib: int | None = Field(None, description="time in bed, min")
    eff: int | None = None
    latency: int | None = None
    phases: dict[str, list[int | None]] = Field(default_factory=dict, description="phase: [min, % of tst]")
    waso: int | None = None
    awakenings: int | None = None
    hr: SleepHR = Field(default_factory=SleepHR)
    breath: int | None = None
    score_device: int | None = None
    quality_index: int | None = Field(None, description="own 0-100 heuristic, see subscores")
    subscores: dict[str, int | None] = Field(default_factory=dict)
    z: dict[str, float] = Field(default_factory=dict, description="robust z vs personal baseline")


class SleepPeriod(_M):
    nights: int
    tst_avg: int | None = None
    debt: int | None = Field(None, description="sum(tst - need) over the period, min; negative = deficit")
    bed_avg: str | None = None
    bed_sd: int | None = None
    midpoint_sd: int | None = None
    social_jetlag: int | None = None
    deep_pct_avg: int | None = None
    rem_pct_avg: int | None = None
    quality_avg: int | None = None
    naps: int = 0


class Sleep(_M):
    need: int
    last_night: Night | None = None
    week: SleepPeriod | None = None
    month: SleepPeriod | None = None


class Heart(_M):
    rhr_device: int | None = None
    sleep_rhr: int | None = None
    awake_avg: int | None = None
    max: int | None = None
    abnormal_count: int | None = None
    z: dict[str, float] = Field(default_factory=dict)
    trends: dict[str, dict[str, float]] = Field(default_factory=dict)


class Anomaly(_M):
    model_config = ConfigDict(extra="allow")
    type: str


class SpO2(_M):
    status: Literal["ok", "no_data"]
    avg: float | None = None
    min: int | None = None
    events: int | None = None
    odi: float | None = Field(None, description="desaturation events per hour of sleep")
    low90_min: int | None = None


class Stress(_M):
    avg: int | None = None
    max: int | None = None
    high_episodes: int = 0
    high_min: int = 0
    trend: dict[str, float] = Field(default_factory=dict)


class Activity(_M):
    model_config = ConfigDict(extra="allow")
    steps: int | None = None
    steps_vs_28d_pct: int | None = None
    hr_zones_min: dict[str, int] | None = None


class Recovery(_M):
    index: int | None = None
    method: Literal["baseline_z", "absolute"]
    drivers: list[str] = Field(default_factory=list)


class Correlation(_M):
    x: str
    y: str
    lag: int
    rho: float
    n: int


class Report(_M):
    schema_version: int = SCHEMA_VERSION
    date: str
    day_complete: bool
    tz: str
    units: Units = Field(default_factory=Units)
    data_quality: DataQuality
    sleep: Sleep
    heart: Heart
    anomalies: list[Anomaly] = Field(default_factory=list)
    anomalies_total: int = 0
    spo2: SpO2
    stress: Stress
    activity: Activity
    recovery: Recovery
    correlations: list[Correlation] = Field(default_factory=list)

    def compact(self) -> dict[str, Any]:
        return _prune(self.model_dump(mode="json"))

    def to_json(self) -> str:
        return json.dumps(self.compact(), ensure_ascii=False, separators=(",", ":"))


def _prune(obj: Any) -> Any:
    """Drops None, empty dicts/lists and, recursively, blocks that become empty."""
    if isinstance(obj, dict):
        out = {k: _prune(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v is not None and v != {} and v != []}
    if isinstance(obj, list):
        return [_prune(v) for v in obj]
    return obj


def json_schema() -> dict[str, Any]:
    return Report.model_json_schema()
