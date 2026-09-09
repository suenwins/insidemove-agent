"""Stage 1 — what gets flagged, and why.

Two tiers:

  PRIMARY   the single completed session's move crossed the ±7% threshold.
            This is the spec's original trigger and always qualifies.

  SECONDARY the move is smaller, but the tape did something structurally
            interesting that a discretionary trader would want to see:
            a volume spike, a gap, a 52-week breakout, a range expansion,
            a capitulation/exhaustion wick, or a moving-average cross.

Primary names fill the daily slots first. Secondary names take at most
MAX_SECONDARY_SLOTS of the remaining ones, so a quiet day surfaces genuinely
interesting tape instead of padding the list with 2% moves, and a busy day is
never crowded out by them.

Critically, the trigger only ever looks at the most recently completed
session. Longer history is used to normalise (average volume, ATR, 52-week
range) — never to decide inclusion. A stock appears today because today's
tape did something, not because of a historical pattern match.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import CFG
from .indicators import atr, sma

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    ticker: str
    close: float
    prev_close: float
    pct_change: float
    volume: float
    avg_volume_20: float
    volume_ratio: float
    gap_pct: float
    dollar_volume: float
    tier: str                       # "primary" | "secondary"
    score: float
    reasons: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @property
    def direction(self) -> str:
        return "up" if self.pct_change >= 0 else "down"


def _direction_ok(pct: float) -> bool:
    if CFG.direction == "up":
        return pct > 0
    if CFG.direction == "down":
        return pct < 0
    return True


def evaluate(ticker: str, df: pd.DataFrame, session: pd.Timestamp) -> Candidate | None:
    """Score one ticker against the most recently completed session."""
    if len(df) < 30:
        return None
    # Only judge the session the whole scan is anchored to. A ticker whose
    # last bar is older (halted, holiday listing) is skipped rather than
    # silently compared against a different day.
    if df.index[-1].date() != session.date():
        return None

    last, prev = df.iloc[-1], df.iloc[-2]
    close, prev_close = float(last["Close"]), float(prev["Close"])
    if prev_close <= 0 or close < CFG.min_price:
        return None

    pct = (close / prev_close - 1) * 100
    vol = float(last["Volume"])
    avg_vol = float(df["Volume"].tail(21).head(20).mean())
    if not np.isfinite(avg_vol) or avg_vol <= 0:
        return None

    dollar_vol = avg_vol * close
    if dollar_vol < CFG.min_avg_dollar_volume:
        return None

    vol_ratio = vol / avg_vol
    gap = (float(last["Open"]) / prev_close - 1) * 100

    reasons: list[str] = []
    tags: list[str] = []
    score = 0.0

    # ---------------- primary trigger --------------------------------------
    is_primary = abs(pct) >= CFG.move_threshold_pct and _direction_ok(pct)
    if is_primary:
        reasons.append(f"{pct:+.1f}% single-session move (≥{CFG.move_threshold_pct:.0f}% threshold)")
        tags.append("threshold_move")
        score += abs(pct)

    # ---------------- secondary signals ------------------------------------
    # Each needs a minimum move so we surface conviction, not noise.
    meaningful = abs(pct) >= CFG.secondary_min_move_pct and _direction_ok(pct)

    if meaningful and vol_ratio >= CFG.volume_spike_x:
        reasons.append(f"volume {vol_ratio:.1f}× the 20-day average on a {pct:+.1f}% move")
        tags.append("volume_spike")
        score += 6 + min(vol_ratio, 10)

    if meaningful and abs(gap) >= CFG.gap_pct and np.sign(gap) == np.sign(pct):
        reasons.append(f"gapped {gap:+.1f}% at the open and held the direction")
        tags.append("gap")
        score += 5 + abs(gap) / 2

    # 52-week breakout: today's close takes out the prior year's extreme,
    # measured excluding today so "new" means genuinely new.
    hist = df.iloc[:-1].tail(252)
    if len(hist) >= 120:
        hi52, lo52 = float(hist["High"].max()), float(hist["Low"].min())
        if close > hi52 and _direction_ok(1):
            reasons.append(f"closed at a new 52-week high (took out ${hi52:,.2f})")
            tags.append("breakout_52w_high")
            score += 8
        elif close < lo52 and _direction_ok(-1):
            reasons.append(f"closed at a new 52-week low (broke ${lo52:,.2f})")
            tags.append("breakdown_52w_low")
            score += 8

    # Range expansion: today's true range dwarfs recent volatility.
    atr_series = atr(df, CFG.atr_period)
    atr_prev = float(atr_series.iloc[-2]) if pd.notna(atr_series.iloc[-2]) else None
    todays_range = float(last["High"]) - float(last["Low"])
    if atr_prev and atr_prev > 0:
        expansion = todays_range / atr_prev
        if meaningful and expansion >= CFG.range_expansion_x:
            reasons.append(f"daily range {expansion:.1f}× ATR({CFG.atr_period}) — volatility expansion")
            tags.append("range_expansion")
            score += 4 + expansion

    # Capitulation / exhaustion wick on heavy volume.
    if todays_range > 0 and vol_ratio >= CFG.wick_volume_x:
        body_low = min(float(last["Open"]), close)
        body_high = max(float(last["Open"]), close)
        lower = (body_low - float(last["Low"])) / todays_range
        upper = (float(last["High"]) - body_high) / todays_range
        if lower >= CFG.wick_ratio and _direction_ok(-1):
            reasons.append(
                f"long lower wick ({lower*100:.0f}% of range) on {vol_ratio:.1f}× volume "
                "— possible capitulation/absorption"
            )
            tags.append("capitulation_wick")
            score += 7
        elif upper >= CFG.wick_ratio and _direction_ok(1):
            reasons.append(
                f"long upper wick ({upper*100:.0f}% of range) on {vol_ratio:.1f}× volume "
                "— possible exhaustion/distribution"
            )
            tags.append("exhaustion_wick")
            score += 7

    # Moving-average cross confirmed on this session.
    fast_ma = sma(df["Close"], CFG.sma_fast)
    slow_ma = sma(df["Close"], CFG.sma_slow)
    f_now, f_prev = fast_ma.iloc[-1], fast_ma.iloc[-2]
    s_now, s_prev = slow_ma.iloc[-1], slow_ma.iloc[-2]
    if pd.notna([f_now, f_prev, s_now, s_prev]).all():
        if f_prev <= s_prev and f_now > s_now and _direction_ok(1):
            reasons.append(f"SMA{CFG.sma_fast} crossed above SMA{CFG.sma_slow} today (golden cross)")
            tags.append("golden_cross")
            score += 5
        elif f_prev >= s_prev and f_now < s_now and _direction_ok(-1):
            reasons.append(f"SMA{CFG.sma_fast} crossed below SMA{CFG.sma_slow} today (death cross)")
            tags.append("death_cross")
            score += 5

    if not reasons:
        return None

    # A secondary-only name needs more than one confirming signal — a single
    # volume spike on its own is too common to be worth a slot.
    tier = "primary" if is_primary else "secondary"
    if tier == "secondary" and len(tags) < 2:
        return None

    return Candidate(
        ticker=ticker,
        close=round(close, 2),
        prev_close=round(prev_close, 2),
        pct_change=round(pct, 2),
        volume=vol,
        avg_volume_20=round(avg_vol),
        volume_ratio=round(vol_ratio, 2),
        gap_pct=round(gap, 2),
        dollar_volume=round(dollar_vol),
        tier=tier,
        score=round(score, 2),
        reasons=reasons,
        tags=tags,
    )


def rank(frames: dict[str, pd.DataFrame], session: pd.Timestamp) -> list[Candidate]:
    """Evaluate the whole universe and return the day's shortlist."""
    candidates: list[Candidate] = []
    for ticker, df in frames.items():
        try:
            cand = evaluate(ticker, df, session)
        except Exception as exc:  # noqa: BLE001
            log.debug("evaluate failed for %s: %s", ticker, exc)
            continue
        if cand:
            candidates.append(cand)

    primary = sorted(
        [c for c in candidates if c.tier == "primary"],
        key=lambda c: abs(c.pct_change),
        reverse=True,
    )
    secondary = sorted(
        [c for c in candidates if c.tier == "secondary"],
        key=lambda c: c.score,
        reverse=True,
    )

    slots = CFG.max_alerts_per_day
    picked = primary[:slots]
    remaining = slots - len(picked)
    if remaining > 0:
        picked += secondary[: min(remaining, CFG.max_secondary_slots)]

    log.info(
        "scan: %d primary, %d secondary qualified; sending %d",
        len(primary), len(secondary), len(picked),
    )
    return picked
