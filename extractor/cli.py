"""Command line: login | sync | probe | status."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone

from .auth import XiaomiAuth
from .client import AGGREGATED_PATH, FITNESS_PATH, LATEST_PATH, MiHealthClient
from .config import Config
from .sqlite_reader import GROUPS, DataExtractor
from .state import DAY, LoadState

# Keys tried by `probe --scan`; HRV is not used by any known client, so these are guesses.
CANDIDATE_KEYS = [
    "heart_rate", "spo2", "stress", "sleep", "steps", "calories", "intensity", "valid_stand",
    "hrv", "heart_rate_variability", "hrv_rmssd", "sleep_hrv", "resting_heart_rate", "rhr",
    "body_energy", "vitality", "breath", "respiratory_rate", "skin_temperature", "temperature",
]


def _client(config: Config) -> MiHealthClient:
    if not config.token_path.exists():
        sys.exit(f"Token not found at {config.token_path}; run `login` first")
    auth = XiaomiAuth.from_file(config.token_path, client_sign=config.client_sign, timeout=config.timeout_s)
    return MiHealthClient(auth, config)


def cmd_login(config: Config, args: argparse.Namespace) -> None:
    import qrcode

    def show(image_url: str, login_url: str) -> None:
        qr = qrcode.QRCode(border=1)
        qr.add_data(login_url)
        qr.print_ascii(invert=True)
        print(
            "\nConfirm the login with your Xiaomi account, either way:\n"
            "  1) scan the QR above with the phone camera, or\n"
            f"  2) open this link in a browser where you can sign in to Xiaomi:\n     {login_url}\n"
            f"QR as image: {image_url}\n"
            "Waiting for confirmation (up to 5 min)...\n"
        )

    auth = XiaomiAuth(token_path=config.token_path, client_sign=config.client_sign, timeout=config.timeout_s)
    auth.login_qr(show, max_wait=args.timeout)
    auth.save()
    print(f"Logged in as user {auth.token.user_id}; token saved to {config.token_path}")


def cmd_sync(config: Config, args: argparse.Namespace) -> None:
    client = _client(config)
    try:
        report = DataExtractor(client, config).sync(args.streams or None, args.backfill_days)
        client.auth.save()  # persist a refreshed serviceToken, if any
    finally:
        client.close()
    for stream, added in sorted(report.added.items()):
        print(f"{stream:16} +{added}")
    for stream, error in report.errors.items():
        print(f"{stream:16} ERROR {error}", file=sys.stderr)
    sys.exit(0 if report.ok else 1)


def cmd_probe(config: Config, args: argparse.Namespace) -> None:
    """Dumps decrypted raw API responses for manual reverse engineering."""
    client = _client(config)
    config.probe_dir.mkdir(parents=True, exist_ok=True)
    end = int(time.time())
    start = end - args.days * DAY
    keys = CANDIDATE_KEYS if args.scan else (args.key or [])
    jobs: list[tuple[str, str, dict]] = []
    if args.endpoint == "latest":
        jobs.append(("latest", LATEST_PATH, {"relative_uid": client.uid}))
    elif args.endpoint == "raw":
        jobs.append(("raw", args.path, json.loads(args.params or "{}")))
    for key in keys:
        limit = args.limit or (args.days + 1 if args.endpoint == "aggregated" else 1440)
        params = {"relative_uid": client.uid, "key": key, "start_time": start, "end_time": end, "limit": limit}
        if args.endpoint == "aggregated":
            jobs.append((key, AGGREGATED_PATH, {**params, "tag": args.tag}))
        elif args.endpoint == "fitness":
            jobs.append((key, FITNESS_PATH, params))
    try:
        for name, path, params in jobs:
            try:
                result = client.request("GET", path, params)
                items = (result.get("result") or {}).get("data_list") or []
                summary = f"{len(items)} items" if isinstance(items, list) else "ok"
            except Exception as exc:
                result, summary = {"error": f"{type(exc).__name__}: {exc}"}, "error"
            out = config.probe_dir / f"{args.endpoint}_{name}_{end}.json"
            out.write_text(json.dumps({"path": path, "params": params, "response": result},
                                      ensure_ascii=False, indent=2), "utf-8")
            print(f"{name:24} {summary:20} -> {out}")
            time.sleep(config.request_pause_s)
    finally:
        client.auth.save()
        client.close()


def cmd_status(config: Config, args: argparse.Namespace) -> None:
    def fmt(ts: int | None) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else "-"

    print(f"{'stream':16} {'last data':22} {'synced until':22} cursor")
    for stream, last_ts, synced_until, cursor, _ in LoadState(config.state_path).summary():
        print(f"{stream:16} {fmt(last_ts):22} {fmt(synced_until):22} {cursor or '-'}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mifit-extract", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="QR login to the Xiaomi account")
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("sync", help="incrementally extract new data to Parquet")
    p.add_argument("--streams", nargs="*", choices=GROUPS)
    p.add_argument("--backfill-days", type=int, help="history depth on the first run")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("probe", help="dump raw decrypted API responses")
    p.add_argument("--endpoint", choices=["aggregated", "fitness", "latest", "raw"], default="fitness")
    p.add_argument("--key", action="append", help="data key (repeatable)")
    p.add_argument("--scan", action="store_true", help="try all candidate keys (HRV discovery)")
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--limit", type=int, help="default: 1440 for fitness, days+1 for aggregated")
    p.add_argument("--tag", default="daily_report")
    p.add_argument("--path", help="API path for --endpoint raw")
    p.add_argument("--params", help="JSON params for --endpoint raw")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("status", help="show incremental-load watermarks")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # login prompts must show up even when output is piped
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs, which carry auth tickets and signed params.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(Config(), args)
