"""FactSheet: everything the LLM needs, pre-computed and pre-worded.

A small model is unreliable at arithmetic and at reading z-scores, so all comparisons, labels and
trends are made here. The model only selects, explains and phrases. Every fact carries an id; the
LLM must cite ids, and `guard.py` rejects numbers that do not appear in the sheet.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import polars as pl

from analytics.periods import STRESS_ZONES, PeriodView, period_view, workout_view
from analytics.processor import Processor
from analytics.sleep import QUALITY_WEIGHTS, clock
from core.fmt import day, dur, num, plural, span

LEVELS = {"high": "важно", "mid": "заметно", "low": "фон"}
DIRECTIONS = {"good": "хорошо", "bad": "плохо", "neutral": "нейтрально"}
METRIC_TITLES = {"sleep": "Сон", "heart": "Пульс", "spo2": "Кислород (SpO2)", "stress": "Стресс",
                 "activity": "Активность", "workout": "Тренировка", "overview": "Сводка"}
PERIOD_TITLES = {"day": "день", "week": "неделя", "month": "месяц"}
SUBSCORE_NAMES = {"duration": "длительность", "eff": "эффективность", "deep": "глубокий сон", "rem": "REM-сон",
                  "continuity": "непрерывность", "regularity": "регулярность"}
STRESS_ZONE_NAMES = {"relaxed": "расслабление", "mild": "лёгкий", "moderate": "средний", "high": "высокий"}
HR_ZONE_NAMES = {"warm_up": "разминка", "fat_burning": "жиросжигание", "aerobic": "аэробная",
                 "anaerobic": "анаэробная", "extreme": "максимальная"}
SPORTS = {"outdoor_walking": ("🚶", "Ходьба"), "outdoor_running": ("🏃", "Бег"), "indoor_running": ("🏃", "Бег (дорожка)"),
          "strength_training": ("🏋️", "Силовая"), "free_training": ("🤸", "Свободная тренировка"),
          "outdoor_cycling": ("🚴", "Велосипед"), "indoor_cycling": ("🚴", "Велотренажёр"),
          "swimming": ("🏊", "Плавание"), "yoga": ("🧘", "Йога"), "hiit": ("🔥", "HIIT")}

# Advice templates keyed by fact tags. Numbers in them are allowed in LLM output too.
ADVICE = {
    "short_sleep": "Сдвиньте отбой на 20–30 минут раньше, чтобы приблизиться к цели сна; начните с сегодняшнего вечера.",
    "sleep_debt": "Закройте недосып постепенно: несколько ночей подряд ложитесь раньше, а не отсыпайтесь до обеда в выходные.",
    "irregular": "Ложитесь и вставайте в одно и то же время ±30 минут, включая выходные: регулярность помогает глубокому сну.",
    "jetlag": "Сократите разницу режима будни/выходные: не сдвигайте подъём в выходные больше чем на час.",
    "late_bed": "Сегодня постарайтесь лечь в своё обычное время — поздний отбой сдвигает режим на следующие дни.",
    "low_eff": "Если не уснули за 20 минут — встаньте, займитесь спокойным делом при тусклом свете и вернитесь в кровать, когда захочется спать.",
    "awakenings": "Проверьте условия в спальне: прохлада 18–20 °C, темнота, тишина; меньше жидкости и алкоголя вечером.",
    "low_deep": "Глубокому сну помогает активность днём (не позже чем за 3 часа до сна) и отказ от алкоголя вечером.",
    "low_rem": "REM-сон приходится на конец ночи — его первым срезает ранний будильник; добавьте времени сна утром.",
    "high_sleep_rhr": "Повышенный пульс во сне часто связан с поздним ужином, алкоголем, перегрузкой или начинающейся простудой — сегодня лучше лёгкая нагрузка и ранний отбой.",
    "rising_rhr": "Пульс покоя растёт — заложите 1–2 дня полегче и следите за самочувствием.",
    "hr_spikes": "Вспомните, что происходило в эти моменты (быстрая ходьба, кофе, волнение). Если пульс часто растёт в покое без причины или с неприятными ощущениями — обсудите это с врачом.",
    "brady": "Низкий пульс днём бывает у тренированных людей; при слабости или головокружении обратитесь к врачу.",
    "spo2_dips": "Повторяющиеся ночные просадки SpO2 стоит обсудить с врачом, особенно если есть храп или дневная сонливость.",
    "spo2_missing": "Включите в Mi Fitness ночной мониторинг SpO2 для браслета, чтобы видеть уровень кислорода во сне.",
    "stress_high": "Добавьте короткие дыхательные паузы (медленный вдох и выдох, 2–3 минуты) в часы, когда стресс обычно растёт.",
    "stress_sleep": "После напряжённых дней особенно важен вечерний «буфер»: 30–60 минут без работы и экранов перед сном.",
    "stress_rising": "Стресс растёт — запланируйте на неделю 1–2 регулярных занятия для разгрузки: прогулку, спорт, хобби.",
    "high_load": "Нагрузка резко выросла относительно прошлого месяца — следующие 2–3 дня сделайте лёгкими.",
    "low_steps": "Добавьте 15–20 минут ходьбы: часть пути пешком или прогулка после обеда.",
    "workout_intense": "После интенсивной тренировки дайте телу восстановиться перед следующей тяжёлой нагрузкой; сон и белок ускоряют восстановление.",
    "workout_easy": "Чтобы тренировка давала больше эффекта, добавьте пару отрезков в аэробной или анаэробной зоне.",
    "good_sleep": "Режим работает — сохраняйте текущее время отбоя и подъёма.",
    "steady": "Показатели стабильны — продолжайте в том же ритме.",
}

CAVEATS = {
    "sleep": "Фазы сна — суммарные минуты за ночь от Xiaomi, без поминутной гипнограммы.",
    "heart": "Днём пульс измеряется примерно раз в 10 минут (чаще только при активности), поминутных шагов нет — "
             "быструю ходьбу нельзя отличить от учащения пульса в покое.",
    "spo2": "Замеры SpO2 браслетом во сне ориентировочные и не заменяют медицинскую пульсоксиметрию.",
    "stress": "Стресс оценивается браслетом по вариабельности пульса раз в 5 минут (шкала Xiaomi 1–100).",
    "workout": "Пульсовые зоны и тренировочный эффект рассчитаны браслетом.",
}


@dataclass
class Fact:
    id: str
    metric: str
    text: str
    level: str = "low"
    direction: str = "neutral"
    tags: tuple[str, ...] = ()


@dataclass
class Advice:
    id: str
    text: str
    fact_ids: list[str]


@dataclass
class FactSheet:
    metric: str
    period: str
    title: str
    facts: list[Fact] = field(default_factory=list)
    advice: list[Advice] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    baseline_note: str | None = None
    kpis: list[tuple[str, str]] = field(default_factory=list)
    has_data: bool = True

    def ranked(self) -> list[Fact]:
        order = {"high": 0, "mid": 1, "low": 2}
        return sorted(self.facts, key=lambda f: order[f.level])

    def render(self) -> str:
        lines = [f"{self.title}"]
        if self.baseline_note:
            lines.append(f"Личная норма: {self.baseline_note}")
        lines.append("ФАКТЫ:")
        lines += [f"{f.id} [{LEVELS[f.level]}, {DIRECTIONS[f.direction]}] {f.text}" for f in self.ranked()]
        if self.advice:
            lines.append("ВОЗМОЖНЫЕ СОВЕТЫ (выбери подходящие и сформулируй своими словами):")
            lines += [f"{a.id} (по {', '.join(a.fact_ids)}): {a.text}" for a in self.advice]
        if self.caveats:
            lines.append("ОГРАНИЧЕНИЯ ДАННЫХ:")
            lines += [f"- {c}" for c in self.caveats]
        return "\n".join(lines)

    def fact_ids(self) -> set[str]:
        return {f.id for f in self.facts} | {a.id for a in self.advice}


class _Builder:
    """Accumulates facts with sequential ids; shared across metrics for overview sheets."""

    def __init__(self, sheet: FactSheet, proc: Processor) -> None:
        self.sheet, self.proc, self.cfg = sheet, proc, proc.cfg
        self._n = 0

    def add(self, metric: str, text: str, level: str = "low", direction: str = "neutral", *tags: str) -> str:
        self._n += 1
        fid = f"F{self._n}"
        self.sheet.facts.append(Fact(fid, metric, text, level, direction, tags))
        return fid

    def finish(self) -> FactSheet:
        """Advice candidates from tags: at most one per tag, important facts first."""
        seen: dict[str, list[str]] = {}
        for f in self.sheet.ranked():
            for t in f.tags:
                seen.setdefault(t, []).append(f.id)
        for i, (tag, ids) in enumerate(seen.items(), 1):
            if tag in ADVICE:
                self.sheet.advice.append(Advice(f"A{i}", ADVICE[tag], ids[:3]))
        self.sheet.advice = self.sheet.advice[:7]
        return self.sheet

    # ---- helpers -----------------------------------------------------------------

    def norm(self, row: dict[str, Any] | None, m: str) -> tuple[float | None, int]:
        """Personal median and number of baseline days for metric `m` as of `row`'s day."""
        if not row:
            return None, 0
        n = row.get(f"{m}_n") or 0
        return (row.get(f"{m}_med") if n >= self.cfg.baseline_min_days else None), n

    def baseline_note(self, row: dict[str, Any] | None, m: str) -> None:
        _, n = self.norm(row, m)
        need = self.cfg.baseline_min_days
        if n < need:
            self.sheet.baseline_note = (f"ещё формируется ({n} из {need} дней) — пока сравниваем с типичными "
                                        f"значениями для взрослых")
        else:
            self.sheet.baseline_note = f"рассчитана по {n} предыдущим дням"


