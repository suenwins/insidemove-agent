"""Market data access.

Price/volume comes from Yahoo Finance via `yfinance` — free, no API key, and
a batched download covers a few thousand tickers in one daily pass. Everything
downstream (signals, indicators, charts) consumes the plain OHLCV frames this
module returns, so swapping in a paid provider later means rewriting only this
file.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

OHLCV = ["Open", "High", "Low", "Close", "Volume"]
CHUNK = 100
MAX_RETRIES = 3


def _normalise(raw: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    """yfinance returns a MultiIndex frame for multi-ticker downloads and a
    flat frame for a single ticker. Flatten both into {ticker: OHLCV frame}."""
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out

    if isinstance(raw.columns, pd.MultiIndex):
        # group_by="ticker" -> level 0 is the ticker
        level0 = set(raw.columns.get_level_values(0))
        ticker_first = bool(level0 & set(tickers))
        for tkr in tickers:
            try:
                sub = raw[tkr] if ticker_first else raw.xs(tkr, axis=1, level=1)
            except KeyError:
                continue
            out[tkr] = sub
    else:
        out[tickers[0]] = raw

    cleaned: dict[str, pd.DataFrame] = {}
    for tkr, df in out.items():
        if df is None or df.empty:
            continue
        missing = [c for c in OHLCV if c not in df.columns]
        if missing:
            continue
        df = df[OHLCV].dropna(how="all")
        df = df[df["Close"].notna() & (df["Close"] > 0)]
        if len(df) < 2:
            continue
        df.index = pd.to_datetime(df.index).tz_localize(None)
        cleaned[tkr] = df.sort_index()
    return cleaned


def _download_chunk(tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw = yf.download(
                tickers,
                period=period,
                interval="1d",
                auto_adjust=False,
                actions=False,
                group_by="ticker",
                progress=False,
                threads=True,
            )
            return _normalise(raw, tickers)
        except Exception as exc:  # noqa: BLE001
            wait = 2**attempt
            log.warning(
                "download chunk failed (attempt %d/%d): %s — retrying in %ds",
                attempt, MAX_RETRIES, exc, wait,
            )
            time.sleep(wait)
    log.error("download chunk permanently failed for %d tickers", len(tickers))
    return {}


def fetch_history(
    tickers: list[str], period: str = "1y", workers: int = 4
) -> dict[str, pd.DataFrame]:
    """Batched daily OHLCV for a list of tickers."""
    tickers = sorted(set(t for t in tickers if t))
    chunks = [tickers[i : i + CHUNK] for i in range(0, len(tickers), CHUNK)]
    result: dict[str, pd.DataFrame] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for got in pool.map(lambda c: _download_chunk(c, period), chunks):
            result.update(got)

    log.info("fetched history for %d/%d tickers", len(result), len(tickers))
    return result


def fetch_one(ticker: str, period: str = "1y") -> pd.DataFrame | None:
    got = _download_chunk([ticker], period)
    return got.get(ticker)


US_EASTERN = ZoneInfo("America/New_York")
CLOSE_HOUR = 16          # 4pm ET regular-session close
SETTLE_MINUTES = 20      # let the closing print settle before trusting the bar


def latest_completed_session(now: datetime | None = None) -> date:
    """The most recent trading date whose US session has actually finished.

    Yahoo serves the current day's partial bar while the market is open. That
    bar is poison for this scanner: its volume is a fraction of a full day, so
    every volume ratio collapses toward zero, and its "close" is just the last
    print rather than a settled closing price. A 24% move on 0.2x volume is not
    a signal, it is an unfinished session being compared against complete ones.

    So anything at or after this date is treated as still in progress and
    dropped. Weekends and holidays need no special case: this only sets a
    ceiling, and the real last session is whatever data exists at or below it.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(US_EASTERN)
    settled = now.replace(hour=CLOSE_HOUR, minute=SETTLE_MINUTES,
                          second=0, microsecond=0)
    return now.date() if now >= settled else (now - timedelta(days=1)).date()


def drop_incomplete_sessions(
    frames: dict[str, pd.DataFrame], ceiling: date | None = None
) -> tuple[dict[str, pd.DataFrame], int]:
    """Trim every frame to the last completed session. Returns the trimmed
    frames and how many carried an in-progress bar."""
    ceiling = ceiling or latest_completed_session()
    limit = pd.Timestamp(ceiling)
    trimmed: dict[str, pd.DataFrame] = {}
    dropped = 0
    for ticker, df in frames.items():
        if len(df) and df.index[-1] > limit:
            dropped += 1
            df = df[df.index <= limit]
        if len(df) >= 2:
            trimmed[ticker] = df
    return trimmed, dropped


def last_session_date(frames: dict[str, pd.DataFrame]) -> date | None:
    """The most recent completed session present across the universe.

    Taking the modal (most common) last date rather than the max protects
    against a single stale or early-reporting symbol shifting the whole scan.
    """
    stamps = [df.index[-1] for df in frames.values() if len(df)]
    if not stamps:
        return None
    return pd.Series(stamps).mode().iloc[0].date()


def company_profile(ticker: str) -> dict:
    """Name / sector / analyst targets. Best-effort — never raises."""
    try:
        info = yf.Ticker(ticker).get_info()
    except Exception as exc:  # noqa: BLE001
        log.warning("profile fetch failed for %s: %s", ticker, exc)
        return {}
    keys = [
        "shortName", "longName", "sector", "industry", "marketCap",
        "targetMeanPrice", "targetHighPrice", "targetLowPrice",
        "numberOfAnalystOpinions", "recommendationKey", "trailingPE",
        "forwardPE", "beta", "averageVolume",
    ]
    return {k: info.get(k) for k in keys if info.get(k) is not None}
