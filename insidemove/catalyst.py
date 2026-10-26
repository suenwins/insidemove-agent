"""Deciding which headlines actually explain a move.

The news feeds return everything *tagged* with a ticker, and for a stock
that just moved 40% most of that is noise about the move itself: market
roundups ("What's going on in today's session", "Most active stocks"),
mover lists, and "XYZ shares soar" stories that restate the price change
without saying why. None of it is a catalyst.

A headline survives only if it passes three tests:

1. **It is about this company.** The ticker or the company's name must
   appear in the headline or summary. A roundup that lists forty tickers
   is not coverage of any one of them.
2. **It is not a roundup or a mover list.** Those are rejected on the
   headline pattern, but a roundup sentence that names this company *and*
   a concrete event is salvaged — that is often the only place a small
   cap's reason appears.
3. **It names a reason, not just a price.** "Shares jump 30%" is dropped
   unless its summary carries an actual event (earnings, FDA, deal,
   offering…).

What survives is tagged with a catalyst type and ranked by how strongly
that type tends to move a stock, so the note leads with the explanation
rather than the freshest article.

SEC filings are a second, independent source. An 8-K is the company
telling the market something material, and its item codes say what —
2.02 is results, 1.01 a material agreement, 3.02 an unregistered share
sale. For small caps that the wires ignore, the filing is frequently the
only record of why the stock moved.
"""
from __future__ import annotations

import json
import logging
import re
import time as _time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from .config import CFG

log = logging.getLogger(__name__)
US_EASTERN = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Catalyst taxonomy — ordered strongest first. The order is the ranking.
# ---------------------------------------------------------------------------

CATEGORIES: list[tuple[str, re.Pattern]] = [
    ("M&A", re.compile(
        r"\b(acquir\w*|acquisition|merg(er|e|es|ing)|buyout|takeover|take-private|"
        r"go(ing)?[- ]private|tender offer|to be (acquired|bought)|deal to (buy|acquire)|"
        r"all-cash deal|per share in cash|strategic alternatives|definitive agreement)\b", re.I)),
    ("Clinical / FDA", re.compile(
        r"\b(FDA|EMA|PDUFA|approv(al|es|ed)|clearance|cleared|complete response letter|CRL|"
        r"phase (1|2|3|i|ii|iii)\b|pivotal|topline|top-line|trial (data|results)|"
        r"clinical (data|trial|hold)|breakthrough (therapy|device) designation|"
        r"fast track|orphan drug|primary endpoint|readout)\b", re.I)),
    ("Earnings", re.compile(
        r"\b(earnings|quarterly results|(first|second|third|fourth)[- ]quarter|"
        r"Q[1-4] (results|earnings|revenue|sales|loss|profit)|fiscal (20\d\d|q[1-4])|"
        r"EPS|revenue (beat|miss|rose|fell|grew|jumped|surged|declined)|"
        r"beats? (estimates|expectations)|miss(es|ed)? (estimates|expectations)|"
        r"reports? (results|profit|loss))\b", re.I)),
    ("Guidance", re.compile(
        r"\b(guidance|outlook|forecast|raises? (its )?(full[- ]year|annual)|"
        r"cuts? (its )?(full[- ]year|annual)|profit warning|preannounce\w*)\b", re.I)),
    ("Financing / dilution", re.compile(
        r"\b(public offering|private placement|registered direct|at-the-market|ATM program|"
        r"prices? (an? )?offering|priced|share sale|convertible (notes|senior notes|debentures)|"
        r"warrants?|dilut\w*|reverse (stock )?split|stock split|shelf registration)\b", re.I)),
    ("Legal / regulatory", re.compile(
        r"\b(lawsuit|sued|class action|SEC (probe|charges|investigation)|investigation|"
        r"subpoena|settle(ment|s|d)|DOJ|antitrust|FTC|recall|ban(ned)?|tariff|sanction\w*|"
        r"short[- ]seller|short report|fraud|delist\w*|Nasdaq (notice|deficiency|compliance)|"
        r"bankruptcy|chapter 11|restat(e|ement)|going concern|default)\b", re.I)),
    ("Contract / partnership", re.compile(
        r"\b(contract|awarded|award of|partnership|partners with|collaborat\w*|"
        r"licens(e|ing) (agreement|deal)|supply agreement|purchase order|"
        r"strategic (investment|agreement)|joint venture|selected by|wins? .{0,30}order)\b", re.I)),
    ("Analyst action", re.compile(
        r"\b(upgrade[sd]?|downgrade[sd]?|initiat(es|ed|ing) (coverage|at)|"
        r"price target|PT (raised|cut|to)|raises? (the )?target|cuts? (the )?target|"
        r"(outperform|underperform|overweight|underweight|buy rating|sell rating))\b", re.I)),
    ("Management", re.compile(
        r"\b(CEO|CFO|chief executive|chief financial|founder)\b.{0,40}"
        r"\b(resign\w*|steps? down|depart\w*|ousted|fired|appoint\w*|named|hired|retir\w*)\b"
        r"|\b(appoints?|names?) .{0,30}\b(CEO|CFO|chief executive)\b", re.I)),
    ("Capital return", re.compile(
        r"\b(buyback|share repurchase|repurchase program|special dividend|"
        r"dividend (hike|increase|cut|suspend\w*)|raises? (its )?dividend)\b", re.I)),
    ("Index change", re.compile(
        r"\b(join(s|ing)? the S&P|added to the (S&P|Nasdaq|Russell)|index (inclusion|addition)|"
        r"removed from the (S&P|index))\b", re.I)),
    ("Product / business", re.compile(
        r"\b(launch(es|ed)?|unveil\w*|introduc(es|ed)|rollout|expan(ds|sion) into|"
        r"record (revenue|sales|orders|deliveries)|deliveries|bookings|backlog|"
        r"layoffs?|restructuring|job cuts|plant closure)\b", re.I)),
]

