"""Rejects analyses that cite unknown facts or contain numbers absent from the FactSheet and profile."""

from __future__ import annotations

import re

from .facts import FactSheet
from .schemas import LIMITS, AnalysisOut

NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
FREE_UP_TO = 12  # small counts ("3 совета", "2 дня") are always fine


def numbers(text: str) -> list[float]:
    return [float(m.replace(",", ".")) for m in NUM_RE.findall(text)]


def allowed_numbers(sheet: FactSheet, extra: str = "") -> set[float]:
    return set(numbers(sheet.render() + "\n" + extra))


def _known(x: float, allowed: set[float]) -> bool:
    if x <= FREE_UP_TO:
        return True
    tol = 1.0 if x.is_integer() else 0.15
    return any(abs(x - a) <= tol for a in allowed)


def check(out: AnalysisOut, sheet: FactSheet, mode: str, extra: str = "") -> list[str]:
    """`extra` is additional trusted text the model may quote numbers from (the user profile)."""
    errors: list[str] = []
    allowed, ids = allowed_numbers(sheet, extra), sheet.fact_ids()
    texts = [("summary", out.summary), ("caveat", out.caveat)]
    texts += [(f"insights[{i}]", it.text) for i, it in enumerate(out.insights)]
    texts += [(f"advice[{i}]", it.text) for i, it in enumerate(out.advice)]
    for where, text in texts:
        bad = sorted({n for n in numbers(text) if not _known(n, allowed)})
        if bad:
            errors.append(f"{where}: числа {', '.join(f'{b:g}' for b in bad)} отсутствуют в фактах")
    for kind, items in (("insights", out.insights), ("advice", out.advice)):
        for i, it in enumerate(items):
            unknown = [f for f in it.fact_ids if f not in ids]
            if not it.fact_ids or unknown:
                errors.append(f"{kind}[{i}]: нужны ссылки на существующие факты" + (f", неизвестные: {unknown}" if unknown else ""))
    (ilo, ihi), (alo, ahi) = LIMITS[mode]
    if not ilo <= len(out.insights) <= ihi:
        errors.append(f"insights: нужно от {ilo} до {ihi} пунктов, а не {len(out.insights)}")
    if not alo <= len(out.advice) <= ahi:
        errors.append(f"advice: нужно от {alo} до {ahi} пунктов, а не {len(out.advice)}")
    if not out.summary.strip():
        errors.append("summary пустой")
    return errors
