"""User profile screen: choice fields via submenus, numbers and notes via a text reply."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import profile as prof
from core.data import analytics_config
from core.fmt import num, plural

from ..keyboards import Nav, ProfCB, home_button
from ..render import esc
from ..screens import show

router = Router(name="profile")

LABELS = {"sex": "⚧ Пол", "birth_year": "🎂 Год рождения", "height_cm": "📏 Рост", "weight_kg": "⚖️ Вес",
          "activity": "🏃 Активность", "goal": "🎯 Цель", "notes": "📝 Важно знать"}
PROMPTS = {"birth_year": "Отправьте год рождения, например <b>1995</b>.",
           "height_cm": "Отправьте рост в сантиметрах, например <b>168</b>.",
           "weight_kg": "Отправьте вес в килограммах, например <b>62</b> или <b>62,5</b>.",
           "notes": f"Напишите, что ИИ стоит учитывать (до {prof.NOTES_MAX} символов): хронические состояния, "
                    "лекарства (например, бета-блокаторы снижают пульс), посменная работа, беременность и т.п."}


class ProfileState(StatesGroup):
    editing = State()


def _today():
    return datetime.now(ZoneInfo(analytics_config().tz)).date()


def _render(p: prof.Profile) -> tuple[str, InlineKeyboardMarkup]:
    age = p.age(_today())
    rows = {
        "sex": prof.SEX.get(p.sex),
        "birth_year": f"{p.birth_year} ({age} {plural(age, 'год', 'года', 'лет')})" if age is not None else None,
        "height_cm": f"{p.height_cm} см" if p.height_cm else None,
        "weight_kg": (f"{p.weight_kg:g} кг".replace(".", ",") + (f" · ИМТ {num(p.bmi, 1)}" if p.bmi else ""))
        if p.weight_kg else None,
        "activity": prof.ACTIVITY.get(p.activity),
        "goal": prof.GOAL.get(p.goal),
        "notes": p.notes or None,
    }
    lines = ["👤 <b>Профиль</b>", ""]
    lines += [f"{LABELS[f]}: <b>{esc(v)}</b>" if v else f"{LABELS[f]}: <i>не указано</i>" for f, v in rows.items()]
    lines += ["", "<i>Профиль учитывается во всех разборах ИИ и в чате: нормы сна и пульса для возраста, ИМТ, "
                  "советы под цель. Хранится в базе бота и отправляется модели только вместе с запросами.</i>"]
    b = InlineKeyboardBuilder()
    for f in prof.FIELDS:
        b.button(text=LABELS[f], callback_data=ProfCB(field=f, action="edit"))
    b.button(text="🗑 Очистить", callback_data=ProfCB(field="", action="clear"))
    b.button(text="◀️ Настройки", callback_data=Nav(to="settings"))
    b.adjust(2, 2, 2, 1, 2)
    b.row(home_button())
    return "\n".join(lines), b.as_markup()


def _choices(field: str) -> tuple[str, InlineKeyboardMarkup]:
    b = InlineKeyboardBuilder()
    for value, text in prof.CHOICES[field].items():
        b.button(text=text, callback_data=ProfCB(field=field, action="set", value=value))
    b.button(text="Не указывать", callback_data=ProfCB(field=field, action="set", value=""))
    b.button(text="◀️ Назад", callback_data=Nav(to="profile"))
    b.adjust(1)
    return f"{LABELS[field]}: выберите вариант", b.as_markup()


def _cancel() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Не указывать", callback_data=ProfCB(field="", action="unset"))
    b.button(text="◀️ Отмена", callback_data=Nav(to="profile"))
    b.adjust(2)
    return b.as_markup()


async def _screen() -> tuple[str, InlineKeyboardMarkup]:
    return _render(await asyncio.to_thread(prof.load))


@router.callback_query(Nav.filter(F.to == "profile"))
async def open_(cb: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() == ProfileState.editing.state:
        await state.clear()
    await show(cb, *await _screen())


@router.message(Command("profile"))
async def open_cmd(message: Message) -> None:
    text, kb = await _screen()
    await message.answer(text, reply_markup=kb)


@router.callback_query(ProfCB.filter())
async def change(cb: CallbackQuery, callback_data: ProfCB, state: FSMContext) -> None:
    field, action = callback_data.field, callback_data.action
    if action == "edit" and field in prof.CHOICES:
        await show(cb, *_choices(field))
    elif action == "edit":
        await state.set_state(ProfileState.editing)
        await state.update_data(profile_field=field)
        await show(cb, f"{LABELS[field]}\n\n{PROMPTS[field]}", _cancel())
    elif action == "set" and field in prof.CHOICES:
        await asyncio.to_thread(prof.save, field, callback_data.value)
        await show(cb, *await _screen())
    elif action == "unset":
        field = (await state.get_data()).get("profile_field", "")
        if field in prof.FIELDS:
            await asyncio.to_thread(prof.save, field, "")
        await state.clear()
        await show(cb, *await _screen())
    elif action == "clear":
        b = InlineKeyboardBuilder()
        b.button(text="🗑 Да, очистить", callback_data=ProfCB(field="", action="clear_yes"))
        b.button(text="◀️ Отмена", callback_data=Nav(to="profile"))
        await show(cb, "Очистить все поля профиля?", b.as_markup())
    elif action == "clear_yes":
        for f in prof.FIELDS:
            await asyncio.to_thread(prof.save, f, "")
        await show(cb, *await _screen())


@router.message(ProfileState.editing, F.text & ~F.text.startswith("/"))
async def value(message: Message, state: FSMContext) -> None:
    field = (await state.get_data()).get("profile_field", "")
    if field not in PROMPTS:
        await state.clear()
        return
    try:
        stored = prof.parse(field, message.text, _today())
    except ValueError as exc:
        await message.answer(f"⚠️ Не получилось: {esc(exc)}. Попробуйте ещё раз.", reply_markup=_cancel())
        return
    await asyncio.to_thread(prof.save, field, stored)
    await state.clear()
    text, kb = await _screen()
    await message.answer("✅ Сохранено\n\n" + text, reply_markup=kb)
