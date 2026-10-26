"""Free catalyst sources that need no API key.

Three of them, each answering a different question:

* **Press-release wires** (GlobeNewswire, PR Newswire, Business Wire,
  Accesswire, Newsfile) — *what did the company itself announce?* This is
  where a small cap's news appears first, often hours before any news site
  writes about it, and frequently the only place it appears at all. Reached
  through Google News with the query restricted to those domains, because
  the wires' own feeds are organised by industry rather than by ticker.
* **Google News search** — *did anyone else report a reason?* One query
  covering thousands of outlets, which catches what Finnhub and Yahoo miss.
* **Nasdaq trading halts** — *did the exchange stop trading, and why?* A
  halt with reason code T1 ("news pending") is hard evidence that a company
  announcement, not flow, moved the stock, and it timestamps it precisely.

Everything here degrades quietly: a feed that is slow, rate-limited or
malformed returns nothing and the scan carries on with the other sources.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import requests

log = logging.getLogger(__name__)

TIMEOUT = 15
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; InsideMoveAgent/1.0)"}

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

# The wires a US-listed company actually files its releases through.
WIRE_DOMAINS = [
    "globenewswire.com",
    "prnewswire.com",
    "businesswire.com",
    "accesswire.com",
    "newsfilecorp.com",
    "einpresswire.com",
]

_TAGS = re.compile(r"<[^>]+>")


def _text(html: str) -> str:
    return " ".join(_TAGS.sub(" ", html or "").split())


def _parse_when(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _rss_items(url: str) -> list[dict]:
    """Fetch an RSS feed and return its items as plain dicts."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("feed unavailable (%s): %s", url.split("?")[0], exc)
        return []

    out = []
    for item in root.iter("item"):
        get = lambda tag: (item.findtext(tag) or "").strip()  # noqa: E731
        out.append({
            "title": get("title"),
            "description": _text(get("description")),
            "link": get("link"),
            "source": (item.findtext("source") or "").strip(),
            "published": _parse_when(get("pubDate")),
            "raw": item,
        })
    return out


def _as_news(entry: dict, fallback_source: str) -> dict:
    """Convert a feed entry into the shape the rest of the app expects."""
    title = entry["title"]
    source = entry["source"] or fallback_source
    # Google News appends " - Publisher" to every title.
    if not entry["source"] and " - " in title:
        head, _, tail = title.rpartition(" - ")
        if head and len(tail) < 40:
            title, source = head, tail

    published = entry["published"] or datetime.now(timezone.utc)
    age = (datetime.now(timezone.utc) - published).total_seconds() / 3600
    return {
        "headline": title,
        "summary": entry["description"][:600],
        "source": source,
        "url": entry["link"],
        "published": published.isoformat(),
        "age_hours": round(age, 1),
        "when": (
            f"{age:.0f}h ago" if age < 48
            else published.astimezone(timezone.utc).strftime("%d %b")
        ),
    }


def _google_query(query: str, start: datetime, end: datetime, fallback: str) -> list[dict]:
    items = _rss_items(GOOGLE_NEWS.format(q=quote_plus(query)))
    out = []
    for entry in items:
        when = entry["published"]
        if when is None or not (start <= when <= end):
            continue
        if not entry["title"]:
            continue
        out.append(_as_news(entry, fallback))
    return out


def _search_terms(ticker: str, company: str | None) -> str:
    """Quoted company name when we have one, ticker as the fallback."""
    if company:
        # Trim the legal suffix — "Zenas BioPharma" finds more than
        # "Zenas BioPharma, Inc." does.
        core = re.split(r",| Inc\.?$| Corp\.?$| Ltd\.?$| plc$", company)[0].strip()
        if len(core) >= 3:
            return f'"{core}"'
    return f'"{ticker}" stock'


def press_releases(ticker: str, company: str | None, start: datetime, end: datetime) -> list[dict]:
    """The company's own announcements, straight off the wires."""
    sites = " OR ".join(f"site:{d}" for d in WIRE_DOMAINS)
    items = _google_query(f"{_search_terms(ticker, company)} ({sites})", start, end, "newswire")
    log.debug("%s: %d wire release(s) in window", ticker, len(items))
    return items


def general_news(ticker: str, company: str | None, start: datetime, end: datetime) -> list[dict]:
    """Everything else Google News indexes for this company."""
    items = _google_query(_search_terms(ticker, company), start, end, "Google News")
    log.debug("%s: %d general headline(s) in window", ticker, len(items))
    return items


# ---------------------------------------------------------------------------
# Nasdaq trading halts
# ---------------------------------------------------------------------------

