from datetime import date

import pytest

from core import profile as prof

TODAY = date(2026, 10, 25)


def test_parse_validates_input():
    assert prof.parse("birth_year", " 1995 ", TODAY) == "1995"
    assert prof.parse("weight_kg", "62,5", TODAY) == "62.5"
    assert prof.parse("notes", "  работаю   посменно ", TODAY) == "работаю посменно"
    for field, bad in (("birth_year", "95"), ("height_cm", "2м"), ("weight_kg", "500"), ("sex", "f")):
        with pytest.raises(ValueError):
            prof.parse(field, bad, TODAY)


def test_render_adds_derived_norms():
    assert prof.Profile().render(TODAY) == ""
    p = prof.from_values({"sex": "f", "birth_year": "1995", "height_cm": "165", "weight_kg": "58.5",
                          "goal": "sleep", "activity": "bogus"})
    text = p.render(TODAY)
    assert "Пол: женский" in text and "Возраст: 31 год" in text and "Вес: 58,5 кг" in text
    assert "ИМТ: 21,5 — норма" in text and "186 уд/мин" in text and "7–9 ч" in text
    assert "активности" not in text  # unknown choice values are ignored


def test_profile_roundtrip(app_env):
    prof.save("birth_year", "1990")
    prof.save("sex", "m")
    p = prof.load()
    assert (p.birth_year, p.sex, p.is_empty) == (1990, "m", False)
