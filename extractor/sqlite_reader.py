"""Data Extractor: pulls raw Mi Fitness biometrics from the Xiaomi Health cloud into Parquet.

The Android Mi Fitness app keeps its local database private (root-only), so data is taken
from the cloud the app syncs to. The module keeps the `sqlite_reader` name from the project
spec; `DataExtractor` is the stable entry point for the rest of the pipeline.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from .client import DataItem, MiHealthClient
from .config import Config
from .fds import parse_all_day_sleep_bytes
from .models import STREAMS
from .state import DAY, LoadState
from .writer import ParquetWriter

log = logging.getLogger(__name__)

# (api key, value fields, output stream, value column)
POINT_METRICS = [
    ("heart_rate", ("bpm",), "heart_rate", "bpm"),
    ("spo2", ("spo2",), "spo2", "spo2"),
    ("stress", ("stress",), "stress", "stress"),
]
DAILY_METRICS = {
    "steps_daily": ("steps", ("steps", "distance", "calories")),
    "hr_daily": ("heart_rate", ("avg_hr", "avg_rhr", "max_hr", "min_hr")),
    "spo2_daily": ("spo2", ("avg_spo2", "max_spo2", "min_spo2", "lack_spo2_count")),
}
# Sync groups as accepted by `sync(streams=...)`; each group may feed several output streams.
GROUPS = ["heart_rate", "spo2", "stress", "sleep", "steps_daily", "hr_daily", "spo2_daily", "workouts"]
POINT_LIMIT = 1440


def metric_value(value: Any, fields: Iterable[str]) -> float | None:
    if isinstance(value, dict):
        for name in fields:
            if name in value:
                return _num(value[name])
        return None
    return _num(value)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def chunks(start: int, end: int, size: int) -> Iterable[tuple[int, int]]:
    while start < end:
        yield start, min(start + size, end)
        start += size


@dataclass
class RunReport:
    run_id: str
    added: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


class DataExtractor:
    def __init__(self, client: MiHealthClient, config: Config) -> None:
        self.client = client
        self.config = config
        self.state = LoadState(config.state_path)
        self.writer = ParquetWriter(config.raw_dir, config.tz)

    # ---- public API -------------------------------------------------------

    def extract(self, stream: str, since: int | None = None, until: int | None = None) -> pl.DataFrame:
        """Reads already-extracted rows of a stream from Parquet."""
        return self.writer.read(STREAMS[stream], since, until)

    def sync(self, streams: Iterable[str] | None = None, backfill_days: int | None = None,
             progress: Callable[[str, int, int], None] | None = None) -> RunReport:
        """Incrementally fetches new data for the given sync groups (all by default).

        `progress(group, index, total)` is called before each group, e.g. to drive a progress bar.
        """
        groups = list(streams or GROUPS)
        unknown = set(groups) - set(GROUPS)
        if unknown:
            raise ValueError(f"Unknown streams: {sorted(unknown)}; choose from {GROUPS}")
        backfill = backfill_days or self.config.backfill_days
        report = RunReport(self.state.start_run())
        handlers: dict[str, Callable[[int, RunReport], None]] = {
            **{m[2]: (lambda now, r, m=m: self._sync_point(m, now, backfill, r)) for m in POINT_METRICS},
            **{name: (lambda now, r, n=name: self._sync_daily(n, now, backfill, r)) for name in DAILY_METRICS},
            "sleep": lambda now, r: self._sync_sleep(now, backfill, r),
            "workouts": lambda now, r: self._sync_workouts(r),
        }
        for i, group in enumerate(groups):
            if progress:
                progress(group, i, len(groups))
            try:
                handlers[group](int(time.time()), report)
            except Exception as exc:  # keep other streams going; the watermark was not advanced
                log.exception("Stream %s failed", group)
                report.errors[group] = f"{type(exc).__name__}: {exc}"
        self.state.finish_run(report.run_id, report.added, json.dumps(report.errors) if report.errors else None)
        return report

    # ---- stream handlers ---------------------------------------------------

    def _write(self, stream: str, rows: list[dict[str, Any]], report: RunReport) -> None:
        added = self.writer.write(STREAMS[stream], rows)
        report.added[stream] = report.added.get(stream, 0) + added

    def _fetch_points(self, key: str, start: int, end: int) -> list[DataItem]:
        """Point samples for [start, end); splits the window while the server reports more pages."""
        items, has_more = self.client.get_fitness_data(key, start, end, limit=POINT_LIMIT)
        time.sleep(self.config.request_pause_s)
        if (has_more or len(items) >= POINT_LIMIT) and end - start > 600:
            mid = (start + end) // 2
            return self._fetch_points(key, start, mid) + self._fetch_points(key, mid, end)
        return items

    def _sync_point(self, metric: tuple, now: int, backfill: int, report: RunReport) -> None:
        api_key, fields, stream, column = metric
        start, end = self.state.window(stream, now, backfill, self.config.overlap_days)
        for chunk_start, chunk_end in chunks(start, end, DAY):
            rows = self._point_rows(metric, self._fetch_points(api_key, chunk_start, chunk_end))
            self._write(stream, rows, report)
            self.state.commit(stream, max_ts=max((r["ts"] for r in rows), default=None), synced_until=chunk_end)

    @staticmethod
    def _point_rows(metric: tuple, items: list[DataItem]) -> list[dict[str, Any]]:
        _, fields, stream, column = metric
        rows = []
        for item in items:
            value = metric_value(item.value, fields)
            if value is not None and value > 0:
                row = {"ts": item.time, column: int(value)}
                if "source" in STREAMS[stream].schema:
                    row["source"] = "point"
                rows.append(row)
        return rows

    def backfill_points(self, stream: str, start: int, end: int) -> int:
        """Fetches point samples of one stream for an arbitrary window (e.g. an old workout).

        Watermarks are not touched; returns the number of new rows written.
        """
        metric = next(m for m in POINT_METRICS if m[2] == stream)
        report = RunReport(run_id="backfill")
        self._write(stream, self._point_rows(metric, self._fetch_points(metric[0], start, end)), report)
        return report.added.get(stream, 0)

    def _fetch_daily(self, key: str, start: int, end: int) -> list[DataItem]:
        items: list[DataItem] = []
        for chunk_start, chunk_end in chunks(start, end, self.config.window_days * DAY):
            days = (chunk_end - chunk_start) // DAY + 1
            items += self.client.get_aggregated(key, chunk_start, chunk_end, limit=days)
            time.sleep(self.config.request_pause_s)
        return items

    def _sync_daily(self, stream: str, now: int, backfill: int, report: RunReport) -> None:
        api_key, fields = DAILY_METRICS[stream]
        start, end = self.state.window(stream, now, backfill, self.config.overlap_days)
        rows = []
        for item in self._fetch_daily(api_key, start, end):
            value = item.value if isinstance(item.value, dict) else {}
            rows.append({"ts": item.time, **{f: _int(value.get(f)) for f in fields}, "raw_json": item.raw_value})
        self._write(stream, rows, report)
        self.state.commit(stream, max_ts=max((r["ts"] for r in rows), default=None), synced_until=end)

    def _sync_sleep(self, now: int, backfill: int, report: RunReport) -> None:
        start, end = self.state.window("sleep_daily", now, backfill, self.config.overlap_days)
        daily, segments = [], []
        for item in self._fetch_daily("sleep", start, end):
            value = item.value if isinstance(item.value, dict) else {}
            daily.append({
                "ts": item.time,
                **{c: _int(value.get(c)) for c in STREAMS["sleep_daily"].schema if c not in ("ts", "raw_json", "extracted_at")},
                "raw_json": item.raw_value,
            })
            for seg in value.get("segment_details") or []:
                if isinstance(seg, dict) and seg.get("bedtime"):
                    segments.append({
                        "ts": _int(seg.get("bedtime")),
                        # Band id that recorded the night; FDS objects are keyed by it.
                        "did": (item.source_sid_list or [str(self.client.uid)])[0],
                        **{c: _int(seg.get(c)) for c in STREAMS["sleep_segments"].schema if c not in ("ts", "did", "extracted_at")},
                    })
        self._write("sleep_daily", daily, report)
        self._write("sleep_segments", segments, report)
        # FDS works off all stored nights in the backfill horizon, so nights that failed
        # earlier are retried even after they drop out of the overlap window.
        stored = self.extract("sleep_segments", since=now - (backfill + 1) * DAY)  # +1: nights crossing the edge
        self._sync_fds([r for r in stored.to_dicts() if r.get("did")], report)
        self.state.commit("sleep_daily", max_ts=max((r["ts"] for r in daily), default=None), synced_until=end)

    def _sync_fds(self, segments: list[dict[str, Any]], report: RunReport) -> None:
        """Per-night sleep detail objects: minute-level HR and SpO2 during sleep plus a sleep report."""
        self.config.raw_fds_dir.mkdir(parents=True, exist_ok=True)
        for seg in segments:
            wake, tz = seg.get("wake_up_time"), seg.get("timezone") or 0
            if not wake or not self.state.fds_needed(wake):
                continue
            try:
                blob = self.client.download_sleep_details(seg["did"], wake, tz)
                time.sleep(self.config.request_pause_s)
            except Exception as exc:
                log.warning("FDS download for %s failed: %s", wake, exc)
                self.state.mark_fds(wake, "failed")
                continue
            if blob is None:
                self.state.mark_fds(wake, "missing")
                continue
            # Keep the decrypted blob: sleep stages inside it are not decoded yet.
            (self.config.raw_fds_dir / f"{wake}.bin").write_bytes(blob)
            details = parse_all_day_sleep_bytes(blob)
            if details is None:
                self.state.mark_fds(wake, "failed")
                continue
            r = details.report
            self._write("heart_rate", [{"ts": t, "bpm": v, "source": "fds_sleep"} for t, v in details.heart_rate], report)
            self._write("spo2", [{"ts": t, "spo2": v, "source": "fds_sleep"} for t, v in details.spo2], report)
            self._write("sleep_report", [{
                "ts": wake, "fds_version": details.version, "sleep_finish": r.get("sleepFinish"),
                "device_bed_time": r.get("deviceBedTime"), "device_wakeup_time": r.get("deviceWakeupTime"),
                "sleep_quality": r.get("sleepQuality"), "sleep_efficiency": r.get("sleepEfficiency"),
                "entry_sleep_duration": r.get("entrySleepDuration"), "lin_bed_duration": r.get("linBedDuration"),
                "go_bed_time": r.get("goBedTime"), "leave_bed_time": r.get("leaveBedTime"),
                "hr_samples": len(details.heart_rate), "spo2_samples": len(details.spo2),
            }], report)
            self.state.mark_fds(wake, "ok")

    def _sync_workouts(self, report: RunReport) -> None:
        cursor = int(self.state.get("workouts").last_cursor or 0)
        for records, new_cursor in self.client.iter_sport_records(cursor):
            rows = []
            for rec in records:
                raw = rec.get("value")
                try:
                    value = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except json.JSONDecodeError:
                    value = {}
                start = _int(value.get("start_time") or rec.get("time")) or 0
                duration = _int(value.get("duration")) or 0
                rows.append({
                    "ts": start, "sid": str(rec.get("sid") or rec.get("did") or f"{rec.get('key')}-{start}"),
                    "sport_type": str(rec.get("key") or rec.get("category") or "unknown"),
                    "end_time": _int(value.get("end_time")) or start + duration, "duration_sec": duration,
                    "calories": _num(value.get("calories") or value.get("total_cal")),
                    "avg_hr": _int(value.get("avg_hrm")), "max_hr": _int(value.get("max_hrm")),
                    "min_hr": _int(value.get("min_hrm")), "watermark": _int(rec.get("watermark")),
                    "raw_json": json.dumps(value, ensure_ascii=False),
                })
            self._write("workouts", rows, report)
            self.state.commit("workouts", max_ts=max((r["ts"] for r in rows), default=None), cursor=str(new_cursor))