# Market-wide pieces and mover lists. These describe the tape, not a company.
ROUNDUP = re.compile(
    r"(what'?s going on|what is going on|most active|top (gainers|losers|movers)|"
    r"biggest (movers|gainers|losers)|(market|stock|premarket|pre-market|after[- ]hours|"
    r"midday|mid-day|intraday) movers|stocks? (moving|on the move|to watch|making (the )?biggest moves)|"
    r"movers and shakers|gap(pers| ups?| downs?)|unusual (volume|options)|"
    r"\b\d+ (stocks|names)\b|these (stocks|names)|trending (stocks|tickers)|"
    r"stock market (today|news)|market (wrap|recap|update|close)|"
    r"(dow|s&p 500|nasdaq)( futures)? (rise|fall|climb|slip|edge|rall)|"
    r"sector (update|movers)|heat ?map|chart of the day|technical (analysis|outlook) for)",
    re.I,
)

# Headlines that only report the price move.
PRICE_ONLY = re.compile(
    r"\b(shares?|stock)\b.{0,40}\b(up|down|jump\w*|soar\w*|surg\w*|spik\w*|rall\w*|"
    r"skyrocket\w*|rocket\w*|plung\w*|plummet\w*|tank\w*|tumbl\w*|crater\w*|slid\w*|"
    r"sink\w*|sank|drop\w*|fall\w*|fell|climb\w*|gain\w*|trad(e|ing) (higher|lower)|"
    r"on the move|moving|halted)\b"
    r"|\bwhy (is|are|did) .{0,40}\b(stock|shares)\b"
    r"|\b(stock|shares) (is|are) (up|down|moving|soaring|sinking)\b",
    re.I,
)

GENERIC_NAME_WORDS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies", "ltd",
    "limited", "plc", "holdings", "holding", "group", "sa", "nv", "ag", "se", "lp",
    "llc", "class", "a", "b", "c", "the", "ordinary", "shares", "common", "stock",
    "adr", "ads", "trust", "and", "of", "de", "&",
}
# First words too common to identify a company on their own.
AMBIGUOUS_FIRST = {
    "american", "global", "first", "united", "national", "new", "general",
    "international", "advanced", "applied", "digital", "energy", "bio", "health",
    "healthcare", "capital", "financial", "real", "north", "south", "west", "east",
    "pacific", "atlantic", "western", "eastern", "great", "universal", "life",
    "medical", "power", "data", "royal", "standard", "smart", "blue", "green",
    "clear", "open", "next", "one", "star", "sun", "summit", "alpha", "beta",
}

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9$\"'(])")


def name_keys(company: str | None) -> list[str]:
    """Lower-case phrases that identify the company in running text."""
    if not company:
        return []
    core = re.sub(r"\(.*?\)", " ", company)
    core = re.sub(r"[^\w&' -]", " ", core)
    words = [w for w in core.split() if w.lower().strip("'") not in GENERIC_NAME_WORDS]
    if not words:
        return []
    keys = []
    if len(words) >= 2:
        keys.append(" ".join(words[:2]).lower())
    first = words[0].lower()
    if len(first) >= 4 and first not in AMBIGUOUS_FIRST:
        keys.append(first)
    elif len(words) == 1:
        keys.append(first)
    return keys


def mentions(text: str, ticker: str, keys: list[str]) -> bool:
    if not text:
        return False
    # Ticker must appear as a standalone upper-case token ("ACME", "$ACME",
    # "NASDAQ: ACME"), never as part of a word.
    if len(ticker) >= 2 and re.search(rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])", text):
        return True
    low = text.lower()
    return any(re.search(rf"\b{re.escape(k)}\b", low) for k in keys)


