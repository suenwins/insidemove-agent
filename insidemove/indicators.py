"""Deterministic technical computation.

Everything Claude is later asked to write about is computed here first, from
price data, and handed over as grounding facts. The model interprets numbers;
it never invents them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# core series
# --------------------------------------------------------------------------

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    ranges = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA).

    Not the same as `ewm(alpha=1/period)`: Wilder seeds the recursion with a
    simple average of the first `period` observations, and only then switches
    to the exponential update. Seeding from the very first observation instead
    — which is what a plain `ewm` does — leaves the series meaningfully off for
    a long time, so RSI and ATR would disagree with every charting platform the
    reader compares them against.
    """
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < period:
        return pd.Series(out, index=series.index)

    start = valid[0]
    seed_end = start + period
    if seed_end > len(values):
        return pd.Series(out, index=series.index)

    prev = float(np.nanmean(values[start:seed_end]))
    out[seed_end - 1] = prev
    for i in range(seed_end, len(values)):
        observation = values[i]
        if np.isnan(observation):
            observation = prev
        prev = (prev * (period - 1) + observation) / period
        out[i] = prev
    return pd.Series(out, index=series.index)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    return rma(true_range(df), period)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index."""
    delta = series.diff()
    gain = delta.clip(lower=0).where(delta.notna())
    loss = (-delta).clip(lower=0).where(delta.notna())

    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)

    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # Zero average loss: 100 if there were gains, but a perfectly flat series
    # has no gains either and is neutral, not maximally overbought.
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return out.where(avg_gain.notna())


# --------------------------------------------------------------------------
# swing structure
# --------------------------------------------------------------------------

def pivots(df: pd.DataFrame, k: int = 3) -> tuple[list[tuple[pd.Timestamp, float]], list[tuple[pd.Timestamp, float]]]:
    """Fractal swing highs and lows: a bar whose high (low) is the extreme of
    the k bars either side of it."""
    highs, lows = [], []
    h, l = df["High"].to_numpy(), df["Low"].to_numpy()
    idx = df.index
    for i in range(k, len(df) - k):
        window_h = h[i - k : i + k + 1]
        window_l = l[i - k : i + k + 1]
        if h[i] == window_h.max() and (window_h.argmax() == k):
            highs.append((idx[i], float(h[i])))
        if l[i] == window_l.min() and (window_l.argmin() == k):
            lows.append((idx[i], float(l[i])))
    return highs, lows


def cluster_levels(levels: list[float], tolerance_pct: float = 1.5) -> list[dict]:
    """Merge levels that sit within tolerance of each other. A level touched
    repeatedly is structurally stronger than one touched once, so we keep the
    touch count."""
    if not levels:
        return []
    ordered = sorted(levels)
    clusters: list[list[float]] = [[ordered[0]]]
    for lvl in ordered[1:]:
        anchor = np.mean(clusters[-1])
        if abs(lvl - anchor) / anchor * 100 <= tolerance_pct:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])
    return [
        {"level": round(float(np.mean(c)), 2), "touches": len(c)} for c in clusters
    ]


# --------------------------------------------------------------------------
# the full snapshot
# --------------------------------------------------------------------------

@dataclass
class TechSnapshot:
    ticker: str
    as_of: str
    close: float
    prev_close: float
    pct_change: float
    open: float
    high: float
    low: float
    volume: float
    avg_volume_20: float
    volume_ratio: float
    sma_fast: float | None
    sma_slow: float | None
    sma_fast_period: int
    sma_slow_period: int
    atr: float | None
    atr_pct: float | None
    atr_period: int
    rsi: float | None
    rsi_period: int
    high_52w: float
    low_52w: float
    pct_from_52w_high: float
    pct_from_52w_low: float
    support: list[dict] = field(default_factory=list)
    resistance: list[dict] = field(default_factory=list)
    implied_support: dict | None = None
    trend: str = "undetermined"
    trend_window: int = 90
    structure: str = ""
    ma_alignment: str = ""
    wick: dict = field(default_factory=dict)
    gap_pct: float = 0.0

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def _distance(price: float, level: float) -> dict:
    diff = price - level
    return {
        "abs": round(diff, 2),
        "pct": round(diff / level * 100, 2) if level else 0.0,
        "side": "above" if diff > 0 else "below",
    }


def _trend(
    df: pd.DataFrame,
    fast: float | None,
    slow: float | None,
    fast_p: int = 20,
    slow_p: int = 50,
    window: int = 90,
) -> tuple[str, str, str]:
    highs, lows = pivots(df.tail(window), k=3)
    hh = len(highs) >= 2 and highs[-1][1] > highs[-2][1]
    hl = len(lows) >= 2 and lows[-1][1] > lows[-2][1]
    lh = len(highs) >= 2 and highs[-1][1] < highs[-2][1]
    ll = len(lows) >= 2 and lows[-1][1] < lows[-2][1]

    if hh and hl:
        structure = "higher highs and higher lows"
        trend = "uptrend"
    elif lh and ll:
        structure = "lower highs and lower lows"
        trend = "downtrend"
    elif lh and hl:
        structure = "lower highs into higher lows (compression)"
        trend = "range/coiling"
    else:
        structure = "no clean swing sequence"
        trend = "range"

    if fast is None or slow is None:
        alignment = "insufficient history for MA alignment"
    elif fast > slow:
        alignment = f"SMA{fast_p} > SMA{slow_p} — bullish alignment"
        if trend == "range":
            trend = "range (bullish MA tilt)"
    else:
        alignment = f"SMA{fast_p} < SMA{slow_p} — bearish alignment"
        if trend == "range":
            trend = "range (bearish MA tilt)"
    return trend, structure, alignment


def analyse(
    ticker: str,
    df: pd.DataFrame,
    fast: int = 20,
    slow: int = 50,
    atr_period: int = 14,
    rsi_period: int = 14,
    lookback: int = 90,
) -> TechSnapshot:
    """Compute the full deterministic picture for one ticker."""
    df = df.dropna(subset=["Close"]).sort_index()
    if len(df) < 2:
        raise ValueError(f"{ticker}: not enough price history")

    last = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(last["Close"])
    prev_close = float(prev["Close"])

    sma_f = sma(df["Close"], fast)
    sma_s = sma(df["Close"], slow)
    atr_s = atr(df, atr_period)
    rsi_s = rsi(df["Close"], rsi_period)

    f_val = float(sma_f.iloc[-1]) if pd.notna(sma_f.iloc[-1]) else None
    s_val = float(sma_s.iloc[-1]) if pd.notna(sma_s.iloc[-1]) else None
    a_val = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else None
    r_val = float(rsi_s.iloc[-1]) if pd.notna(rsi_s.iloc[-1]) else None

    vol = float(last["Volume"])
    avg_vol = float(df["Volume"].tail(21).head(20).mean())
    window = df.tail(lookback)

    # --- structural levels from swing pivots in the chart window ----------
    highs, lows = pivots(window, k=3)
    res_raw = [lvl for _, lvl in highs if lvl > close]
    sup_raw = [lvl for _, lvl in lows if lvl < close]

    resistance = cluster_levels(res_raw)
    support = cluster_levels(sup_raw)

    # Dynamic MA levels join whichever side they sit on.
    for label, val in (("SMA20", f_val), ("SMA50", s_val)):
        if val is None:
            continue
        entry = {"level": round(val, 2), "touches": 0, "kind": label}
        (resistance if val > close else support).append(entry)

    for lst in (resistance, support):
        for item in lst:
            item.setdefault("kind", "swing")
            item["distance"] = _distance(close, item["level"])

    resistance.sort(key=lambda x: x["level"])                  # nearest first
    support.sort(key=lambda x: x["level"], reverse=True)       # nearest first

    # --- "open air" case: nothing below, so derive an ATR floor -----------
    implied = None
    if not support and a_val:
        ref = float(window["Low"].min())
        implied = {
            "level": round(ref - a_val, 2),
            "basis": f"${ref:,.2f} (lowest low of the last {len(window)} sessions) "
                     f"− 1×ATR(${a_val:,.2f})",
            "note": "No structural support below price — this is an ATR-derived implied floor,"
                    " not a tested level.",
        }

    hi52 = float(df["High"].tail(252).max())
    lo52 = float(df["Low"].tail(252).min())

    trend, structure, alignment = _trend(df, f_val, s_val, fast, slow, lookback)

    rng = float(last["High"]) - float(last["Low"])
    body_low = min(float(last["Open"]), close)
    body_high = max(float(last["Open"]), close)
    wick = {
        "lower_pct_of_range": round((body_low - float(last["Low"])) / rng * 100, 1) if rng else 0.0,
        "upper_pct_of_range": round((float(last["High"]) - body_high) / rng * 100, 1) if rng else 0.0,
        "range": round(rng, 2),
    }

    return TechSnapshot(
        ticker=ticker,
        as_of=df.index[-1].strftime("%Y-%m-%d"),
        close=round(close, 2),
        prev_close=round(prev_close, 2),
        pct_change=round((close / prev_close - 1) * 100, 2),
        open=round(float(last["Open"]), 2),
        high=round(float(last["High"]), 2),
        low=round(float(last["Low"]), 2),
        volume=vol,
        avg_volume_20=round(avg_vol, 0),
        volume_ratio=round(vol / avg_vol, 2) if avg_vol else 0.0,
        sma_fast=round(f_val, 2) if f_val else None,
        sma_slow=round(s_val, 2) if s_val else None,
        sma_fast_period=fast,
        sma_slow_period=slow,
        atr=round(a_val, 2) if a_val else None,
        atr_pct=round(a_val / close * 100, 2) if a_val else None,
        atr_period=atr_period,
        rsi=round(r_val, 1) if r_val else None,
        rsi_period=rsi_period,
        high_52w=round(hi52, 2),
        low_52w=round(lo52, 2),
        pct_from_52w_high=round((close / hi52 - 1) * 100, 2) if hi52 else 0.0,
        pct_from_52w_low=round((close / lo52 - 1) * 100, 2) if lo52 else 0.0,
        support=support[:4],
        resistance=resistance[:4],
        implied_support=implied,
        trend=trend,
        trend_window=len(window),
        structure=structure,
        ma_alignment=alignment,
        wick=wick,
        gap_pct=round((float(last["Open"]) / prev_close - 1) * 100, 2),
    )