def _last(df: pl.DataFrame) -> dict[str, Any] | None:
    return df.row(-1, named=True) if not df.is_empty() else None


def _mean(s: pl.Series) -> float | None:
    s = s.drop_nulls()
    return float(s.mean()) if s.len() else None


def _slope_per_week(df: pl.DataFrame, col: str, min_points: int = 5) -> float | None:
    w = df.filter(pl.col(col).is_not_null())
    if w.height < min_points:
        return None
    x = (w["date"] - w["date"].min()).dt.total_days().cast(pl.Float64)
    y = w[col].cast(pl.Float64)
    var = ((x - x.mean()) ** 2).sum()
    return ((x - x.mean()) * (y - y.mean())).sum() / var * 7 if var else None


def _signed(x: float, unit: str = "", nd: int = 0) -> str:
    v = num(abs(x), nd)
    return f"{'+' if x > 0 else '−' if x < 0 else '±'}{v}{unit}"


def _anoms(view: PeriodView, *types: str) -> list[tuple[date, dict[str, Any]]]:
    return [(d, a) for d, items in view.extra.get("anomalies", {}).items() for a in items if a["type"] in types]


def _title(metric: str, view: PeriodView) -> str:
    t = f"{METRIC_TITLES[metric]} · {PERIOD_TITLES[view.period]} · {span(view.start, view.end)}"
    return t + ("" if view.complete else " (день ещё идёт)")


# ---- sleep -------------------------------------------------------------------------

