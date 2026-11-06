"""Command line: init-db | build | report | schema | sql."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import AnalyticsConfig
from .processor import Processor
from .report import json_schema
from .warehouse import connect, init_db


def cmd_init_db(cfg: AnalyticsConfig, args: argparse.Namespace) -> None:
    print(f"Views created in {init_db(cfg)}")


def cmd_build(cfg: AnalyticsConfig, args: argparse.Namespace) -> None:
    for table, rows in Processor(cfg).build().items():
        print(f"{table:16} {rows} rows -> {cfg.gold_dir / table}")


def cmd_report(cfg: AnalyticsConfig, args: argparse.Namespace) -> None:
    target = args.date or datetime.now(ZoneInfo(cfg.tz)).date() - timedelta(days=1)
    path, report = Processor(cfg).write_report(target)
    size = len(report.to_json().encode())
    if args.stdout:
        print(json.dumps(report.compact(), ensure_ascii=False, indent=1))
    print(f"{path} ({size} bytes compact)", file=sys.stderr)
    if size > cfg.report_max_bytes:
        print(f"warning: report exceeds {cfg.report_max_bytes} bytes", file=sys.stderr)


def cmd_schema(cfg: AnalyticsConfig, args: argparse.Namespace) -> None:
    print(json.dumps(json_schema(), ensure_ascii=False, indent=1))


def cmd_sql(cfg: AnalyticsConfig, args: argparse.Namespace) -> None:
    con = connect(cfg)
    try:
        con.sql(args.query).show(max_rows=args.limit)
    finally:
        con.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m analytics", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create/refresh DuckDB views in data/warehouse.duckdb")
    sub.add_parser("build", help="recompute gold tables (daily_metrics, sleep_nights, anomalies)")
    p = sub.add_parser("report", help="write the LLM JSON report for a day (default: yesterday)")
    p.add_argument("--date", type=date.fromisoformat)
    p.add_argument("--stdout", action="store_true", help="also print the report")
    sub.add_parser("schema", help="print the report JSON Schema")
    p = sub.add_parser("sql", help="run a query against the views")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=40)
    args = parser.parse_args(argv)
    handler = {"init-db": cmd_init_db, "build": cmd_build, "report": cmd_report,
               "schema": cmd_schema, "sql": cmd_sql}[args.cmd]
    handler(AnalyticsConfig(), args)
