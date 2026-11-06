"""Incremental Load Checker: per-stream watermarks, FDS download ledger, run log."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

DAY = 86_400
MAX_FDS_ATTEMPTS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark (
    stream TEXT PRIMARY KEY,
    last_ts INTEGER,          -- max data timestamp seen for the stream
    synced_until INTEGER,     -- end of the last successfully written fetch window
    last_cursor TEXT,         -- server-side cursor (workouts watermark)
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fds_done (
    wakeup_ts INTEGER PRIMARY KEY,
    status TEXT NOT NULL,     -- ok | missing | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    fetched_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    stream_counts_json TEXT,
    error TEXT
);
"""


@dataclass(frozen=True)
class Watermark:
    last_ts: int | None
    synced_until: int | None
    last_cursor: str | None


class LoadState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def get(self, stream: str) -> Watermark:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT last_ts, synced_until, last_cursor FROM watermark WHERE stream = ?", (stream,)
            ).fetchone()
        return Watermark(*row) if row else Watermark(None, None, None)

    def window(self, stream: str, now: int, backfill_days: int, overlap_days: int,
               max_days: int = 365) -> tuple[int, int]:
        """Fetch window [start, now] for a time-based stream.

        Restart from the newest data seen (minus an overlap for late band syncs and
        recomputed daily summaries); fall back to the last synced window end for
        streams that returned nothing yet, or to a backfill on the first run.
        """
        mark = self.get(stream)
        anchor = mark.last_ts if mark.last_ts is not None else mark.synced_until
        if anchor is None:
            start = now - backfill_days * DAY
        else:
            start = min(anchor, now) - overlap_days * DAY
        return max(start, now - max_days * DAY), now

    def commit(self, stream: str, *, max_ts: int | None = None, synced_until: int | None = None,
               cursor: str | None = None) -> None:
        """Advance the watermark; call only after the data has been durably written."""
        prev = self.get(stream)
        known = [v for v in (prev.last_ts, max_ts) if v is not None]
        last_ts = max(known) if known else None
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO watermark (stream, last_ts, synced_until, last_cursor, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(stream) DO UPDATE SET
                     last_ts = excluded.last_ts,
                     synced_until = COALESCE(excluded.synced_until, watermark.synced_until),
                     last_cursor = COALESCE(excluded.last_cursor, watermark.last_cursor),
                     updated_at = excluded.updated_at""",
                (stream, last_ts, synced_until, cursor, int(time.time())),
            )

    def fds_needed(self, wakeup_ts: int) -> bool:
        with closing(self._connect()) as db:
            row = db.execute("SELECT status, attempts FROM fds_done WHERE wakeup_ts = ?", (wakeup_ts,)).fetchone()
        return row is None or (row[0] != "ok" and row[1] < MAX_FDS_ATTEMPTS)

    def mark_fds(self, wakeup_ts: int, status: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO fds_done (wakeup_ts, status, attempts, fetched_at) VALUES (?, ?, 1, ?)
                   ON CONFLICT(wakeup_ts) DO UPDATE SET
                     status = excluded.status, attempts = fds_done.attempts + 1, fetched_at = excluded.fetched_at""",
                (wakeup_ts, status, int(time.time())),
            )

    def start_run(self) -> str:
        run_id = uuid.uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO runs (run_id, started_at) VALUES (?, ?)", (run_id, int(time.time())))
        return run_id

    def finish_run(self, run_id: str, counts: dict[str, int], error: str | None = None) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE runs SET finished_at = ?, stream_counts_json = ?, error = ? WHERE run_id = ?",
                (int(time.time()), json.dumps(counts), error, run_id),
            )

    def last_run(self) -> tuple[int, int | None, dict[str, int], str | None] | None:
        """(started_at, finished_at, counts, error) of the latest run, or None."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT started_at, finished_at, stream_counts_json, error FROM runs "
                             "ORDER BY started_at DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return row[0], row[1], json.loads(row[2] or "{}"), row[3]

    def last_success(self) -> int | None:
        """finished_at of the latest run that completed without errors."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT max(finished_at) FROM runs WHERE finished_at IS NOT NULL AND error IS NULL").fetchone()
        return row[0] if row else None

    def summary(self) -> list[tuple]:
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT stream, last_ts, synced_until, last_cursor, updated_at FROM watermark ORDER BY stream"
            ).fetchall()