def _sleep_day(b: _Builder, view: PeriodView) -> None:
    n, row = view.extra["night"], _last(view.days)
    need = b.cfg.sleep_need_min
    tst = n["tst"]
    gap = tst - need
    med, _ = b.norm(row, "tst")
    usual = f" (обычно {dur(med)})" if med else ""
    if gap < -20:
        b.add("sleep", f"Сон {dur(tst)} при цели {dur(need)} — не хватило {dur(-gap)}{usual}",
              "high" if gap < -60 else "mid", "bad", "short_sleep")
    else:
        b.add("sleep", f"Сон {dur(tst)} при цели {dur(need)} — цель выполнена{usual}", "mid", "good", "good_sleep")

    prev = b.proc.nights.filter(pl.col("is_main") & (pl.col("date") < n["date"])
                                & (pl.col("date") >= n["date"] - timedelta(days=7)))
    shift = n["bed_min"] - prev["bed_min"].median() if prev.height >= 3 else None
    bed = f"Отбой {clock(n['bed_min'])}, подъём {clock(n['wake_min'])}"
    if shift is not None and abs(shift) >= 45:
        b.add("sleep", f"{bed} — отбой на {dur(abs(shift))} {'позже' if shift > 0 else 'раньше'} обычного",
              "mid", "bad" if shift > 0 else "neutral", *(("late_bed",) if shift > 0 else ()))
    else:
        b.add("sleep", bed + (" — в обычное время" if shift is not None else ""))

    eff, lat = n["eff"], n.get("latency")
    poor = eff < 85 or (lat or 0) > 30
    b.add("sleep", f"Эффективность сна {num(eff)} % (в постели {dur(n['tib'])})"
          + (f", засыпание за {num(lat)} мин" if lat is not None else ""),
          "mid" if poor else "low", "bad" if poor else "good", *(("low_eff",) if poor else ()))

    for phase, name, lo, hi, tag in (("deep", "Глубокий сон", 13, 23, "low_deep"), ("rem", "REM-сон", 20, 25, "low_rem")):
        pct = n.get(f"{phase}_pct")
        if pct is None:
            continue
        if pct < lo:
            b.add("sleep", f"{name} {dur(n[phase])} ({num(pct)} %) — ниже типичных {lo}–{hi} %", "mid", "bad", tag)
        else:
            label = f"в типичном диапазоне {lo}–{hi} %" if pct <= hi else f"выше типичных {lo}–{hi} % — это хорошо"
            b.add("sleep", f"{name} {dur(n[phase])} ({num(pct)} %) — {label}", "low", "good")

    waso, cnt = n.get("waso") or 0, n.get("awake_count") or 0
    bad = waso > 30 or cnt >= 4
    b.add("sleep", f"Пробуждений: {cnt}, бодрствование внутри сна {dur(waso)}", "mid" if bad else "low",
          "bad" if bad else "good", *(("awakenings",) if bad else ()))

    rhr = n.get("sleep_rhr")
    if rhr is not None:
        m, _ = b.norm(row, "sleep_rhr")
        if m is not None and rhr - m >= b.cfg.sleep_hr_elevated_bpm:
            b.add("sleep", f"Пульс покоя во сне {num(rhr)} уд/мин — на {num(rhr - m)} выше вашей нормы {num(m)}",
                  "high", "bad", "high_sleep_rhr")
        elif m is not None:
            b.add("sleep", f"Пульс покоя во сне {num(rhr)} уд/мин — в пределах вашей нормы {num(m)}", "low", "good")
        elif rhr > 70:
            b.add("sleep", f"Пульс покоя во сне {num(rhr)} уд/мин — выше типичных для взрослых 45–70", "mid", "bad",
                  "high_sleep_rhr")
        else:
            b.add("sleep", f"Пульс покоя во сне {num(rhr)} уд/мин (минимум {num(n.get('hr_min'))})")
        dip = n.get("hr_dip")
        if dip is not None and dip <= -2:
            b.add("sleep", f"Во второй половине ночи пульс был выше, чем в первой, на {num(-dip, 1)} уд/мин — "
                           "признак неполного восстановления", "mid", "bad", "high_sleep_rhr")
        elif dip is not None and dip >= 3:
            b.add("sleep", f"К утру пульс снижался на {num(dip, 1)} уд/мин — нормальное восстановление", "low", "good")

    subs = {k: n.get(f"q_{k}") for k in QUALITY_WEIGHTS if n.get(f"q_{k}") is not None}
    weakest = min(subs, key=subs.get) if subs else None
    b.add("sleep", f"Оценка сна Xiaomi {num(n.get('sleep_score'))}/100, индекс качества {num(n.get('quality_index'))}/100"
          + (f"; слабее всего — {SUBSCORE_NAMES[weakest]} ({num(subs[weakest])}/100)" if weakest else ""))

    short = b.proc.nights.filter(pl.col("is_main") & (pl.col("date") <= n["date"])).sort("date", descending=True)
    streak = 0
    for t in short["tst"]:
        if t >= need - 45:
            break
        streak += 1
    if streak >= 2:
        b.add("sleep", f"{streak}-я короткая ночь подряд", "high", "bad", "sleep_debt")
    b.sheet.kpis = [("Сон", dur(tst)), ("Эффективность", f"{num(eff)} %"),
                    ("Оценка", num(n.get("sleep_score"))), ("Качество", num(n.get("quality_index")))]
    b.baseline_note(row, "tst")


