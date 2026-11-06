"""Current bot user (Telegram id) for per-user data paths, settings, Redis keys and the LLM budget.

The bot sets it per update (middleware), the worker per task (Celery signals); `asyncio.to_thread`
copies the context, so repositories called from threads see it too. Unset means single-user mode:
CLI tools and tests keep using MIFIT_DATA_DIR directly.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

current: ContextVar[int | None] = ContextVar("current_user", default=None)


def get() -> int | None:
    uid = current.get()
    if uid is not None:
        return uid
    env = os.environ.get("MIFIT_USER_ID", "")
    return int(env) if env.isdigit() else None


def require() -> int:
    uid = get()
    if uid is None:
        raise RuntimeError("no current user")
    return uid


@contextmanager
def use(user_id: int | None) -> Iterator[None]:
    token = current.set(user_id)
    try:
        yield
    finally:
        current.reset(token)


def key(name: str) -> str:
    """Namespaces a settings/Redis key by the current user (unchanged in single-user mode)."""
    uid = get()
    return f"u{uid}:{name}" if uid is not None else name
