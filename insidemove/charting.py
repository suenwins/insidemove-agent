"""Stage 4 chart: candlesticks + volume + SMA overlays + marked levels.

Rendered locally with matplotlib rather than QuickChart. QuickChart has no
native candlestick type and would need the whole 90-bar series stuffed into a
URL; drawing here is faster, free, has no request-size ceiling, and keeps the
chart consistent with the numbers in the write-up because both come from the
same frame.
"""
from __future__ import annotations

import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from .config import CFG  # noqa: E402
from .indicators import TechSnapshot, sma  # noqa: E402

log = logging.getLogger(__name__)

BG = "#0e1117"
FG = "#e6e6e6"
GRID = "#242a35"
UP = "#26a69a"
DOWN = "#ef5350"
SMA_FAST_C = "#f5c542"
SMA_SLOW_C = "#6aa9ff"
SUP_C = "#26a69a"
RES_C = "#ef5350"


def render(
    ticker: str,
    df: pd.DataFrame,
    snap: TechSnapshot,
    out_dir: Path | None = None,
    days: int | None = None,
) -> Path:
    days = days or CFG.chart_days
    out_dir = out_dir or CFG.cache_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # SMAs are computed on the full history, then sliced — otherwise the first
    # 50 bars of the visible window would have no slow average.
    fast = sma(df["Close"], CFG.sma_fast)
    slow = sma(df["Close"], CFG.sma_slow)
    view = df.tail(days)
    fast = fast.reindex(view.index)
    slow = slow.reindex(view.index)

    fig, (ax, vax) = plt.subplots(
        2, 1, figsize=(12, 7.5), sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1], "hspace": 0.06},
    )
    fig.patch.set_facecolor(BG)

    x = mdates.date2num(view.index.to_pydatetime())
    width = 0.62

    for xi, (_, row) in zip(x, view.iterrows()):
        o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
        colour = UP if c >= o else DOWN
        ax.vlines(xi, l, h, color=colour, linewidth=0.9, zorder=2)
        height = abs(c - o) or (h - l) * 0.002 or 0.01
        ax.add_patch(
            Rectangle(
                (xi - width / 2, min(o, c)), width, height,
                facecolor=colour, edgecolor=colour, linewidth=0.6, zorder=3,
            )
        )

    ax.plot(x, fast.to_numpy(), color=SMA_FAST_C, linewidth=1.3,
            label=f"SMA{CFG.sma_fast}", zorder=4)
    ax.plot(x, slow.to_numpy(), color=SMA_SLOW_C, linewidth=1.3,
            label=f"SMA{CFG.sma_slow}", zorder=4)

    # --- horizontal levels -------------------------------------------------
    def draw_level(level: float, colour: str, label: str) -> None:
        ax.axhline(level, color=colour, linestyle="--", linewidth=1.0, alpha=0.75, zorder=1)
        ax.annotate(
            f"{label} {level:,.2f}",
            xy=(x[-1], level), xytext=(4, 0), textcoords="offset points",
            va="center", ha="left", fontsize=8, color=colour,
            bbox=dict(boxstyle="round,pad=0.22", facecolor=BG, edgecolor=colour, alpha=0.85),
        )

    for lvl in snap.resistance[:3]:
        if lvl.get("kind") == "swing":
            draw_level(lvl["level"], RES_C, "R")
    for lvl in snap.support[:3]:
        if lvl.get("kind") == "swing":
            draw_level(lvl["level"], SUP_C, "S")
    if snap.implied_support:
        draw_level(snap.implied_support["level"], SUP_C, "S(ATR)")

    ax.axhline(snap.close, color=FG, linestyle="-", linewidth=0.8, alpha=0.4, zorder=1)

    # --- volume ------------------------------------------------------------
    vol_colours = [UP if c >= o else DOWN for o, c in zip(view["Open"], view["Close"])]
    vax.bar(x, view["Volume"].to_numpy(), width=width, color=vol_colours, alpha=0.8)
    avg_vol = view["Volume"].rolling(20, min_periods=1).mean()
    vax.plot(x, avg_vol.to_numpy(), color=SMA_SLOW_C, linewidth=1.0, alpha=0.8,
             label="20d avg volume")

    # --- cosmetics ---------------------------------------------------------
    arrow = "▲" if snap.pct_change >= 0 else "▼"
    ax.set_title(
        f"{ticker}   ${snap.close:,.2f}   {arrow} {snap.pct_change:+.2f}%   "
        f"· {days}-day view · as of {snap.as_of}",
        color=FG, fontsize=13, pad=12, loc="left",
    )

    for a in (ax, vax):
        a.set_facecolor(BG)
        a.grid(True, color=GRID, linewidth=0.6, alpha=0.7)
        a.tick_params(colors=FG, labelsize=8)
        for spine in a.spines.values():
            spine.set_color(GRID)

    ax.legend(loc="upper left", fontsize=8, facecolor=BG, edgecolor=GRID, labelcolor=FG)
    vax.legend(loc="upper left", fontsize=7, facecolor=BG, edgecolor=GRID, labelcolor=FG)
    vax.set_ylabel("Volume", color=FG, fontsize=8)
    ax.set_ylabel("Price ($)", color=FG, fontsize=8)

    # Volume in millions, so matplotlib's "1e7" offset label does not collide
    # with the price panel above it.
    vax.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(
            lambda v, _: f"{v/1e9:.1f}B" if v >= 1e9 else (f"{v/1e6:.0f}M" if v >= 1e6 else f"{v/1e3:.0f}K")
        )
    )
    ax.set_xlim(x[0] - 1, x[-1] + 1)

    vax.xaxis.set_major_locator(mdates.AutoDateLocator())
    vax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.autofmt_xdate(rotation=0, ha="center")

    path = out_dir / f"{ticker}_{snap.as_of}.png"
    fig.savefig(path, dpi=130, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    log.info("chart rendered: %s", path)
    return path
