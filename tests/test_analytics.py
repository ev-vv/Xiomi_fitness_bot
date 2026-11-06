from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from analytics.anomalies import runs
from analytics.baseline import add_baselines
from analytics.config import AnalyticsConfig
from analytics.processor import Processor
from analytics.report import Report
from analytics.sleep import band, clock, ramp
from analytics.warehouse import connect, init_db

from synth import TARGET, TZ, make_lake, ts

AFTER = datetime.combine(TARGET + timedelta(days=1), time(9), ZoneInfo(TZ))


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    return make_lake(tmp_path_factory.mktemp("lake"))


@pytest.fixture(scope="module")
def proc(lake):
    return Processor(lake)


@pytest.fixture(scope="module")
def report(proc):
    return proc.report(TARGET, now=AFTER)


# ---- pure functions ----------------------------------------------------------

def test_baseline_excludes_target_day_and_warms_up():
    cfg = AnalyticsConfig()
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(11)]
    values = [8000.0, 9000, 7000, 8000, 9000, 7000, 8000, 9000, 7000, 8000, 30000]
    out = add_baselines(pl.DataFrame({"date": days, "steps": values}), cfg, ("steps",))
    last = out.row(-1, named=True)
    assert last["steps_med"] == 8000 and last["steps_n"] == 10  # the spike is not part of its own baseline
    assert last["steps_z"] > 10
    assert out.row(3, named=True)["steps_z"] is None  # 3 prior days < baseline_min_days


def test_runs_split_on_gaps_and_flag_changes():
    df = pl.DataFrame({"ts": [0, 60, 120, 900, 960, 1020, 1080], "v": [1, 1, 1, 1, 0, 1, 1]})
    r = runs(df, pl.col("v") == 1, max_gap_s=120)
    assert r.group_by("run").len().sort("run")["len"].to_list() == [3, 1, 2]


def test_quality_curves():
    x = pl.DataFrame({"x": [4.0, 5.5, 7.0, 30.0]})
    assert x.select(ramp(pl.col("x"), 4, 7))["x"].to_list() == [0, 50, 100, 100]
    y = pl.DataFrame({"x": [5.0, 13.0, 23.0, 29.0, 35.0]})
    assert y.select(band(pl.col("x"), 5, 13, 23, 35))["x"].to_list() == [0, 100, 100, 50, 0]
    assert clock(0) == "12:00" and clock(-30) == "11:30" and clock(690) == "23:30" and clock(750) == "00:30"


# ---- warehouse -------------------------------------------------------------------

def test_views_handle_missing_stream(tmp_path):
    cfg = make_lake(tmp_path, with_spo2=False, days=3)
    con = connect(cfg)
    assert con.sql("SELECT count(*) FROM spo2").fetchall() == [(0,)]
    assert con.sql("SELECT count(*) FROM sleep_nights WHERE is_main").fetchall() == [(3,)]
    rep = Processor(cfg, con).report(TARGET, now=AFTER)
    assert rep.spo2.status == "no_data"
    assert rep.data_quality.baseline_status == "warming_up"
    assert "spo2" in rep.data_quality.streams_missing


def test_silver_flags(lake):
    con = connect(lake)
    probe = [ts(TARGET, 18, 30), ts(TARGET, 19), ts(TARGET, 19, 10)]  # workout 18:00-19:00 + 10 min recovery
    flags = dict(con.sql(f"SELECT ts, in_workout FROM hr WHERE ts IN {tuple(probe)}").fetchall())
    assert flags == dict(zip(probe, [True, True, False]))
    assert con.sql(f"SELECT count(*) FROM sleep_nights WHERE date = DATE '{TARGET}'").fetchall() == [(1,)]


def test_persisted_warehouse(lake):
    path = init_db(lake)
    con = connect(lake, path, read_only=True)
    assert con.sql("SELECT count(*) FROM raw_heart_rate").fetchall()[0][0] > 0


# ---- processor / report ----------------------------------------------------------

def test_anomalies_detected(report):
    types = [a.type for a in report.anomalies]
    spikes = [a.model_dump() for a in report.anomalies if a.type == "hr_spike"]
    assert len(spikes) == 1 and spikes[0]["at"] == "14:00" and spikes[0]["peak"] == 130  # workout at 18:00 ignored
    assert "sleep_hr_elevated" in types
    assert report.stress.high_episodes == 1 and report.stress.high_min == 65
    assert report.spo2.status == "ok" and report.spo2.events == 4 and report.spo2.min == 88
    assert report.spo2.low90_min == 5


def test_sleep_block(report):
    night = report.sleep.last_night
    assert night.bed == "23:30" and night.wake == "07:30"
    assert night.tib == 495 and night.latency == 10
    assert sum(v[0] for v in night.phases.values()) == night.tst
    assert night.hr.rhr >= 62  # +10 bpm night
    assert 0 <= night.quality_index <= 100
    assert report.sleep.week.nights == 7 and report.sleep.month.nights == 28


def test_baseline_ready_and_trends(report):
    assert report.data_quality.baseline_status == "ready"
    assert report.heart.z["sleep_rhr"] > 2
    assert report.recovery.method == "baseline_z"
    assert "sleep_rhr-" in report.recovery.drivers


def test_activity_block(report):
    a = report.activity.model_dump()
    assert a["workouts_7d"] == 3 and a["load_7d"] == 240
    assert a["acwr"] is not None and not a["acwr_risk"]
    assert a["last"]["days_ago"] == 0 and a["hr_zones_min"] == {"warm_up": 30, "fat_burn": 10}


def test_report_is_compact_and_valid(report):
    blob = report.to_json()
    assert len(blob.encode()) <= AnalyticsConfig().report_max_bytes
    assert Report.model_validate_json(blob).date == TARGET.isoformat()
    for forbidden in ('"did"', '"sid"', "raw_json"):
        assert forbidden not in blob


def test_build_is_idempotent(lake, proc):
    first = proc.build()
    second = Processor(lake).build()
    assert first == second and first["daily_metrics"] == proc.daily.height
    con = connect(lake)
    assert con.sql("SELECT count(*) FROM gold_daily_metrics").fetchall() == [(proc.daily.height,)]
    assert con.sql("SELECT count(DISTINCT type) FROM gold_anomalies").fetchall()[0][0] >= 3


def test_real_data_smoke():
    cfg = AnalyticsConfig()
    if not any((cfg.raw_dir / "heart_rate").glob("date=*/data.parquet")):
        pytest.skip("no local data")
    proc = Processor(cfg)
    last = proc.daily["date"].max()
    assert len(proc.report(last).to_json()) <= cfg.report_max_bytes
