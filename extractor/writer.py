"""Idempotent Parquet writer: Hive partitions by local date, dedup by primary key."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import polars as pl

from .models import StreamSpec


class ParquetWriter:
    def __init__(self, root: Path, tz: str) -> None:
        self.root = root
        self.tz = tz

    def to_frame(self, spec: StreamSpec, rows: Iterable[Mapping[str, Any]]) -> pl.DataFrame:
        now = int(time.time())
        records = [{**{c: r.get(c) for c in spec.schema}, "extracted_at": now} for r in rows]
        return pl.DataFrame(records, schema=spec.schema, orient="row") if records else pl.DataFrame(schema=spec.schema)

    def write(self, spec: StreamSpec, rows: Iterable[Mapping[str, Any]]) -> int:
        """Merges rows into their date partitions; returns the number of new primary keys added."""
        frame = self.to_frame(spec, rows).filter(pl.col("ts").is_not_null())
        if frame.is_empty():
            return 0
        frame = frame.with_columns(
            pl.from_epoch("ts", time_unit="s").dt.replace_time_zone("UTC")
            .dt.convert_time_zone(self.tz).dt.strftime("%Y-%m-%d").alias("_date")
        )
        added = 0
        for (date,), part in frame.group_by("_date"):
            added += self._merge(spec, self.root / spec.name / f"date={date}" / "data.parquet", part.drop("_date"))
        return added

    def _merge(self, spec: StreamSpec, path: Path, new: pl.DataFrame) -> int:
        before = 0
        if path.exists():
            existing = pl.read_parquet(path)
            # Schema evolution: columns added to the spec later are filled with nulls.
            missing = [pl.lit(None, dtype).alias(c) for c, dtype in spec.schema.items() if c not in existing.columns]
            existing = existing.with_columns(missing).select(list(spec.schema)).cast(spec.schema)  # type: ignore[arg-type]
            before = existing.height
            new = pl.concat([existing, new], how="vertical")
        merged = new.unique(subset=list(spec.pk), keep="last", maintain_order=True).sort("ts")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            merged.write_parquet(tmp, compression="zstd")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return merged.height - before

    def read(self, spec: StreamSpec, since: int | None = None, until: int | None = None) -> pl.DataFrame:
        base = self.root / spec.name
        if not any(base.glob("date=*/data.parquet")):
            return pl.DataFrame(schema=spec.schema)
        lf = pl.scan_parquet(base / "date=*" / "data.parquet", hive_partitioning=False)
        if since is not None:
            lf = lf.filter(pl.col("ts") >= since)
        if until is not None:
            lf = lf.filter(pl.col("ts") < until)
        return lf.sort("ts").collect()