def _sleep_period(b: _Builder, view: PeriodView) -> None:
    nights, need = view.nights, b.cfg.sleep_need_min
    k = nights.height
    avg = nights["tst"].mean()
    met = nights.filter(pl.col("tst") >= need - 15).height
    debt = (nights["tst"] - need).sum()
    b.add("sleep", f"В среднем {dur(avg)} сна за {k} {plural(k, 'ночь', 'ночи', 'ночей')}; цель {dur(need)} "
                   f"выполнена в {met} из {k}", "high" if avg < need - 45 else "mid",
          "bad" if avg < need - 20 else "good", *(("short_sleep",) if avg < need - 20 else ("good_sleep",)))
    if debt < -60:
        b.add("sleep", f"Накопленный недосып за период — {dur(-debt)}", "high" if debt < -240 else "mid", "bad", "sleep_debt")
    lo, hi = nights.sort("tst").row(0, named=True), nights.sort("tst").row(-1, named=True)
    if k >= 2:
        b.add("sleep", f"Самая короткая ночь — {day(lo['date'], True)}: {dur(lo['tst'])}; "
                       f"самая длинная — {day(hi['date'], True)}: {dur(hi['tst'])}")
    sd = nights["bed_min"].std() if k >= 3 else None
    if sd is not None:
        label, lvl, dirn, tags = ("стабильный", "low", "good", ()) if sd < 30 else \
            ("плавающий", "mid", "bad", ("irregular",)) if sd < 60 else ("нерегулярный", "high", "bad", ("irregular",))
        b.add("sleep", f"Отбой в среднем в {clock(nights['bed_min'].mean())}, разброс ±{num(sd)} мин — режим {label}",
              lvl, dirn, *tags)
    weekend = nights.filter(pl.col("date").dt.weekday() >= 6)["midpoint_min"]
    weekday = nights.filter(pl.col("date").dt.weekday() < 6)["midpoint_min"]
    if len(weekend) and len(weekday):
        jl = weekend.median() - weekday.median()
        if abs(jl) >= 60:
            b.add("sleep", f"В выходные середина сна смещается на {dur(abs(jl))} {'позже' if jl > 0 else 'раньше'} "
                           "будней («социальный джетлаг»)", "mid", "bad", "jetlag")
    for phase, name, lo_, hi_, tag in (("deep_pct", "глубокого сна", 13, 23, "low_deep"),
                                       ("rem_pct", "REM-сна", 20, 25, "low_rem")):
        v = _mean(nights[phase])
        if v is None:
            continue
        low = v < lo_
        b.add("sleep", f"Доля {name} в среднем {num(v)} % (типично {lo_}–{hi_} %)", "mid" if low else "low",
              "bad" if low else "good", *((tag,) if low else ()))
    q, s = _mean(nights["quality_index"]), _mean(nights["sleep_score"])
    b.add("sleep", f"Средний индекс качества {num(q)}/100, оценка Xiaomi {num(s)}/100")
    aw = _mean(nights["awake_count"])
    if aw is not None and aw >= 3:
        b.add("sleep", f"В среднем {num(aw, 1)} пробуждения за ночь", "mid", "bad", "awakenings")
    rhr = _mean(nights["sleep_rhr"])
    slope = _slope_per_week(nights, "sleep_rhr")
    if rhr is not None:
        trend = ""
        if slope is not None and abs(slope) >= 1:
            trend = f", тренд {_signed(slope, ' уд/мин')} в неделю"
        b.add("sleep", f"Пульс покоя во сне в среднем {num(rhr)} уд/мин{trend}",
              "mid" if slope and slope >= 1.5 else "low", "bad" if slope and slope >= 1.5 else "neutral",
              *(("rising_rhr",) if slope and slope >= 1.5 else ()))
    elevated = _anoms(view, "sleep_hr_elevated")
    if elevated:
        b.add("sleep", f"Ночей с повышенным пульсом во сне: {len(elevated)} ({', '.join(day(d) for d, _ in elevated[:4])})",
              "high", "bad", "high_sleep_rhr")
    pn = view.prev_nights
    if pn.height >= 3:
        d_tst, d_q = avg - pn["tst"].mean(), (q or 0) - (_mean(pn["quality_index"]) or 0)
        b.add("sleep", f"По сравнению с предыдущим периодом: сон {_signed(d_tst, ' мин')}, качество {_signed(d_q)}",
              "mid" if abs(d_tst) >= 20 else "low", "good" if d_tst > 0 else "bad" if d_tst < -20 else "neutral")
    b.sheet.kpis = [("Сон в среднем", dur(avg)), ("Ночей", str(k)), ("Недосып", dur(-debt) if debt < 0 else "нет"),
                    ("Качество", num(q))]
    b.baseline_note(_last(view.days), "tst")


# ---- heart ---------------------------------------------------------------------------

