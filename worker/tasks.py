"""Celery tasks. Long ones report progress via `update_state(state="PROGRESS", meta={...})`."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import redis

from ai.analysis import analyze
from ai.facts import overview_facts, period_facts, workout_facts
from analytics.periods import latest_date, period_view, workout_view
from analytics.processor import Processor
from charts import render_period, render_workout
from core import data, user
from core.config import settings
from core.fmt import day
from extractor.auth import AuthError, TokenExpiredError, XiaomiAuth
from extractor.client import MiHealthClient
from extractor.config import Config
from extractor.sqlite_reader import DataExtractor

from .celery_app import app

log = logging.getLogger(__name__)


def sync_task_key() -> str:
    """Redis key holding the running sync task id of the current user."""
    return user.key("sync:task")


@lru_cache
def rds() -> redis.Redis:
    return redis.Redis.from_url(settings().redis_url, decode_responses=True)


def today() -> date:
    return datetime.now(ZoneInfo(Config().tz)).date()


def _charts_dir() -> Path:
    p = Config().data_dir / "cache" / "charts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _xiaomi_client(base: Config) -> MiHealthClient:
    auth = XiaomiAuth.from_file(base.token_path, client_sign=base.client_sign, timeout=base.timeout_s)
    return MiHealthClient(auth, base)


# ---- sync / login --------------------------------------------------------------------

@app.task(bind=True, name="sync_data")
def sync_data(self, backfill_days: int | None = None) -> dict[str, Any]:
    r = rds()
    lock = r.lock(user.key("lock:sync"), timeout=1800)
    if not lock.acquire(blocking=False):
        return {"status": "busy", "task_id": r.get(sync_task_key())}
    r.set(sync_task_key(), self.request.id or "", ex=1800)
    started = time.monotonic()
    try:
        base = Config()
        if not base.token_path.exists():
            return {"status": "auth_required", "reason": "Вход в Xiaomi ещё не выполнен"}
        self.update_state(state="PROGRESS", meta={"stage": "connect"})
        client = _xiaomi_client(base)
        try:
            report = DataExtractor(client, base).sync(
                backfill_days=backfill_days,
                progress=lambda g, i, n: self.update_state(state="PROGRESS",
                                                           meta={"stage": "download", "group": g, "i": i, "n": n}))
            client.auth.save()
        except (AuthError, TokenExpiredError) as exc:
            return {"status": "auth_required", "reason": str(exc)}
        finally:
            client.close()
        if report.errors and all("Token" in e or "AuthError" in e for e in report.errors.values()):
            return {"status": "auth_required", "reason": "Токен Xiaomi истёк"}
        self.update_state(state="PROGRESS", meta={"stage": "build"})
        cfg = data.analytics_config()
        Processor(cfg).build()
        data.touch_version(cfg)
        return {"status": "ok", "added": report.added, "errors": report.errors,
                "seconds": round(time.monotonic() - started)}
    finally:
        r.delete(sync_task_key())
        try:
            lock.release()
        except redis.exceptions.LockError:
            pass


@app.task(bind=True, name="xiaomi_login")
def xiaomi_login(self) -> dict[str, Any]:
    base = Config()
    auth = XiaomiAuth(token_path=base.token_path, client_sign=base.client_sign, timeout=base.timeout_s)
    try:
        auth.login_qr(lambda image, url: self.update_state(state="PROGRESS", meta={"stage": "qr", "image": image,
                                                                                    "url": url}), max_wait=300)
    except Exception as exc:  # noqa: BLE001 - shown to the user as a status
        return {"status": "error", "reason": str(exc)}
    auth.save()
    return {"status": "ok"}


# ---- dashboards and analyses ---------------------------------------------------------

def _view(metric: str, period: str, end: str | None):
    proc = data.processor()
    view = period_view(proc, metric, period, date.fromisoformat(end) if end else None, today())
    return proc, view


@app.task(name="render_dashboard")
def render_dashboard(metric: str, period: str, end: str | None = None) -> dict[str, Any]:
    proc, view = _view(metric, period, end)
    if view is None:
        return {"status": "no_data"}
    sheet = period_facts(proc, view)
    cfg = proc.cfg
    path = _charts_dir() / f"{metric}_{period}_{view.end}_{int(data.data_version(cfg))}_{cfg.sleep_need_min}.png"
    if not path.exists() or not view.complete:
        path.write_bytes(render_period(view, sheet.kpis, cfg.sleep_need_min))
    latest = latest_date(proc, metric)
    return {"status": "ok", "path": str(path), "title": sheet.title, "kpis": sheet.kpis,
            "start": view.start.isoformat(), "end": view.end.isoformat(), "has_data": view.has_data,
            "has_next": latest is not None and view.end < latest,
            "latest": latest.isoformat() if latest else None}


@app.task(name="analyze_period")
def analyze_period(metric: str, period: str, end: str | None = None, force: bool = False) -> dict[str, Any]:
    proc, view = _view(metric, period, end)
    if view is None:
        return {"status": "no_data"}
    sheet = period_facts(proc, view)
    return {"status": "ok", "title": sheet.title, **analyze(sheet, force=force).to_dict()}


def _maybe_backfill_hr(ts: int) -> bool:
    """Tries once per workout to fetch minute HR for its window from the cloud."""
    r, base = rds(), Config()
    key = user.key(f"hr_backfill:{ts}")
    if not base.token_path.exists() or not r.set(key, "1", nx=True, ex=7 * 86400):
        return False
    row = data.processor().f.workouts.filter(data.processor().f.workouts["ts"] == ts)
    if row.is_empty():
        return False
    client = _xiaomi_client(base)
    try:
        added = DataExtractor(client, base).backfill_points("heart_rate", ts - 60, int(row["end_time"][0]) + 60)
    except Exception as exc:  # noqa: BLE001 - optional enrichment
        log.info("HR backfill for workout %s failed: %s", ts, exc)
        return False
    finally:
        client.close()
    if added:
        data.touch_version(data.analytics_config())
    return bool(added)


@app.task(name="render_workout")
def render_workout_task(ts: int) -> dict[str, Any]:
    proc = data.processor()
    v = workout_view(proc, ts)
    if v is None:
        return {"status": "no_data"}
    if v["track"].is_empty() and _maybe_backfill_hr(ts):
        proc = data.processor()
        v = workout_view(proc, ts)
    sheet = workout_facts(proc, ts)
    path = _charts_dir() / f"workout_{ts}_{int(data.data_version(proc.cfg))}.png"
    if not path.exists():
        name, when = sheet.title.split(" · ")[1], sheet.title.split(" · ")[2]
        path.write_bytes(render_workout(v, name, when, sheet.kpis))
    return {"status": "ok", "path": str(path), "title": sheet.title, "kpis": sheet.kpis,
            "has_track": not v["track"].is_empty()}


@app.task(name="analyze_workout")
def analyze_workout(ts: int, force: bool = False) -> dict[str, Any]:
    sheet = workout_facts(data.processor(), ts)
    if sheet is None:
        return {"status": "no_data"}
    return {"status": "ok", "title": sheet.title, **analyze(sheet, force=force).to_dict()}


def brief_dates(now: datetime) -> tuple[date, date]:
    """(activity date, sleep date): before noon the day is too fresh, so activity is yesterday's."""
    d = now.date()
    return (d - timedelta(days=1) if now.hour < 12 else d), d


