import polars as pl

from extractor.models import STREAMS
from extractor.writer import ParquetWriter

# 2025-10-09 21:00:00 UTC == 2025-10-10 00:00 Moscow
MIDNIGHT_MSK = 1760043600


def test_partitions_by_local_date(tmp_path):
    writer = ParquetWriter(tmp_path, "Europe/Moscow")
    writer.write(STREAMS["stress"], [{"ts": MIDNIGHT_MSK - 60, "stress": 30}, {"ts": MIDNIGHT_MSK, "stress": 40}])
    dates = sorted(p.parent.name for p in (tmp_path / "stress").glob("date=*/data.parquet"))
    assert dates == ["date=2025-10-09", "date=2025-10-10"]


def test_rewrite_is_idempotent_and_keeps_latest(tmp_path):
    writer = ParquetWriter(tmp_path, "Europe/Moscow")
    spec = STREAMS["heart_rate"]
    rows = [{"ts": MIDNIGHT_MSK + i * 60, "bpm": 60 + i, "source": "point"} for i in range(5)]
    assert writer.write(spec, rows) == 5
    assert writer.write(spec, rows) == 0
    assert writer.write(spec, [{"ts": MIDNIGHT_MSK, "bpm": 99, "source": "point"},
                               {"ts": MIDNIGHT_MSK, "bpm": 58, "source": "fds_sleep"}]) == 1
    df = writer.read(spec)
    assert df.height == 6
    assert df.filter((pl.col("ts") == MIDNIGHT_MSK) & (pl.col("source") == "point"))["bpm"].item() == 99
    assert df.schema["bpm"] == pl.Int16


def test_read_filters_and_empty(tmp_path):
    writer = ParquetWriter(tmp_path, "UTC")
    spec = STREAMS["spo2"]
    assert writer.read(spec).is_empty()
    writer.write(spec, [{"ts": t, "spo2": 97, "source": "point"} for t in (100, 200, 300)])
    assert writer.read(spec, since=150, until=300)["ts"].to_list() == [200]


def test_duckdb_can_read_partitions(tmp_path):
    import duckdb

    writer = ParquetWriter(tmp_path, "UTC")
    writer.write(STREAMS["stress"], [{"ts": 86400 * d, "stress": d} for d in range(3)])
    rows = duckdb.sql(
        f"SELECT date, stress FROM read_parquet('{tmp_path}/stress/*/*.parquet', hive_partitioning=true) ORDER BY 1"
    ).fetchall()
    assert [r[1] for r in rows] == [0, 1, 2]


def test_schema_evolution_fills_new_columns(tmp_path):
    from dataclasses import replace

    writer = ParquetWriter(tmp_path, "UTC")
    full = STREAMS["sleep_segments"]
    old = replace(full, schema={k: v for k, v in full.schema.items() if k != "did"})
    writer.write(old, [{"ts": 100, "wake_up_time": 200}])
    assert writer.write(full, [{"ts": 300, "wake_up_time": 400, "did": "band"}]) == 1
    df = writer.read(full)
    assert df["did"].to_list() == [None, "band"]
