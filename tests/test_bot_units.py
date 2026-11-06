import asyncio
from datetime import date

from aiogram.exceptions import TelegramBadRequest

from bot import keyboards as kb
from bot.render import analysis_html, caption, chat_html, split
from bot.status import Status, bar


def test_callback_data_fits_telegram_limit():
    for cd in (kb.MetricCB(metric="stress", period="month", end="2026-10-21", force=1), kb.WorkoutCB(ts=1789567444, force=1),
               kb.AskCB(metric="sleep", period="week", end="2026-10-21"), kb.SetCB(key="sleep_need_min")):
        packed = cd.pack()
        assert len(packed.encode()) <= 64
        assert type(cd).unpack(packed) == cd
    markup = kb.after_analysis("sleep", "week", "2026-10-21", "2026-10-14", None)
    assert all(len(b.callback_data) <= 64 for row in markup.inline_keyboard for b in row if b.callback_data)


def test_render_helpers():
    assert chat_html("<b>ok</b> <script>x</script> <i>open") == "<b>ok</b> &lt;script&gt;x&lt;/script&gt; open"
    parts = split("\n".join(["строка " * 50] * 40), limit=1000)
    assert all(len(p) <= 1000 for p in parts) and len(parts) > 3
    assert len(caption("Т" * 2000, [])) <= 1024
    res = {"out": {"summary": "S <x>", "insights": [{"text": "I", "fact_ids": ["F1"]}],
                   "advice": [{"text": "A", "fact_ids": ["A1"]}], "caveat": ""}, "source": "template", "note": "нет связи"}
    a, adv = analysis_html(res, "week")
    assert "S &lt;x&gt;" in a and "Инсайты" in a and "без ИИ" in a and adv.startswith("💡")
    assert bar(1, 2, 4) == "▰▰▱▱"


class FakeBot:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send_message(self, chat_id, text, reply_markup=None):
        bot = self

        class Msg:
            async def edit_text(self, t, reply_markup=None):
                bot.edits.append(t)

            async def delete(self):
                raise TelegramBadRequest(method=None, message="gone")
        self.sent.append(text)
        return Msg()


def test_status_throttles_and_dedupes():
    async def run():
        fb = FakeBot()
        st = Status(fb, 1)
        await st.show("a")
        await st.show("b")          # throttled
        await st.show("c", force=True)
        await st.show("c", force=True)  # same text
        await st.delete()           # tolerant to TelegramBadRequest
        return fb
    fb = asyncio.run(run())
    assert fb.sent == ["a"] and fb.edits == ["c"]


def test_settings_cycle(app_env):
    from bot.handlers.settings import _state, _toggle

    assert _state()["sleep_need_min"] == "480"
    assert _toggle("sleep_need_min")["sleep_need_min"] == "510"
    s = _toggle("autosync")
    assert s["autosync"] == "0"
    from core import data

    assert data.analytics_config().sleep_need_min == 510


def test_brief_dates():
    from datetime import datetime

    from worker.tasks import brief_dates

    assert brief_dates(datetime(2026, 10, 22, 8, 0)) == (date(2026, 10, 21), date(2026, 10, 22))
    assert brief_dates(datetime(2026, 10, 22, 19, 0)) == (date(2026, 10, 22), date(2026, 10, 22))
