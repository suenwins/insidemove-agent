#!/usr/bin/env python3
"""Offline self-test — no network, no API keys required.

Run this after install to confirm the maths, the signal engine and the chart
renderer all work on the machine before you point real money-adjacent
decisions at it:

    python3 selftest.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DRY_RUN", "1")

from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from insidemove import charting, indicators, signals  # noqa: E402
from insidemove.config import CFG  # noqa: E402

PASS, FAIL = "  ✓", "  ✗"
failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    print(f"{PASS if condition else FAIL} {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures += 1


def synth(seed=7, n=300, drift=0.0004, vol=0.02, shock=None, vol_x=1.0, wick=None):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end="2026-09-04", periods=n)
    ret = rng.normal(drift, vol, n)
    if shock is not None:
        ret[-1] = shock
    close = 100 * np.exp(np.cumsum(ret))
    op = close * (1 + rng.normal(0, vol / 3, n))
    hi = np.maximum(op, close) * (1 + abs(rng.normal(0, vol / 3, n)))
    lo = np.minimum(op, close) * (1 - abs(rng.normal(0, vol / 3, n)))
    v = rng.lognormal(15, 0.25, n)
    v[-1] *= vol_x
    if wick == "lower":
        lo[-1] = min(op[-1], close[-1]) * 0.90
    return pd.DataFrame(
        {"Open": op, "High": hi, "Low": lo, "Close": close, "Volume": v}, index=dates
    )


def main() -> int:
    session = pd.Timestamp("2026-09-04")

    print("\nIndicator maths")
    # Wilder's published RSI worked example.
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
              46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03]
    r = indicators.rsi(pd.Series(closes), 14)
    check("RSI(14) matches Wilder's reference", abs(r.iloc[14] - 70.46) < 0.05,
          f"got {r.iloc[14]:.2f}, expected 70.46")
    check("RSI second value matches", abs(r.iloc[15] - 66.25) < 0.05,
          f"got {r.iloc[15]:.2f}, expected 66.25")
    check("flat series is neutral, not 100", indicators.rsi(pd.Series([5.0] * 40), 14).iloc[-1] == 50.0)
    check("monotonic rise pins RSI at 100",
          indicators.rsi(pd.Series(np.arange(1, 40, dtype=float)), 14).iloc[-1] == 100.0)

    df = synth(shock=-0.104, vol_x=4.0)
    a = indicators.atr(df, 14)
    tr = indicators.true_range(df).to_numpy()
    ref = tr[0:14].mean()
    for i in range(14, len(tr)):
        ref = (ref * 13 + tr[i]) / 14
    check("ATR(14) matches Wilder RMA convention", abs(a.iloc[-1] - ref) < 1e-9,
          f"delta {abs(a.iloc[-1]-ref):.2e}")

    print("\nSignal engine")
    c = signals.evaluate("TEST1", df, session)
    check("a >7% move is flagged primary", c is not None and c.tier == "primary",
          f"{c.pct_change:+.2f}%, tags={c.tags}" if c else "no candidate")

    df2 = synth(seed=11, shock=-0.045, vol_x=6.0, wick="lower")
    c2 = signals.evaluate("TEST2", df2, session)
    check("sub-threshold move with 2+ signals is flagged secondary",
          c2 is not None and c2.tier == "secondary",
          f"{c2.pct_change:+.2f}%, tags={c2.tags}" if c2 else "no candidate")

    quiet = signals.evaluate("TEST3", synth(seed=3, shock=0.004), session)
    check("a quiet session produces nothing", quiet is None)

    stale = signals.evaluate("TEST4", df, pd.Timestamp("2026-09-05"))
    check("a ticker whose last bar is not the scan session is skipped", stale is None)

    thin = synth(seed=5, shock=-0.10)
    thin["Volume"] = 100  # far below the dollar-volume floor
    check("illiquid names are filtered out", signals.evaluate("THIN", thin, session) is None)

    print("\nSnapshot and chart")
    snap = indicators.analyse("TEST1", df, CFG.sma_fast, CFG.sma_slow,
                              CFG.atr_period, CFG.rsi_period, CFG.chart_days)
    check("snapshot % change agrees with the signal engine",
          c is not None and abs(snap.pct_change - c.pct_change) < 0.01)
    check("RSI in range", 0 <= snap.rsi <= 100, f"{snap.rsi}")
    check("ATR positive", snap.atr > 0, f"{snap.atr}")
    check("levels found", bool(snap.support or snap.resistance or snap.implied_support))

    path = charting.render("TEST1", df, snap, out_dir=CFG.cache_dir)
    size = path.stat().st_size
    check("chart renders", size > 20_000, f"{size:,} bytes at {path}")

    print("\nHTML sanitiser")
    from insidemove.ai import sanitise_html
    got = sanitise_html("AT&T <b>rose</b> <script>x</script> 5>3")
    check("keeps allowed tags, escapes the rest",
          "<b>rose</b>" in got and "&lt;script&gt;" in got and "&amp;" in got, got)

    print("\nTemplate engine (no API, no key)")
    import re
    from insidemove import notes, templates

    heads = [{"headline": "Acme cuts full-year guidance", "summary": "", "source": "Reuters",
              "url": "", "published": "", "age_hours": 6.2}]
    payload = {
        "ticker": "TEST1", "company": "Acme Robotics & Co", "sector": "Technology",
        "session": {"as_of": snap.as_of, "close": snap.close, "prev_close": snap.prev_close,
                    "pct_change": snap.pct_change, "gap_pct": snap.gap_pct,
                    "volume": snap.volume, "avg_volume_20": snap.avg_volume_20,
                    "volume_ratio": snap.volume_ratio},
        "valuation": {"trailing_pe": 24.3},
        "analyst": {"analyst_count": 14},
        "discount_to_target": {"target_mean": 58.40, "abs": 12.41, "pct": 27.0},
        "insider_transactions_6m": [{"name": "J. Ortiz", "action": "open-market buy", "code": "P",
                                     "shares_changed": 25000, "value": 1149750,
                                     "date": "2026-09-02", "routine": False}],
        "peers_same_session": [{"ticker": "BQR", "pct_change": -1.2},
                               {"ticker": "ZLT", "pct_change": 0.4}],
        "news_last_72h": heads,
        "technical_context": {"rsi": snap.rsi, "trend": snap.trend,
                              "pct_from_52w_high": snap.pct_from_52w_high,
                              "pct_from_52w_low": snap.pct_from_52w_low},
    }

    rendered = {
        "catalyst": notes.catalyst_line("TEST1", {"pct_change": snap.pct_change,
                                                  "prev_close": snap.prev_close,
                                                  "close": snap.close,
                                                  "volume_ratio": snap.volume_ratio}, heads),
        "equity": notes.equity_note(payload),
        "technical": notes.technical_note(snap.to_dict()),
    }

    check("template engine is the default", notes.ENGINE == "template", notes.ENGINE)
    check("no Anthropic key required", not notes.requires_anthropic_key())

    ALLOWED = {"b", "i", "code", "pre", "u", "s", "a"}
    for name, text in rendered.items():
        tags = re.findall(r"</?([a-zA-Z]+)[^>]*>", text)
        bad = sorted(set(t.lower() for t in tags) - ALLOWED)
        opens = [t.lower() for t in re.findall(r"<([a-zA-Z]+)>", text)]
        closes = [t.lower() for t in re.findall(r"</([a-zA-Z]+)>", text)]
        balanced = sorted(opens) == sorted(closes)
        check(f"{name}: only Telegram-safe tags", not bad, ",".join(bad) or "clean")
        check(f"{name}: tags balanced", balanced,
              f"{len(opens)} open / {len(closes)} close")

    check("equity note labels FACT and INFERENCE",
          "<b>FACT:</b>" in rendered["equity"] and "<b>INFERENCE:</b>" in rendered["equity"])
    check("technical note has all 8 sections",
          all(f"<b>{i}." in rendered["technical"] for i in range(1, 9)))
    check("technical note quotes only computed levels",
          f"{snap.close:,.2f}" in rendered["technical"] and str(snap.atr) in rendered["technical"])
    check("ampersand in a company name is escaped",
          "&amp;" in templates.esc("Acme Robotics & Co"))

    peer_read = templates._peer_read(payload)
    check("peer classification is shared by section 4 and the verdict",
          peer_read is not None and peer_read[0] in {"sector", "mixed", "idiosyncratic"},
          peer_read[0] if peer_read else "none")

    empty = notes.equity_note({"session": {"pct_change": -9.0, "volume_ratio": 1.0}})
    check("equity note survives a payload with nothing in it", len(empty) > 400)
    check("missing data is reported, not invented",
          "No Form 4 filings" in empty and "No peer data" in empty)

    print("\nCallback payloads (no database)")
    from insidemove import callbacks

    payload = callbacks.encode("approve", "BRK-B", "2026-09-04", "secondary")
    back = callbacks.decode(payload)
    check("round-trips verb, ticker, session and tier",
          back == {"verb": "approve", "ticker": "BRK-B",
                   "session_date": "2026-09-04", "tier": "secondary"}, payload)
    check("fits Telegram's 64-byte limit with room to spare",
          len(payload.encode()) <= 24, f"{len(payload.encode())} bytes")
    rejected = 0
    for bad in ["", "x:AAPL:20260904", "a:AAPL", "a:AAPL:2026-09-04", "a:!!:20260904"]:
        try:
            callbacks.decode(bad)
        except callbacks.BadCallback:
            rejected += 1
    check("rejects every malformed payload", rejected == 5, f"{rejected}/5")

    print("\nPrice cache")
    import shutil
    import tempfile
    from insidemove import cache

    scratch = Path(tempfile.mkdtemp())
    try:
        bars = {t: synth(seed=s, n=320) for t, s in (("AAA", 1), ("BBB", 2))}
        real_fetch = cache.data.fetch_history
        cache.data.fetch_history = lambda tickers, period="1y", workers=4: {
            t: bars[t].tail(300 if period == "1y" else 22) for t in tickers if t in bars
        }

        c1 = cache.PriceCache(scratch / "p.parquet")
        first = c1.update(["AAA", "BBB"])
        c1.save()
        check("first run downloads full history", first["cold"] == 2 and first["warm"] == 0)

        for t in bars:
            last = bars[t].index[-1]
            bars[t] = pd.concat([bars[t], pd.DataFrame(
                {"Open": [100.0], "High": [101.0], "Low": [99.0],
                 "Close": [100.5], "Volume": [1e6]},
                index=[last + pd.Timedelta(days=1)])])

        c2 = cache.PriceCache(scratch / "p.parquet")
        before = len(c2.load())
        second = c2.update(["AAA", "BBB"])
        c2.save()
        added = len(c2.load()) - before
        check("second run refreshes instead of re-downloading",
              second["cold"] == 0 and second["warm"] == 2)
        check("appends exactly the missing bars", added == 2, f"{added} rows for 2 tickers")

        # A split restates every historical bar; appending blindly would leave
        # pre- and post-split prices side by side.
        bars["BBB"] = bars["BBB"].assign(
            **{col: bars["BBB"][col] / 4 for col in ["Open", "High", "Low", "Close"]})
        c3 = cache.PriceCache(scratch / "p.parquet")
        third = c3.update(["AAA", "BBB"])
        c3.save()
        check("detects a 4:1 split and refetches that ticker", third["restated"] == 1)
        check("purges the pre-split history",
              c3.frames(["BBB"])["BBB"]["Close"].max() < 40,
              f"max close {c3.frames(['BBB'])['BBB']['Close'].max():.2f}")
        check("frames() returns usable OHLCV",
              set(c3.frames(["AAA"])["AAA"].columns) == set(cache.COLUMNS))
    finally:
        cache.data.fetch_history = real_fetch
        shutil.rmtree(scratch, ignore_errors=True)

    print("\nSession completeness guard")
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo
    from insidemove import data as _data

    ET = ZoneInfo("America/New_York")
    for label, when, expect in [
        ("market open at 11:14 ET", _dt(2026, 9, 8, 11, 14, tzinfo=ET), "2026-09-07"),
        ("one minute before close", _dt(2026, 9, 8, 15, 59, tzinfo=ET), "2026-09-07"),
        ("settled after the close", _dt(2026, 9, 8, 16, 25, tzinfo=ET), "2026-09-08"),
        ("15:00 MYT scan window", _dt(2026, 9, 9, 3, 0, tzinfo=ET), "2026-09-08"),
    ]:
        check(f"{label} -> {expect}",
              _data.latest_completed_session(when).isoformat() == expect,
              _data.latest_completed_session(when).isoformat())

    idx = pd.bdate_range(end="2026-09-08", periods=40)
    partial = pd.DataFrame(
        {"Open": [10.0] * 40, "High": [11.0] * 40, "Low": [9.0] * 40,
         "Close": [10.0] * 39 + [12.4],            # +24% intraday
         "Volume": [1_000_000] * 39 + [180_000]},  # on 0.2x volume
        index=idx)
    trimmed, dropped = _data.drop_incomplete_sessions(
        {"SLE": partial}, ceiling=pd.Timestamp("2026-09-07").date())
    check("an in-progress bar is trimmed away", dropped == 1)
    check("the impossible 24%-on-0.2x-volume bar is gone",
          trimmed["SLE"]["Close"].iloc[-1] == 10.0)
    kept, none_dropped = _data.drop_incomplete_sessions(
        {"SLE": partial}, ceiling=pd.Timestamp("2026-09-08").date())
    check("after the close nothing is dropped",
          none_dropped == 0 and len(kept["SLE"]) == 40)

    print("\nNews window anchoring")
    from datetime import timezone as _tz
    from insidemove import news as _news

    start, end = _news.session_window("2026-09-04", 24)
    check("window straddles the session, not today",
          start.date().isoformat() == "2026-09-04" or start.date().isoformat() == "2026-09-03",
          f"{start:%Y-%m-%d %H:%M}Z to {end:%Y-%m-%d %H:%M}Z")
    check("window closes after that session, not now",
          end < _dt.now(_tz.utc) - _td(days=1),
          f"ends {end:%Y-%m-%d %H:%M}Z")
    check("window is exactly the configured length",
          abs((end - start).total_seconds() / 3600 - 24) < 0.01)
    live_start, live_end = _news.session_window(None, 24)
    check("with no session given it falls back to trailing-now",
          (live_end - _dt.now(_tz.utc)).total_seconds() < 5)

    print("\nCatalyst relevance filter")
    from insidemove import catalyst as _cat
    from datetime import date as _date

    def _h(headline, summary="", source="ChartMill", age=10):
        return {"headline": headline, "summary": summary, "source": source,
                "url": "", "published": "", "age_hours": age, "when": f"{age}h ago"}

    noise = [
        _h("What's going on in today's after hours session"),
        _h("What's going on in today's session"),
        _h("Most active stocks in Monday's session"),
    ]
    check("market roundups are rejected",
          _cat.filter_relevant(noise, "ZBIO", "Zenas BioPharma, Inc.") == [])
    check("price-only headline with no reason is rejected",
          _cat.filter_relevant([_h("Zenas BioPharma stock soars 179%", "Shares rocketed.")],
                               "ZBIO", "Zenas BioPharma, Inc.") == [])
    check("other companies' news is rejected",
          _cat.filter_relevant([_h("Nvidia beats estimates", source="Reuters")],
                               "ZBIO", "Zenas BioPharma, Inc.") == [])
    salvaged = _cat.filter_relevant(
        [_h("Most active stocks in Monday's session",
            "Apple slipped. Zenas BioPharma (ZBIO) jumped after positive Phase 3 topline results.")],
        "ZBIO", "Zenas BioPharma, Inc.")
    check("roundup sentence naming the company and an event is salvaged",
          len(salvaged) == 1 and salvaged[0]["tag"] == "Clinical / FDA", str(salvaged))
    why = _cat.filter_relevant(
        [_h("Why Zenas BioPharma shares are skyrocketing today",
            "The company said obexelimab met the primary endpoint.", source="Motley Fool")],
        "ZBIO", "Zenas BioPharma, Inc.")
    check("'why is it up' article kept when its summary names the reason",
          len(why) == 1 and why[0]["tag"] == "Clinical / FDA" and why[0]["detail"])
    ranked = _cat.filter_relevant(
        [_h("Acme to present at investor conference", source="PR", age=2),
         _h("Acme agrees to be acquired by Globex for $40 per share in cash", source="Reuters", age=9)],
        "ACME", "Acme Robotics Inc.")
    check("stronger catalyst type ranks above fresher filler",
          [r["tag"] for r in ranked] == ["M&A", "Company news"], str([r["tag"] for r in ranked]))
    sub = {"filings": {"recent": {
        "form": ["8-K", "424B5", "4", "8-K"],
        "filingDate": ["2026-09-08", "2026-09-08", "2026-09-08", "2026-09-01"],
        "items": ["2.02,9.01", "", "", "1.01"],
        "acceptanceDateTime": ["2026-09-08T07:02:11.000Z", "", "", ""],
        "accessionNumber": ["0000001-26-000001", "0000001-26-000002", "x", "y"],
        "primaryDocument": ["a.htm", "b.htm", "c", "d"]}}}
    filings = _cat.parse_filings(sub, 42, _date(2026, 9, 7), _date(2026, 9, 8))
    check("8-K item 2.02 read as an earnings release",
          filings and filings[0]["tag"] == "Earnings", str(filings[:1]))
    check("424B prospectus read as financing / dilution",
          any(f["tag"] == "Financing / dilution" for f in filings))
    check("Form 4 and out-of-window filings ignored", len(filings) == 2, str(len(filings)))
    empty = templates.catalyst_line("ZBIO", {}, [], report={
        "rejected": 3, "hours": 48,
        "counts": {"news API": 3, "press wires": 0, "Google News": 0},
        "sec_available": False})
    check("no-catalyst line says how many noise headlines were ignored", "3 market-roundup" in empty)
    check("no-catalyst line names the sources that were searched",
          "press wires" in empty and "Google News" in empty, empty)
    check("no-catalyst line flags SEC being unreachable", "SEC_CONTACT" in empty, empty)
    from insidemove import catalyst as _c2
    check("SEC availability is reported, not silently assumed",
          _c2.sec_available() is True)
    check("EDGAR User-Agent carries a contact address when configured",
          "(" in _c2._user_agent() if _c2.CFG.sec_contact else _c2._user_agent() == "InsideMoveAgent/1.0",
          _c2._user_agent())

    # Regressions taken straight from a live run's output.
    peer = _cat.filter_relevant(
        [_h("Genmab stock surges 9% following positive phase 3 ovarian cancer trial results",
            "Shares of Genmab rose. Rival Corcept Therapeutics fell on the read-across.",
            source="Yahoo")],
        "CORT", "Corcept Therapeutics Incorporated")
    check("another company's news is labelled a peer read-across, not this ticker's own",
          len(peer) == 1 and peer[0]["tag"] == "Peer / sector news"
          and "another company" in peer[0]["source"], str(peer))
    check("the peer line quotes the sentence about this company",
          peer and "Corcept" in (peer[0]["detail"] or ""), str(peer[:1]))
    salvage_event = _cat.filter_relevant(
        [_h("Ovarian cancer readouts shake the sector",
            "Genmab rose. Corcept Therapeutics said its own phase 3 trial missed the primary endpoint.",
            source="Reuters")],
        "CORT", "Corcept Therapeutics Incorporated")
    check("an event attributed to this company in the body wins over the peer framing",
          salvage_event and salvage_event[0]["tag"] == "Clinical / FDA"
          and "Corcept" in salvage_event[0]["headline"], str(salvage_event[:1]))
    check("a peer story with nothing about this company is dropped",
          _cat.filter_relevant(
              [_h("Genmab stock surges 9% following positive phase 3 trial results",
                  "Genmab said the trial met its endpoint.", source="Yahoo")],
              "CORT", "Corcept Therapeutics Incorporated") == [])
    check("Yahoo quote page is rejected as boilerplate",
          _cat.filter_relevant(
              [_h("Exascale Labs Holdings Inc. (XLAB) stock price, news, quote and history",
                  "", source="Yahoo Finance Australia")],
              "XLAB", "Exascale Labs Holdings Inc.") == [])
    for junk in ("XLAB stock forecast 2027", "Should you buy BIO-key International stock?",
                 "BIO-key International institutional ownership data",
                 "LKQ Corporation earnings call transcript"):
        check(f"SEO filler rejected: {junk[:38]}",
              _cat.filter_relevant([_h(junk, "", source="MarketBeat")],
                                   "XLAB", "Exascale Labs Holdings Inc.") == [])
    check("a bare press release with no keyword is still kept",
          len(_cat.filter_relevant(
              [_h("BIO-key International Issues Shareholder Letter", "",
                  source="GlobeNewswire")],
              "BKYI", "BIO-key International, Inc.")) == 1)
    check("the same bare headline from an aggregator is dropped",
          _cat.filter_relevant(
              [_h("BIO-key International Issues Shareholder Letter", "",
                  source="Simply Wall St")],
              "BKYI", "BIO-key International, Inc.") == [])
    wire_first = _cat.filter_relevant(
        [_h("Acme wins $40M supply contract", "", source="Reuters", age=2),
         _h("Acme Announces $40 Million Supply Agreement", "", source="PR Newswire", age=8)],
        "ACME", "Acme Robotics Inc.")
    check("the company's own wire release outranks the second-hand report",
          "Newswire" in wire_first[0]["source"], str([w["source"] for w in wire_first]))

    check("an earnings preview is not a catalyst",
          _cat.filter_relevant(
              [_h("Nautilus Biotechnology, Inc. expected to post a loss of 14 cents a share "
                  "- Earnings Preview", "", source="TradingView")],
              "NAUT", "Nautilus Biotechnology, Inc.") == [])
    check("'what to expect ahead of earnings' is not a catalyst",
          _cat.filter_relevant([_h("LKQ: here's what to expect ahead of earnings", "",
                                   source="Zacks")], "LKQ", "LKQ Corporation") == [])
    check("the actual result is still kept",
          len(_cat.filter_relevant(
              [_h("LKQ reports Q3 results, revenue misses estimates", "", source="Reuters")],
              "LKQ", "LKQ Corporation")) == 1)
    opposite = _cat.filter_relevant(
        [_h("XLAB Stock Surges On $8.2M NVIDIA GPU Server Contract", "",
            source="StocksToTrade")],
        "XLAB", "Exascale Labs Holdings Inc.", move=-28.9)
    check("a catalyst describing the opposite direction is flagged, not asserted",
          opposite and opposite[0]["conflicts"] is True, str(opposite[:1]))
    check("the flag appears in the rendered line",
          "⚠️" in templates.catalyst_item(opposite[0], link=False),
          templates.catalyst_item(opposite[0], link=False))
    same_dir = _cat.filter_relevant(
        [_h("XLAB Stock Surges On $8.2M NVIDIA GPU Server Contract", "", source="StocksToTrade"),
         _h("XLAB cuts full-year guidance", "", source="Reuters", age=20)],
        "XLAB", "Exascale Labs Holdings Inc.", move=-28.9)
    check("a same-direction catalyst outranks a conflicting one",
          same_dir[0]["tag"] == "Guidance", str([(d["tag"], d["conflicts"]) for d in same_dir]))

    print("\nFree catalyst feeds (no key)")
    from insidemove import feeds as _feeds
    from datetime import datetime as _dt, timezone as _tz

    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Zenas BioPharma Announces Positive Phase 3 Results - GlobeNewswire</title>
        <link>https://example.com/a</link>
        <pubDate>Mon, 08 Sep 2026 11:30:00 GMT</pubDate>
        <description>&lt;p&gt;Obexelimab met the primary endpoint.&lt;/p&gt;</description></item>
      <item><title>Old news from last month - Reuters</title>
        <link>https://example.com/b</link>
        <pubDate>Fri, 08 Aug 2026 11:30:00 GMT</pubDate>
        <description>x</description></item>
    </channel></rss>"""
    import xml.etree.ElementTree as _ET
    parsed = [
        {"title": (i.findtext("title") or "").strip(),
         "description": _feeds._text(i.findtext("description")),
         "link": i.findtext("link") or "", "source": "",
         "published": _feeds._parse_when(i.findtext("pubDate")), "raw": i}
        for i in _ET.fromstring(rss).iter("item")
    ]
    first = _feeds._as_news(parsed[0], "newswire")
    check("RSS title splits headline from publisher",
          first["headline"].endswith("Phase 3 Results") and first["source"] == "GlobeNewswire",
          f"{first['headline']} / {first['source']}")
    check("RSS description is stripped of HTML", "<p>" not in first["summary"])
    win_start = _dt(2026, 9, 8, 0, 0, tzinfo=_tz.utc)
    win_end = _dt(2026, 9, 9, 0, 0, tzinfo=_tz.utc)
    in_window = [p for p in parsed
                 if p["published"] and win_start <= p["published"] <= win_end]
    check("out-of-window feed items are excluded", len(in_window) == 1)
    check("wire search is restricted to press-release domains",
          all(d in " OR ".join(f"site:{x}" for x in _feeds.WIRE_DOMAINS)
              for d in ("globenewswire.com", "prnewswire.com", "businesswire.com")))
    check("company name drives the query, not just the ticker",
          _feeds._search_terms("ZBIO", "Zenas BioPharma, Inc.") == '"Zenas BioPharma"',
          _feeds._search_terms("ZBIO", "Zenas BioPharma, Inc."))
    check("query falls back to the ticker with no company name",
          "ZBIO" in _feeds._search_terms("ZBIO", None))

    halt_xml = b"""<?xml version="1.0"?>
    <rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>
      <item><title>ZBIO</title>
        <ndaq:IssueSymbol>ZBIO</ndaq:IssueSymbol>
        <ndaq:ReasonCode>T1</ndaq:ReasonCode>
        <ndaq:HaltDate>09/08/2026</ndaq:HaltDate>
        <ndaq:HaltTime>09:14:00</ndaq:HaltTime>
        <ndaq:ResumptionDate>09/08/2026</ndaq:ResumptionDate>
        <ndaq:ResumptionTradeTime>09:45:00</ndaq:ResumptionTradeTime></item>
      <item><title>NOIS</title>
        <ndaq:IssueSymbol>NOIS</ndaq:IssueSymbol>
        <ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
        <ndaq:HaltDate>09/08/2026</ndaq:HaltDate>
        <ndaq:HaltTime>10:02:00</ndaq:HaltTime></item>
    </channel></rss>"""
    halts = _feeds.parse_halts(halt_xml)
    check("halt feed parsed despite its namespace", set(halts) == {"ZBIO", "NOIS"}, str(list(halts)))
    _feeds._halt_cache = halts
    news_halt = _feeds.halt_item("ZBIO", "2026-09-08")
    check("news-pending halt becomes a catalyst record",
          news_halt and news_halt["tag"] == "Trading halt" and "news pending" in news_halt["headline"])
    check("halt record carries the resumption time",
          news_halt and "09:45:00" in news_halt["headline"], str(news_halt))
    check("per-ticker Yahoo feed is queried by symbol, not by name",
          "s=BKYI" in _feeds.YAHOO_TICKER_RSS.format(ticker="BKYI"))
    check("volatility pause is not treated as a catalyst",
          _feeds.halt_item("NOIS", "2026-09-08") is None)
    _feeds._halt_cache = {}

    print("\nBootstrap without a chat id")
    import importlib
    import insidemove.config as _cfgmod

    saved = os.environ.get("TELEGRAM_CHAT_ID")
    os.environ["TELEGRAM_CHAT_ID"] = ""
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
    try:
        importlib.reload(_cfgmod)
        # bot.py must be able to start on the token alone, or the chat id can
        # never be discovered — a deadlock, since /chatid is how you get it.
        check("token alone is enough to start the listener",
              bool(_cfgmod.CFG.telegram_token))
        check("the scan still insists on a chat id to send to",
              "TELEGRAM_CHAT_ID" in _cfgmod.CFG.missing_credentials())
        check("get_chat_id helper exists",
              (Path(__file__).parent / "scripts" / "get_chat_id.py").exists())
    finally:
        if saved is None:
            os.environ.pop("TELEGRAM_CHAT_ID", None)
        else:
            os.environ["TELEGRAM_CHAT_ID"] = saved
        importlib.reload(_cfgmod)

    print("\nDeployment files")
    try:
        import yaml
    except ImportError:
        yaml = None
        print("  – pyyaml not installed, skipping workflow validation"
              " (pip install pyyaml)")

    for name in ("scan", "callback", "keepalive"):
        if yaml is None:
            break
        path = Path(__file__).parent / ".github" / "workflows" / f"{name}.yml"
        if not path.exists():
            check(f"{name}.yml present", False, "missing")
            continue
        try:
            doc = yaml.safe_load(path.read_text())
            ok = bool(doc.get("jobs")) and all(
                "runs-on" in j and "steps" in j for j in doc["jobs"].values())
        except Exception as exc:  # noqa: BLE001
            ok, doc = False, str(exc)
        check(f"{name}.yml is valid and has runnable jobs", ok)

    check("Cloudflare Worker present", (Path(__file__).parent / "worker" / "index.js").exists())

    print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) FAILED.'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
