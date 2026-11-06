from __future__ import annotations

import asyncio
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import anthropic
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ai import chat as ai_chat
from ai.client import MODEL_NAMES, async_client, check_ready, describe_error
from ai.facts import METRIC_TITLES, PERIOD_TITLES, overview_facts
from analytics.periods import latest_date, period_bounds
from core import data, profile
from core.config import settings
from core.fmt import ago, day, span
from storage.db import add_chat, clear_chat, recent_chat
from worker.tasks import brief_dates

from ..keyboards import AskCB, ChatCB, Nav, chat_answer, chat_ready, chat_stop, main_menu
from ..render import chat_html, esc, plain, sources_html, split
from ..screens import home_text, show

router = Router(name="chat")

EXAMPLES = """💡 <b>Примеры вопросов</b>
• Почему я сегодня плохо выспался?
• Как менялся мой пульс покоя за последний месяц?
• В какие дни недели у меня больше всего стресса?
• Сравни сон на этой и прошлой неделе.
• Как прошла моя последняя силовая тренировка?
• Влияют ли тренировки на мой глубокий сон?
• Что мне улучшить в первую очередь?
• Найди в интернете, какой пульс покоя считается нормой для моего возраста.
• Сравни мой сон со средними для моего пола и возраста по исследованиям."""

RETRYABLE = (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.InternalServerError)
MAX_ATTEMPTS = 3
EDIT_INTERVAL = 1.2


class ChatState(StatesGroup):
    active = State()


_locks: dict[int, asyncio.Lock] = {}
_cancel: dict[int, asyncio.Event] = {}
_last_question: dict[int, str] = {}


def _context(focus: str | None) -> str:
    proc = data.processor()
    now = datetime.now(ZoneInfo(proc.cfg.tz))
    target, sleep_d = brief_dates(now)
    if proc.nights.is_empty() or proc.nights.filter(proc.nights["date"] == sleep_d).is_empty():
        sleep_d = latest_date(proc, "sleep") or sleep_d
    sheet = overview_facts(proc, target, now.date(), sleep_date=sleep_d)
    lines = [f"Сегодня {day(now.date(), True)} {now.year} ({now.date().isoformat()}), сейчас {now:%H:%M}.",
             f"Данные обновлены: {ago(data.last_sync())}. Цель сна: {proc.cfg.sleep_need_min} мин.",
             "", "СВОДКА:", sheet.render()]
    if card := profile.load().render(now.date()):
        lines += ["", card]
    if focus:
        lines += ["", f"Пользователь только что смотрел раздел «{focus}» — вопросы, скорее всего, о нём."]
    return "\n".join(lines)


async def _ready_card() -> str:
    cfg = settings()
    model = MODEL_NAMES.get(cfg.llm_model_chat, cfg.llm_model_chat)
    (ok, why), last = await asyncio.gather(check_ready(cfg.llm_model_chat), asyncio.to_thread(data.last_sync))
    fresh = last is not None and time.time() - last < 6 * 3600

    def spo2() -> bool:
        return not data.processor().f.spo2.is_empty()
    has_spo2, prof = await asyncio.gather(asyncio.to_thread(spo2), asyncio.to_thread(profile.load))
    lines = [f"💬 <b>Разговор с ИИ</b> · {esc(model)}",
             f"{'🟢' if ok else '🔴'} {esc(why)}",
             f"{'🟢' if fresh else '🟡'} Данные обновлены {ago(last)}",
             f"{'🟢' if has_spo2 else '🟡'} SpO2: {'есть замеры' if has_spo2 else 'нет данных'}",
             "🟢 Профиль учитывается" if not prof.is_empty else "🟡 Профиль не заполнен: ⚙️ Настройки → 👤 Профиль",
             ""]
    lines.append("Спросите что угодно о сне, пульсе, стрессе, шагах и тренировках — просто напишите сообщение. "
                 "Если нужно сравнить с нормами или узнать, что пишут исследования, попросите поискать в интернете."
                 if ok else "ИИ сейчас недоступен — дашборды в меню продолжают работать.")
    return "\n".join(lines)


@router.callback_query(Nav.filter(F.to == "chat"))
async def enter_cb(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChatState.active)
    await state.update_data(focus=None)
    await show(cb, "💬 Проверяю готовность ИИ…")
    await cb.message.edit_text(await _ready_card(), reply_markup=chat_ready())


@router.message(Command("chat"))
async def enter_cmd(message: Message, state: FSMContext) -> None:
    await state.set_state(ChatState.active)
    await state.update_data(focus=None)
    msg = await message.answer("💬 Проверяю готовность ИИ…")
    await msg.edit_text(await _ready_card(), reply_markup=chat_ready())


@router.callback_query(AskCB.filter())
async def ask_about(cb: CallbackQuery, callback_data: AskCB, state: FSMContext) -> None:
    start, end = period_bounds(callback_data.period, date.fromisoformat(callback_data.end))
    focus = f"{METRIC_TITLES[callback_data.metric]} · {PERIOD_TITLES[callback_data.period]} · {span(start, end)}"
    await state.set_state(ChatState.active)
    await state.update_data(focus=focus)
    await cb.answer()
    await cb.message.answer(f"💬 Задайте вопрос про «{esc(focus)}» — например, «почему так?» или «что изменить?»",
                            reply_markup=chat_ready())


