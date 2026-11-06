"""End-to-end handler flows with a fake Bot API session and eager Celery (no Telegram, no network)."""

import asyncio
import itertools
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from synth import TARGET

OWNER, FRIEND = 42, 43
USER = User(id=OWNER, is_bot=False, first_name="Owner")
_ids = itertools.count(100)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendPhoto, EditMessageText)):
            chat = Chat(id=getattr(method, "chat_id", None) or OWNER, type="private")
            return Message(message_id=next(_ids), date=datetime.now(), chat=chat,
                           text=getattr(method, "text", None) or getattr(method, "caption", None) or "").as_(bot)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover - not used
        raise NotImplementedError

    def texts(self) -> list[str]:
        return [getattr(c, "text", None) or getattr(c, "caption", None) or "" for c in self.calls
                if isinstance(c, (SendMessage, SendPhoto, EditMessageText))]


@pytest.fixture
def env(app_env, tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST")
    monkeypatch.setenv("OWNER_IDS", f"{OWNER},{FRIEND}")
    root = tmp_path / "data"
    home = root / "users" / str(OWNER)  # the owner sees the synthetic lake; FRIEND has no data and no login
    home.mkdir(parents=True)
    for entry in app_env.iterdir():
        (home / entry.name).symlink_to(entry)
    (home / "secrets").mkdir()
    (home / "secrets" / "token.json").write_text("{}")
    monkeypatch.setenv("MIFIT_DATA_DIR", str(root))
    from core import config

    config.settings.cache_clear()
    from worker import tasks
    from worker.celery_app import app

    app.conf.update(task_always_eager=True, task_store_eager_result=True, result_backend="cache+memory://")
    monkeypatch.setattr(tasks, "today", lambda: TARGET + timedelta(days=1))
    monkeypatch.setattr(tasks, "_maybe_backfill_hr", lambda ts: False)  # the fake token must not reach Xiaomi
    from ai import client as llm

    def offline(**kw):
        raise RuntimeError("offline")
    monkeypatch.setattr(llm, "sync_client", lambda: SimpleNamespace(messages=SimpleNamespace(parse=offline)))
    from bot.app import build

    session = FakeSession()
    bot, dp = build(storage=MemoryStorage(), session=session)
    yield bot, dp, session
    app.conf.update(task_always_eager=False, task_store_eager_result=False)
    for router in dp.sub_routers:  # module-level routers: detach so the next test can build again
        router._parent_router = None


def msg(text: str, user: User = USER) -> Update:
    chat = Chat(id=user.id, type="private")
    return Update(update_id=next(_ids), message=Message(message_id=next(_ids), date=datetime.now(), chat=chat,
                                                         from_user=user, text=text))


def cb(data: str, user: User = USER) -> Update:
    chat = Chat(id=user.id, type="private")
    m = Message(message_id=next(_ids), date=datetime.now(), chat=chat, text="menu")
    return Update(update_id=next(_ids), callback_query=CallbackQuery(id=str(next(_ids)), from_user=user,
                                                                     chat_instance="x", message=m, data=data))


def feed(bot, dp, *updates):
    async def run():
        for u in updates:
            await dp.feed_update(bot, u)
    asyncio.run(run())


def test_strangers_get_only_their_id(env):
    bot, dp, s = env
    stranger = User(id=7, is_bot=False, first_name="X")
    feed(bot, dp, msg("/start", stranger), msg("/sleep", stranger), cb("nav:sleep", stranger))
    assert len(s.calls) == 1 and "Доступ к боту закрыт" in s.calls[0].text and "<code>7</code>" in s.calls[0].text


def test_menu_and_period_screens(env):
    bot, dp, s = env
    feed(bot, dp, msg("/start"), cb("nav:sleep"))
    texts = s.texts()
    assert "Fitness Analytics" in texts[0] and "Восстановление" in texts[0]
    assert "Выберите период" in texts[1]


def test_metric_flow_sends_photo_analysis_and_advice(env):
    bot, dp, s = env
    feed(bot, dp, cb(f"m:sleep:week:{TARGET.isoformat()}:0"))
    photos = [c for c in s.calls if isinstance(c, SendPhoto)]
    sent = [c.text for c in s.calls if isinstance(c, SendMessage)]
    assert len(photos) == 1 and "Сон · неделя" in photos[0].caption
    assert any(t.startswith("🧠 <b>Анализ</b>") and "Инсайты" in t for t in sent)
    advice = [c for c in s.calls if isinstance(c, SendMessage) and c.text.startswith("💡")][0]
    buttons = [b.text for row in advice.reply_markup.inline_keyboard for b in row]
    assert "◀️ Пред. неделя" in buttons and "💬 Спросить ИИ об этом" in buttons
    assert any(isinstance(c, DeleteMessage) for c in s.calls)  # the status message is cleaned up
    assert any("⏳ Собираю данные" in t for t in s.texts())


def test_workouts_list_and_detail(env):
    bot, dp, s = env
    feed(bot, dp, cb("wl:0"))
    kb = [c for c in s.calls if isinstance(c, EditMessageText)][-1].reply_markup
    first = kb.inline_keyboard[0][0]
    assert first.callback_data.startswith("w:")
    feed(bot, dp, cb(first.callback_data))
    assert any(isinstance(c, SendPhoto) for c in s.calls)
    assert any(isinstance(c, SendMessage) and c.text.startswith("💡") for c in s.calls)


def test_chat_flow(env, monkeypatch):
    bot, dp, s = env
    from bot.handlers import chat as chat_h

    async def ready(model):
        return True, "Claude API доступен"
    monkeypatch.setattr(chat_h, "check_ready", ready)

    async def fake_answer(q, history, context, *, on_status, on_text, cancel=None, client=None):
        assert "СВОДКА" in context and q == "Как я спал?"
        await on_status("Смотрю сон за неделю")
        await on_text("Вы спали <b>хорошо</b>.")
        return chat_h.ai_chat.ChatResult(text="Вы спали <b>хорошо</b>.", tools=["get_metric", "web_search"],
                                         sources=[("Сон & возраст", "https://example.org/a?b=1&c=2")],
                                         calls=['get_metric {"metric": "sleep"}', 'web_search {"query": "сон норма"}'])
    monkeypatch.setattr(chat_h.ai_chat, "answer", fake_answer)
    monkeypatch.setattr(chat_h, "async_client", lambda: SimpleNamespace(with_options=lambda **kw: None))
    feed(bot, dp, cb("nav:chat"), msg("Как я спал?"))
    texts = s.texts()
    assert any("🟢 Claude API доступен" in t for t in texts)
    assert texts[-1] == ("Вы спали <b>хорошо</b>.\n\n🌐 <b>Источники</b>\n"
                         '• <a href="https://example.org/a?b=1&amp;c=2">Сон &amp; возраст</a>')
    from storage.db import recent_chat

    history = recent_chat(OWNER)
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[1]["content"] == ('Вы спали <b>хорошо</b>.\n\n[Бот: для этого ответа выполнено: get_metric {"metric": '
                                     '"sleep"}; web_search {"query": "сон норма"}. Источники: https://example.org/a?b=1&c=2]')
    feed(bot, dp, msg("/menu"), msg("просто текст"))
    assert "Fitness Analytics" in s.texts()[-1]  # outside chat, free text shows the menu


def test_profile_flow(env):
    bot, dp, s = env
    from core import profile as prof, user

    feed(bot, dp, cb("nav:settings"), cb("nav:profile"), cb("pf:sex:edit:"), cb("pf:sex:set:f"),
         cb("pf:birth_year:edit:"), msg("19"), msg("1995"))
    texts = s.texts()
    assert "Профиль: <b>не заполнен</b>" in texts[0] and "не указано" in texts[1]
    assert "⚠️ Не получилось" in texts[-2] and texts[-1].startswith("✅ Сохранено")
    with user.use(OWNER):
        p = prof.load()
    assert (p.sex, p.birth_year) == ("f", 1995)
    feed(bot, dp, msg("1990"))  # editing state is cleared after a save: free text goes to the menu
    with user.use(OWNER):
        assert prof.load().birth_year == 1995
    with user.use(FRIEND):
        assert prof.load().is_empty  # profiles are per user


def test_second_user_is_isolated(env, monkeypatch):
    bot, dp, s = env
    from core import user
    from storage.db import add_usage, get_setting, recent_chat, spent_today

    friend = User(id=FRIEND, is_bot=False, first_name="Friend")
    feed(bot, dp, msg("/start", friend))
    assert "Добро пожаловать" in s.texts()[-1]
    kb = s.calls[-1].reply_markup.inline_keyboard
    assert kb[0][0].text == "🔐 Войти через QR"
    feed(bot, dp, cb("s:alerts", friend))  # friend turns alerts off — the owner's setting is unchanged
    with user.use(FRIEND):
        assert get_setting("alerts") == "0" and recent_chat(FRIEND) == []
        add_usage("chat", "claude-haiku-5-5", {"input_tokens": 1}, 0.30)
    with user.use(OWNER):
        assert get_setting("alerts") == "1" and spent_today() == 0.0
    with user.use(FRIEND):
        assert spent_today() == 0.30


def test_tick_serves_each_user_with_own_data(env, monkeypatch):
    from core import push, user
    from storage.db import get_setting, set_setting
    from worker import tasks

    sent = []
    monkeypatch.setattr(push, "send_message", lambda chat_id, html, kb=None: sent.append((chat_id, html)) or True)
    with user.use(OWNER):
        for k, v in (("autosync", "0"), ("alerts", "0"), ("brief_time", "00:00")):
            set_setting(k, v)
    done = tasks.tick.run()
    assert [c for c, _ in sent] == [OWNER] and "brief" in done[str(OWNER)]  # FRIEND has no Xiaomi login yet
    assert done[str(FRIEND)] == {}
    with user.use(OWNER):
        assert get_setting("brief_sent_on")
    with user.use(FRIEND):
        assert get_setting("brief_sent_on") == ""
