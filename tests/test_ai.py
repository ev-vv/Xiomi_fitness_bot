from types import SimpleNamespace

import anthropic
import httpx
import pytest

from ai import analysis
from ai.facts import period_facts
from ai.guard import check
from ai.schemas import AnalysisOut, Item
from analytics.periods import period_view
from core import data
from storage import db
from synth import TARGET


@pytest.fixture
def sheet(app_env):
    proc = data.processor()
    return period_facts(proc, period_view(proc, "sleep", "week", TARGET))


def good(sheet) -> AnalysisOut:
    f = [x.id for x in sheet.ranked()]
    a = [x.id for x in sheet.advice]
    return AnalysisOut(summary="Неделя прошла ровно.",
                       insights=[Item(text="Режим стабильный.", fact_ids=[f[0]]), Item(text="Сон хороший.", fact_ids=[f[1]])],
                       advice=[Item(text="Сохраняйте режим.", fact_ids=[a[0]]), Item(text="Ложитесь вовремя.", fact_ids=[f[0]]),
                               Item(text="Гуляйте днём.", fact_ids=[f[1]])])


class FakeMessages:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def parse(self, **kw):
        self.calls.append(kw)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        usage = SimpleNamespace(input_tokens=5000, output_tokens=1500, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(parsed_output=out, stop_reason="end_turn", usage=usage)


def client(outputs):
    return SimpleNamespace(messages=FakeMessages(outputs))


def test_guard_rejects_invented_numbers_and_ids(sheet):
    out = good(sheet)
    assert check(out, sheet, "period") == []
    out.summary = "Вы спали 731 минуту."
    out.advice[0].fact_ids = ["F99"]
    errors = check(out, sheet, "period")
    assert any("731" in e for e in errors) and any("F99" in e for e in errors)


def test_analysis_retries_once_then_caches(sheet):
    bad = good(sheet).model_copy(update={"summary": "Сон 999 минут."})
    c = client([bad, good(sheet)])
    res = analysis.analyze(sheet, client=c)
    assert res.source == "llm" and len(c.messages.calls) == 2
    assert "999" in c.messages.calls[1]["messages"][2]["content"]
    assert c.messages.calls[0]["output_config"] == {"effort": "medium"}
    assert c.messages.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    again = analysis.analyze(sheet, client=client([]))
    assert again.source == "cache"
    assert db.usage_summary()[0][1] == 2  # two billed calls recorded


def test_analysis_falls_back_to_template(sheet):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    res = analysis.analyze(sheet, client=client([anthropic.APIConnectionError(request=req)]), force=True)
    assert res.source == "template" and res.note == "Нет связи с Claude API"
    assert res.out.advice


def test_budget_exceeded_uses_template(sheet, monkeypatch):
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "0.000001")
    from core import config

    config.settings.cache_clear()
    db.add_usage("chat", "claude-haiku-5-5", {"input_tokens": 100}, 0.01)
    c = client([good(sheet)])
    res = analysis.analyze(sheet, client=c, force=True)
    assert res.source == "template" and "лимит" in res.note and not c.messages.calls


def test_cost_table():
    from ai.client import cost

    assert cost("claude-haiku-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == pytest.approx(0.60)


def test_check_ready_falls_back_to_count_tokens(monkeypatch):
    import asyncio

    import httpx2

    from ai import client as llm

    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.url.path.startswith("/v1/models/"):
            return httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": "no"}})
        return httpx2.Response(200, json={"input_tokens": 3})

    fake = anthropic.AsyncAnthropic(api_key="k", base_url="https://proxy.test",
                                    http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    monkeypatch.setattr(llm, "async_client", lambda: fake)
    assert asyncio.run(llm.check_ready("claude-haiku-5-5")) == (True, "Claude API доступен")
    assert seen == ["/v1/models/claude-haiku-5-5", "/v1/messages/count_tokens"]


def test_profile_reaches_analysis_and_guard(sheet):
    from core import profile as prof

    prof.save("birth_year", "1995")
    text = prof.render_current()
    out = good(sheet).model_copy(update={"summary": "Для ваших 31 года ориентир максимального пульса 186 уд/мин."})
    assert check(out, sheet, "period") and check(out, sheet, "period", text) == []
    c = client([good(sheet)])
    analysis.analyze(sheet, client=c)
    assert "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ" in c.messages.calls[0]["messages"][0]["content"]
    assert analysis.analyze(sheet, client=client([])).source == "cache"
    prof.save("birth_year", "1980")  # a changed profile must not reuse the old analysis
    c2 = client([good(sheet)])
    assert analysis.analyze(sheet, client=c2).source == "llm" and len(c2.messages.calls) == 1


def test_cost_counts_web_searches():
    from ai.client import cost, usage_dict

    u = usage_dict(SimpleNamespace(input_tokens=0, output_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=2)))
    assert cost("claude-haiku-5-5", u) == pytest.approx(0.02)
