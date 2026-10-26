"""Insider filings, analyst view and peer context for the Stage 3 note.

Every function degrades gracefully: a missing API key or a premium-gated
endpoint returns an empty result and the report simply states that the data
was unavailable, rather than the model filling the gap with invention.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import requests

from .config import CFG

log = logging.getLogger(__name__)
FINNHUB = "https://finnhub.io/api/v1"


def _finnhub(path: str, **params) -> dict | list | None:
    if not CFG.finnhub_api_key:
        return None
    params["token"] = CFG.finnhub_api_key
    try:
        resp = requests.get(f"{FINNHUB}{path}", params=params, timeout=20)
        if resp.status_code in (401, 403):
            log.info("finnhub %s not available on this plan", path)
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("finnhub %s failed: %s", path, exc)
        return None


def insider_transactions(ticker: str, months: int = 6, limit: int = 12) -> list[dict]:
    """Recent Form 4 activity. Finnhub codes: P=open-market buy, S=sale,
    A=grant/award, M=option exercise. Grants and exercises are routine comp,
    so they are labelled rather than dropped — the distinction matters."""
    frm = (date.today() - timedelta(days=30 * months)).isoformat()
    data = _finnhub("/stock/insider-transactions", symbol=ticker, **{"from": frm})
    if not data or not isinstance(data, dict):
        return []

    code_map = {
        "P": "open-market buy",
        "S": "sale",
        "A": "grant/award",
        "M": "option exercise",
        "F": "shares withheld for tax",
        "G": "gift",
    }
    out = []
    for row in data.get("data", []):
        share = row.get("share")
        change = row.get("change")
        price = row.get("transactionPrice") or 0
        code = (row.get("transactionCode") or "").upper()
        out.append(
            {
                "name": row.get("name", "unknown"),
                "date": row.get("transactionDate", ""),
                "filing_date": row.get("filingDate", ""),
                "code": code,
                "action": code_map.get(code, f"code {code}"),
                "shares_changed": change,
                "shares_held_after": share,
                "price": price,
                "value": round(abs(change or 0) * price, 2) if price else None,
                "routine": code in {"A", "M", "F"},
            }
        )
    out.sort(key=lambda r: r["date"] or "", reverse=True)
    return out[:limit]


def analyst_view(ticker: str, profile: dict | None = None) -> dict:
    """Consensus rating spread plus a mean price target.

    Finnhub's price-target endpoint is premium, so the target falls back to
    Yahoo's consensus (carried on the profile dict) — which is free and
    sourced from the same sell-side estimates.
    """
    out: dict = {"source": []}

    trends = _finnhub("/stock/recommendation-trends", symbol=ticker)
    if isinstance(trends, list) and trends:
        latest = trends[0]
        out["ratings"] = {
            "period": latest.get("period"),
            "strong_buy": latest.get("strongBuy", 0),
            "buy": latest.get("buy", 0),
            "hold": latest.get("hold", 0),
            "sell": latest.get("sell", 0),
            "strong_sell": latest.get("strongSell", 0),
        }
        out["analyst_count"] = sum(
            v for k, v in out["ratings"].items() if k != "period" and isinstance(v, int)
        )
        out["source"].append("Finnhub recommendation trends")

    target = _finnhub("/stock/price-target", symbol=ticker)
    if isinstance(target, dict) and target.get("targetMean"):
        out["target_mean"] = target["targetMean"]
        out["target_high"] = target.get("targetHigh")
        out["target_low"] = target.get("targetLow")
        out["source"].append("Finnhub price target")
    elif profile:
        if profile.get("targetMeanPrice"):
            out["target_mean"] = profile["targetMeanPrice"]
            out["target_high"] = profile.get("targetHighPrice")
            out["target_low"] = profile.get("targetLowPrice")
            out["analyst_count"] = out.get("analyst_count") or profile.get("numberOfAnalystOpinions")
            out["source"].append("Yahoo Finance consensus")
        if profile.get("recommendationKey"):
            out["recommendation"] = profile["recommendationKey"]
    return out


def peers(ticker: str, profile: dict | None = None) -> list[str]:
    data = _finnhub("/stock/peers", symbol=ticker)
    if isinstance(data, list) and data:
        return [p for p in data if p != ticker][:6]
    return []


def peer_performance(ticker: str, peer_list: list[str], frames: dict) -> list[dict]:
    """Same-session % move for each peer, to separate an idiosyncratic
    single-stock event from a sector-wide repricing."""
    out = []
    for peer in peer_list:
        df = frames.get(peer)
        if df is None or len(df) < 2:
            continue
        close = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2])
        if prev <= 0:
            continue
        out.append(
            {
                "ticker": peer,
                "pct_change": round((close / prev - 1) * 100, 2),
                "close": round(close, 2),
                "as_of": df.index[-1].strftime("%Y-%m-%d"),
            }
        )
    return out


def earnings_event(ticker: str, session_date: str) -> dict | None:
    """Did this company report around the scanned session?

    Uses Finnhub's earnings calendar (free tier, same key already in use).
    An earnings date is a hard fact rather than an interpretation: either the
    company reported in the window or it did not. `hour` is Finnhub's code —
    bmo = before market open, amc = after market close.
    """
    from datetime import date as _date

    try:
        session = _date.fromisoformat(session_date)
    except ValueError:
        return None

    rows = _finnhub(
        "/calendar/earnings",
        symbol=ticker,
        **{"from": (session - timedelta(days=1)).isoformat(),
           "to": (session + timedelta(days=1)).isoformat()},
    )
    events = (rows or {}).get("earningsCalendar") if isinstance(rows, dict) else None
    if not events:
        return None

    # A report after the close moves the *next* session, one before the open
    # moves the same session; both are inside the window either way.
    event = events[0]
    when = {"bmo": "before the open", "amc": "after the close", "dmh": "during the session"}.get(
        (event.get("hour") or "").lower(), ""
    )
    parts = [f"Reported earnings on {event.get('date')}"]
    if when:
        parts[0] += f" {when}"
    eps, est = event.get("epsActual"), event.get("epsEstimate")
    if eps is not None and est is not None:
        verb = "beat" if eps > est else "missed" if eps < est else "matched"
        parts.append(f"EPS ${eps:.2f} vs ${est:.2f} estimate ({verb})")
    elif eps is not None:
        parts.append(f"EPS ${eps:.2f}")
    rev, rev_est = event.get("revenueActual"), event.get("revenueEstimate")
    if rev is not None and rev_est:
        verb = "above" if rev > rev_est else "below" if rev < rev_est else "in line with"
        parts.append(f"revenue ${rev/1e6:,.0f}M, {verb} the ${rev_est/1e6:,.0f}M estimate")
    quarter = event.get("quarter")
    year = event.get("year")
    label = f" (Q{quarter} {year})" if quarter and year else ""

    return {
        "headline": f"{'; '.join(parts)}{label}.",
        "summary": "",
        "source": "Finnhub earnings calendar",
        "url": "",
        "published": "",
        "age_hours": 0.0,
        "when": event.get("date") or "",
        "tag": "Earnings",
        "detail": None,
        "earnings": True,
    }
