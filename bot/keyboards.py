"""Inline keyboards and callback data."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class Nav(CallbackData, prefix="nav"):
    to: str


class MetricCB(CallbackData, prefix="m"):
    metric: str
    period: str
    end: str = ""
    force: int = 0


class WorkoutsCB(CallbackData, prefix="wl"):
    page: int


class WorkoutCB(CallbackData, prefix="w"):
    ts: int
    force: int = 0


class ChatCB(CallbackData, prefix="c"):
    action: str


class AskCB(CallbackData, prefix="ask"):
    metric: str
    period: str
    end: str


class SetCB(CallbackData, prefix="s"):
    key: str


class ProfCB(CallbackData, prefix="pf"):
    field: str
    action: str  # edit | set | unset | clear | clear_yes
    value: str = ""


class SyncCB(CallbackData, prefix="sy"):
    action: str


METRIC_BUTTONS = {"sleep": "😴 Анализ сна", "heart": "❤️ Анализ пульса", "spo2": "🩸 Кислород SpO2",
                  "stress": "🧠 Анализ стресса"}
PERIOD_BUTTONS = {"day": "День", "week": "Неделя", "month": "Месяц"}


def home_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🏠 Меню", callback_data=Nav(to="home").pack())


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить данные", callback_data=SyncCB(action="start"))
    b.button(text="📋 Сводка дня", callback_data=Nav(to="brief"))
    for metric, text in METRIC_BUTTONS.items():
        b.button(text=text, callback_data=Nav(to=metric))
    b.button(text="🏋️ Тренировки", callback_data=WorkoutsCB(page=0))
    b.button(text="💬 Разговор с ИИ", callback_data=Nav(to="chat"))
    b.button(text="⚙️ Настройки", callback_data=Nav(to="settings"))
    b.adjust(2, 2, 2, 2, 1)
    return b.as_markup()


def periods(metric: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for period, text in PERIOD_BUTTONS.items():
        b.button(text=text, callback_data=MetricCB(metric=metric, period=period))
    b.button(text="◀️ Назад", callback_data=Nav(to="home"))
    b.adjust(3, 1)
    return b.as_markup()


def after_analysis(metric: str, period: str, end: str, prev_end: str, next_end: str | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    names = {"day": "день", "week": "неделя", "month": "месяц"}
    b.button(text=f"◀️ Пред. {names[period]}", callback_data=MetricCB(metric=metric, period=period, end=prev_end))
    if next_end:
        b.button(text=f"След. {names[period]} ▶️", callback_data=MetricCB(metric=metric, period=period, end=next_end))
    b.button(text="💬 Спросить ИИ об этом", callback_data=AskCB(metric=metric, period=period, end=end))
    b.button(text="🔁 Переанализировать", callback_data=MetricCB(metric=metric, period=period, end=end, force=1))
    b.button(text="📅 Другой период", callback_data=Nav(to=metric))
    b.add(home_button())
    b.adjust(2 if next_end else 1, 1, 2, 1)
    return b.as_markup()


def retry(callback: CallbackData) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔁 Повторить", callback_data=callback.pack()),
                                                  home_button()]])


def back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[home_button()]])


def chat_ready() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💡 Примеры вопросов", callback_data=ChatCB(action="examples"))
    b.button(text="🧹 Новый диалог", callback_data=ChatCB(action="new"))
    b.button(text="🏠 Выйти", callback_data=ChatCB(action="exit"))
    b.adjust(1, 2)
    return b.as_markup()


def chat_answer() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔁 Ещё раз", callback_data=ChatCB(action="retry"))
    b.button(text="🧹 Новый диалог", callback_data=ChatCB(action="new"))
    b.button(text="🏠 Выйти", callback_data=ChatCB(action="exit"))
    b.adjust(3)
    return b.as_markup()


def chat_stop() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏹ Стоп",
                                                                       callback_data=ChatCB(action="stop").pack())]])


def login() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔐 Войти через QR",
                                                                       callback_data=SyncCB(action="login").pack())],
                                                 [home_button()]])
