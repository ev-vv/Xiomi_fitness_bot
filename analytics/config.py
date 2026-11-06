"""Analytics thresholds and paths, overridable via MIFIT_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from extractor.config import Config


def _env(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


@dataclass(frozen=True)
class AnalyticsConfig:
    base: Config = field(default_factory=Config)
    # Baseline: robust median/MAD over the days before the target day.
    baseline_window_days: int = field(default_factory=lambda: int(_env("MIFIT_BASELINE_DAYS", 28)))
    baseline_min_days: int = field(default_factory=lambda: int(_env("MIFIT_BASELINE_MIN_DAYS", 7)))
    z_day_threshold: float = field(default_factory=lambda: _env("MIFIT_Z_DAY", 2.0))
    # Heart rate spikes at rest.
    hr_spike_z: float = field(default_factory=lambda: _env("MIFIT_HR_SPIKE_Z", 3.5))
    hr_spike_min_bpm: int = field(default_factory=lambda: int(_env("MIFIT_HR_SPIKE_BPM", 100)))
    hr_spike_fallback_bpm: int = field(default_factory=lambda: int(_env("MIFIT_HR_SPIKE_FALLBACK_BPM", 110)))
    hr_spike_min_minutes: int = field(default_factory=lambda: int(_env("MIFIT_HR_SPIKE_MIN", 5)))
    hr_mad_floor: float = 2.0
    brady_bpm: int = 40
    workout_recovery_s: int = 600
    sleep_hr_elevated_bpm: float = field(default_factory=lambda: _env("MIFIT_SLEEP_HR_ELEVATED", 5.0))
    # SpO2 desaturation (ODI-like).
    spo2_drop_pct: int = 3
    spo2_low_pct: int = 90
    spo2_median_window: str = "10m"
    # Stress.
    stress_high: int = 80
    stress_min_samples: int = 6
    # Sleep.
    sleep_need_min: int = field(default_factory=lambda: int(_env("MIFIT_SLEEP_NEED_MIN", 480)))
    # Correlations are reported only with enough pairs.
    corr_min_pairs: int = 14
    acwr_risk: float = 1.5
    report_max_bytes: int = 4096

    @property
    def tz(self) -> str:
        return self.base.tz

    @property
    def data_dir(self) -> Path:
        return self.base.data_dir

    @property
    def raw_dir(self) -> Path:
        return self.base.raw_dir

    @property
    def gold_dir(self) -> Path:
        return self.data_dir / "gold"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports" / "daily"

    @property
    def warehouse_path(self) -> Path:
        return self.data_dir / "warehouse.duckdb"
