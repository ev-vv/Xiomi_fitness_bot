from datetime import timedelta
from types import SimpleNamespace

import pytest

from ai.facts import period_facts, workout_facts
from analytics.periods import METRICS, period_view, workout_list, workout_view
from charts import render_period, render_workout
from core import data
from synth import TARGET, make_lake

PNG = b"\x89PNG"


def test_all_dashboards_render(app_env):
    proc = data.processor()
    for metric in METRICS:
        for period in ("day", "week", "month"):
            view = period_view(proc, metric, period, TARGET, TARGET + timedelta(days=1))
            png = render_period(view, period_facts(proc, view).kpis, proc.cfg.sleep_need_min)
            assert png.startswith(PNG) and len(png) > 20_000
    ts = workout_list(proc)["ts"][0]
    s = workout_facts(proc, ts)
    assert render_workout(workout_view(proc, ts), "Ходьба", "5 окт", s.kpis).startswith(PNG)


def test_spo2_without_data_renders_hint(tmp_path):
    from analytics import Processor

    proc = Processor(make_lake(tmp_path, with_spo2=False, days=3))
    view = period_view(proc, "spo2", "week", TARGET)
    assert not view.has_data
    assert render_period(view, [], 480).startswith(PNG)


@pytest.fixture
def eager(app_env, monkeypatch):
    from worker import tasks
    from worker.celery_app import app

    app.conf.update(task_always_eager=True, result_backend="cache+memory://")
    monkeypatch.setattr(tasks, "today", lambda: TARGET + timedelta(days=1))
    # No network: the analysis client always fails -> template.
    from ai import client as llm

    def boom(**kw):
        raise RuntimeError("offline")
    monkeypatch.setattr(llm, "sync_client", lambda: SimpleNamespace(messages=SimpleNamespace(parse=boom)))
    yield tasks
    app.conf.update(task_always_eager=False)


def test_dashboard_and_analysis_tasks(eager):
    r = eager.render_dashboard.delay("sleep", "week", TARGET.isoformat()).get()
    assert r["status"] == "ok" and r["path"].endswith(".png") and r["end"] == TARGET.isoformat()
    assert r["has_next"] is False and r["latest"] == TARGET.isoformat()
    a = eager.analyze_period.delay("sleep", "week", TARGET.isoformat()).get()
    assert a["source"] == "template" and a["out"]["advice"]
    w = eager.render_workout_task.delay(int(workout_list(data.processor())["ts"][0])).get()
    assert w["status"] == "ok"