def _heart_day(b: _Builder, view: PeriodView) -> None:
    row = _last(view.days) or {}
    rhr = row.get("rhr_device")
    if rhr is not None:
        m, _ = b.norm(row, "rhr_device")
        if m is not None and abs(rhr - m) >= 4:
            up = rhr > m
            b.add("heart", f"Пульс покоя по браслету {rhr} уд/мин — на {num(abs(rhr - m))} {'выше' if up else 'ниже'} "
                           f"вашей нормы {num(m)}", "high" if up else "low", "bad" if up else "good",
                  *(("high_sleep_rhr",) if up else ()))
        elif m is not None:
            b.add("heart", f"Пульс покоя по браслету {rhr} уд/мин — в пределах вашей нормы {num(m)}", "low", "good")
        else:
            out = rhr > 80
            b.add("heart", f"Пульс покоя по браслету {rhr} уд/мин" + (" — выше типичных для взрослых 50–80" if out else
                                                                       " — в типичном для взрослых диапазоне 50–80"),
                  "mid" if out else "low", "bad" if out else "good")
    if row.get("sleep_rhr") is not None and b.sheet.metric != "overview":
        b.add("heart", f"Пульс покоя во сне прошлой ночью {num(row['sleep_rhr'])} уд/мин")
    awake = row.get("awake_hr_avg")
    if awake is not None:
        b.add("heart", f"Днём средний пульс {num(awake)} уд/мин, максимум за сутки {num(row.get('hr_max'))} уд/мин")
    spikes = [a for _, a in _anoms(view, "hr_spike")]
    for a in sorted(spikes, key=lambda a: -a["peak"])[:3]:
        over = f", на {a['over_baseline']} выше обычного для этого часа" if a.get("over_baseline") else ""
        b.add("heart", f"В {a['at']} пульс до {a['peak']} уд/мин в течение {a['dur']} мин без записанной тренировки{over}",
              "mid", "bad", "hr_spikes")
    if len(spikes) > 3:
        b.add("heart", f"Всего эпизодов повышенного пульса в покое: {len(spikes)}", "mid", "bad", "hr_spikes")
    for _, a in _anoms(view, "bradycardia"):
        b.add("heart", f"В {a['at']} пульс опускался до {a['peak']} уд/мин на {a['dur']} мин днём", "mid", "bad", "brady")
    for w in view.extra.get("workouts", pl.DataFrame()).iter_rows(named=True):
        name = SPORTS.get(w["sport_type"], ("", w["sport_type"]))[1]
        b.add("heart", f"Тренировка «{name}» в {w['start_local'].strftime('%H:%M')}, {dur(w['duration_sec'] / 60)}, "
                       f"средний пульс {w['avg_hr']} уд/мин", "low", "good")
    zones = view.extra.get("zones")
    if zones:
        names = {"warm_up": "разминка", "fat_burn": "жиросжигание", "aerobic": "аэробная", "anaerobic": "анаэробная",
                 "extreme": "максимальная"}
        b.add("heart", "Минуты в пульсовых зонах: " + ", ".join(f"{names[k]} {v}" for k, v in zones.items()))
    if row.get("abnormal_hr_count"):
        b.add("heart", f"Браслет отметил эпизодов аномального пульса: {row['abnormal_hr_count']}", "high", "bad", "hr_spikes")
    cov = row.get("hr_coverage_pct")
    if cov is not None and cov < 50:
        b.sheet.caveats.append(f"Пульсом покрыто около {num(cov)} % минут суток.")
    b.sheet.kpis = [("Пульс покоя", f"{num(rhr)}"), ("Во сне", num(row.get("sleep_rhr"))),
                    ("Днём", num(awake)), ("Макс.", num(row.get("hr_max")))]
    b.baseline_note(row, "rhr_device")


def _heart_period(b: _Builder, view: PeriodView) -> None:
    d = view.days
    rhr = _mean(d["rhr_device"])
    slope = _slope_per_week(d, "rhr_device")
    if rhr is not None:
        lo = d.sort("rhr_device").drop_nulls("rhr_device")
        rising = slope is not None and slope >= 1.5
        trend = f", тренд {_signed(slope, ' уд/мин')} в неделю" if slope is not None and abs(slope) >= 1 else ", без выраженного тренда"
        b.add("heart", f"Пульс покоя по браслету в среднем {num(rhr)} уд/мин{trend}", "mid" if rising else "low",
              "bad" if rising else "neutral", *(("rising_rhr",) if rising else ()))
        if lo.height >= 2:
            b.add("heart", f"Самый низкий пульс покоя — {day(lo['date'][0], True)}: {lo['rhr_device'][0]}; "
                           f"самый высокий — {day(lo['date'][-1], True)}: {lo['rhr_device'][-1]} уд/мин")
    srhr = _mean(d["sleep_rhr"])
    if srhr is not None:
        s2 = _slope_per_week(d, "sleep_rhr")
        b.add("heart", f"Пульс покоя во сне в среднем {num(srhr)} уд/мин"
              + (f", тренд {_signed(s2, ' уд/мин')} в неделю" if s2 is not None and abs(s2) >= 1 else ""))
    awake = _mean(d["awake_hr_avg"])
    if awake is not None:
        b.add("heart", f"Средний дневной пульс {num(awake)} уд/мин")
    spikes = _anoms(view, "hr_spike")
    if spikes:
        hours = [int(a["at"][:2]) for _, a in spikes]
        top = statistics.mode(hours)
        b.add("heart", f"Эпизодов повышенного пульса в покое: {len(spikes)} за {len({x for x, _ in spikes})} "
                       f"{plural(len({x for x, _ in spikes}), 'день', 'дня', 'дней')}; чаще всего около {top:02d}:00",
              "mid", "bad", "hr_spikes")
    elevated = _anoms(view, "sleep_hr_elevated")
    if elevated:
        b.add("heart", f"Ночей с повышенным пульсом во сне: {len(elevated)}", "high", "bad", "high_sleep_rhr")
    zones = {name: int(d[col].fill_null(0).sum()) for col, name in (("zone_fat_burn", "жиросжигание"),
             ("zone_aerobic", "аэробная"), ("zone_anaerobic", "анаэробная")) if col in d.columns}
    workouts = view.extra.get("workouts", pl.DataFrame())
    if any(zones.values()) or workouts.height:
        b.add("activity", f"Тренировок за период: {workouts.height}; минуты в зонах: "
              + ", ".join(f"{k} {v}" for k, v in zones.items()))
    pd_ = view.prev_days
    prhr = _mean(pd_["rhr_device"]) if not pd_.is_empty() else None
    if rhr is not None and prhr is not None and pd_["rhr_device"].drop_nulls().len() >= 3:
        delta = rhr - prhr
        b.add("heart", f"По сравнению с предыдущим периодом пульс покоя {_signed(delta, ' уд/мин', 1)}",
              "mid" if abs(delta) >= 2 else "low", "bad" if delta >= 2 else "good" if delta <= -2 else "neutral")
    b.sheet.kpis = [("Пульс покоя", num(rhr)), ("Во сне", num(srhr)), ("Днём", num(awake)), ("Эпизодов", str(len(spikes)))]
    b.baseline_note(_last(d), "rhr_device")


# ---- spo2 ---------------------------------------------------------------------------------

