"""Process-wide cached `Processor`, reloaded when the worker publishes a new data version.

After every sync/build the worker touches `data/gold/.version`; readers compare its mtime, so the
bot and all worker processes pick up fresh data without any extra signalling channel.
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path

from analytics.config import AnalyticsConfig
from analytics.processor import Processor
from extractor.state import LoadState

_lock = threading.Lock()
_cache: dict[str, object] = {}


def analytics_config() -> AnalyticsConfig:
    """AnalyticsConfig with the user's sleep goal from the settings table."""
    from storage.db import get_setting

    cfg = AnalyticsConfig()
    try:
        need = int(get_setting("sleep_need_min") or cfg.sleep_need_min)
    except ValueError:
        need = cfg.sleep_need_min
    return dataclasses.replace(cfg, sleep_need_min=need)


def version_path(cfg: AnalyticsConfig) -> Path:
    return cfg.gold_dir / ".version"


def data_version(cfg: AnalyticsConfig) -> float:
    p = version_path(cfg)
    return p.stat().st_mtime if p.exists() else 0.0


def touch_version(cfg: AnalyticsConfig) -> None:
    p = version_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def processor(cfg: AnalyticsConfig | None = None) -> Processor:
    """One cached Processor per data dir (i.e. per user), rebuilt when its data version or sleep goal changes."""
    cfg = cfg or analytics_config()
    key = (data_version(cfg), cfg.sleep_need_min)
    with _lock:
        hit = _cache.get(str(cfg.data_dir))
        if hit is None or hit[0] != key:  # type: ignore[index]
            hit = _cache[str(cfg.data_dir)] = (key, Processor(cfg))
        return hit[1]  # type: ignore[index]


def reset() -> None:
    with _lock:
        _cache.clear()


def last_sync(cfg: AnalyticsConfig | None = None) -> int | None:
    cfg = cfg or AnalyticsConfig()
    if not cfg.base.state_path.exists():
        return None
    return LoadState(cfg.base.state_path).last_success()
