from __future__ import annotations

import asyncio

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ai.facts import SPORTS
from analytics.periods import workout_list
from core import data
from core.fmt import day, dur
from worker.tasks import analyze_workout, render_workout_task

from ..keyboards import WorkoutCB, WorkoutsCB, back_home, home_button, retry
from ..render import analysis_html, caption, esc, split
from ..screens import show
from ..status import Status, TaskFailed, bar, spinner, submit, track

router = Router(name="workouts")
PAGE = 8


def _rows() -> list[dict]:
    return list(workout_list(data.processor()).iter_rows(named=True))


def _list_kb(rows: list[dict], page: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    pages = max(1, -(-len(rows) // PAGE))
    for r in rows[page * PAGE:(page + 1) * PAGE]:
        emoji, name = SPORTS.get(r["sport_type"], ("🏅", r["sport_type"]))
        b.button(text=f"{emoji} {day(r['date'])} · {name} · {dur(r['duration_sec'] / 60)}"
                      + (f" · {r['avg_hr']} уд/мин" if r.get("avg_hr") else ""),
                 callback_data=WorkoutCB(ts=r["ts"]))
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=WorkoutsCB(page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f"стр {page + 1}/{pages}", callback_data=WorkoutsCB(page=page).pack()))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=WorkoutsCB(page=page + 1).pack()))
    b.row(*nav)
    b.row(home_button())
    return b.as_markup()


async def _list(target: CallbackQuery | Message, page: int) -> None:
    rows = await asyncio.to_thread(_rows)
    if not rows:
        await show(target, "🏋️ Тренировок пока нет. Запишите тренировку на браслете и обновите данные.", back_home())
        return
    await show(target, f"🏋️ <b>Тренировки</b> ({len(rows)})\nВыберите тренировку для разбора:", _list_kb(rows, page))


@router.callback_query(WorkoutsCB.filter())
async def list_cb(cb: CallbackQuery, callback_data: WorkoutsCB) -> None:
    await _list(cb, callback_data.page)


@router.message(Command("workouts"))
async def list_cmd(message: Message) -> None:
    await _list(message, 0)


@router.callback_query(WorkoutCB.filter())
async def workout(cb: CallbackQuery, callback_data: WorkoutCB, bot: Bot) -> None:
    await cb.answer()
    chat_id, q = cb.message.chat.id, callback_data
    st = Status(bot, chat_id)
    await st.show(f"⏳ Собираю данные тренировки {bar(1, 3)}", force=True)
    try:
        render_id = await submit(render_workout_task, q.ts)
        analyze_id = await submit(analyze_workout, q.ts, bool(q.force))

        async def drawing(state: str, meta: dict, elapsed: float) -> None:
            await st.show(f"🎨 Рисую дашборд {bar(2, 3)} {spinner(elapsed)}")
        r = await track(render_id, drawing, timeout=180)
        if r.get("status") != "ok":
            await st.show("📭 Тренировка не найдена.", back_home(), force=True)
            return
        cap = caption(r["title"], r["kpis"])
        if not r.get("has_track"):
            cap += "\n<i>Поминутного пульса за тренировку в облаке нет — показаны зоны от браслета.</i>"
        await bot.send_photo(chat_id, FSInputFile(r["path"]), caption=cap[:1024])
        await st.restart(f"🧠 ИИ анализирует {bar(3, 3)}")

        async def thinking(state: str, meta: dict, elapsed: float) -> None:
            await st.show(f"🧠 ИИ анализирует {bar(3, 3)} {spinner(elapsed)} {int(elapsed)} с")
        a = await track(analyze_id, thinking, timeout=240)
    except TaskFailed as exc:
        await st.show(f"❌ Не получилось: {esc(exc)}", retry(q), force=True)
        return
    except Exception as exc:  # noqa: BLE001
        await st.show(f"❌ Фоновый обработчик недоступен: {esc(type(exc).__name__)}", retry(q), force=True)
        return
    analysis, advice = analysis_html(a, "day")
    for chunk in split(analysis):
        await bot.send_message(chat_id, chunk)
    b = InlineKeyboardBuilder()
    b.button(text="🔁 Переанализировать", callback_data=WorkoutCB(ts=q.ts, force=1))
    b.button(text="📋 К списку", callback_data=WorkoutsCB(page=0))
    b.row(home_button())
    b.adjust(2, 1)
    await bot.send_message(chat_id, advice, reply_markup=b.as_markup())
    await st.delete()
