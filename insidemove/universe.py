"""Ticker universe: S&P 500 + NASDAQ constituents.

Refreshed weekly and cached on disk so a source outage on any given day
never stops the scan — we simply reuse the last good list.

UNIVERSE_MODE:
  core  -> S&P 500 + NASDAQ-100 (~550 names, fast, mega/large cap only)
  wide  -> the above plus every NASDAQ-listed common stock (~3,500 names).
           Liquidity filters in the scanner keep the junk out. This is the
           default because 7% single-session moves are rare in the S&P 500 —
           the interesting movers are usually mid caps.
"""
from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from . import state
from .config import CFG

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; InsideMoveAgent/1.0)"}
UNIVERSE_MODE = os.getenv("UNIVERSE_MODE", "wide").strip().lower()

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"

# Symbols that are not ordinary common stock; excluded from the wide universe.
_BAD_SUFFIXES = ("W", "R", "U")  # warrants, rights, units (heuristic, 5-char)


def _get(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _norm(sym: str) -> str:
    """Yahoo uses '-' where the exchanges use '.' for share classes."""
    return sym.strip().upper().replace(".", "-")


def _sp500() -> dict[str, dict[str, str]]:
    tables = pd.read_html(io.StringIO(_get(SP500_URL)))
    df = next(t for t in tables if "Symbol" in t.columns)
    out = {}
    for _, row in df.iterrows():
        sym = _norm(str(row["Symbol"]))
        out[sym] = {
            "name": str(row.get("Security", sym)),
            "sector": str(row.get("GICS Sector", "")),
            "index": "S&P 500",
        }
    return out


def _ndx() -> dict[str, dict[str, str]]:
    """Wikipedia renames these columns from time to time, and the old
    `next(...)` raised a StopIteration whose message was empty — which is how
    this failed with a blank error. Be explicit about what was not found."""
    tables = pd.read_html(io.StringIO(_get(NDX_URL)))
    candidates = ("Ticker", "Symbol", "Ticker symbol")
    df = None
    for table in tables:
        cols = [str(c) for c in table.columns]
        if any(c in cols for c in candidates):
            df = table
            break
    if df is None:
        seen = [list(map(str, t.columns))[:4] for t in tables[:5]]
        raise ValueError(
            f"no table with a ticker column on the Nasdaq-100 page; saw {seen}"
        )
    col = next(c for c in candidates if c in [str(x) for x in df.columns])
    name_col = next((c for c in ("Company", "Security") if c in df.columns), None)
    sector_col = next((c for c in ("GICS Sector", "Sector") if c in df.columns), None)
    out = {}
    for _, row in df.iterrows():
        sym = _norm(str(row[col]))
        out[sym] = {
            "name": str(row[name_col]) if name_col else sym,
            "sector": str(row[sector_col]) if sector_col else "",
            "index": "NASDAQ-100",
        }
    return out


def _nasdaq_all() -> dict[str, dict[str, str]]:
    text = _get(NASDAQ_LISTED)
    out: dict[str, dict[str, str]] = {}
    for line in text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) < 7 or parts[0].startswith("File Creation"):
            continue
        symbol, name, _cat, test_issue, status, _lot, etf = parts[:7]
        if test_issue.strip() == "Y" or etf.strip() == "Y":
            continue
        if status.strip() in {"D", "E", "Q"}:  # deficient / delinquent / bankrupt
            continue
        sym = symbol.strip().upper()
        if not sym or not sym.isalpha():
            continue
        if len(sym) == 5 and sym[-1] in _BAD_SUFFIXES:
            continue
        out[_norm(sym)] = {"name": name.strip(), "sector": "", "index": "NASDAQ"}
    return out


def build_universe() -> dict[str, dict[str, str]]:
    """Fetch from all sources; partial failure is tolerated."""
    universe: dict[str, dict[str, str]] = {}
    sources = [("S&P 500", _sp500), ("NASDAQ-100", _ndx)]
    if UNIVERSE_MODE == "wide":
        sources.append(("NASDAQ listed", _nasdaq_all))

    for label, fn in sources:
        try:
            got = fn()
            # Earlier sources win: they carry sector/name metadata.
            for sym, meta in got.items():
                if sym not in universe:
                    universe[sym] = meta
            log.info("universe: %s contributed %d symbols", label, len(got))
        except Exception as exc:  # noqa: BLE001
            log.warning("universe: source %s failed: %s", label, exc)

    return universe


def get_universe(force: bool = False) -> dict[str, dict[str, str]]:
    cached = state.kv_get("universe")
    stamp = state.kv_get("universe_ts")
    fresh = False
    if stamp:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
        fresh = age < timedelta(days=CFG.universe_refresh_days)

    if cached and fresh and not force:
        return cached

    built = build_universe()
    if len(built) < 100:
        if cached:
            log.warning("universe: refresh returned only %d symbols, keeping cache", len(built))
            return cached
        raise RuntimeError("Could not build a usable ticker universe and no cache exists")

    state.kv_set("universe", built)
    state.kv_set("universe_ts", datetime.now(timezone.utc).isoformat())
    log.info("universe: refreshed, %d symbols", len(built))
    return built


def meta_for(ticker: str) -> dict[str, str]:
    uni = state.kv_get("universe") or {}
    return uni.get(ticker, {"name": ticker, "sector": "", "index": ""})
