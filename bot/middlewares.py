"""Access control: only users from OWNER_IDS; each update runs as its sender (per-user data and settings)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from core import user as current_user

log = logging.getLogger(__name__)


class OwnerOnly(BaseMiddleware):
    def __init__(self, owners: set[int]) -> None:
        self.owners = owners
        if not owners:
            log.warning("OWNER_IDS is empty: the bot answers nobody except with the user's id")

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is not None and user.id in self.owners:
            with current_user.use(user.id):
                return await handler(event, data)
        if isinstance(event, Update) and event.message is not None and user is not None \
                and (event.message.text or "").startswith("/start"):
            # Tell the person their id so the bot's owner can add it to OWNER_IDS.
            hint = ("Бот ещё не настроен" if not self.owners else "Доступ к боту закрыт")
            await event.message.answer(f"{hint}. Ваш Telegram ID: <code>{user.id}</code> — его нужно добавить "
                                       "в OWNER_IDS в файле .env и перезапустить бота.")
        log.info("dropped update from user %s", user.id if user else None)
        return None
