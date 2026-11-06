from __future__ import annotations

from typing import Any

from core.fmt import dur

from . import theme as t

COMPARE = (("duration_sec", "Длительность, мин", 1 / 60), ("avg_hr", "Средний пульс", 1), ("train_load", "Нагрузка", 1))


def chart(v: dict[str, Any], title: str, subtitle: str, kpis: list[tuple[str, str]]) -> bytes:
    track = v["track"]
    med = v["same_type_median"]
    rows = [1.3] + ([1.4] if not track.is_empty() else []) + ([0.9] if med else [])
    fig, axes = t.dashboard(title, subtitle, kpis, rows=rows)
    ax_z, rest = axes[0], axes[1:]

    zones = [(label, v["zones_s"].get(key, 0), color) for key, label, color in t.HR_ZONES]
    total = sum(s for _, s, _ in zones) or 1
    t.hbars(ax_z, [label for label, *_ in zones], [s / 60 for _, s, _ in zones], [c for *_, c in zones],
            [f"{dur(s / 60)} · {s / total * 100:.0f} %" for _, s, _ in zones])
    ax_z.set_title("Время в пульсовых зонах", loc="left")

    if not track.is_empty():
        ax = rest.pop(0)
        ax.plot(track["ts_local"].to_list(), track["bpm"].to_list(), color=t.BLUE, linewidth=1.6)
        ax.set_title("Пульс во время тренировки, уд/мин", loc="left")
        t.time_axis(ax)
    if med:
        ax = rest.pop(0)
        ax.set_axis_off()
        ax.set_title(f"Сравнение с вашими обычными тренировками этого типа ({v['same_type_n']} шт.)", loc="left")
        r = v["row"]
        for i, (col, label, k) in enumerate(COMPARE):
            sub = ax.inset_axes((i / 3 + 0.02, 0.0, 1 / 3 - 0.06, 0.85))
            this, usual = (r.get(col) or 0) * k, (med.get(col) or 0) * k
            sub.bar([0, 1], [this, usual], color=[t.BLUE, t.MUTED], width=0.6)
            for xi, val in enumerate((this, usual)):
                sub.text(xi, val, f"{val:.0f}", ha="center", va="bottom", fontsize=10.5)
            sub.set_xticks([0, 1], ["эта", "обычно"])
            sub.set_yticks([])
            sub.set_title(label, fontsize=11, loc="center")
            sub.set_ylim(0, max(this, usual) * 1.25 or 1)
    return t.to_png(fig)