def _spo2(b: _Builder, view: PeriodView) -> None:
    if not view.has_data:
        b.sheet.has_data = False
        b.add("spo2", "За этот период нет ночных замеров SpO2 — вероятно, мониторинг SpO2 во сне выключен",
              "high", "neutral", "spo2_missing")
        b.sheet.kpis = [("SpO2", "нет данных")]
        return
    rows = [view.extra["summary"]] if view.period == "day" else view.extra.get("per_night", [])
    avg = statistics.mean(r["avg"] for r in rows)
    mn = min(r["min"] for r in rows)
    b.add("spo2", f"Средний SpO2 во сне {num(avg, 1)} %, минимум {mn} %", "mid" if mn < 90 else "low",
          "bad" if mn < 90 else "good")
    odis = [r["odi"] for r in rows if r.get("odi") is not None]
    if odis:
        odi = max(odis)
        label = "в пределах обычного" if odi < 5 else "умеренно частые просадки" if odi < 15 else "частые просадки"
        b.add("spo2", f"Индекс просадок (ODI) до {num(odi, 1)} в час — {label}", "high" if odi >= 15 else
              "mid" if odi >= 5 else "low", "bad" if odi >= 5 else "good", *(("spo2_dips",) if odi >= 5 else ()))
    low = sum(r.get("low90_min") or 0 for r in rows)
    if low:
        b.add("spo2", f"Время с SpO2 ниже 90 %: {dur(low)}", "mid", "bad", "spo2_dips")
    if view.period != "day":
        bad = [r for r in rows if (r.get("odi") or 0) >= 5]
        b.add("spo2", f"Ночей с замерами: {len(rows)}, из них с частыми просадками: {len(bad)}")
    b.sheet.kpis = [("SpO2 средний", f"{num(avg, 1)} %"), ("Минимум", f"{mn} %")]


# ---- stress -------------------------------------------------------------------------------

def _zone_share(zones: pl.DataFrame | None) -> dict[str, float]:
    if zones is None or zones.is_empty():
        return {}
    total = sum(zones[z[0]].sum() for z in STRESS_ZONES) or 1
    return {z[0]: zones[z[0]].sum() / total * 100 for z in STRESS_ZONES}


def _stress_day(b: _Builder, view: PeriodView) -> None:
    row = _last(view.days) or {}
    avg, mx = row.get("stress_avg"), row.get("stress_max")
    m, _ = b.norm(row, "stress_avg")
    if avg is not None:
        if m is not None and avg - m >= 8:
            b.add("stress", f"Средний стресс днём {num(avg)} — выше вашей нормы {num(m)}", "high", "bad", "stress_high")
        elif m is not None:
            b.add("stress", f"Средний стресс днём {num(avg)} (ваша норма {num(m)})", "low", "good")
        else:
            label = "низкий" if avg < 40 else "умеренный" if avg < 60 else "повышенный"
            b.add("stress", f"Средний стресс днём {num(avg)} — {label} по шкале Xiaomi", "mid" if avg >= 60 else "low",
                  "bad" if avg >= 60 else "good", *(("stress_high",) if avg >= 60 else ()))
        b.add("stress", f"Максимум стресса за день {num(mx)}")
    share = _zone_share(view.extra.get("zones"))
    if share:
        b.add("stress", "Время по зонам: " + ", ".join(f"{STRESS_ZONE_NAMES[k]} {num(v)} %" for k, v in share.items()
                                                       if v >= 0.5))
    eps = view.extra.get("episodes", [])
    for e in eps[:3]:
        b.add("stress", f"В {e['at']} высокий стресс (до {e['peak']}) держался {dur(e['dur'])}", "mid", "bad", "stress_high")
    night = b.proc.nights.filter(pl.col("is_main") & (pl.col("date") == view.end + timedelta(days=1)))
    if not night.is_empty() and eps:
        b.add("stress", f"Следующей ночью сон длился {dur(night['tst'][0])}", "low", "neutral", "stress_sleep")
    b.sheet.kpis = [("Стресс средний", num(avg)), ("Максимум", num(mx)), ("Эпизодов ≥80", str(len(eps)))]
    b.baseline_note(row, "stress_avg")


def _stress_period(b: _Builder, view: PeriodView) -> None:
    d = view.days.filter(pl.col("stress_avg").is_not_null())
    avg = _mean(d["stress_avg"])
    slope = _slope_per_week(d, "stress_avg")
    rising = slope is not None and slope >= 3
    b.add("stress", f"Средний дневной стресс {num(avg)}" + (f", тренд {_signed(slope)} в неделю" if slope is not None
                                                             and abs(slope) >= 2 else ", без выраженного тренда"),
          "mid" if rising else "low", "bad" if rising else "neutral", *(("stress_rising",) if rising else ()))
    if d.height >= 2:
        top = d.sort("stress_avg").row(-1, named=True)
        calm = d.sort("stress_avg").row(0, named=True)
        b.add("stress", f"Самый напряжённый день — {day(top['date'], True)} ({num(top['stress_avg'])}), "
                        f"самый спокойный — {day(calm['date'], True)} ({num(calm['stress_avg'])})")
    high_days = d.filter(pl.col("stress_high_min") >= 30)
    if high_days.height:
        b.add("stress", f"Дней с высоким стрессом (≥80) дольше 30 мин: {high_days.height}", "mid", "bad", "stress_high")
    share = _zone_share(view.extra.get("zones"))
    if share:
        b.add("stress", "Время по зонам за период: " + ", ".join(f"{STRESS_ZONE_NAMES[k]} {num(v)} %"
                                                                for k, v in share.items() if v >= 0.5))
    # Stress on day d vs the following night's sleep.
    nights = b.proc.nights.filter(pl.col("is_main")).select(
        (pl.col("date") - pl.duration(days=1)).alias("date"), pl.col("tst").alias("next_tst"))
    pairs = d.select("date", "stress_avg").join(nights, on="date").drop_nulls()
    if pairs.height >= 6:
        med = pairs["stress_avg"].median()
        hi, lo = pairs.filter(pl.col("stress_avg") > med), pairs.filter(pl.col("stress_avg") <= med)
        if hi.height >= 3 and lo.height >= 3:
            diff = hi["next_tst"].mean() - lo["next_tst"].mean()
            if abs(diff) >= 15:
                b.add("stress", f"После более напряжённых дней вы спали в среднем на {dur(abs(diff))} "
                                f"{'меньше' if diff < 0 else 'больше'}", "mid", "bad" if diff < 0 else "neutral",
                      *(("stress_sleep",) if diff < 0 else ()))
    pd_ = view.prev_days.filter(pl.col("stress_avg").is_not_null())
    if avg is not None and pd_.height >= 3:
        delta = avg - pd_["stress_avg"].mean()
        b.add("stress", f"По сравнению с предыдущим периодом стресс {_signed(delta)}", "mid" if abs(delta) >= 5 else "low",
              "bad" if delta >= 5 else "good" if delta <= -5 else "neutral")
    b.sheet.kpis = [("Стресс средний", num(avg)), ("Дней с пиками", str(high_days.height))]
    b.baseline_note(_last(view.days), "stress_avg")