def classify(text: str) -> str | None:
    for name, pattern in CATEGORIES:
        if pattern.search(text or ""):
            return name
    return None


def rank_of(tag: str | None) -> int:
    order = [name for name, _ in CATEGORIES] + ["Trading halt", "SEC filing", "Company news"]
    return order.index(tag) if tag in order else len(order)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text or "") if s.strip()]


def _clip(text: str, n: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def assess(item: dict, ticker: str, company: str | None) -> dict | None:
    """Return the item tagged with its catalyst type, or None if it does not
    explain the move."""
    keys = name_keys(company)
    headline = item.get("headline") or ""
    summary = item.get("summary") or ""

    if ROUNDUP.search(headline):
        # Salvage: the one sentence in the roundup that is about this company
        # and names an event.
        for sentence in _sentences(summary):
            if mentions(sentence, ticker, keys):
                tag = classify(sentence)
                if tag:
                    return {**item, "headline": _clip(sentence), "tag": tag,
                            "source": f"{item.get('source') or 'roundup'} roundup",
                            "detail": None}
        return None

    if not (mentions(headline, ticker, keys) or mentions(summary, ticker, keys)):
        return None

    head_tag = classify(headline)
    if head_tag:
        return {**item, "tag": head_tag, "detail": None}

    # The headline itself names no event. Look for one in the summary,
    # preferring the sentence that talks about this company.
    sentences = _sentences(summary)
    ordered = [s for s in sentences if mentions(s, ticker, keys)] + sentences
    for sentence in ordered:
        tag = classify(sentence)
        if tag:
            return {**item, "tag": tag, "detail": _clip(sentence, 180)}

    if PRICE_ONLY.search(headline):
        return None           # restates the move, gives no reason
    return {**item, "tag": "Company news", "detail": None}


def filter_relevant(items: list[dict], ticker: str, company: str | None) -> list[dict]:
    kept = [a for a in (assess(i, ticker, company) for i in items) if a]
    # Strongest catalyst type first; within a type, freshest first.
    kept.sort(key=lambda i: (rank_of(i["tag"]), i.get("age_hours", 0)))
    return kept


# ---------------------------------------------------------------------------
# SEC EDGAR — the company's own disclosures. Free, no key.
# ---------------------------------------------------------------------------

EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers.json"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
CIK_CACHE_DAYS = 7

ITEM_8K = {
    "1.01": ("Contract / partnership", "entered a material definitive agreement"),
    "1.02": ("Contract / partnership", "terminated a material agreement"),
    "1.03": ("Legal / regulatory", "bankruptcy or receivership"),
    "2.01": ("M&A", "completed an acquisition or disposition of assets"),
    "2.02": ("Earnings", "results of operations (earnings release)"),
    "2.03": ("Financing / dilution", "took on a material debt obligation"),
    "2.05": ("Product / business", "restructuring / exit costs"),
    "2.06": ("Legal / regulatory", "material impairment"),
    "3.01": ("Legal / regulatory", "exchange delisting notice or listing-rule failure"),
    "3.02": ("Financing / dilution", "unregistered sale of equity (new shares issued)"),
    "3.03": ("Financing / dilution", "modified shareholder rights (often a reverse split)"),
    "4.02": ("Legal / regulatory", "prior financials no longer reliable (restatement)"),
    "5.01": ("M&A", "change in control"),
    "5.02": ("Management", "director or officer departure / appointment"),
    "5.03": ("Financing / dilution", "charter or bylaw amendment (often a reverse split)"),
    "5.07": ("Company news", "shareholder vote results"),
    "7.01": ("Company news", "Reg FD disclosure (press release or presentation)"),
    "8.01": ("Company news", "other material event (usually a press release)"),
}
# Exhibits and auditor changes explain nothing about a price move.
IGNORED_ITEMS = {"9.01", "4.01", "5.05", "5.08"}

OTHER_FORMS = {
    "6-K": ("SEC filing", "6-K report from a foreign issuer"),
    "S-1": ("Financing / dilution", "S-1 registration — shares being registered for sale"),
    "S-3": ("Financing / dilution", "S-3 shelf registration"),
    "F-1": ("Financing / dilution", "F-1 registration — shares being registered for sale"),
    "F-3": ("Financing / dilution", "F-3 shelf registration"),
    "424B": ("Financing / dilution", "prospectus — an offering has been priced"),
    "SC 13D": ("M&A", "an investor disclosed an active stake above 5%"),
    "SCHEDULE 13D": ("M&A", "an investor disclosed an active stake above 5%"),
    "SC TO-T": ("M&A", "third-party tender offer"),
    "SC TO-C": ("M&A", "tender offer communication"),
    "425": ("M&A", "merger communication"),
    "DEFM14A": ("M&A", "merger proxy"),
    "PREM14A": ("M&A", "preliminary merger proxy"),
    "NT 10-K": ("Legal / regulatory", "late annual report notice"),
    "NT 10-Q": ("Legal / regulatory", "late quarterly report notice"),
}


def _user_agent() -> str:
    contact = (CFG.sec_contact or "").strip()
    return f"InsideMoveAgent/1.0 {contact}".strip() if contact else (
        "InsideMoveAgent/1.0 (personal research bot; github.com/suenwins/insidemove-agent)"
    )


def _cik_map() -> dict[str, int]:
    path = CFG.data_dir / "sec_tickers.json"
    try:
        if path.exists() and _time.time() - path.stat().st_mtime < CIK_CACHE_DAYS * 86400:
            return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        pass
    try:
        resp = requests.get(EDGAR_TICKERS, headers={"User-Agent": _user_agent()}, timeout=20)
        resp.raise_for_status()
        mapping = {
            str(row["ticker"]).upper(): int(row["cik_str"])
            for row in resp.json().values()
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("SEC ticker map unavailable: %s", exc)
        return {}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mapping))
    except Exception:  # noqa: BLE001
        pass
    return mapping


