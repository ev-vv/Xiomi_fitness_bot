from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import profile
from core.config import settings
from core.fmt import dur
from storage.db import get_setting, set_setting

from ..keyboards import Nav, SetCB, home_button
from ..screens import show

router = Router(name="settings")

SLEEP_GOALS = ["420", "450", "480", "510", "540"]
BRIEF_TIMES = ["off", "07:00", "07:30", "08:00", "08:30", "09:00", "10:00"]


def _state() -> dict[str, str]:
    keys = ("sleep_need_min", "brief_time", "brief_enabled", "autosync", "alerts")
    return {**{k: get_setting(k) for k in keys}, "profile": "" if profile.load().is_empty else "1"}


def _render(s: dict[str, str]) -> tuple[str, object]:
    brief = s["brief_time"] if s["brief_enabled"] == "1" else "выкл."
    onoff = lambda v: "вкл." if v == "1" else "выкл."  # noqa: E731
    text = (f"⚙️ <b>Настройки</b>\n\n"
            f"😴 Цель сна: <b>{dur(int(s['sleep_need_min']))}</b>\n"
            f"☀️ Утренняя сводка: <b>{brief}</b>\n"
            f"🔄 Автообновление каждые {settings().autosync_hours} ч: <b>{onoff(s['autosync'])}</b>\n"
            f"⚠️ Оповещения об аномалиях: <b>{onoff(s['alerts'])}</b>\n"
            f"👤 Профиль: <b>{'заполнен' if s['profile'] else 'не заполнен'}</b>\n\n"
            "<i>Нажмите на кнопку, чтобы изменить значение.</i>")
    b = InlineKeyboardBuilder()
    b.button(text="😴 Цель сна", callback_data=SetCB(key="sleep_need_min"))
    b.button(text="☀️ Время сводки", callback_data=SetCB(key="brief_time"))
    b.button(text="🔄 Автообновление", callback_data=SetCB(key="autosync"))
    b.button(text="⚠️ Оповещения", callback_data=SetCB(key="alerts"))
    b.button(text="👤 Профиль", callback_data=Nav(to="profile"))
    b.adjust(2, 2, 1)
    b.row(home_button())
    return text, b.as_markup()


def _cycle(values: list[str], cur: str) -> str:
    return values[(values.index(cur) + 1) % len(values)] if cur in values else values[0]


def _toggle(key: str) -> dict[str, str]:
    s = _state()
    if key == "sleep_need_min":
        set_setting(key, _cycle(SLEEP_GOALS, s[key]))
    elif key == "brief_time":
        cur = s["brief_time"] if s["brief_enabled"] == "1" else "off"
        nxt = _cycle(BRIEF_TIMES, cur)
        set_setting("brief_enabled", "0" if nxt == "off" else "1")
        if nxt != "off":
            set_setting("brief_time", nxt)
    elif key in ("autosync", "alerts"):
        set_setting(key, "0" if s[key] == "1" else "1")
    return _state()


@router.callback_query(Nav.filter(F.to == "settings"))
async def open_(cb: CallbackQuery) -> None:
    text, kb = _render(await asyncio.to_thread(_state))
    await show(cb, text, kb)


@router.message(Command("settings"))
async def open_cmd(message: Message) -> None:
    text, kb = _render(await asyncio.to_thread(_state))
    await message.answer(text, reply_markup=kb)


@router.callback_query(SetCB.filter())
async def change(cb: CallbackQuery, callback_data: SetCB) -> None:
    text, kb = _render(await asyncio.to_thread(_toggle, callback_data.key))
    await show(cb, text, kb)