@app.task(name="daily_brief")
def daily_brief(force: bool = False) -> dict[str, Any]:
    proc = data.processor()
    now = datetime.now(ZoneInfo(proc.cfg.tz))
    target, sleep_d = brief_dates(now)
    if proc.nights.is_empty() or proc.nights.filter(proc.nights["date"] == sleep_d).is_empty():
        sleep_d = latest_date(proc, "sleep") or sleep_d
    sheet = overview_facts(proc, target, now.date(), sleep_date=sleep_d)
    res = analyze(sheet, force=force)
    return {"status": "ok", "title": sheet.title, "facts": [f.text for f in sheet.ranked()[:8]], **res.to_dict()}


# ---- scheduled ---------------------------------------------------------------------------

@app.task(name="tick")
def tick() -> dict[str, Any]:
    """Scheduled work for every allowed user, each in their own data, settings and chat."""
    done: dict[str, Any] = {}
    for uid in sorted(settings().owners):
        with user.use(uid):
            try:
                done[str(uid)] = _tick_user(uid)
            except Exception:  # noqa: BLE001 - one user's failure must not stop the others
                log.exception("tick failed for user %s", uid)
    return done


def _tick_user(uid: int) -> dict[str, Any]:
    from bot.render import brief_html  # pure formatting, no aiogram objects
    from core.push import send_message
    from storage.db import get_setting, set_setting

    done: dict[str, Any] = {}
    cfg = settings()
    if not Config().token_path.exists():
        return done  # not logged in to Xiaomi yet: nothing to sync, brief or alert
    if get_setting("autosync") == "1":
        last = data.last_sync() or 0
        if time.time() - last >= cfg.autosync_hours * 3600 and not rds().get(sync_task_key()):
            sync_data.delay()
            done["autosync"] = True
    now = datetime.now(ZoneInfo(Config().tz))
    if get_setting("brief_enabled") == "1" and get_setting("brief_sent_on") != now.date().isoformat():
        hh, mm = map(int, (get_setting("brief_time") or cfg.brief_time).split(":"))
        if (now.hour, now.minute) >= (hh, mm):
            res = daily_brief.run()
            send_message(uid, brief_html(res, morning=True))
            set_setting("brief_sent_on", now.date().isoformat())
            done["brief"] = True
    if get_setting("alerts") == "1":
        done["alerts"] = _alerts(uid, now.date())
    return done


ALERT_TYPES = {"sleep_hr_elevated", "hr_spike", "bradycardia"}


def _alerts(uid: int, d: date) -> int:
    from bot.render import alert_html
    from core.push import send_message

    proc, r, sent = data.processor(), rds(), 0
    for a in proc.anomalies(d):
        if a["type"] not in ALERT_TYPES:
            continue
        key = user.key(f"alerted:{d}:{a['type']}:{a.get('at', '')}")
        if r.set(key, "1", nx=True, ex=3 * 86400):
            send_message(uid, alert_html(a, day(d)))
            sent += 1
    return sent
