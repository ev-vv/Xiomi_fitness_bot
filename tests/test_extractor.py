"""End-to-end sync over a fake API client (no network)."""

import json

import polars as pl
from dataclasses import replace
from pathlib import Path

from extractor.client import DataItem
from extractor.config import Config
from extractor.sqlite_reader import DataExtractor

NOW_DAY = 1_760_000_000 // 86400 * 86400
FDS = json.loads((Path(__file__).parent / "fixtures" / "ts_fds_vectors.json").read_text("utf-8"))


def item(key, ts, value):
    return DataItem.from_api({"key": key, "time": ts, "value": json.dumps(value), "source_sid_list": ["band-1"]})


class FakeClient:
    def __init__(self):
        self.calls = []
        self.fds_calls = []

    def get_fitness_data(self, key, start, end, limit=1440):
        self.calls.append(("fitness", key, start, end))
        if key == "heart_rate":
            # Pretend the server pages anything longer than 6 hours.
            items = [item(key, t, {"bpm": 60 + (t // 60) % 10}) for t in range(start, end, 600)]
            return items[:36], end - start > 6 * 3600
        if key == "spo2":
            return [item(key, start + 100, {"spo2": 97})], False
        return [], False  # stress: nothing

    def get_aggregated(self, key, start, end, tag="daily_report", limit=30):
        self.calls.append(("aggregated", key, start, end))
        days = range(start // 86400 * 86400, end, 86400)
        if key == "sleep":
            return [item(key, d, {
                "total_duration": 480, "sleep_score": 80, "sleep_deep_duration": 90,
                "segment_details": [{"bedtime": d - 7 * 3600, "wake_up_time": d + 3600, "duration": 480, "timezone": 12}],
            }) for d in days]
        if key == "steps":
            return [item(key, d, {"steps": 8000, "distance": 6000, "calories": 300}) for d in days]
        return []

    uid = 42

    def download_sleep_details(self, did, timestamp, timezone):
        assert did == "band-1"
        self.fds_calls.append(timestamp)
        return bytes.fromhex(FDS["v2"]["hex"])

    def iter_sport_records(self, watermark, limit=50):
        if watermark < 5:
            yield [{"sid": "w1", "key": "outdoor_run", "time": NOW_DAY - 3600, "watermark": 5,
                    "value": json.dumps({"start_time": NOW_DAY - 3600, "duration": 1800, "avg_hrm": 140})}], 5


def make(tmp_path):
    config = replace(Config(), data_dir=tmp_path, request_pause_s=0, backfill_days=3, overlap_days=1, tz="UTC")
    client = FakeClient()
    return DataExtractor(client, config), client


def test_full_sync_then_incremental(tmp_path):
    extractor, client = make(tmp_path)
    report = extractor.sync()
    assert report.ok, report.errors
    assert report.added["heart_rate"] > 400  # point samples + FDS sleep samples
    point = extractor.extract("heart_rate").filter(pl.col("source") == "point")["ts"].to_list()
    assert all(b - a == 600 for a, b in zip(point, point[1:]))  # paged windows were split, no gaps
    assert report.added["sleep_report"] >= 3
    assert set(extractor.extract("sleep_segments")["did"].to_list()) == {"band-1"}
    assert report.added["workouts"] == 1
    hr = extractor.extract("heart_rate")
    assert set(hr["source"].unique()) == {"point", "fds_sleep"}
    assert extractor.extract("workouts")["avg_hr"].to_list() == [140]
    assert len(list((tmp_path / "raw_fds").glob("*.bin"))) == report.added["sleep_report"]

    fds_before = len(client.fds_calls)
    client.calls.clear()
    second = extractor.sync()
    assert second.ok
    # Only the overlap window is re-fetched, duplicates are absorbed, FDS nights are not re-downloaded.
    hr_calls = [c for c in client.calls if c[:2] == ("fitness", "heart_rate")]
    assert min(c[2] for c in hr_calls) >= extractor.state.get("heart_rate").last_ts - 2 * 86400
    assert len(client.fds_calls) == fds_before
    assert second.added.get("workouts", 0) == 0
    assert extractor.extract("heart_rate").height == hr.height + second.added.get("heart_rate", 0)


def test_failing_stream_does_not_advance_watermark(tmp_path):
    extractor, client = make(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("API down")

    client.get_fitness_data = boom
    report = extractor.sync(["heart_rate", "steps_daily"])
    assert "heart_rate" in report.errors and report.added["steps_daily"] > 0
    assert extractor.state.get("heart_rate").last_ts is None
