"""Celery application: RabbitMQ broker, Redis results, a 5-minute `tick` for scheduled work."""

from __future__ import annotations

from typing import Any

from celery import Celery, Task

from core import user
from core.config import settings
from extractor.config import Config


class UserTask(Task):
    """Carries the current bot user to the worker: added as a `user_id` kwarg on publish, applied on run."""

    typing = False  # the extra `user_id` kwarg is consumed in __call__, not by the task functions

    def apply_async(self, args: Any = None, kwargs: Any = None, **options: Any) -> Any:
        kwargs = dict(kwargs or {})
        if (uid := user.get()) is not None:
            kwargs.setdefault("user_id", uid)
        return super().apply_async(args, kwargs, **options)

    def __call__(self, *args: Any, user_id: int | None = None, **kwargs: Any) -> Any:
        with user.use(user_id if user_id is not None else user.get()):
            return super().__call__(*args, **kwargs)


app = Celery("fitness", broker=settings().amqp_url, backend=settings().redis_url, include=["worker.tasks"],
             task_cls=UserTask)
app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=3600,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    # Some VPN/DNS setups make the AMQP handshake take ~5 s; the default 4 s timeout would never connect.
    broker_connection_timeout=15,
    timezone=Config().tz,
    beat_schedule={"tick": {"task": "tick", "schedule": 300.0}},
)
