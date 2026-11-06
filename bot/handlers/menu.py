from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ai.client import MODEL_NAMES, check_ready
from core import data
from core.config import settings
from core.fmt import ago
from storage.db import spent_today, usage_summary

from ..keyboards import Nav, back_home, login, main_menu
from ..render import esc
from ..screens import WELCOME, home_text, needs_login, show

router = Router(name="menu")

HELP = """<b>Что умеет бот</b>
🔄 <b>Обновить данные</b> — загрузка новых данных из облака Xiaomi и пересчёт аналитики.
📋 <b>Сводка дня</b> — главное за день одним экраном.
😴 ❤️ 🩸 🧠 <b>Анализ</b> — дашборд за день, неделю или месяц, разбор и советы ИИ.
🏋️ <b>Тренировки</b> — разбор каждой тренировки по пульсовым зонам.
💬 <b>Разговор с ИИ</b> — вопросы о любых метриках своими словами; по просьбе ИИ ищет нормы и исследования
в интернете.
⚙️ <b>Настройки</b> — цель сна, утренняя сводка, автообновление, оповещения.
👤 <b>Профиль</b> — пол, возраст, рост, вес, активность и цель: ИИ учитывает их во всех разборах.

Команды: /menu /sync /brief /sleep /heart /spo2 /stress /workouts /chat /settings /profile /usage /health"""


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if await asyncio.to_thread(needs_login):
        await message.answer(WELCOME, reply_markup=login())
        return
    await message.answer(await home_text(), reply_markup=main_menu())


@router.callback_query(Nav.filter(F.to == "home"))
async def home(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show(cb, await home_text(), main_menu())


@router.message(Command("help"))
async def help_(message: Message) -> None:
    await message.answer(HELP, reply_markup=back_home())


@router.message(Command("usage"))
async def usage(message: Message) -> None:
    rows = await asyncio.to_thread(usage_summary, 30)
    today = await asyncio.to_thread(spent_today)
    names = {"chat": "чат", "day": "анализ дня", "period": "анализ периода", "workout": "тренировки",
             "brief": "сводки"}
    lines = ["💳 <b>Расход на ИИ за 30 дней</b>"]
    total = 0.0
    for route, calls, inp, out, cread, cost in rows:
        total += cost or 0
        lines.append(f"• {names.get(route, route)}: {calls} запр., вход {inp or 0:,} / выход {out or 0:,} ток."
                     f" (из кэша {cread or 0:,}) — ${cost or 0:.4f}".replace(",", " "))
    lines.append(f"\nИтого: <b>${total:.4f}</b>, сегодня ${today:.4f} из лимита ${settings().llm_daily_budget_usd:.2f}")
    await message.answer("\n".join(lines), reply_markup=back_home())


@router.message(Command("health"))
async def health(message: Message) -> None:
    cfg = settings()
    import redis.asyncio as aioredis

    async def redis_ok() -> tuple[bool, str]:
        try:
            r = aioredis.from_url(cfg.redis_url)
            await r.ping()
            await r.aclose()
            return True, "Redis доступен"
        except Exception as exc:  # noqa: BLE001
            return False, f"Redis: {exc}"

    async def broker_ok() -> tuple[bool, str]:
        from worker.celery_app import app

        def ping() -> bool:
            with app.connection_for_write() as conn:
                conn.ensure_connection(max_retries=1, timeout=15)
            return True
        try:
            await asyncio.to_thread(ping)
            return True, "RabbitMQ доступен"
        except Exception as exc:  # noqa: BLE001
            return False, f"RabbitMQ: {exc}"

    claude, rds, broker = await asyncio.gather(check_ready(cfg.llm_model_chat), redis_ok(), broker_ok())
    token = data.analytics_config().base.token_path.exists()
    lines = ["🩺 <b>Состояние</b>"]
    for ok, text in (claude, rds, broker):
        lines.append(f"{'🟢' if ok else '🔴'} {esc(text)}")
    lines.append(f"{'🟢' if token else '🔴'} Вход в Xiaomi {'выполнен' if token else 'не выполнен'}")
    lines.append(f"🕒 Данные обновлены: {ago(data.last_sync())}")
    lines.append(f"🤖 Модель: {MODEL_NAMES.get(cfg.llm_model_chat, cfg.llm_model_chat)}")
    await message.answer("\n".join(lines), reply_markup=back_home())
