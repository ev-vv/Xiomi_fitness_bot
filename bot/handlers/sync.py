from __future__ import annotations

import io

import qrcode
import redis.asyncio as aioredis
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.config import settings
from worker.tasks import sync_data, sync_task_key, xiaomi_login

from ..keyboards import Nav, SyncCB, back_home, home_button, login, retry
from ..render import esc
from ..status import Status, TaskFailed, bar, spinner, submit, track

router = Router(name="sync")

GROUPS = {"heart_rate": "пульс", "spo2": "SpO2", "stress": "стресс", "sleep": "сон", "steps_daily": "шаги",
          "hr_daily": "пульс за день", "spo2_daily": "SpO2 за день", "workouts": "тренировки"}
ADDED = {"heart_rate": "замеров пульса", "stress": "замеров стресса", "spo2": "замеров SpO2",
         "sleep_daily": "ночей", "sleep_report": "отчётов о сне", "steps_daily": "дней шагов",
         "workouts": "тренировок"}


@router.message(Command("sync"))
async def sync_cmd(message: Message, bot: Bot) -> None:
    await run_sync(bot, message.chat.id)


@router.callback_query(SyncCB.filter(F.action == "start"))
async def sync_cb(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer("Обновляю данные…")
    await run_sync(bot, cb.message.chat.id)


async def run_sync(bot: Bot, chat_id: int) -> None:
    st = Status(bot, chat_id)
    await st.show("🔄 Подключаюсь к Xiaomi…", force=True)
    try:
        r = aioredis.from_url(settings().redis_url, decode_responses=True)
        running = await r.get(sync_task_key())
        await r.aclose()
        task_id = running or await submit(sync_data)
        if running:
            await st.show("🔄 Обновление уже идёт — показываю его прогресс", force=True)

        async def progress(state: str, meta: dict, elapsed: float) -> None:
            stage = meta.get("stage")
            if stage == "download":
                i, n = meta.get("i", 0), meta.get("n", 1)
                await st.show(f"⬇️ Загружаю: {GROUPS.get(meta.get('group'), meta.get('group'))}\n"
                              f"{bar(i, n)} {i}/{n}")
            elif stage == "build":
                await st.show(f"🧮 Пересчитываю аналитику {bar(1, 1)} {spinner(elapsed)}")
            elif state == "PENDING":
                await st.show(f"⏳ Задача в очереди {spinner(elapsed)}")
            else:
                await st.show(f"🔄 Подключаюсь к Xiaomi… {spinner(elapsed)}")

        res = await track(task_id, progress, timeout=1800)
        if res.get("status") == "busy" and res.get("task_id"):
            res = await track(res["task_id"], progress, timeout=1800)
    except TaskFailed as exc:
        await st.show(f"❌ Обновление не удалось: {esc(exc)}", retry(SyncCB(action="start")), force=True)
        return
    except Exception as exc:  # noqa: BLE001 - broker/redis down
        await st.show(f"❌ Фоновый обработчик недоступен: {esc(type(exc).__name__)}. Проверьте Redis и RabbitMQ.",
                      retry(SyncCB(action="start")), force=True)
        return
    if res.get("status") == "auth_required":
        await st.show(f"🔐 Нужен вход в аккаунт Xiaomi: {esc(res.get('reason', ''))}", login(), force=True)
        return
    added = res.get("added") or {}
    lines = [f"✅ Готово за {res.get('seconds', 0)} с"]
    new = [f"+{v} {ADDED[k]}" for k, v in added.items() if v and k in ADDED]
    lines.append(", ".join(new) if new else "Новых данных нет — браслет, вероятно, ещё не синхронизировался с телефоном.")
    for group, err in (res.get("errors") or {}).items():
        lines.append(f"⚠️ {GROUPS.get(group, group)}: {esc(err[:120])}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Сводка дня", callback_data=Nav(to="brief").pack()), home_button()]])
    await st.show("\n".join(lines), kb, force=True)


@router.callback_query(SyncCB.filter(F.action == "login"))
async def login_cb(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    chat_id = cb.message.chat.id
    st = Status(bot, chat_id)
    await st.show("🔐 Запрашиваю QR-код у Xiaomi…", force=True)
    sent = False
    try:
        task_id = await submit(xiaomi_login)

        async def progress(state: str, meta: dict, elapsed: float) -> None:
            nonlocal sent
            if meta.get("stage") == "qr" and not sent:
                sent = True
                img = qrcode.make(meta["url"])
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть ссылку входа",
                                                                                 url=meta["url"])]])
                await bot.send_photo(chat_id, BufferedInputFile(buf.getvalue(), "qr.png"), reply_markup=kb,
                                     caption="Отсканируйте QR камерой телефона или откройте ссылку и подтвердите "
                                             "вход в аккаунт Xiaomi.")
                await st.restart("⏳ Жду подтверждения входа…")
            elif sent:
                left = max(0, 300 - int(elapsed))
                await st.show(f"⏳ Жду подтверждения входа… {spinner(elapsed)} осталось {left // 60}:{left % 60:02d}")
        res = await track(task_id, progress, timeout=360)
    except TaskFailed as exc:
        await st.show(f"❌ Вход не удался: {esc(exc)}", retry(SyncCB(action="login")), force=True)
        return
    except Exception as exc:  # noqa: BLE001
        await st.show(f"❌ Фоновый обработчик недоступен: {esc(type(exc).__name__)}", back_home(), force=True)
        return
    if res.get("status") != "ok":
        await st.show(f"❌ Вход не удался: {esc(res.get('reason', ''))}", retry(SyncCB(action="login")), force=True)
        return
    await st.show("✅ Вход выполнен, загружаю данные…", force=True)
    await run_sync(bot, chat_id)
