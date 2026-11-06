"""Live status messages: throttled edits, progress bars, spinners and Celery task tracking."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

SPINNER = "◐◓◑◒"
MIN_EDIT_INTERVAL = 1.1  # Telegram tolerates ~1 edit/s per chat


def bar(i: int, n: int, width: int = 8) -> str:
    k = max(0, min(width, round(width * i / max(n, 1))))
    return "▰" * k + "▱" * (width - k)


def spinner(t: float) -> str:
    return SPINNER[int(t * 2) % len(SPINNER)]


class Status:
    """One message that is edited in place; edits are throttled and deduplicated."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self.bot, self.chat_id = bot, chat_id
        self.message: Message | None = None
        self._text = ""
        self._last = 0.0

    async def show(self, text: str, kb: InlineKeyboardMarkup | None = None, force: bool = False) -> None:
        if text == self._text and kb is None:
            return
        now = time.monotonic()
        if not force and self.message and now - self._last < MIN_EDIT_INTERVAL:
            return
        self._text, self._last = text, now
        try:
            if self.message is None:
                self.message = await self.bot.send_message(self.chat_id, text, reply_markup=kb)
            else:
                await self.message.edit_text(text, reply_markup=kb)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
        except TelegramBadRequest:  # "message is not modified" or the message was deleted
            pass

    async def restart(self, text: str) -> None:
        """Moves the status below newly sent content."""
        await self.delete()
        await self.show(text, force=True)

    async def delete(self) -> None:
        if self.message is not None:
            try:
                await self.message.delete()
            except TelegramBadRequest:
                pass
        self.message, self._text = None, ""


class TaskFailed(Exception):
    pass


async def track(task_id: str, on_progress: Callable[[str, dict[str, Any], float], Awaitable[None]],
                timeout: float = 900, interval: float = 0.7) -> dict[str, Any]:
    """Polls a Celery result until done; `on_progress(state, meta, elapsed)` is called while waiting."""
    from celery.result import AsyncResult

    from worker.celery_app import app

    res = AsyncResult(task_id, app=app)
    start = time.monotonic()
    while True:
        state, info = await asyncio.to_thread(lambda: (res.state, res.info))
        if state == "SUCCESS":
            return info if isinstance(info, dict) else {"status": "ok", "result": info}
        if state in ("FAILURE", "REVOKED"):
            raise TaskFailed(f"{type(info).__name__}: {info}" if isinstance(info, BaseException) else str(info))
        elapsed = time.monotonic() - start
        if elapsed > timeout:
            raise TaskFailed("операция заняла слишком много времени")
        await on_progress(state, info if isinstance(info, dict) else {}, elapsed)
        await asyncio.sleep(interval)


async def submit(task: Any, *args: Any) -> str:
    """Publishes a Celery task without blocking the event loop."""
    result = await asyncio.to_thread(task.delay, *args)
    return result.id