# ---- activity (for overview / chat) -----------------------------------------------------

def _activity(b: _Builder, target: date) -> None:
    from analytics.activity import steps_block, workouts_block
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    st = steps_block(b.proc.daily, target)
    if st.get("steps") is not None:
        pct = st.get("steps_vs_28d_pct")
        low = st["steps"] < 5000
        b.add("activity", f"Шагов: {st['steps']}" + (f" ({_signed(pct, ' %')} к вашей обычной активности)" if pct is not None else ""),
              "mid" if low else "low", "bad" if low else "good", *(("low_steps",) if low else ()))
    w = workouts_block(b.proc.f.workouts, target, datetime.combine(target, time(23, 59), ZoneInfo(b.cfg.tz)), b.cfg)
    if w.get("acwr") is not None:
        risky = w["acwr_risk"]
        b.add("activity", f"Тренировок за 7 дней: {w['workouts_7d']}, нагрузка {w['load_7d']}; соотношение недельной "
                          f"нагрузки к средней за месяц {num(w['acwr'], 2)}" + (" — резкий рост" if risky else ""),
              "high" if risky else "low", "bad" if risky else "neutral", *(("high_load",) if risky else ()))
    last = w.get("last")
    if last:
        name = SPORTS.get(last["type"], ("", last["type"]))[1]
        b.add("activity", f"Последняя тренировка — «{name}» {last['days_ago']} дн назад"
              + (f", до полного восстановления ещё {last['recovery_left_h']} ч" if last.get("recovery_left_h") else ""))


# ---- public builders ----------------------------------------------------------------------

def period_facts(proc: Processor, view: PeriodView) -> FactSheet:
    sheet = FactSheet(view.metric, view.period, _title(view.metric, view), has_data=view.has_data)
    b = _Builder(sheet, proc)
    if view.metric == "spo2":
        _spo2(b, view)
    elif not view.has_data:
        sheet.has_data = False
        b.add(view.metric, "За этот период нет данных", "high")
    else:
        {("sleep", True): _sleep_day, ("sleep", False): _sleep_period,
         ("heart", True): _heart_day, ("heart", False): _heart_period,
         ("stress", True): _stress_day, ("stress", False): _stress_period}[(view.metric, view.period == "day")](b, view)
    sheet.caveats.insert(0, CAVEATS[view.metric])
    if not any(f.tags for f in sheet.facts):
        sheet.facts[0].tags = ("steady",)
    return b.finish()


def overview_facts(proc: Processor, target: date, today: date | None = None,
                   sleep_date: date | None = None) -> FactSheet:
    """Top facts of every metric for one day: daily brief and chat context.

    `sleep_date` selects the night (wake-up date); by default the same day as `target`.
    """
    sheet = FactSheet("overview", "day", f"Сводка · {day(target, True)}" + ("" if today is None or target < today
                                                                             else " (день ещё идёт)"))
    b = _Builder(sheet, proc)
    for metric, builder in (("sleep", _sleep_day), ("heart", _heart_day), ("stress", _stress_day)):
        view = period_view(proc, metric, "day", sleep_date or target if metric == "sleep" else target, today)
        if view is None or not view.has_data:
            continue
        before = len(sheet.facts)
        builder(b, view)
        # Keep only the 3 most important facts per metric.
        own = sorted(sheet.facts[before:], key=lambda f: {"high": 0, "mid": 1, "low": 2}[f.level])[:3]
        sheet.facts[before:] = own
    _activity(b, target)
    spo2 = period_view(proc, "spo2", "day", target, today)
    if spo2 is None or not spo2.has_data:
        sheet.caveats.append("Ночных замеров SpO2 нет.")
    sheet.kpis = []
    sheet.baseline_note = None
    return b.finish()


