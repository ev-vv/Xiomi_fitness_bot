"""Output stream definitions: Parquet schema and primary key per stream.

Every stream has an epoch-seconds `ts` column (UTC) used for date partitioning
and an `extracted_at` column; aggregate streams keep the original payload in `raw_json`.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class StreamSpec:
    name: str
    schema: dict[str, pl.DataType]
    pk: tuple[str, ...]


def _spec(name: str, pk: tuple[str, ...], **columns: pl.DataType) -> StreamSpec:
    return StreamSpec(name, {"ts": pl.Int64, **columns, "extracted_at": pl.Int64}, pk)


I16, I32, I64, F64, STR, BOOL = pl.Int16, pl.Int32, pl.Int64, pl.Float64, pl.String, pl.Boolean

STREAMS: dict[str, StreamSpec] = {
    s.name: s
    for s in (
        # Point-level raw samples.
        _spec("heart_rate", ("ts", "source"), bpm=I16, source=STR),
        _spec("spo2", ("ts", "source"), spo2=I16, source=STR),
        _spec("stress", ("ts",), stress=I16),
        # Sleep.
        _spec(
            "sleep_daily", ("ts",),
            total_duration=I32, sleep_score=I32, sleep_deep_duration=I32, sleep_light_duration=I32,
            sleep_rem_duration=I32, sleep_awake_duration=I32, avg_hr=I32, min_hr=I32, max_hr=I32,
            avg_spo2=I32, raw_json=STR,
        ),
        _spec(  # ts = bedtime
            "sleep_segments", ("ts",),
            wake_up_time=I64, duration=I32, sleep_deep_duration=I32, sleep_light_duration=I32,
            sleep_awake_duration=I32, awake_count=I32, timezone=I32, did=STR,
        ),
        _spec(  # ts = segment wake_up_time (FDS object timestamp)
            "sleep_report", ("ts",),
            fds_version=I16, sleep_finish=BOOL, device_bed_time=I64, device_wakeup_time=I64,
            sleep_quality=I32, sleep_efficiency=I32, entry_sleep_duration=I64, lin_bed_duration=I64,
            go_bed_time=I64, leave_bed_time=I64, hr_samples=I32, spo2_samples=I32,
        ),
        # Daily aggregates.
        _spec("steps_daily", ("ts",), steps=I32, distance=I32, calories=I32, raw_json=STR),
        _spec("hr_daily", ("ts",), avg_hr=I32, avg_rhr=I32, max_hr=I32, min_hr=I32, raw_json=STR),
        _spec("spo2_daily", ("ts",), avg_spo2=I32, max_spo2=I32, min_spo2=I32, lack_spo2_count=I32, raw_json=STR),
        # Workouts, ts = start_time.
        _spec(
            "workouts", ("sid",),
            sid=STR, sport_type=STR, end_time=I64, duration_sec=I64, calories=F64,
            avg_hr=I32, max_hr=I32, min_hr=I32, watermark=I64, raw_json=STR,
        ),
    )
}
