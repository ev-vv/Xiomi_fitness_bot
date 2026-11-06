import asyncio
from types import SimpleNamespace

import pytest

from ai import chat
from ai.tools import TOOLS, run_tool
from synth import TARGET


class Block(SimpleNamespace):
    def to_dict(self):
        return {k: v for k, v in vars(self).items()}


class FakeStream:
    def __init__(self, events, final):
        self.events, self.final = events, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for e in self.events:
                yield e
        return gen()

    async def get_final_message(self):
        return self.final


class FakeClient:
    def __init__(self, turns):
        self.turns, self.requests = list(turns), []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        return self.turns.pop(0)


USAGE = SimpleNamespace(input_tokens=8000, output_tokens=300, cache_read_input_tokens=6000, cache_creation_input_tokens=0)


def test_tools_return_fact_text(app_env):
    names = {t["name"] for t in TOOLS}
    assert names == {"get_overview", "get_metric", "compare_periods", "find_anomalies", "list_workouts", "get_workout"}
    for t in TOOLS:
        assert t["strict"] and t["input_schema"]["additionalProperties"] is False
    text, err = run_tool("get_metric", {"metric": "sleep", "period": "week", "end_date": TARGET.isoformat()})
    assert not err and "ФАКТЫ" in text
    text, err = run_tool("find_anomalies", {"start_date": TARGET.isoformat(), "end_date": TARGET.isoformat(),
                                            "metric": "heart"})
    assert "14:00" in text and "стресс" not in text
    text, _ = run_tool("list_workouts", {"start_date": None, "end_date": None, "sport_type": None})
    wid = int(text.split("id=")[1].split(" ")[0])
    assert "зонам" in run_tool("get_workout", {"workout_id": wid})[0]
    assert run_tool("get_metric", {"metric": "sleep", "period": "week", "end_date": "вчера"})[1] is True


def test_chat_turn_with_tool_call(app_env):
    tool_use = Block(type="tool_use", id="tu1", name="get_metric",
                     input={"metric": "sleep", "period": "week", "end_date": TARGET.isoformat()})
    first = FakeStream(
        [SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="tool_use", name="get_metric"))],
        SimpleNamespace(content=[tool_use], stop_reason="tool_use", usage=USAGE))
    answer_text = "За неделю вы спали в среднем <b>7 ч 50 мин</b>."
    second = FakeStream([SimpleNamespace(type="text", text=answer_text)],
                        SimpleNamespace(content=[Block(type="text", text=answer_text)], stop_reason="end_turn", usage=USAGE))
    fake = FakeClient([first, second])
    statuses, texts = [], []

    async def on_status(s):
        statuses.append(s)

    async def on_text(t):
        texts.append(t)
    res = asyncio.run(chat.answer("Как я спал?", [], "СВОДКА", on_status=on_status, on_text=on_text, client=fake))
    assert chat.ChatResult(text="Без инструментов").history_text() == "Без инструментов"
    assert res.text == answer_text and res.tools == ["get_metric"]
    assert res.history_text().endswith(f'[Бот: для этого ответа выполнено: get_metric {{"metric": "sleep", '
                                       f'"period": "week", "end_date": "{TARGET.isoformat()}"}}]')
    assert statuses == ["Смотрю данные", "Смотрю сон за неделю"]
    second_req = fake.requests[1]["messages"]
    assert second_req[1]["role"] == "assistant" and second_req[2]["content"][0]["tool_use_id"] == "tu1"
    assert "ФАКТЫ" in second_req[2]["content"][0]["content"]
    assert fake.requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert fake.requests[0]["output_config"] == {"effort": "medium"}


def test_chat_cancel(app_env):
    ev = asyncio.Event()
    ev.set()
    stream = FakeStream([SimpleNamespace(type="text", text="a")], None)

    async def noop(_):
        pass
    with pytest.raises(chat.Cancelled):
        asyncio.run(chat.answer("q", [], "c", on_status=noop, on_text=noop, cancel=ev, client=FakeClient([stream])))


def test_chat_web_search_pause_turn_and_sources(app_env):
    from ai.tools import CHAT_TOOLS

    assert CHAT_TOOLS[-1]["type"].startswith("web_search") and CHAT_TOOLS[-1]["max_uses"] == 2
    search = Block(type="server_tool_use", id="srv1", name="web_search", input={"query": "пульс покоя норма"})
    found = Block(type="web_search_tool_result", tool_use_id="srv1", content=[])
    first = FakeStream(
        [SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="server_tool_use",
                                                                                   name="web_search"))],
        SimpleNamespace(content=[search, found], stop_reason="pause_turn", usage=USAGE))
    cite = SimpleNamespace(type="web_search_result_location", url="https://example.org/rhr", title="Норма пульса")
    text = Block(type="text", text="Норма — 60–100 уд/мин.", citations=[cite, cite])
    second = FakeStream([SimpleNamespace(type="text", text=text.text)],
                        SimpleNamespace(content=[text], stop_reason="end_turn", usage=USAGE))
    fake = FakeClient([first, second])
    statuses = []

    async def on_status(s):
        statuses.append(s)

    async def noop(_):
        pass
    res = asyncio.run(chat.answer("Найди норму пульса", [], "СВОДКА", on_status=on_status, on_text=noop, client=fake))
    assert statuses == ["Ищу в интернете"] and res.tools == ["web_search"]
    assert res.sources == [("Норма пульса", "https://example.org/rhr")]
    assert res.calls == ['web_search {"query": "пульс покоя норма"}'] and "https://example.org/rhr" in res.history_text()
    resumed = fake.requests[1]["messages"]
    assert resumed[-1]["role"] == "assistant" and resumed[-1]["content"][0]["type"] == "server_tool_use"
