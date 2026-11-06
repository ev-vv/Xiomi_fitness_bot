from __future__ import annotations

from datetime import date

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message

from analytics.periods import METRICS, shift
from worker.tasks import analyze_period, render_dashboard

from ..keyboards import METRIC_BUTTONS, MetricCB, Nav, after_analysis, back_home, periods, retry
from ..render import analysis_html, caption, esc, split
from ..screens import show
from ..status import Status, TaskFailed, bar, spinner, submit, track

router = Router(name="metrics")

INTRO = {
    "sleep": "Длительность, фазы, эффективность, пульс во сне и регулярность режима.",
    "heart": "Пульс покоя, пульс в течение дня, эпизоды выше нормы и пульсовые зоны.",
    "spo2": "Уровень кислорода во сне и ночные просадки.",
    "stress": "Стресс в течение дня, время по зонам и связь со сном.",
}


@router.callback_query(Nav.filter(F.to.in_(METRICS)))
async def choose(cb: CallbackQuery, callback_data: Nav) -> None:
    m = callback_data.to
    await show(cb, f"<b>{METRIC_BUTTONS[m]}</b>\n{INTRO[m]}\n\nВыберите период:", periods(m))


@router.message(Command("sleep", "heart", "spo2", "stress"))
async def choose_cmd(message: Message) -> None:
    m = message.text.split()[0].lstrip("/").split("@")[0]
    await message.answer(f"<b>{METRIC_BUTTONS[m]}</b>\n{INTRO[m]}\n\nВыберите период:", reply_markup=periods(m))


@router.callback_query(MetricCB.filter())
async def run(cb: CallbackQuery, callback_data: MetricCB, bot: Bot) -> None:
    await cb.answer()
    await show_metric(bot, cb.message.chat.id, callback_data)


async def show_metric(bot: Bot, chat_id: int, q: MetricCB) -> None:
    st = Status(bot, chat_id)
    end = q.end or None
    await st.show(f"⏳ Собираю данные {bar(1, 3)}", force=True)
    try:
        render_id = await submit(render_dashboard, q.metric, q.period, end)
        analyze_id = await submit(analyze_period, q.metric, q.period, end, bool(q.force))

        async def drawing(state: str, meta: dict, elapsed: float) -> None:
            await st.show(f"🎨 Рисую дашборд {bar(2, 3)} {spinner(elapsed)}")
        r = await track(render_id, drawing, timeout=120)
        if r.get("status") == "no_data":
            await st.show("📭 Данных для этого раздела пока нет. Обновите данные в меню.", back_home(), force=True)
            return
        await bot.send_chat_action(chat_id, "upload_photo")
        await bot.send_photo(chat_id, FSInputFile(r["path"]), caption=caption(r["title"], r["kpis"]))
        await st.restart(f"🧠 ИИ анализирует {bar(3, 3)}")

        async def thinking(state: str, meta: dict, elapsed: float) -> None:
            await st.show(f"🧠 ИИ анализирует {bar(3, 3)} {spinner(elapsed)} {int(elapsed)} с")
        a = await track(analyze_id, thinking, timeout=240)
    except TaskFailed as exc:
        await st.show(f"❌ Не получилось: {esc(exc)}", retry(q), force=True)
        return
    except Exception as exc:  # noqa: BLE001 - broker/redis down
        await st.show(f"❌ Фоновый обработчик недоступен: {esc(type(exc).__name__)}. Проверьте Redis и RabbitMQ.",
                      retry(q), force=True)
        return
    if a.get("status") != "ok":
        await st.show("📭 Нет данных для анализа.", back_home(), force=True)
        return
    analysis, advice = analysis_html(a, q.period)
    for chunk in split(analysis):
        await bot.send_message(chat_id, chunk)
    end_d = date.fromisoformat(r["end"])
    nxt = None
    if r.get("has_next") and r.get("latest"):
        nxt = min(shift(q.period, end_d, 1), date.fromisoformat(r["latest"])).isoformat()
    kb = after_analysis(q.metric, q.period, r["end"], shift(q.period, end_d, -1).isoformat(), nxt)
    chunks = split(advice)
    for chunk in chunks[:-1]:
        await bot.send_message(chat_id, chunk)
    await bot.send_message(chat_id, chunks[-1], reply_markup=kb)
    await st.delete()