NASDAQ_HALTS = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
NDAQ_NS = "{http://www.nasdaqtrader.com/}"

HALT_REASONS = {
    "T1": "news pending — the company is about to announce something material",
    "T2": "news released — trading resumed after the announcement was disseminated",
    "T5": "single-stock trading pause (5% or more move in five minutes)",
    "T6": "extraordinary market activity — possible erroneous trades",
    "T8": "ETF component halt",
    "T12": "the exchange requested additional information from the company",
    "H4": "non-compliance with listing rules",
    "H9": "the company is not current in its required filings",
    "H10": "SEC trading suspension",
    "H11": "regulatory concern",
    "LUDP": "limit up / limit down volatility pause",
    "LUDS": "limit up / limit down volatility pause (straddle state)",
    "M": "volatility trading pause",
    "MWC1": "market-wide circuit breaker — level 1",
    "MWC2": "market-wide circuit breaker — level 2",
    "MWC3": "market-wide circuit breaker — level 3",
    "D": "operations halt (securities delivery)",
    "IPO1": "IPO not yet trading",
}
# Halts that say something about the company. A volatility pause is just the
# move itself tripping a circuit breaker — it explains nothing.
MEANINGFUL_HALTS = {"T1", "T2", "T12", "H4", "H9", "H10", "H11", "D"}

_halt_cache: dict | None = None


def _halt_field(item, name: str) -> str:
    """Read a child element by local name, whatever namespace it carries.

    The feed's fields live in a Nasdaq namespace, and the prefix has changed
    before. Matching on the local name survives that."""
    for child in item:
        if child.tag.rpartition("}")[2] == name and (child.text or "").strip():
            return child.text.strip()
    return ""


def parse_halts(xml: bytes) -> dict[str, list[dict]]:
    """Ticker -> halt records, from the Nasdaq trade-halt feed."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        log.warning("halt feed unparseable: %s", exc)
        return {}

    halts: dict[str, list[dict]] = {}
    for item in root.iter("item"):
        symbol = _halt_field(item, "IssueSymbol").upper()
        reason = _halt_field(item, "ReasonCode").upper()
        if not symbol or not reason:
            continue
        halts.setdefault(symbol, []).append({
            "reason_code": reason,
            "reason": HALT_REASONS.get(reason, f"halt code {reason}"),
            "date": _halt_field(item, "HaltDate"),
            "time": _halt_field(item, "HaltTime"),
            "resumption_date": _halt_field(item, "ResumptionDate"),
            "resumption_time": _halt_field(item, "ResumptionTradeTime"),
        })
    return halts


def trading_halts() -> dict[str, list[dict]]:
    """Today's halt feed, fetched once per process."""
    global _halt_cache
    if _halt_cache is not None:
        return _halt_cache
    try:
        resp = requests.get(NASDAQ_HALTS, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        _halt_cache = parse_halts(resp.content)
        log.info("Nasdaq halt feed: %d symbol(s) halted today", len(_halt_cache))
    except Exception as exc:  # noqa: BLE001
        log.warning("Nasdaq halt feed unavailable: %s", exc)
        _halt_cache = {}
    return _halt_cache


def halt_item(ticker: str, session_date: str | None = None) -> dict | None:
    """A halt on this ticker, as a catalyst-shaped record.

    The feed only carries the current day, so it is useful when the scan runs
    the morning after (the usual case) and silent otherwise.
    """
    records = [
        h for h in trading_halts().get(ticker.upper(), [])
        if h["reason_code"] in MEANINGFUL_HALTS
    ]
    if not records:
        return None
    if session_date:
        same = [h for h in records if _same_day(h.get("date"), session_date)]
        records = same or records
    halt = records[0]
    stamp = " ".join(x for x in (halt.get("date"), halt.get("time")) if x)
    back = (
        f" Trading resumed {halt['resumption_date']} {halt['resumption_time']} ET."
        if halt.get("resumption_date") else ""
    )
    return {
        "headline": f"Trading halted on Nasdaq ({halt['reason_code']}): {halt['reason']}.{back}",
        "summary": "",
        "source": "Nasdaq trade halts",
        "url": "https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts",
        "published": "",
        "age_hours": 0.0,
        "when": f"{stamp} ET" if stamp else "today",
        "tag": "Trading halt",
        "detail": None,
    }


def _same_day(raw: str | None, session_date: str) -> bool:
    """The feed dates are MM/DD/YYYY; the session is YYYY-MM-DD."""
    if not raw:
        return False
    try:
        month, day, year = raw.split("/")
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}" == session_date
    except ValueError:
        return False
