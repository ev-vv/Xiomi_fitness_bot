from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from worker.tasks import daily_brief

from ..keyboards import Nav, back_home, retry
from ..render import brief_html, esc
from ..status import Status, TaskFailed, spinner, submit, track

router = Router(name="brief")


async def run_brief(bot: Bot, chat_id: int) -> None:
    st = Status(bot, chat_id)
    await st.show("📋 Готовлю сводку дня…", force=True)
    try:
        task_id = await submit(daily_brief)

        async def progress(state: str, meta: dict, elapsed: float) -> None:
            await st.show(f"🧠 Собираю главное за день {spinner(elapsed)} {int(elapsed)} с")
        res = await track(task_id, progress, timeout=180)
    except TaskFailed as exc:
        await st.show(f"❌ Не получилось: {esc(exc)}", retry(Nav(to="brief")), force=True)
        return
    except Exception as exc:  # noqa: BLE001
        await st.show(f"❌ Фоновый обработчик недоступен: {esc(type(exc).__name__)}", retry(Nav(to="brief")), force=True)
        return
    await st.delete()
    await bot.send_message(chat_id, brief_html(res), reply_markup=back_home())


@router.callback_query(Nav.filter(F.to == "brief"))
async def brief_cb(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    await run_brief(bot, cb.message.chat.id)


@router.message(Command("brief"))
async def brief_cmd(message: Message, bot: Bot) -> None:
    await run_brief(bot, message.chat.id)
