"""Runtime configuration, overridable via environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _default_data_dir() -> Path:
    """MIFIT_DATA_DIR, or its users/<id> subfolder when the bot/worker serves a specific user."""
    root = Path(os.environ.get("MIFIT_DATA_DIR", "data")).resolve()
    from core.user import get

    uid = get()
    return root / "users" / str(uid) if uid is not None else root


@dataclass(frozen=True)
class Config:
    data_dir: Path = field(default_factory=_default_data_dir)
    region: str = field(default_factory=lambda: os.environ.get("MIFIT_REGION", "ru"))
    tz: str = field(default_factory=lambda: os.environ.get("MIFIT_TZ", "Europe/Moscow"))
    user_agent: str = field(
        default_factory=lambda: os.environ.get("MIFIT_USER_AGENT", "Android-12-3.53.1-vivo-V2284A")
    )
    client_sign: str | None = field(default_factory=lambda: os.environ.get("MI_CLIENT_SIGN"))
    overlap_days: int = field(default_factory=lambda: _env_int("MIFIT_OVERLAP_DAYS", 2))
    backfill_days: int = field(default_factory=lambda: _env_int("MIFIT_BACKFILL_DAYS", 30))
    window_days: int = field(default_factory=lambda: _env_int("MIFIT_WINDOW_DAYS", 7))
    request_pause_s: float = field(default_factory=lambda: float(os.environ.get("MIFIT_REQUEST_PAUSE", "0.5")))
    timeout_s: float = 30.0

    @property
    def api_base(self) -> str:
        return os.environ.get("MIFIT_API_BASE") or f"https://{self.region}.hlth.io.mi.com"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "secrets" / "token.json"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state" / "extract_state.sqlite"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def raw_fds_dir(self) -> Path:
        return self.data_dir / "raw_fds"

    @property
    def probe_dir(self) -> Path:
        return self.data_dir / "probe"
