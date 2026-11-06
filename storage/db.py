"""Small synchronous repository; the bot calls it through `asyncio.to_thread`."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Select, create_engine, delete, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core import user
from core.config import settings

from .models import Analysis, Base, ChatMessage, LlmUsage, Setting

# User-editable settings with their defaults (strings, parsed by the callers).
DEFAULTS = {
    "sleep_need_min": "480",
    "brief_time": None,  # filled from Settings.brief_time
    "brief_enabled": "1",
    "autosync": "1",
    "alerts": "1",
    "brief_sent_on": "",
}


_engine_lock = threading.Lock()


def engine(path: str | None = None) -> Engine:
    # The bot opens the DB from several to_thread workers at once; create the tables only once.
    with _engine_lock:
        return _engine(path)


@lru_cache
def _engine(path: str | None) -> Engine:
    db = Path(path or settings().db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    _migrate(eng)
    return eng


def _migrate(eng: Engine) -> None:
    """Additive schema changes for databases created by older versions (create_all never alters tables)."""
    if "user_id" not in {c["name"] for c in inspect(eng).get_columns("llm_usage")}:
        with eng.begin() as con:
            con.execute(text("ALTER TABLE llm_usage ADD COLUMN user_id INTEGER"))


engine.cache_clear = _engine.cache_clear  # type: ignore[attr-defined]


def _session() -> Session:
    return Session(engine(), expire_on_commit=False)


# ---- settings -------------------------------------------------------------------

def get_setting(key: str) -> str:
    """Per-user value (keys are namespaced by core.user), falling back to the defaults."""
    with _session() as s:
        row = s.get(Setting, user.key(key))
    if row is not None:
        return row.value
    if key == "brief_time":
        return settings().brief_time
    return DEFAULTS.get(key) or ""


def set_setting(key: str, value: str) -> None:
    with _session() as s, s.begin():
        s.merge(Setting(key=user.key(key), value=value))


# ---- chat history ---------------------------------------------------------------

def add_chat(chat_id: int, role: str, text: str) -> None:
    with _session() as s, s.begin():
        s.add(ChatMessage(chat_id=chat_id, role=role, text=text))


def recent_chat(chat_id: int, limit: int = 20) -> list[dict[str, str]]:
    """Last `limit` messages as API message dicts, oldest first, starting with a user turn."""
    with _session() as s:
        rows = s.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat_id)
                         .order_by(ChatMessage.id.desc()).limit(limit)).all()
    msgs = [{"role": r.role, "content": r.text} for r in reversed(rows)]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def clear_chat(chat_id: int) -> None:
    with _session() as s, s.begin():
        s.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_id))


# ---- analysis cache -------------------------------------------------------------

def get_analysis(key: str) -> dict[str, Any] | None:
    with _session() as s:
        row = s.get(Analysis, key)
    return row.payload if row else None


def put_analysis(key: str, payload: dict[str, Any]) -> None:
    with _session() as s, s.begin():
        s.merge(Analysis(key=key, payload=payload))


# ---- LLM usage ------------------------------------------------------------------

def add_usage(route: str, model: str, usage: dict[str, int], cost_usd: float) -> None:
    with _session() as s, s.begin():
        s.add(LlmUsage(user_id=user.get(), route=route, model=model, cost_usd=cost_usd,
                       input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
                       cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                       cache_write_tokens=usage.get("cache_creation_input_tokens", 0)))


def _mine(q: Select) -> Select:
    uid = user.get()
    return q.where(LlmUsage.user_id == uid) if uid is not None else q


def spent_since(since: datetime) -> float:
    """Current user's LLM spend (everyone's in single-user mode)."""
    with _session() as s:
        return float(s.scalar(_mine(select(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0))
                                    .where(LlmUsage.created_at >= since))) or 0.0)


def spent_today() -> float:
    return spent_since(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))


def usage_summary(days: int = 30) -> list[tuple[str, int, int, int, int, float]]:
    """(route, calls, input, output, cache_read, cost) over the last `days`."""
    since = datetime.now() - timedelta(days=days)
    with _session() as s:
        rows = s.execute(_mine(
            select(LlmUsage.route, func.count(), func.sum(LlmUsage.input_tokens), func.sum(LlmUsage.output_tokens),
                   func.sum(LlmUsage.cache_read_tokens), func.sum(LlmUsage.cost_usd))
            .where(LlmUsage.created_at >= since)).group_by(LlmUsage.route)
        ).all()
    return [tuple(r) for r in rows]
