"""DuckDB over Parquet: views only, Parquet stays the single source of truth.

Bronze `raw_<stream>` views read `data/raw/<stream>/date=*/data.parquet`; silver views clean
values, attach local time (`ts_local`, naive TIMESTAMP in the configured tz) and context flags.
Daily aggregates from the cloud carry `ts` = UTC midnight of the calendar day, so their `date`
is taken in UTC; point samples and sleep get their `date` in local time.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl

from extractor.models import STREAMS

from .config import AnalyticsConfig

GOLD_TABLES = ("daily_metrics", "sleep_nights", "anomalies")

_DUCK_TYPES = {
    pl.Int16: "SMALLINT", pl.Int32: "INTEGER", pl.Int64: "BIGINT",
    pl.Float64: "DOUBLE", pl.String: "VARCHAR", pl.Boolean: "BOOLEAN",
}

_SEGMENT_JSON = (
    '[{"bedtime":"BIGINT","wake_up_time":"BIGINT","duration":"INTEGER","sleep_deep_duration":"INTEGER",'
    '"sleep_light_duration":"INTEGER","sleep_rem_duration":"INTEGER","sleep_awake_duration":"INTEGER",'
    '"awake_count":"INTEGER","avg_hr":"INTEGER","min_hr":"INTEGER","max_hr":"INTEGER",'
    '"avg_breath":"INTEGER","sleep_efficiency":"INTEGER"}]'
)


def _q(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _bronze_sql(raw_dir: Path, stream: str) -> str:
    base = raw_dir / stream
    if any(base.glob("date=*/data.parquet")):
        return f"SELECT * FROM read_parquet({_q(base / 'date=*' / 'data.parquet')}, union_by_name = true)"
    # No files yet: a typed empty relation keeps downstream queries valid.
    cols = ", ".join(f"CAST(NULL AS {_DUCK_TYPES[t]}) AS {c}" for c, t in STREAMS[stream].schema.items())
    return f"SELECT {cols} WHERE false"


def _silver_sql(cfg: AnalyticsConfig) -> dict[str, str]:
    tz = cfg.tz
    local = lambda col: f"(to_timestamp({col}) AT TIME ZONE {_q(tz)})"  # noqa: E731
    utc_date = "CAST(to_timestamp(ts) AT TIME ZONE 'UTC' AS DATE)"
    j = lambda path, typ="INTEGER": f"CAST(json_extract(raw_json, '$.{path}') AS {typ})"  # noqa: E731
    return {
        # One row per sleep segment; `date` = local date of waking up, `is_main` = longest of that date.
        "sleep_nights": f"""
            WITH seg_json AS (
                SELECT * FROM (
                    SELECT d.ts AS day_ts, d.sleep_score,
                           unnest(from_json(json_extract(d.raw_json, '$.segment_details'), {_q(_SEGMENT_JSON)})) AS s
                    FROM raw_sleep_daily d
                )
                QUALIFY row_number() OVER (PARTITION BY s.wake_up_time ORDER BY day_ts DESC) = 1
            ),
            seg AS (
                SELECT s.bedtime AS sleep_start, s.wake_up_time AS sleep_end, s.duration,
                       s.sleep_deep_duration AS deep, s.sleep_light_duration AS light, s.sleep_rem_duration AS rem,
                       s.sleep_awake_duration AS awake, s.awake_count, s.avg_hr AS device_avg_hr,
                       s.min_hr AS device_min_hr, s.avg_breath, s.sleep_efficiency AS device_eff, sleep_score
                FROM seg_json
                UNION ALL
                SELECT ts, wake_up_time, duration, sleep_deep_duration, sleep_light_duration, NULL,
                       sleep_awake_duration, awake_count, NULL, NULL, NULL, NULL, NULL
                FROM raw_sleep_segments WHERE wake_up_time NOT IN (SELECT s.wake_up_time FROM seg_json)
            )
            SELECT CAST({local('seg.sleep_end')} AS DATE) AS date,
                   seg.* EXCLUDE (device_eff, sleep_score),
                   {local('seg.sleep_start')} AS bed_local, {local('seg.sleep_end')} AS wake_local,
                   coalesce(r.sleep_efficiency, seg.device_eff) AS device_eff,
                   r.go_bed_time, r.leave_bed_time, r.lin_bed_duration, r.entry_sleep_duration AS latency_s,
                   row_number() OVER (PARTITION BY CAST({local('seg.sleep_end')} AS DATE)
                                      ORDER BY seg.duration DESC) = 1 AS is_main,
                   seg.sleep_score
            FROM seg LEFT JOIN raw_sleep_report r ON r.ts = seg.sleep_end
        """,
        "workouts": f"""
            SELECT ts, end_time, duration_sec, sport_type, calories, avg_hr, max_hr,
                   CAST({local('ts')} AS DATE) AS date, {local('ts')} AS start_local,
                   {j('train_load')} AS train_load, {j('train_effect', 'DOUBLE')} AS train_effect,
                   {j('recover_time')} AS recover_h,
                   {j('hrm_warm_up_duration')} AS warm_up_s, {j('hrm_fat_burning_duration')} AS fat_burning_s, {j('hrm_aerobic_duration')} AS aerobic_s,
                   {j('hrm_anaerobic_duration')} AS anaerobic_s, {j('hrm_extreme_duration')} AS extreme_s
            FROM raw_workouts
        """,
        "hr": f"""
            SELECT h.ts, h.bpm, h.source, {local('h.ts')} AS ts_local,
                   h.source = 'fds_sleep'
                       OR EXISTS (SELECT 1 FROM sleep_nights n WHERE h.ts >= n.sleep_start AND h.ts < n.sleep_end)
                       AS is_sleep,
                   EXISTS (SELECT 1 FROM workouts w WHERE h.ts >= w.ts AND h.ts < w.end_time + {cfg.workout_recovery_s}) AS in_workout
            FROM raw_heart_rate h
            WHERE h.bpm BETWEEN 30 AND 220
            QUALIFY row_number() OVER (PARTITION BY h.ts ORDER BY h.source = 'fds_sleep' DESC) = 1
        """,
        "spo2": f"""
            SELECT p.ts, p.spo2, p.source, {local('p.ts')} AS ts_local,
                   p.source = 'fds_sleep'
                       OR EXISTS (SELECT 1 FROM sleep_nights n WHERE p.ts >= n.sleep_start AND p.ts < n.sleep_end)
                       AS is_sleep
            FROM raw_spo2 p
            WHERE p.spo2 BETWEEN 70 AND 100
            QUALIFY row_number() OVER (PARTITION BY p.ts ORDER BY p.source = 'fds_sleep' DESC) = 1
        """,
        "stress": f"""
            SELECT s.ts, s.stress, {local('s.ts')} AS ts_local,
                   EXISTS (SELECT 1 FROM sleep_nights n WHERE s.ts >= n.sleep_start AND s.ts < n.sleep_end) AS is_sleep
            FROM raw_stress s WHERE s.stress BETWEEN 1 AND 100
        """,
        "daily_hr": f"""
            SELECT {utc_date} AS date, nullif(avg_hr, 0) AS avg_hr, nullif(avg_rhr, 0) AS rhr_device,
                   nullif(max_hr, 0) AS max_hr, nullif(min_hr, 0) AS min_hr,
                   {j('abnormal_hr_count')} AS abnormal_hr_count,
                   {j('warm_up_hr_zone_duration')} AS zone_warm_up, {j('fat_burning_hr_zone_duration')} AS zone_fat_burn,
                   {j('aerobic_hr_zone_duration')} AS zone_aerobic, {j('anaerobic_hr_zone_duration')} AS zone_anaerobic,
                   {j('extreme_hr_zone_duration')} AS zone_extreme
            FROM raw_hr_daily
        """,
        "daily_steps": f"SELECT {utc_date} AS date, steps, distance, calories FROM raw_steps_daily",
        "daily_spo2": f"""
            SELECT {utc_date} AS date, nullif(avg_spo2, 0) AS avg_spo2, nullif(min_spo2, 0) AS min_spo2,
                   lack_spo2_count
            FROM raw_spo2_daily
        """,
    }


def create_views(con: duckdb.DuckDBPyConnection, cfg: AnalyticsConfig) -> None:
    for stream in STREAMS:
        con.execute(f"CREATE OR REPLACE VIEW raw_{stream} AS {_bronze_sql(cfg.raw_dir, stream)}")
    for name, sql in _silver_sql(cfg).items():
        con.execute(f"CREATE OR REPLACE VIEW {name} AS {sql}")
    for table in GOLD_TABLES:
        files = cfg.gold_dir / table / "date=*" / "data.parquet"
        if any(files.parent.parent.glob("date=*/data.parquet")):
            con.execute(f"CREATE OR REPLACE VIEW gold_{table} AS "
                        f"SELECT * FROM read_parquet({_q(files)}, hive_partitioning = false, union_by_name = true)")


def connect(cfg: AnalyticsConfig, path: Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """In-memory connection with fresh views by default; pass `path` to use the persisted warehouse."""
    if path is None:
        con = duckdb.connect()
    else:
        con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        create_views(con, cfg)
    return con


def init_db(cfg: AnalyticsConfig) -> Path:
    cfg.warehouse_path.parent.mkdir(parents=True, exist_ok=True)
    connect(cfg, cfg.warehouse_path).close()
    return cfg.warehouse_path
