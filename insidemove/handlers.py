"""Stage 3 + Stage 4 — what happens when a button is tapped.

Callback data is self-contained (`a:AAPL:20260904`), so a tap resolves with
no database lookup and a button pressed weeks later still works. See
`callbacks.py` for why that replaced the token table.
"""
from __future__ import annotations

import logging

import pandas as pd

from . import (
    callbacks, charting, data, fundamentals, indicators, news, notes,
    signals, telegram_api as tg, universe,
)
from .config import CFG

log = logging.getLogger(__name__)


def _busy(callback_id: str, msg: str) -> None:
    tg.answer_callback(callback_id, msg)


def _why_flagged(ticker: str, df, action: dict) -> list[str]:
    """Recompute the scan's reasons at button-press time.

    The old build carried these along in the callback token. Recomputing them
    is cheap, and it re-derives them against the same session the button names
    rather than trusting a payload that could be days old.
    """
    try:
        session = pd.Timestamp(action["session_date"])
        candidate = signals.evaluate(ticker, df.loc[:session], session)
        if candidate:
            return candidate.reasons
    except Exception as exc:  # noqa: BLE001
        log.debug("could not recompute reasons for %s: %s", ticker, exc)
    tier = action.get("tier") or "flagged"
    return [f"{tier} signal on the {action['session_date']} session"]


# --------------------------------------------------------------------------
# Stage 3 — equity research note
# --------------------------------------------------------------------------

def handle_approve(action: dict, chat_id: int, message_id: int, callback_id: str) -> None:
    ticker = action["ticker"]
    _busy(callback_id, f"Researching {ticker}…")
    tg.edit_markup(chat_id, message_id)  # strip buttons so it can't be double-fired

    status = tg.send_message(
        f"⏳ <b>{ticker}</b> — assembling equity research note "
        f"(news, insider filings, analyst view, peer tape)…",
        reply_to=message_id,
    )

    df = data.fetch_one(ticker, period="1y")
    if df is None or len(df) < 30:
        tg.send_message(f"❌ <b>{ticker}</b> — could not retrieve price history.")
        return

    snap = indicators.analyse(
        ticker, df, CFG.sma_fast, CFG.sma_slow, CFG.atr_period, CFG.rsi_period, CFG.chart_days
    )
    profile = data.company_profile(ticker)
    meta = universe.meta_for(ticker)
    headlines = news.recent_news(
        ticker, session_date=action["session_date"], hours=72, limit=8
    )
    insiders = fundamentals.insider_transactions(ticker)
    analysts = fundamentals.analyst_view(ticker, profile)
    peer_list = fundamentals.peers(ticker, profile)
    peer_frames = data.fetch_history(peer_list, period="1mo") if peer_list else {}
    peer_perf = fundamentals.peer_performance(ticker, peer_list, peer_frames)

    target = analysts.get("target_mean")
    payload = {
        "ticker": ticker,
        "company": profile.get("longName") or profile.get("shortName") or meta.get("name"),
        "sector": profile.get("sector") or meta.get("sector"),
        "industry": profile.get("industry"),
        "market_cap": profile.get("marketCap"),
        "session": {
            "as_of": snap.as_of,
            "close": snap.close,
            "prev_close": snap.prev_close,
            "pct_change": snap.pct_change,
            "open": snap.open,
            "gap_pct": snap.gap_pct,
            "volume": snap.volume,
            "avg_volume_20": snap.avg_volume_20,
            "volume_ratio": snap.volume_ratio,
        },
        "why_flagged": _why_flagged(ticker, df, action),
        "valuation": {
            "trailing_pe": profile.get("trailingPE"),
            "forward_pe": profile.get("forwardPE"),
            "beta": profile.get("beta"),
        },
        "analyst": analysts,
        "discount_to_target": (
            {
                "target_mean": target,
                "abs": round(target - snap.close, 2),
                "pct": round((target / snap.close - 1) * 100, 2),
            }
            if target
            else None
        ),
        "insider_transactions_6m": insiders or "no Form 4 filings available in the data source",
        "peers_same_session": peer_perf or "peer data unavailable",
        "news_last_72h": headlines or "no headlines in window",
        "technical_context": {
            "rsi": snap.rsi,
            "sma_fast": snap.sma_fast,
            "sma_slow": snap.sma_slow,
            "trend": snap.trend,
            "pct_from_52w_high": snap.pct_from_52w_high,
            "pct_from_52w_low": snap.pct_from_52w_low,
        },
    }

    try:
        note = notes.equity_note(payload)
    except Exception as exc:  # noqa: BLE001
        log.exception("equity note failed for %s", ticker)
        tg.send_message(f"❌ <b>{ticker}</b> — research note failed: {tg.escape(str(exc))}")
        return

    arrow = "🟢▲" if snap.pct_change >= 0 else "🔴▼"
    head = (
        f"📋 <b>{ticker} — AI Equity Research Note</b>\n"
        f"{arrow} {snap.pct_change:+.2f}% · ${snap.close:,.2f} · session {snap.as_of}\n"
        f"<i>Every claim below is labelled FACT (from data) or INFERENCE (interpretation).</i>\n"
        f"{'─' * 24}\n\n"
    )

    session = action["session_date"]
    tier = action.get("tier", "")
    markup = tg.keyboard([
        [("📊 PTC (Professional Trading Chart)", callbacks.encode("ptc", ticker, session, tier))],
        [("❌ Dismiss", callbacks.encode("dismiss", ticker, session, tier))],
    ])
    tg.send_message(head + note, reply_markup=markup, reply_to=message_id)


