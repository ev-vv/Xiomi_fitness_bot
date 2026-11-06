"""Shared look of all dashboards: palette (validated with the dataviz checker), header with KPI tiles, axes.

Static PNGs for Telegram use the light surface only. Colors with < 3:1 contrast (aqua, yellow) always come
with direct labels or a legend, never alone.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from core.fmt import MONTHS  # noqa: E402

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT2 = "#52514e"
MUTED = "#8a8984"
GRID = "#e6e5e0"
TINT = "#f0efec"

BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
CRITICAL = "#d03b3b"

# Sleep phases: deep / light / REM / awake (passes CVD + normal-vision checks as an adjacent set).
PHASES = (("deep", "Глубокий", "#256abf"), ("light", "Лёгкий", AQUA), ("rem", "REM", VIOLET), ("awake", "Бодрствование", ORANGE))
# Xiaomi stress zones, low -> high.
STRESS = (("relaxed", "Расслабление", BLUE), ("mild", "Лёгкий", AQUA), ("moderate", "Средний", YELLOW),
          ("high", "Высокий", RED))
# HR zones: ordinal single-hue ramp (validated monotone).
HR_ZONES = (("warm_up", "Разминка", "#86b6ef"), ("fat_burning", "Жиросжигание", "#5598e7"),
            ("aerobic", "Аэробная", "#2a78d6"), ("anaerobic", "Анаэробная", "#1c5cab"),
            ("extreme", "Максимальная", "#104281"))

WIDTH_IN, DPI = 10.0, 128  # 1280 px wide

plt.rcParams.update({
    "font.family": ["Noto Sans", "DejaVu Sans"],
    "font.size": 12,
    "axes.edgecolor": GRID,
    "axes.labelcolor": TEXT2,
    "axes.titlecolor": TEXT,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.titlepad": 10,
    "axes.facecolor": SURFACE,
    "axes.grid": True,
    "axes.grid.axis": "y",
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.spines.left": False,
    "xtick.color": TEXT2,
    "ytick.color": TEXT2,
    "xtick.major.size": 0,
    "ytick.major.size": 0,
    "legend.frameon": False,
    "legend.fontsize": 11,
    "lines.linewidth": 2,
    "figure.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
})


def dashboard(title: str, subtitle: str, kpis: Sequence[tuple[str, str]], rows: Sequence[float],
              height: float | None = None) -> tuple[Figure, list[plt.Axes]]:
    """Figure with a title block, a row of KPI tiles and `len(rows)` plot rows (height ratios)."""
    plot_h = sum(rows) * 2.1
    fig = plt.figure(figsize=(WIDTH_IN, height or 2.3 + plot_h))
    top = 1 - 1.9 / fig.get_figheight()
    fig.text(0.05, 1 - 0.38 / fig.get_figheight(), title, fontsize=20, fontweight="bold", color=TEXT, va="top")
    fig.text(0.05, 1 - 0.80 / fig.get_figheight(), subtitle, fontsize=13, color=TEXT2, va="top")
    _kpi_tiles(fig, kpis, y=1 - 1.15 / fig.get_figheight(), h=0.62 / fig.get_figheight())
    gs = fig.add_gridspec(len(rows), 1, height_ratios=list(rows), left=0.08, right=0.97,
                          top=top - 0.25 / fig.get_figheight(), bottom=0.55 / fig.get_figheight(), hspace=0.55)
    return fig, [fig.add_subplot(gs[i]) for i in range(len(rows))]


def _kpi_tiles(fig: Figure, kpis: Sequence[tuple[str, str]], y: float, h: float) -> None:
    if not kpis:
        return
    n = len(kpis)
    left, right, gap = 0.05, 0.97, 0.015
    w = (right - left - gap * (n - 1)) / n
    for i, (label, value) in enumerate(kpis):
        x = left + i * (w + gap)
        ax = fig.add_axes((x, y - h, w, h))
        ax.set_axis_off()
        ax.add_patch(matplotlib.patches.FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0,rounding_size=0.08",
                                                       transform=ax.transAxes, facecolor=TINT, edgecolor="none"))
        ax.text(0.07, 0.66, value, fontsize=17, fontweight="bold", color=TEXT, va="center", transform=ax.transAxes)
        ax.text(0.07, 0.24, label, fontsize=10.5, color=TEXT2, va="center", transform=ax.transAxes)


def empty(ax: plt.Axes, message: str) -> None:
    ax.set_axis_off()
    ax.text(0.5, 0.5, message, ha="center", va="center", color=MUTED, fontsize=13, transform=ax.transAxes)


def time_axis(ax: plt.Axes) -> None:
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))


def date_labels(dates: Sequence[date], n_max: int = 10) -> tuple[list[int], list[str]]:
    """Tick positions/labels for categorical day bars: '15 окт' and every k-th day for long periods."""
    step = max(1, -(-len(dates) // n_max))
    pos = list(range(0, len(dates), step))
    return pos, [f"{dates[i].day} {MONTHS[dates[i].month - 1]}" for i in pos]


def clock_label(hours_from_noon: float) -> str:
    h = int(round(hours_from_noon * 60)) // 60 % 24
    return f"{(h + 12) % 24:02d}:00"


def legend(ax: plt.Axes, ncol: int | None = None) -> None:
    """Legend on the title line, right-aligned (titles are left-aligned and kept short)."""
    handles, labels = ax.get_legend_handles_labels()
    if len(handles) >= 2:
        ax.legend(handles, labels, loc="lower right", bbox_to_anchor=(1, 1.0), ncol=ncol or len(handles),
                  handlelength=1.2, columnspacing=1.0, borderaxespad=0.2, fontsize=10.5)


def hbars(ax: plt.Axes, names: Sequence[str], values: Sequence[float], colors: Sequence[str] | str,
          value_labels: Sequence[str], track: float | None = None) -> None:
    """Horizontal bars with the category name above each bar (no clipped tick labels on the left)."""
    y = [i * 1.0 for i in range(len(names))][::-1]
    if track is not None:
        ax.barh(y, [track] * len(y), color=TINT, height=0.3)
    ax.barh(y, values, color=colors, height=0.3)
    top = max([track or 0, *values]) or 1
    for yi, name, v, lbl in zip(y, names, values, value_labels):
        ax.text(0, yi + 0.2, name, va="bottom", fontsize=10.5, color=TEXT2)
        ax.text(v + top * 0.01, yi, lbl, va="center", fontsize=10.5, color=TEXT)
    ax.set_yticks([])
    ax.set_xticks([])
    ax.grid(False)
    ax.set_xlim(0, top * 1.18)
    ax.set_ylim(-0.3, len(names) - 0.3)


def int_y(ax: plt.Axes) -> None:
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True, nbins=6))


def to_png(fig: Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI)
    plt.close(fig)
    return buf.getvalue()
