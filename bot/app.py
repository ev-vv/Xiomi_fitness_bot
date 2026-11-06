"""Bot entry point: aiogram dispatcher with Redis FSM storage, routers and commands."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, ErrorEvent, Message

from core.config import settings
from storage.db import engine

from .handlers import brief, chat, menu, metrics, profile, settings as settings_h, sync, workouts
from .keyboards import main_menu
from .middlewares import OwnerOnly
from .screens import home_text

log = logging.getLogger(__name__)

COMMANDS = [
    ("menu", "Главное меню"), ("sync", "Обновить данные"), ("brief", "Сводка дня"), ("sleep", "Анализ сна"),
    ("heart", "Анализ пульса"), ("spo2", "Кислород SpO2"), ("stress", "Анализ стресса"), ("workouts", "Тренировки"),
    ("chat", "Разговор с ИИ"), ("settings", "Настройки"), ("profile", "Профиль"), ("usage", "Расход на ИИ"),
    ("health", "Состояние сервисов"), ("help", "Помощь"),
]

fallback = Router(name="fallback")


@fallback.message(F.text)
async def anything(message: Message) -> None:
    await message.answer(await home_text(), reply_markup=main_menu())


def build(storage: BaseStorage | None = None, session: BaseSession | None = None) -> tuple[Bot, Dispatcher]:
    cfg = settings()
    if session is None and cfg.telegram_proxy:
        session = AiohttpSession(proxy=cfg.telegram_proxy)
    bot = Bot(cfg.bot_token.get_secret_value(), session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=storage or RedisStorage.from_url(cfg.redis_url))
    dp.update.outer_middleware(OwnerOnly(cfg.owners))
    # Commands first, the chat's free-text handler late, the fallback last.
    dp.include_routers(menu.router, sync.router, metrics.router, workouts.router, brief.router, settings_h.router,
                       profile.router, chat.router, fallback)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> None:
        log.exception("unhandled error", exc_info=event.exception)
        upd = event.update
        target = upd.message or (upd.callback_query.message if upd.callback_query else None)
        if target is not None:
            await target.answer("❌ Что-то пошло не так. Попробуйте ещё раз или откройте /menu.")

    return bot, dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine()  # create SQLite tables
    bot, dp = build()
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    log.info("bot started")
    await dp.start_polling(bot)