# --------------------------------------------------------------------------
# Stage 4 — technical chart analysis
# --------------------------------------------------------------------------

def handle_ptc(action: dict, chat_id: int, message_id: int, callback_id: str) -> None:
    ticker = action["ticker"]
    _busy(callback_id, f"Building {ticker} chart…")
    tg.edit_markup(chat_id, message_id)

    df = data.fetch_one(ticker, period="1y")
    if df is None or len(df) < 60:
        tg.send_message(f"❌ <b>{ticker}</b> — not enough history for technical analysis.")
        return

    snap = indicators.analyse(
        ticker, df, CFG.sma_fast, CFG.sma_slow, CFG.atr_period, CFG.rsi_period, CFG.chart_days
    )

    # 1. the chart itself
    try:
        chart_path = charting.render(ticker, df, snap)
        arrow = "▲" if snap.pct_change >= 0 else "▼"
        tg.send_photo(
            chart_path,
            caption=(
                f"📊 <b>{ticker}</b> — {CFG.chart_days}-day candles, "
                f"SMA{CFG.sma_fast}/SMA{CFG.sma_slow}, support &amp; resistance marked\n"
                f"{arrow} {snap.pct_change:+.2f}% · ${snap.close:,.2f} · {snap.as_of}"
            ),
            reply_to=message_id,
        )
    except Exception:  # noqa: BLE001
        log.exception("chart render failed for %s", ticker)
        tg.send_message(f"⚠️ <b>{ticker}</b> — chart render failed; sending the analysis anyway.")

    # 2. the write-up, grounded in the same computed numbers
    payload = snap.to_dict()
    payload["threshold_definitions"] = {
        "capitulation_volume": "2.5× or more of the 20-day average volume, "
                               "paired with a lower wick ≥60% of the session range",
        "atr_period": CFG.atr_period,
        "rsi_period": CFG.rsi_period,
        "chart_window_days": CFG.chart_days,
    }
    payload["note"] = (
        "All levels, averages and ratios above were computed deterministically from "
        "daily OHLCV. Do not introduce any number not present here."
    )

    try:
        note = notes.technical_note(payload)
    except Exception as exc:  # noqa: BLE001
        log.exception("technical note failed for %s", ticker)
        tg.send_message(f"❌ <b>{ticker}</b> — technical write-up failed: {tg.escape(str(exc))}")
        return

    head = (
        f"📐 <b>{ticker} — AI Technical Chart Analysis</b>\n"
        f"Close ${snap.close:,.2f} · RSI {snap.rsi} · ATR(14) ${snap.atr} "
        f"({snap.atr_pct}% of price)\n"
        f"{'─' * 24}\n\n"
    )
    tg.send_message(head + note, reply_to=message_id)


# --------------------------------------------------------------------------
# dismiss
# --------------------------------------------------------------------------

def handle_dismiss(action: dict, chat_id: int, message_id: int, callback_id: str) -> None:
    _busy(callback_id, f"{action['ticker']} dismissed")
    tg.edit_markup(chat_id, message_id)


HANDLERS = {"approve": handle_approve, "ptc": handle_ptc, "dismiss": handle_dismiss}


def dispatch(callback: dict) -> None:
    """Route one callback_query. Everything needed is inside the payload."""
    callback_id = callback.get("id", "")
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    try:
        action = callbacks.decode(callback.get("data", ""))
    except callbacks.BadCallback as exc:
        log.warning("undecodable callback: %s", exc)
        tg.answer_callback(callback_id, "Unrecognised action.")
        return

    handler = HANDLERS[action["verb"]]
    try:
        handler(action, chat_id, message_id, callback_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("handler %s failed", action["verb"])
        tg.send_message(f"❌ Something went wrong: {tg.escape(str(exc))}")