def workout_facts(proc: Processor, ts: int) -> FactSheet | None:
    v = workout_view(proc, ts)
    if v is None:
        return None
    r = v["row"]
    emoji, name = SPORTS.get(r["sport_type"], ("🏅", r["sport_type"]))
    sheet = FactSheet("workout", "day", f"Тренировка · {name} · {day(r['date'], True)} {r['start_local'].strftime('%H:%M')}")
    b = _Builder(sheet, proc)
    minutes = r["duration_sec"] / 60
    b.add("workout", f"{name}: {dur(minutes)}, средний пульс {r['avg_hr']}, максимум {r['max_hr']} уд/мин, "
                     f"{num(r['calories'])} ккал", "mid")
    zones = {k: s for k, s in v["zones_s"].items() if s}
    total = sum(zones.values()) or 1
    share = {k: s / total * 100 for k, s in zones.items()}
    if share:
        top = max(share, key=share.get)
        b.add("workout", "Время по пульсовым зонам: " + ", ".join(f"{HR_ZONE_NAMES[k]} {num(p)} % ({dur(zones[k] / 60)})"
                                                                for k, p in share.items()), "mid")
        hard = share.get("anaerobic", 0) + share.get("extreme", 0)
        if hard >= 30:
            b.add("workout", f"Интенсивная тренировка: {num(hard)} % времени в анаэробной и максимальной зонах",
                  "high", "neutral", "workout_intense")
        elif share.get(top) and top in ("warm_up", "fat_burning") and share[top] >= 60:
            b.add("workout", f"Лёгкая нагрузка: {num(share[top])} % времени в зоне «{HR_ZONE_NAMES[top]}»",
                  "mid", "neutral", "workout_easy")
    te = r.get("train_effect")
    if te is not None:
        label = ("почти без эффекта" if te < 1 else "лёгкий эффект" if te < 2 else "поддерживающий" if te < 3
                 else "развивающий" if te < 4 else "сильно развивающий" if te < 5 else "чрезмерный")
        b.add("workout", f"Тренировочный эффект {num(te, 1)} из 5 — {label}; нагрузка {r.get('train_load')}",
              "high" if te >= 5 else "mid", "bad" if te >= 5 else "good", *(("workout_intense",) if te >= 4 else ()))
    if r.get("recover_h"):
        b.add("workout", f"Браслет рекомендовал восстановление {r['recover_h']} ч", "mid", "neutral")
    med = v["same_type_median"]
    if med:
        diffs = []
        if med["duration_sec"]:
            diffs.append(f"длительность {_signed((r['duration_sec'] - med['duration_sec']) / 60, ' мин')}")
        if med["avg_hr"]:
            diffs.append(f"средний пульс {_signed(r['avg_hr'] - med['avg_hr'], ' уд/мин')}")
        if med["train_load"] and r.get("train_load") is not None:
            diffs.append(f"нагрузка {_signed(r['train_load'] - med['train_load'])}")
        b.add("workout", f"По сравнению с вашими обычными тренировками этого типа ({v['same_type_n']} шт.): "
              + ", ".join(diffs))
    if v["days_since_prev"] is not None:
        b.add("workout", f"Предыдущая тренировка была за {v['days_since_prev']} дн до этой")
    if v["acwr"] is not None and v["acwr"] > proc.cfg.acwr_risk:
        b.add("workout", f"Недельная нагрузка в {num(v['acwr'], 1)} раза выше средней за месяц", "high", "bad", "high_load")
    if v["track"].is_empty():
        sheet.caveats.append("Поминутного пульса за время тренировки нет — анализ по зонам и итогам от браслета.")
    sheet.caveats.insert(0, CAVEATS["workout"])
    sheet.kpis = [("Длительность", dur(minutes)), ("Пульс ср/макс", f"{r['avg_hr']}/{r['max_hr']}"),
                  ("Нагрузка", num(r.get("train_load"))), ("Эффект", num(te, 1))]
    if not any(f.tags for f in sheet.facts):
        sheet.facts[0].tags = ("steady",)
    return b.finish()


def activity_facts(proc: Processor, target: date) -> FactSheet:
    sheet = FactSheet("activity", "day", f"Активность · {day(target, True)}")
    b = _Builder(sheet, proc)
    _activity(b, target)
    if not sheet.facts:
        sheet.has_data = False
        b.add("activity", "Нет данных об активности за этот день", "high")
    return b.finish()


ANOMALY_METRIC = {"hr_spike": "heart", "bradycardia": "heart", "sleep_hr_elevated": "heart", "stress_high": "stress"}
METRIC_OF_FIELD = {"tst": "sleep", "deep_pct": "sleep", "rem_pct": "sleep", "sleep_score": "sleep",
                   "quality_index": "sleep", "rhr_device": "heart", "sleep_rhr": "heart", "awake_hr_avg": "heart",
                   "stress_avg": "stress", "steps": "activity"}
FIELD_NAMES = {"tst": "длительность сна, мин", "deep_pct": "доля глубокого сна, %", "rem_pct": "доля REM-сна, %",
               "sleep_score": "оценка сна", "quality_index": "индекс качества сна", "rhr_device": "пульс покоя",
               "sleep_rhr": "пульс покоя во сне", "awake_hr_avg": "средний дневной пульс", "stress_avg": "средний стресс",
               "steps": "шаги"}


def anomaly_metric(a: dict[str, Any]) -> str:
    return ANOMALY_METRIC.get(a["type"]) or METRIC_OF_FIELD.get(a.get("metric", ""), "other")


def describe_anomaly(a: dict[str, Any]) -> str:
    t = a["type"]
    if t == "hr_spike":
        return f"в {a['at']} пульс до {a['peak']} уд/мин {a['dur']} мин в покое (без тренировки)"
    if t == "bradycardia":
        return f"в {a['at']} пульс опускался до {a['peak']} уд/мин на {a['dur']} мин"
    if t == "stress_high":
        return f"в {a['at']} высокий стресс до {a['peak']} в течение {a['dur']} мин"
    if t == "sleep_hr_elevated":
        return f"пульс во сне {a['value']} уд/мин — на {a['delta']} выше нормы"
    name = FIELD_NAMES.get(a.get("metric", ""), a.get("metric", ""))
    if t == "day_deviation":
        return f"{name}: {a['value']} — сильно {'выше' if a['z'] > 0 else 'ниже'} вашей нормы"
    if t == "out_of_reference":
        lo, hi = a["ref"]
        rng = f"{lo}–{hi}" if lo is not None and hi is not None else (f"от {lo}" if lo is not None else f"до {hi}")
        return f"{name}: {a['value']} — вне типичного диапазона {rng}"
    return str(a)
