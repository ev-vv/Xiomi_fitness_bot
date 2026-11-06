from datetime import timedelta

import pytest

from ai.facts import overview_facts, period_facts, workout_facts
from ai.guard import check
from ai.analysis import template, mode_for
from analytics.periods import METRICS, period_view, workout_list
from core import data
from synth import TARGET

AFTER = TARGET + timedelta(days=1)


@pytest.fixture
def proc(app_env):
    return data.processor()


def _sheet(proc, metric, period, end=TARGET):
    return period_facts(proc, period_view(proc, metric, period, end, AFTER))


def test_every_metric_and_period_builds_compact_sheet(proc):
    for metric in METRICS:
        for period in ("day", "week", "month"):
            sheet = _sheet(proc, metric, period)
            assert sheet.facts and sheet.kpis
            assert len(sheet.render()) < 3000
            assert sheet.advice, f"{metric}/{period} has no advice candidates"


def test_heart_day_reports_the_spike_not_the_workout(proc):
    text = _sheet(proc, "heart", "day").render()
    assert "В 14:00 пульс до 130" in text
    assert "18:" not in text.split("ВОЗМОЖНЫЕ")[0].replace("Тренировка «Ходьба» в 18:00", "")


def test_sleep_day_flags_elevated_night_hr(proc):
    sheet = _sheet(proc, "sleep", "day")
    hr = [f for f in sheet.facts if "Пульс покоя во сне" in f.text]
    assert hr and hr[0].level == "high" and "выше вашей нормы" in hr[0].text
    assert sheet.baseline_note.startswith("рассчитана")
    assert any("Повышенный пульс во сне" in a.text for a in sheet.advice)


def test_stress_and_spo2_events(proc):
    stress = _sheet(proc, "stress", "day").render()
    assert "В 10:00 высокий стресс" in stress
    spo2 = _sheet(proc, "spo2", "day")
    assert spo2.has_data and "минимум 88 %" in spo2.render()
    assert any("ODI" in f.text for f in spo2.facts)


def test_period_sheet_compares_with_previous(proc):
    text = _sheet(proc, "sleep", "week").render()
    assert "По сравнению с предыдущим периодом" in text
    assert "Ночей с повышенным пульсом во сне: 1" in text


def test_overview_and_workout(proc):
    ov = overview_facts(proc, TARGET, AFTER)
    assert {f.metric for f in ov.facts} >= {"sleep", "heart", "stress", "activity"}
    assert len({f.id for f in ov.facts}) == len(ov.facts)
    ts = workout_list(proc)["ts"][0]
    w = workout_facts(proc, ts)
    assert "Время по пульсовым зонам" in w.render()


def test_template_passes_guard(proc):
    for metric in METRICS:
        for period in ("day", "week"):
            sheet = _sheet(proc, metric, period)
            mode = mode_for(sheet)
            out = template(sheet, mode)
            errors = check(out, sheet, mode)
            # Templates may have fewer insights than the LLM limits require, but never bad numbers or ids.
            assert not [e for e in errors if "числа" in e or "ссылки" in e], errors