def _form_meta(form: str, items: str) -> list[tuple[str, str]]:
    """(tag, plain-English description) pairs for one filing."""
    if form.startswith("8-K"):
        out = []
        for code in [c.strip() for c in (items or "").split(",") if c.strip()]:
            if code in IGNORED_ITEMS:
                continue
            if code in ITEM_8K:
                out.append(ITEM_8K[code])
        return out
    for prefix, meta in OTHER_FORMS.items():
        if form == prefix or form.startswith(prefix + "/") or (prefix == "424B" and form.startswith("424B")):
            return [meta]
    return []


def parse_filings(sub: dict, cik: int, start_day: date, end_day: date) -> list[dict]:
    """Material filings dated inside [start_day, end_day] from an EDGAR
    submissions document."""
    recent = (sub.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    out = []
    for i, form in enumerate(forms):
        try:
            filed = date.fromisoformat(recent["filingDate"][i])
        except (KeyError, IndexError, ValueError):
            continue
        if filed > end_day:
            continue
        if filed < start_day:
            break                          # newest-first; nothing older can qualify
        metas = _form_meta(form, (recent.get("items") or [""] * len(forms))[i])
        if not metas:
            continue
        # EDGAR stamps acceptance time in US Eastern despite the trailing "Z".
        accepted = None
        raw = (recent.get("acceptanceDateTime") or [None] * len(forms))[i]
        if raw:
            try:
                accepted = datetime.fromisoformat(raw.replace("Z", "")[:19]).replace(tzinfo=US_EASTERN)
            except ValueError:
                accepted = None
        accession = (recent.get("accessionNumber") or [""] * len(forms))[i]
        doc = (recent.get("primaryDocument") or [""] * len(forms))[i]
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{doc}"
            if accession and doc else ""
        )
        # Lead with the most informative item on the filing.
        metas.sort(key=lambda m: rank_of(m[0]))
        tag = metas[0][0] if metas[0][0] != "Company news" else "SEC filing"
        desc = "; ".join(m[1] for m in metas)
        when_dt = accepted or datetime.combine(filed, datetime.min.time(), tzinfo=US_EASTERN)
        out.append({
            "headline": f"Form {form} filed: {desc}",
            "summary": "",
            "source": "SEC EDGAR",
            "url": url,
            "published": when_dt.isoformat(),
            "age_hours": round((datetime.now(US_EASTERN) - when_dt).total_seconds() / 3600, 1),
            "when": when_dt.strftime("%d %b %H:%M ET") if accepted else filed.strftime("%d %b"),
            "tag": tag,
            "detail": None,
            "filing": True,
        })
    return out


def sec_filings(ticker: str, start: datetime, end: datetime) -> list[dict]:
    if not CFG.sec_filings_enabled:
        return []
    cik = _cik_map().get(ticker.upper().replace(".", "-"))
    if not cik:
        return []
    try:
        resp = requests.get(
            EDGAR_SUBMISSIONS.format(cik=cik),
            headers={"User-Agent": _user_agent()},
            timeout=20,
        )
        resp.raise_for_status()
        sub = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("SEC filings unavailable for %s: %s", ticker, exc)
        return []
    start_day = start.astimezone(US_EASTERN).date()
    end_day = end.astimezone(US_EASTERN).date()
    return parse_filings(sub, cik, start_day, end_day)
