"""User profile (sex, age, body, goals) kept in the settings table and rendered into every LLM context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core.fmt import num, plural

# key -> (button label, choices or None for free input); choices map stored value -> shown text.
SEX = {"f": "женский", "m": "мужской"}
ACTIVITY = {"low": "низкая (сидячий образ жизни)", "medium": "средняя (1–3 тренировки в неделю)",
            "high": "высокая (4+ тренировки в неделю)", "athlete": "спортсмен"}
GOAL = {"sleep": "наладить сон", "recovery": "восстановление и меньше стресса", "weight": "снизить вес",
        "endurance": "выносливость", "health": "общее здоровье"}
CHOICES = {"sex": SEX, "activity": ACTIVITY, "goal": GOAL}
FIELDS = ("sex", "birth_year", "height_cm", "weight_kg", "activity", "goal", "notes")
NOTES_MAX = 300


def key(field: str) -> str:
    return f"profile_{field}"


@dataclass
class Profile:
    sex: str = ""
    birth_year: int | None = None
    height_cm: int | None = None
    weight_kg: float | None = None
    activity: str = ""
    goal: str = ""
    notes: str = ""

    @property
    def is_empty(self) -> bool:
        return not any((self.sex, self.birth_year, self.height_cm, self.weight_kg, self.activity, self.goal,
                        self.notes))

    def age(self, today: date) -> int | None:
        return today.year - self.birth_year if self.birth_year else None

    @property
    def bmi(self) -> float | None:
        if not self.height_cm or not self.weight_kg:
            return None
        return round(self.weight_kg / (self.height_cm / 100) ** 2, 1)

    def render(self, today: date) -> str:
        """Profile block for prompts; empty string when nothing is filled in."""
        if self.is_empty:
            return ""
        lines = ["ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ (указан им самим в настройках):"]
        age = self.age(today)
        if self.sex:
            lines.append(f"- Пол: {SEX[self.sex]}")
        if age is not None:
            lines.append(f"- Возраст: {age} {plural(age, 'год', 'года', 'лет')}")
        if self.height_cm:
            lines.append(f"- Рост: {self.height_cm} см")
        if self.weight_kg:
            lines.append(f"- Вес: {self.weight_kg:g} кг".replace(".", ","))
        if (bmi := self.bmi) is not None:
            lines.append(f"- ИМТ: {num(bmi, 1)} — {bmi_category(bmi)} (по ВОЗ норма 18,5–24,9)")
        if self.activity:
            lines.append(f"- Уровень активности: {ACTIVITY[self.activity]}")
        if self.goal:
            lines.append(f"- Цель: {GOAL[self.goal]}")
        if self.notes:
            lines.append(f"- Важно знать: {self.notes}")
        if age is not None:
            lines.append(f"- Расчётный максимальный пульс: {round(208 - 0.7 * age)} уд/мин (формула Танака, "
                         "индивидуально может отличаться на 10–15 уд/мин)")
            lo, hi = sleep_norm_h(age)
            lines.append(f"- Рекомендуемая длительность сна для возраста: {lo}–{hi} ч (National Sleep Foundation)")
        return "\n".join(lines)


def bmi_category(bmi: float) -> str:
    if bmi < 18.5:
        return "ниже нормы"
    if bmi < 25:
        return "норма"
    if bmi < 30:
        return "избыточный вес"
    return "ожирение"


def sleep_norm_h(age: int) -> tuple[int, int]:
    if age < 18:
        return 8, 10
    if age < 65:
        return 7, 9
    return 7, 8


def parse(field: str, text: str, today: date) -> str:
    """Validates free input and returns the value to store; raises ValueError with a user-facing reason."""
    t = text.strip().replace(",", ".")
    if field == "birth_year":
        if not t.isdigit() or not today.year - 100 <= int(t) <= today.year - 10:
            raise ValueError(f"нужен год от {today.year - 100} до {today.year - 10}, например 1995")
        return t
    if field == "height_cm":
        if not t.isdigit() or not 100 <= int(t) <= 230:
            raise ValueError("нужен рост в сантиметрах от 100 до 230, например 168")
        return t
    if field == "weight_kg":
        try:
            w = float(t)
        except ValueError:
            raise ValueError("нужен вес в килограммах, например 62 или 62,5") from None
        if not 30 <= w <= 250:
            raise ValueError("нужен вес от 30 до 250 кг")
        return f"{w:g}"
    if field == "notes":
        if len(text.strip()) > NOTES_MAX:
            raise ValueError(f"не больше {NOTES_MAX} символов")
        return " ".join(text.split())
    raise ValueError(f"поле {field} выбирается кнопками")


def from_values(values: dict[str, str]) -> Profile:
    def to_int(v: str) -> int | None:
        return int(v) if v.isdigit() else None

    def choice(field: str) -> str:
        v = values.get(field, "")
        return v if v in CHOICES[field] else ""

    w = values.get("weight_kg", "")
    return Profile(sex=choice("sex"), birth_year=to_int(values.get("birth_year", "")),
                   height_cm=to_int(values.get("height_cm", "")), weight_kg=float(w) if w else None,
                   activity=choice("activity"), goal=choice("goal"), notes=values.get("notes", ""))


def load() -> Profile:
    from storage.db import get_setting

    return from_values({f: get_setting(key(f)) for f in FIELDS})


def save(field: str, value: str) -> None:
    from storage.db import set_setting

    set_setting(key(field), value)


def render_current() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from core.data import analytics_config

    return load().render(datetime.now(ZoneInfo(analytics_config().tz)).date())
