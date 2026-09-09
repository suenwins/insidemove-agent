"""News with a hard freshness window.

The spec is strict about this: the catalyst blurb must come from headlines
published inside the same trailing window as the price move. "Top headlines
for this ticker" is not acceptable — it surfaces month-old stories that have
nothing to do with today's tape. So every headline is filtered on its publish
timestamp, and when nothing survives the filter we say so rather than
substituting something stale.

Primary source: Finnhub company-news (free tier, reliable publish timestamps).
Fallback: Yahoo Finance news via yfinance (no key required).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .config import CFG

US_EASTERN = ZoneInfo("America/New_York")
# Coverage of a session keeps arriving for a few hours after the bell, so the
# window closes after the close rather than at it.
POST_CLOSE_HOURS = 4

log = logging.getLogger(__name__)

FINNHUB = "https://finnhub.io/api/v1"
NO_CATALYST = (
    "No confirmed catalyst found in the {hours}h around that session — "
    "move may be technical or flow-driven."
)


def session_window(session_date: str | date | None, hours: int) -> tuple[datetime, datetime]:
    """The publish window that could plausibly explain a given session's move.

    Anchoring to *now* is only correct when the scan runs shortly after the
    close. Across a weekend — or a Friday session read on the following
    Tuesday because Monday was Labor Day — "the last 24 hours" covers days the
    move did not happen in, which surfaces unrelated stories and reports
    genuine catalysts as missing.

    So the window is tied to the session itself: it ends a few hours after
    that session's close and reaches back `hours` from there, covering
    pre-market, the session, and the immediate after-hours reaction.
    """
    now = datetime.now(timezone.utc)
    if session_date is None:
        return now - timedelta(hours=hours), now

    if isinstance(session_date, str):
        session_date = date.fromisoformat(session_date)

    close = datetime.combine(session_date, time(16, 0), tzinfo=US_EASTERN)
    end = (close + timedelta(hours=POST_CLOSE_HOURS)).astimezone(timezone.utc)
    end = min(end, now)                      # never claim to see the future
    return end - timedelta(hours=hours), end


def _finnhub_news(ticker: str, hours: int, window: tuple[datetime, datetime]) -> list[dict]:
    if not CFG.finnhub_api_key:
        return []
    start, end = window
    params = {
        "symbol": ticker,
        # Pad both ends by a day; we filter precisely on timestamp below.
        "from": (start - timedelta(days=1)).strftime("%Y-%m-%d"),
        "to": (end + timedelta(days=1)).strftime("%Y-%m-%d"),
        "token": CFG.finnhub_api_key,
    }
    try:
        resp = requests.get(f"{FINNHUB}/company-news", params=params, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("finnhub news failed for %s: %s", ticker, exc)
        return []

    items = []
    for art in raw if isinstance(raw, list) else []:
        ts = art.get("datetime")
        if not ts:
            continue
        published = datetime.fromtimestamp(ts, tz=timezone.utc)
        if not (start <= published <= end):
            continue
        items.append(_item(
            (art.get("headline") or "").strip(),
            (art.get("summary") or "").strip()[:600],
            art.get("source") or "",
            art.get("url") or "",
            published,
        ))
    return items


def _item(headline: str, summary: str, source: str, url: str, published: datetime) -> dict:
    now = datetime.now(timezone.utc)
    age = (now - published).total_seconds() / 3600
    return {
        "headline": headline,
        "summary": summary,
        "source": source,
        "url": url,
        "published": published.isoformat(),
        "age_hours": round(age, 1),
        # Beyond a couple of days "18h ago" stops being meaningful and an
        # actual date is what tells you whether it belongs to the session.
        "when": (
            f"{age:.0f}h ago" if age < 48
            else published.astimezone(US_EASTERN).strftime("%d %b")
        ),
    }


def _yahoo_news(ticker: str, hours: int, window: tuple[datetime, datetime]) -> list[dict]:
    try:
        import yfinance as yf

        raw = yf.Ticker(ticker).news or []
    except Exception as exc:  # noqa: BLE001
        log.warning("yahoo news failed for %s: %s", ticker, exc)
        return []

    start, end = window
    items = []
    for art in raw:
        content = art.get("content", art)
        ts = art.get("providerPublishTime")
        published = None
        if ts:
            published = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            pub = content.get("pubDate") or content.get("displayTime")
            if pub:
                try:
                    published = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
                except ValueError:
                    published = None
        if not published or not (start <= published <= end):
            continue
        provider = content.get("provider") or {}
        items.append(_item(
            (content.get("title") or art.get("title") or "").strip(),
            (content.get("summary") or "").strip()[:600],
            provider.get("displayName") if isinstance(provider, dict) else str(provider),
            (content.get("canonicalUrl") or {}).get("url", art.get("link", "")),
            published,
        ))
    return items


def recent_news(
    ticker: str,
    session_date: str | date | None = None,
    hours: int | None = None,
    limit: int = 6,
) -> list[dict]:
    """Headlines published inside the scanned session's window, newest first."""
    hours = hours or CFG.news_window_hours
    window = session_window(session_date, hours)
    items = _finnhub_news(ticker, hours, window) or _yahoo_news(ticker, hours, window)
    items = [i for i in items if i["headline"]]
    items.sort(key=lambda i: i["published"], reverse=True)

    # De-duplicate near-identical syndicated headlines.
    seen: set[str] = set()
    unique = []
    for item in items:
        key = item["headline"].lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:limit]


def no_catalyst_line(hours: int | None = None) -> str:
    return NO_CATALYST.format(hours=hours or CFG.news_window_hours)
