"""Deterministic synthetic raw lake for analytics tests, written through the real ParquetWriter.

35 days ending on TARGET. Every day: a night 23:30 -> 07:30 with 1-min sleep HR, awake HR every
10 min, stress every 5 min, daily aggregates; a workout every 3rd day. On TARGET only:
  - resting HR spike 14:00-14:29 at 1-min resolution (must be detected),
  - workout 18:00-19:00 with HR 150 (must NOT be flagged),
  - sleep HR +10 bpm (elevated night RHR),
  - stress 85 from 10:00 to 11:00 (13 samples),
  - SpO2 during the night with 3 short dips and one run below 90 %.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from extractor.config import Config
from extractor.models import STREAMS
from extractor.writer import ParquetWriter

from analytics.config import AnalyticsConfig

TZ = "Europe/Moscow"
TARGET = date(2025, 10, 5)
DAYS = 35


def ts(d: date, hh: int, mm: int = 0) -> int:
    return int(datetime.combine(d, time(hh, mm), ZoneInfo(TZ)).timestamp())


def utc_midnight(d: date) -> int:
    return int(datetime.combine(d, time(0), ZoneInfo("UTC")).timestamp())


def make_lake(root: Path, with_spo2: bool = True, days: int = DAYS) -> AnalyticsConfig:
    w = ParquetWriter(root / "raw", TZ)
    rows: dict[str, list[dict]] = {name: [] for name in STREAMS}
    for i in range(days):
        d = TARGET - timedelta(days=days - 1 - i)
        hit = d == TARGET
        bed, wake = ts(d - timedelta(days=1), 23, 30), ts(d, 7, 30)
        deep, light, rem = 100 + (i % 3) * 5, 250 + (i % 4) * 5, 110 + (i % 2) * 5
        seg = {"bedtime": bed, "wake_up_time": wake, "duration": deep + light + rem, "sleep_deep_duration": deep,
               "sleep_light_duration": light, "sleep_rem_duration": rem, "sleep_awake_duration": 5,
               "awake_count": 1, "avg_hr": 55, "min_hr": 50, "max_hr": 70, "avg_breath": 15,
               "sleep_efficiency": 95, "timezone": 12}
        rows["sleep_daily"].append({"ts": utc_midnight(d), "total_duration": seg["duration"], "sleep_score": 80 + i % 7,
                                    "sleep_deep_duration": deep, "sleep_light_duration": light,
                                    "sleep_rem_duration": rem, "sleep_awake_duration": 5,
                                    "raw_json": json.dumps({"segment_details": [seg]})})
        rows["sleep_segments"].append({"ts": bed, "wake_up_time": wake, "duration": seg["duration"],
                                       "sleep_deep_duration": deep, "sleep_light_duration": light,
                                       "sleep_awake_duration": 5, "awake_count": 1, "timezone": 12, "did": "x"})
        rows["sleep_report"].append({"ts": wake, "fds_version": 5, "sleep_finish": True, "device_bed_time": bed,
                                     "device_wakeup_time": wake, "sleep_efficiency": 95, "entry_sleep_duration": 600,
                                     "go_bed_time": bed - 600, "leave_bed_time": wake + 300,
                                     "lin_bed_duration": wake - bed + 900})
        for k, t in enumerate(range(bed, wake, 60)):
            rows["heart_rate"].append({"ts": t, "bpm": 52 + k % 7 + (10 if hit else 0), "source": "fds_sleep"})
            if hit and with_spo2:
                dip = k in (60, 61, 200, 330) or 400 <= k < 405
                rows["spo2"].append({"ts": t, "spo2": (88 if k >= 400 else 92) if dip else 96 + k % 2,
                                     "source": "fds_sleep"})
        for k, t in enumerate(range(ts(d, 8), ts(d, 23), 600)):
            rows["heart_rate"].append({"ts": t, "bpm": 66 + (k * 7) % 9, "source": "point"})
        for k, t in enumerate(range(ts(d, 8), ts(d, 22), 300)):
            rows["stress"].append({"ts": t, "stress": 25 + (k * 5) % 11})
        rows["hr_daily"].append({"ts": utc_midnight(d), "avg_hr": 72, "avg_rhr": 60 + i % 3, "max_hr": 120,
                                 "min_hr": 50, "raw_json": json.dumps(
                                     {"avg_rhr": 60 + i % 3, "abnormal_hr_count": 0, "warm_up_hr_zone_duration": 30,
                                      "fat_burning_hr_zone_duration": 10, "aerobic_hr_zone_duration": 0})})
        rows["steps_daily"].append({"ts": utc_midnight(d), "steps": 8000 + (i % 5) * 500, "distance": 6000,
                                    "calories": 300, "raw_json": "{}"})
        if i % 3 == 0 or hit:
            start = ts(d, 18)
            rows["workouts"].append({"ts": start, "sid": f"w{i}", "sport_type": "outdoor_walking",
                                     "end_time": start + 3600, "duration_sec": 3600, "calories": 300.0,
                                     "avg_hr": 120, "max_hr": 150, "raw_json": json.dumps(
                                         {"train_load": 80, "train_effect": 3.0, "recover_time": 20,
                                          "hrm_aerobic_duration": 1200, "hrm_fat_burning_duration": 1800})})
        if hit:
            rows["heart_rate"] += [{"ts": t, "bpm": 130, "source": "point"} for t in range(ts(d, 14), ts(d, 14, 30), 60)]
            rows["heart_rate"] += [{"ts": t, "bpm": 150, "source": "point"} for t in range(ts(d, 18), ts(d, 19), 60)]
            rows["stress"] = [r for r in rows["stress"] if not ts(d, 10) <= r["ts"] <= ts(d, 11)]
            rows["stress"] += [{"ts": t, "stress": 85} for t in range(ts(d, 10), ts(d, 11) + 1, 300)]
    for name, rs in rows.items():
        if rs:
            w.write(STREAMS[name], rs)
    return AnalyticsConfig(base=Config(data_dir=root, tz=TZ))
