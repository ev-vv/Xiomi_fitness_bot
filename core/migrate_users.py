"""One-time move of single-user data into data/users/<id>/ (run with the containers stopped).

    python -m core.migrate_users <telegram_id> [--no-backup]

Makes a copy of the data dir first, then moves the per-user files and namespaces the per-user rows in
the bot database. Safe to run again: what is already moved or prefixed is skipped.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

# Per-user entries of the data dir; state/bot.sqlite (the shared bot DB) stays where it is.
USER_ENTRIES = ("raw", "raw_fds", "gold", "secrets", "cache", "reports", "probe", "warehouse.duckdb",
                "state/extract_state.sqlite")
PREFIXED = re.compile(r"^u\d+:")


def move_files(root: Path, uid: int) -> list[str]:
    dest = root / "users" / str(uid)
    moved = []
    for entry in USER_ENTRIES:
        src, dst = root / entry, dest / entry
        if not src.exists() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved.append(entry)
    if (dest / "secrets").exists():
        (dest / "secrets").chmod(0o700)
    return moved


def namespace_db(db: Path, uid: int) -> dict[str, int]:
    counts = {"settings": 0, "analyses": 0, "llm_usage": 0}
    if not db.exists():
        return counts
    con = sqlite3.connect(db)
    try:
        with con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("settings", "analyses"):
                if table not in tables:
                    continue
                for (key,) in con.execute(f"SELECT key FROM {table}").fetchall():
                    if not PREFIXED.match(key):
                        con.execute(f"UPDATE OR IGNORE {table} SET key = ? WHERE key = ?", (f"u{uid}:{key}", key))
                        counts[table] += 1
            if "llm_usage" in tables:
                cols = {r[1] for r in con.execute("PRAGMA table_info(llm_usage)")}
                if "user_id" not in cols:
                    con.execute("ALTER TABLE llm_usage ADD COLUMN user_id INTEGER")
                counts["llm_usage"] = con.execute("UPDATE llm_usage SET user_id = ? WHERE user_id IS NULL",
                                                  (uid,)).rowcount
    finally:
        con.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("telegram_id", type=int, help="Telegram id of the current (first) user")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()
    root = Path(os.environ.get("MIFIT_DATA_DIR", "data")).resolve()
    db = Path(os.environ.get("DB_PATH", "data/state/bot.sqlite")).resolve()
    if not args.no_backup:
        backup = root.with_name(f"{root.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copytree(root, backup, symlinks=True)
        print(f"backup: {backup}")
    print("moved:", ", ".join(move_files(root, args.telegram_id)) or "nothing (already migrated)")
    print("database:", namespace_db(db, args.telegram_id))


if __name__ == "__main__":
    main()
