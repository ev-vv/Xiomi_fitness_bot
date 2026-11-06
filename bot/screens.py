"""Helpers shared by handlers: editing the menu message in place and the home screen text."""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from analytics.periods import latest_date
from core import data
from extractor.config import Config
from core.fmt import ago, dur, num

from .render import esc


async def show(target: CallbackQuery | Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Edits the menu message when possible (no chat spam), otherwise sends a new one."""
    if isinstance(target, CallbackQuery):
        await target.answer()
        msg = target.message
        if isinstance(msg, Message) and msg.text is not None:
            try:
                await msg.edit_text(text, reply_markup=kb)
                return
            except TelegramBadRequest:
                pass
        if msg is not None:
            await msg.answer(text, reply_markup=kb)
        return
    await target.answer(text, reply_markup=kb)


WELCOME = ("👋 <b>Добро пожаловать!</b>\n\n"
           "Бот анализирует сон, пульс, SpO2, стресс и тренировки с браслета Xiaomi. Данные берутся из облака "
           "Mi Fitness, поэтому сначала нужно войти в свой аккаунт Xiaomi.\n\n"
           "1. Нажмите «🔐 Войти через QR».\n"
           "2. Откройте ссылку на телефоне, где установлен Mi Fitness, и подтвердите вход "
           "(или отсканируйте QR камерой).\n"
           "3. Бот сам загрузит данные за последние 30 дней.\n\n"
           "Потом заполните ⚙️ Настройки → 👤 Профиль, чтобы ИИ учитывал ваш возраст и цели.")


def needs_login() -> bool:
    """The current user has not connected a Xiaomi account yet."""
    return not Config().token_path.exists()


def _home_text() -> str:
    lines = ["🏠 <b>Fitness Analytics</b>"]
    try:
        proc = data.processor()
    except Exception as exc:  # noqa: BLE001 - no data yet is a normal first-run state
        return lines[0] + f"\n\nДанных пока нет ({esc(type(exc).__name__)}). Нажмите «🔄 Обновить данные»."
    lines.append(f"<i>данные обновлены: {ago(data.last_sync())}</i>\n")
    parts = []
    if not proc.nights.is_empty():
        n = proc.nights.filter(proc.nights["is_main"]).sort("date").row(-1, named=True)
        parts.append(f"😴 {dur(n['tst'])} · качество {num(n['quality_index'])}")
    d = proc.daily
    rhr = d.filter(d["rhr_device"].is_not_null()).sort("date")
    if rhr.height:
        parts.append(f"❤️ пульс покоя {rhr['rhr_device'][-1]}")
    st = d.filter(d["stress_avg"].is_not_null()).sort("date")
    if st.height:
        parts.append(f"🧠 стресс {num(st['stress_avg'][-1])}")
    lines.append("   ".join(parts) if parts else "Данных пока нет — нажмите «🔄 Обновить данные».")
    last = latest_date(proc, "heart")
    if last is not None:
        rec = proc.report(last).recovery
        if rec.index is not None:
            lines.append(f"🔋 Восстановление {rec.index}/100")
    lines.append("\nВыберите раздел:")
    return "\n".join(lines)


async def home_text() -> str:
    return await asyncio.to_thread(_home_text)