@router.callback_query(ChatCB.filter())
async def chat_action(cb: CallbackQuery, callback_data: ChatCB, state: FSMContext, bot: Bot) -> None:
    chat_id, action = cb.message.chat.id, callback_data.action
    if action == "stop":
        if chat_id in _cancel:
            _cancel[chat_id].set()
        await cb.answer("Останавливаю…")
    elif action == "examples":
        await cb.answer()
        await cb.message.answer(EXAMPLES)
    elif action == "new":
        await asyncio.to_thread(clear_chat, chat_id)
        await state.set_state(ChatState.active)
        await state.update_data(focus=None)
        await cb.answer("История очищена")
        await cb.message.answer("🧹 Начали новый диалог. Задайте вопрос.")
    elif action == "exit":
        await state.clear()
        await cb.answer()
        await cb.message.answer(await home_text(), reply_markup=main_menu())
    elif action == "retry":
        await cb.answer()
        q = _last_question.get(chat_id)
        if q:
            await state.set_state(ChatState.active)
            await _handle(bot, chat_id, q, state, repeat=True)


@router.message(ChatState.active, F.text & ~F.text.startswith("/"))
async def question(message: Message, state: FSMContext, bot: Bot) -> None:
    await _handle(bot, message.chat.id, message.text.strip(), state)


class _Stream:
    """Accumulates streamed text and edits one message at most every EDIT_INTERVAL seconds."""

    def __init__(self, msg: Message) -> None:
        self.msg, self.text, self.status, self._last = msg, "", "🤔 Думаю…", 0.0

    async def render(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last < EDIT_INTERVAL:
            return
        self._last = time.monotonic()
        body = plain(self.text[-3500:]) + " ▌" if self.text else ""
        head = f"<i>{esc(self.status)}</i>" if self.status else ""
        try:
            await self.msg.edit_text("\n\n".join(p for p in (head, body) if p) or "…", reply_markup=chat_stop())
        except TelegramBadRequest:
            pass

    async def on_status(self, s: str) -> None:
        self.status = f"🔎 {s}…"
        await self.render(force=True)

    async def on_text(self, t: str) -> None:
        self.text += t
        self.status = ""
        await self.render()


async def _handle(bot: Bot, chat_id: int, q: str, state: FSMContext, repeat: bool = False) -> None:
    lock = _locks.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        await bot.send_message(chat_id, "⏳ В очереди — отвечу после текущего вопроса.")
    async with lock:
        _last_question[chat_id] = q
        cancel = _cancel[chat_id] = asyncio.Event()
        focus = (await state.get_data()).get("focus")
        msg = await bot.send_message(chat_id, "<i>🤔 Думаю…</i>", reply_markup=chat_stop())
        view = _Stream(msg)
        await bot.send_chat_action(chat_id, "typing")
        try:
            history, context = await asyncio.gather(asyncio.to_thread(recent_chat, chat_id, 20),
                                                    asyncio.to_thread(_context, focus))
            if repeat and history and history[-1]["role"] == "assistant":
                history = history[:-2]
            client = async_client().with_options(max_retries=0)
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    res = await ai_chat.answer(q, history, context, on_status=view.on_status, on_text=view.on_text,
                                               cancel=cancel, client=client)
                    break
                except RETRYABLE as exc:
                    if view.text or attempt == MAX_ATTEMPTS:
                        raise
                    view.status = f"⚠️ {describe_error(exc)}, повторяю ({attempt + 1}/{MAX_ATTEMPTS})"
                    await view.render(force=True)
                    await asyncio.sleep(2 * attempt)
                except anthropic.RateLimitError as exc:
                    wait = int(exc.response.headers.get("retry-after", "20")) if exc.response is not None else 20
                    if view.text or attempt == MAX_ATTEMPTS or wait > 60:
                        raise
                    view.status = f"⏳ Лимит запросов, повтор через {wait} с"
                    await view.render(force=True)
                    await asyncio.sleep(wait)
        except ai_chat.Cancelled:
            text = (plain(view.text) + "\n\n" if view.text else "") + "<i>⏹ Остановлено.</i>"
            await msg.edit_text(text[:4096], reply_markup=chat_answer())
            return
        except Exception as exc:  # noqa: BLE001 - every failure becomes a visible status
            await msg.edit_text(f"❌ {esc(describe_error(exc))}", reply_markup=chat_answer())
            return
        finally:
            _cancel.pop(chat_id, None)
        body = chat_html(res.text.strip() or "Не получилось сформулировать ответ, попробуйте переформулировать.")
        chunks = split(body + sources_html(res.sources))
        await msg.edit_text(chunks[0], reply_markup=None if len(chunks) > 1 else chat_answer())
        for i, chunk in enumerate(chunks[1:], 2):
            await bot.send_message(chat_id, chunk, reply_markup=chat_answer() if i == len(chunks) else None)
        if not repeat:
            await asyncio.to_thread(add_chat, chat_id, "user", q)
        await asyncio.to_thread(add_chat, chat_id, "assistant", res.history_text().strip())
