"""Deterministic note rendering — no language model involved.

Every line here derives from data that was already computed before this
module was called: OHLCV, the indicator snapshot, Form 4 filings, analyst
consensus, peer moves, and headlines inside the freshness window.

The FACT / INFERENCE discipline is kept, and it means the same thing it did
before. FACT is a value present in the input. INFERENCE is a reading derived
from those values — here by an explicit rule rather than by a model, which
makes it reproducible: the same inputs always produce the same sentence.

What is genuinely lost versus a model: Stage 3's synthesis across
heterogeneous sources. What is gained: nothing is ever paraphrased, so no
number can drift between what was computed and what you read.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

RULE = "─" * 24


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def money(value: float | None, dp: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.{dp}f}"


def pct(value: float | None, dp: int = 2, sign: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{dp}f}%" if sign else f"{value:.{dp}f}%"


def big(value: float | None) -> str:
    """Volume-scale numbers: 12,300,000 -> 12.3M."""
    if value is None:
        return "n/a"
    value = float(value)
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= cut:
            return f"{value/cut:.1f}{suffix}"
    return f"{value:,.0f}"


def rsi_band(rsi: float | None) -> str:
    if rsi is None:
        return "unavailable"
    if rsi < 30:
        return "oversold"
    if rsi < 45:
        return "weak"
    if rsi <= 55:
        return "neutral"
    if rsi <= 70:
        return "firm"
    return "overbought"


def distance_phrase(price: float, level: float | None, label: str) -> str:
    """'$68.75 (17.6%) below SMA20' — the exact shape the spec asked for."""
    if level is None:
        return f"{label}: not enough history to compute"
    diff = price - level
    side = "above" if diff >= 0 else "below"
    return f"{money(abs(diff))} ({abs(diff/level*100):.1f}%) {side} {label} ({money(level)})"


def _fact(text: str) -> str:
    return f"<b>FACT:</b> {text}"


def _inf(text: str) -> str:
    return f"<b>INFERENCE:</b> {text}"


def _join(lines: list[str]) -> str:
    return "\n".join(l for l in lines if l is not None)


# ===========================================================================
# Stage 2 — the one-line catalyst
# ===========================================================================

# What each catalyst type usually means for a move. Rule-based, so it is
# labelled INFERENCE wherever it appears.
TAG_READ = {
    "M&A": "Deal news reprices a stock toward the offer or the implied value in a "
           "single session; the move is explained if the price sits near the terms.",
    "Clinical / FDA": "A binary regulatory or trial outcome — the classic cause of a "
                      "multi-fold move in a small-cap biotech, in either direction.",
    "Earnings": "A results print reprices the stock on the numbers and, usually more "
                "so, on what management says about the next quarter.",
    "Guidance": "A change in the forward outlook — markets move on expectations, so "
                "guidance often matters more than the quarter just reported.",
    "Financing / dilution": "New shares or convertibles dilute existing holders; a sharp "
                            "drop is the typical reaction. If the stock rose instead, the "
                            "raise may be funding something the market values, or the "
                            "offering was priced at a premium.",
    "Legal / regulatory": "Legal, listing or regulatory risk — these moves can extend if "
                          "the issue is unresolved.",
    "Contract / partnership": "A new contract or partner — sizeable relative to revenue "
                              "for small caps, which is why it can move them sharply.",
    "Analyst action": "A rating or target change — usually a smaller, faster-fading "
                      "catalyst than company news.",
    "Management": "A leadership change — the market is pricing the change in direction.",
    "Capital return": "A buyback or dividend change — signals management's view of value.",
    "Index change": "Index inclusion forces passive buying; the move is flow, not fundamentals.",
    "Product / business": "An operating update — relevance depends on its size versus the business.",
    "Trading halt": "The exchange stopped trading in the stock. A halt for news "
                    "pending means a company announcement caused the move; the "
                    "announcement itself is what to read next.",
    "Peer / sector news": "The catalyst belongs to another company in the same "
                          "space and this stock moved in sympathy. Read-across "
                          "moves reverse more often than news-driven ones, because "
                          "nothing about this company actually changed.",
    "SEC filing": "The company filed a material disclosure in the window; open the "
                  "filing to see the event itself.",
    "Company news": "Company-specific news in the window, but not a recognisable "
                    "catalyst type — treat it as a lead to verify, not an explanation.",
}


DIRECTION_CAVEAT = (
    "describes a move the other way — most likely the catalyst for an earlier "
    "session, with this session being the reaction to it"
)


def catalyst_item(item: dict, link: bool = True) -> str:
    """One catalyst as Telegram HTML: [type] headline — detail (source, when)."""
    tag = esc(item.get("tag") or "News")
    head = esc((item.get("headline") or "").rstrip(" ."))
    url = item.get("url") or ""
    if link and url.startswith("http"):
        head = f'<a href="{html.escape(url, quote=True)}">{head}</a>'
    line = f"<b>[{tag}]</b> {head}"
    if item.get("detail"):
        line += f" — {esc(item['detail'])}"
    source = esc(item.get("source") or "unknown source")
    when = item.get("when")
    line += f" <i>({source}, {esc(when)})</i>" if when else f" <i>({source})</i>"
    if item.get("conflicts"):
        line += f"\n⚠️ <i>{DIRECTION_CAVEAT}.</i>"
    return line


def catalyst_line(ticker: str, candidate: dict, headlines: list[dict],
                  report: dict | None = None) -> str:
    """The strongest company-specific catalyst, or an honest statement that
    there is none. `headlines` must already be filtered by
    `news.catalyst_news` — roundups and price-only stories never reach here."""
    if not headlines:
        from .news import no_catalyst_line

        return no_catalyst_line(report=report or {})
    return catalyst_item(headlines[0])


# ===========================================================================
# Stage 3 — equity research note
# ===========================================================================

def _catalyst_section(payload: dict) -> list[str]:
    news = payload.get("news_last_72h")
    rejected = payload.get("news_rejected") or 0
    report = payload.get("news_report") or {}
    out = ["<b>1. Proximate Catalyst</b>"]
    if not isinstance(news, list) or not news:
        noise = (
            f" {rejected} in-window headline(s) were market roundups or restated the "
            f"price move without a reason, and were ignored."
            if rejected else ""
        )
        searched = ", ".join((report.get("counts") or {}).keys())
        where = f" Sources searched: {esc(searched)}." if searched else ""
        if report.get("sec_available") is False:
            where += (" SEC filings could not be reached this run (set SEC_CONTACT), "
                      "so a company disclosure may exist that is not reflected here.")
        out.append(_fact(
            "No company-specific news or material SEC filing inside the lookback window."
            + noise + where
        ))
        out.append(_inf(
            "With no catalyst on the tape, the move is more likely technical, "
            "flow-driven (short squeeze, low float), a sympathy move with a peer, or "
            "reacting to information not yet on the wires."
        ))
        return out

    for item in news[:3]:
        out.append("• " + catalyst_item(item))

    lead = news[0]
    session = payload.get("session", {})
    move = session.get("pct_change")
    if lead.get("conflicts"):
        out.append(_inf(
            f"The item above {DIRECTION_CAVEAT} — treat the "
            f"{pct(move, 2, sign=True)} session as the unwind of that reaction "
            f"rather than as news-driven, unless a same-direction catalyst emerges."
        ))
        return out
    read = TAG_READ.get(lead.get("tag"), TAG_READ["Company news"])
    strength = (
        "is the most likely driver of"
        if lead.get("tag") not in {"Company news", "SEC filing", "Trading halt",
                                   "Peer / sector news"}
        else "may relate to"
    )
    out.append(_inf(
        f"The {esc(lead.get('tag', 'news')).lower()} item above {strength} the "
        f"{pct(move, 2, sign=True)} move. {read}"
    ))
    if move is not None and abs(move) >= 50:
        out.append(_inf(
            f"A move of {abs(move):.0f}% is large even for this kind of news — "
            f"low float, short covering or retail momentum is likely amplifying it."
        ))
    return out


def _fundamentals_section(payload: dict) -> list[str]:
    out = ["", "<b>2. Fundamentals vs. Move</b>"]
    session = payload.get("session", {})
    close = session.get("close")
    gap = payload.get("discount_to_target")
    analyst = payload.get("analyst") or {}
    valuation = payload.get("valuation") or {}

    if gap and gap.get("target_mean"):
        out.append(
            _fact(
                f"Mean analyst price target {money(gap['target_mean'])} against a close of "
                f"{money(close)} — a {abs(gap['pct']):.1f}% "
                f"{'discount to' if gap['pct'] > 0 else 'premium over'} target "
                f"({money(abs(gap['abs']))} per share)."
            )
        )
        count = analyst.get("analyst_count")
        if count:
            out.append(_fact(f"Consensus drawn from {count} analyst estimate(s)."))

        if gap["pct"] > 25:
            out.append(
                _inf(
                    "The gap to consensus is wide. Either the sell side has not yet "
                    "revised for this move, or the market is pricing something the "
                    "published targets have not caught up with. Targets lag; treat the "
                    "gap as a question, not a valuation."
                )
            )
        elif gap["pct"] < -10:
            out.append(
                _inf(
                    "Price sits above consensus, so the move is not obviously supported "
                    "by published fundamentals — the burden is on the upside case."
                )
            )
        else:
            out.append(
                _inf("Price sits broadly in line with consensus; the move is not an obvious "
                     "dislocation against published targets.")
            )
    else:
        out.append(_fact("No analyst price target available from the configured sources."))
        out.append(
            _inf("Without consensus, there is no external anchor to judge the move against; "
                 "the technical picture carries more weight than usual here.")
        )

    ratios = []
    if valuation.get("trailing_pe"):
        ratios.append(f"trailing P/E {valuation['trailing_pe']:.1f}")
    if valuation.get("forward_pe"):
        ratios.append(f"forward P/E {valuation['forward_pe']:.1f}")
    if valuation.get("beta"):
        ratios.append(f"beta {valuation['beta']:.2f}")
    if ratios:
        out.append(_fact("Valuation: " + ", ".join(ratios) + "."))
    return out


def _insider_section(payload: dict) -> list[str]:
    out = ["", "<b>3. Insider Activity</b>"]
    rows = payload.get("insider_transactions_6m")
    if not isinstance(rows, list) or not rows:
        out.append(_fact("No Form 4 filings returned by the configured data source."))
        out.append(
            _inf("Absence here means the source had nothing, which is not the same as "
                 "insiders having done nothing. Treat it as unknown, not as a clean bill.")
        )
        return out

    discretionary = [r for r in rows if not r.get("routine")]
    routine = [r for r in rows if r.get("routine")]

    for row in rows[:5]:
        value = f" ≈ {money(row['value'], 0)}" if row.get("value") else ""
        shares = row.get("shares_changed")
        shares_txt = f"{abs(shares):,.0f} sh" if shares else "shares n/d"
        out.append(
            f"• {esc(row.get('name', 'unknown'))} — {esc(row.get('action', '?'))}, "
            f"{shares_txt}{value} <i>({esc(row.get('date') or 'date n/d')})</i>"
        )

    buys = [r for r in discretionary if r["code"] == "P"]
    sells = [r for r in discretionary if r["code"] == "S"]

    out.append(
        _fact(
            f"{len(rows)} filing(s) in the window: {len(buys)} open-market buy(s), "
            f"{len(sells)} sale(s), {len(routine)} routine compensation event(s) "
            f"(grants, option exercises, tax withholding)."
        )
    )
    if buys and not sells:
        out.append(
            _inf("Open-market buying with no offsetting sales is the more meaningful "
                 "pattern of the two — insiders buy for one reason and sell for many.")
        )
    elif sells and not buys:
        out.append(
            _inf("Sales without offsetting purchases. Check the filing dates against a "
                 "10b5-1 plan before reading intent into them; scheduled sales look "
                 "identical to discretionary ones in this data.")
        )
    elif not discretionary:
        out.append(
            _inf("Everything in the window is routine compensation activity. This carries "
                 "no directional signal.")
        )
    else:
        out.append(_inf("Activity runs both directions; no clean directional read."))
    return out


def _peer_read(payload: dict) -> tuple[str, float] | None:
    """Classify the peer tape once, so section 4 and the verdict cannot
    contradict each other by applying different thresholds to the same data."""
    peers = payload.get("peers_same_session")
    move = (payload.get("session") or {}).get("pct_change")
    if not isinstance(peers, list) or not peers or move is None:
        return None

    moves = sorted(p["pct_change"] for p in peers)
    median = moves[len(moves) // 2]
    same_direction = (median >= 0) == (move >= 0)

    if same_direction and abs(median) >= abs(move) * 0.5:
        return "sector", median
    if same_direction and abs(median) >= abs(move) * 0.15:
        return "mixed", median
    return "idiosyncratic", median


def _peer_section(payload: dict) -> list[str]:
    out = ["", "<b>4. Peer Context</b>"]
    peers = payload.get("peers_same_session")
    session = payload.get("session", {})
    move = session.get("pct_change")

    if not isinstance(peers, list) or not peers:
        out.append(_fact("No peer data available from the configured sources."))
        out.append(_inf("Cannot separate a single-stock event from a sector move without it."))
        return out

    for peer in peers[:6]:
        out.append(f"• <code>{esc(peer['ticker'])}</code> {pct(peer['pct_change'], 2, sign=True)}")

    read = _peer_read(payload)
    if read is None:
        return out
    verdict, median = read
    out.append(_fact(f"Median peer move this session: {pct(median, 2, sign=True)}."))

    out.append(_inf({
        "sector": "Peers moved materially in the same direction, so this reads as a "
                  "sector-wide repricing rather than a single-stock event. The name is "
                  "the messenger, not the story.",
        "mixed": "Peers leaned the same way but far less. Part sector drift, part "
                 "something specific to this name.",
        "idiosyncratic": "Peers did not follow. This is idiosyncratic — whatever moved it "
                         "is specific to this company.",
    }[verdict]))
    return out


def _watch_section(payload: dict) -> list[str]:
    """Checkpoints are selected by rule from what the data actually shows,
    so they name conditions that exist rather than generic advice."""
    out = ["", "<b>5. What to Watch</b>"]
    items: list[str] = []

    gap = payload.get("discount_to_target")
    session = payload.get("session", {})
    tech = payload.get("technical_context", {})

    if gap and abs(gap.get("pct", 0)) > 20:
        items.append(
            f"Analyst target revisions over the next 5–7 sessions. Consensus currently "
            f"sits {abs(gap['pct']):.0f}% from the price; if targets move toward the "
            f"price rather than the reverse, the sell side has accepted the repricing."
        )

    ratio = session.get("volume_ratio")
    if ratio and ratio >= 2:
        items.append(
            f"Whether volume normalises. Today ran {ratio:.1f}× the 20-day average; "
            f"a return toward 1× within 2–3 sessions suggests a one-off event rather "
            f"than a change in who owns the stock."
        )

    from_low = tech.get("pct_from_52w_low")
    from_high = tech.get("pct_from_52w_high")
    if from_low is not None and from_low < 8:
        items.append(
            f"Whether the 52-week low holds — price is {from_low:.1f}% above it. "
            f"A close below turns this from a drawdown into a breakdown."
        )
    elif from_high is not None and from_high > -3:
        items.append(
            f"Whether the 52-week high is accepted or rejected — price is "
            f"{abs(from_high):.1f}% from it. Breakouts that fail back inside the "
            f"range tend to fail hard."
        )

    news = payload.get("news_last_72h")
    if not isinstance(news, list) or not news:
        items.append(
            "Any headline that explains the move. A large move with no catalyst on the "
            "tape usually means the catalyst has not been published yet, not that there isn't one."
        )

    if tech.get("rsi") is not None:
        band = rsi_band(tech["rsi"])
        if band in {"oversold", "overbought"}:
            items.append(
                f"RSI is {tech['rsi']:.0f} ({band}). Watch whether it resets toward 50 "
                f"through price or through time — a sideways reset is the healthier of the two."
            )

    for item in items[:3] or ["No specific checkpoint stands out from the available data."]:
        out.append(f"• {item}")
    return out


def _verdict_section(payload: dict) -> list[str]:
    out = ["", "<b>6. Verdict</b>"]
    session = payload.get("session", {})
    move = session.get("pct_change") or 0
    ratio = session.get("volume_ratio") or 0
    gap = payload.get("discount_to_target")
    peers = payload.get("peers_same_session")
    news = payload.get("news_last_72h")

    has_news = isinstance(news, list) and bool(news)
    heavy = ratio >= 2
    direction = "decline" if move < 0 else "advance"

    parts = []
    if has_news and heavy:
        parts.append(
            f"A {abs(move):.1f}% {direction} on {ratio:.1f}× normal volume with news on "
            f"the tape is the signature of a genuine repricing, not a liquidity air pocket. "
            f"Heavy volume means the shares changed hands at these levels rather than "
            f"gapping through them."
        )
    elif heavy and not has_news:
        parts.append(
            f"A {abs(move):.1f}% {direction} on {ratio:.1f}× normal volume with nothing "
            f"published to explain it. Volume that heavy is rarely random — the more likely "
            f"reading is that information exists and has not reached the wires."
        )
    elif has_news:
        parts.append(
            f"A {abs(move):.1f}% {direction} on {ratio:.1f}× average volume with news "
            f"present. Ordinary participation for a move this size, which argues for a "
            f"contained reaction rather than a wholesale change of view."
        )
    else:
        parts.append(
            f"A {abs(move):.1f}% {direction} on unremarkable volume and no catalyst. "
            f"This has the shape of drift or flow rather than a repricing."
        )

    if gap and gap.get("pct", 0) > 25:
        parts.append(
            "Against consensus the price now looks heavily discounted, but published "
            "targets lag events by days — the gap is more likely stale data than free money."
        )

    read = _peer_read(payload)
    if read:
        parts.append({
            "sector": "With peers moving alike, treat this as sector exposure rather than "
                      "a single-name call.",
            "mixed": "Peers drifted the same way but much less, so only part of this is "
                     "sector beta.",
            "idiosyncratic": "With peers unmoved, the driver is company-specific.",
        }[read[0]])

    out.append(_inf(" ".join(parts)))
    return out


def equity_note(payload: dict) -> str:
    return _join(
        _catalyst_section(payload)
        + _fundamentals_section(payload)
        + _insider_section(payload)
        + _peer_section(payload)
        + _watch_section(payload)
        + _verdict_section(payload)
        + [
            "",
            "<i>Rules-based note — no language model. FACT lines are values from the "
            "source data; INFERENCE lines are readings derived from those values by "
            "fixed rule, so the same inputs always produce the same wording.</i>",
        ]
    )


# ===========================================================================
# Stage 4 — technical analysis
# ===========================================================================

_LEVEL_MEANING = {
    "SMA20": "short-term trend line — where the last month of buyers sit",
    "SMA50": "intermediate trend line — a reclaim changes the structural read",
    "swing": "prior swing level where price previously turned",
}


def _implication(level: dict, close: float, above: bool) -> str:
    kind = level.get("kind", "swing")
    if kind == "SMA20":
        return (
            "reclaiming it would be the first evidence the down-leg is pausing"
            if above else
            "losing it would confirm short-term control has changed hands"
        )
    if kind == "SMA50":
        return (
            "a close above turns the intermediate trend neutral at minimum"
            if above else
            "a close below opens the intermediate downtrend"
        )
    touches = level.get("touches", 0)
    tested = f"tested {touches}×" if touches > 1 else "single touch, untested"
    return (
        f"{tested}; a break above puts the next level in play"
        if above else
        f"{tested}; a break below removes the nearest floor"
    )


def _support_section(snap: dict) -> list[str]:
    out = ["<b>1. Support</b>"]
    close = snap["close"]
    support = snap.get("support") or []
    implied = snap.get("implied_support")

    if support:
        for lvl in support[:3]:
            dist = lvl["distance"]
            kind = lvl.get("kind", "swing")
            tag = kind if kind != "swing" else "prior swing low"
            out.append(
                f"• <code>{money(lvl['level'])}</code> — {tag}, "
                f"{abs(dist['pct']):.1f}% below price"
            )
        nearest = support[0]
        out.append(
            _fact(f"Nearest structural support {money(nearest['level'])}, "
                  f"{money(abs(close - nearest['level']))} below the close.")
        )
    elif implied:
        out.append(f"• <code>{money(implied['level'])}</code> — ATR-derived implied floor")
        out.append(_fact(f"Derivation: {esc(implied['basis'])}."))
        out.append(
            _inf(
                "Price is in open air: there is no prior swing low beneath it in the "
                "chart window, so this floor is arithmetic, not a level the market has "
                "ever defended. Expect it to hold poorly."
            )
        )
    else:
        out.append(_fact("No structural support identified in the chart window."))
    return out


def _resistance_section(snap: dict) -> list[str]:
    out = ["", "<b>2. Resistance (nearest first)</b>"]
    resistance = snap.get("resistance") or []
    if not resistance:
        out.append(_fact("No resistance in the chart window — price is at or near its highs."))
        return out

    close = snap["close"]
    # Two lines per level rather than a padded single-line table: Telegram
    # reflows long lines on a phone, so column padding only ever looks broken.
    for lvl in resistance[:4]:
        kind = lvl.get("kind", "swing")
        context = kind if kind != "swing" else "prior swing high"
        away = abs(lvl["distance"]["pct"])
        out.append(
            f"<code>{money(lvl['level'])}</code> · {context} · {away:.1f}% above\n"
            f"    ↳ {_implication(lvl, close, above=True)}"
        )
    out.append(
        _fact(f"First overhead level {money(resistance[0]['level'])}, "
              f"{abs(resistance[0]['distance']['pct']):.1f}% above the close.")
    )
    return out


def _position_section(snap: dict) -> list[str]:
    close = snap["close"]
    out = ["", "<b>3. Price Position</b>"]
    out.append(_fact(distance_phrase(close, snap.get("sma_fast"), f"SMA{snap['sma_fast_period']}")))
    out.append(_fact(distance_phrase(close, snap.get("sma_slow"), f"SMA{snap['sma_slow_period']}")))
    out.append(
        _fact(
            f"52-week range {money(snap['low_52w'])} – {money(snap['high_52w'])}; "
            f"price is {abs(snap['pct_from_52w_high']):.1f}% below the high and "
            f"{snap['pct_from_52w_low']:.1f}% above the low."
        )
    )
    return out


def _volume_section(snap: dict) -> list[str]:
    out = ["", "<b>4. Volume Read</b>"]
    ratio = snap.get("volume_ratio") or 0
    move = snap.get("pct_change") or 0
    wick = snap.get("wick") or {}
    lower = wick.get("lower_pct_of_range", 0)

    out.append(
        _fact(f"{big(snap.get('volume'))} traded against a 20-day average of "
              f"{big(snap.get('avg_volume_20'))} — {ratio:.1f}×.")
    )

    if ratio >= 2 and move < 0:
        out.append(_inf("Heavy volume on a down day is distribution: supply is being absorbed "
                        "at lower prices, and sellers were willing to accept them."))
    elif ratio >= 2 and move > 0:
        out.append(_inf("Heavy volume on an up day is accumulation: buyers paid up rather than "
                        "waiting, which is what conviction looks like on a tape."))
    elif ratio < 0.8:
        out.append(_inf("Volume is below average, so the move carries little participation. "
                        "Price changes on thin volume revert more often than they continue."))
    else:
        out.append(_inf("Volume is unremarkable — the move is not confirmed by participation "
                        "either way."))

    threshold = "2.5×+ average volume paired with a lower wick of 60%+ of the session range"
    if ratio >= 2.5 and lower >= 60:
        out.append(
            _fact(f"Capitulation signature present: {ratio:.1f}× volume with a lower wick "
                  f"{lower:.0f}% of the range.")
        )
        out.append(_inf("Buyers stepped in well below the close. That is what exhaustion looks "
                        "like, though a single bar confirms nothing until it holds."))
    else:
        out.append(
            _fact(f"No capitulation spike. The explicit threshold is {threshold}; "
                  f"this session printed {ratio:.1f}× with a {lower:.0f}% lower wick.")
        )
        out.append(_inf("Without an exhaustion bar there is no evidence the selling is finished."))
    return out


def _structure_section(snap: dict) -> list[str]:
    out = ["", "<b>5. Price Action Structure</b>"]
    window = snap.get("trend_window", 90)
    out.append(
        _fact(f"Trend classification over the last {window} sessions: "
              f"{esc(snap.get('trend', 'undetermined'))}.")
    )
    out.append(_fact(f"Swing structure: {esc(snap.get('structure', 'n/a'))}."))
    from_high = snap.get("pct_from_52w_high")
    if from_high is not None and from_high < -20 and "up" in (snap.get("trend") or ""):
        out.append(
            _inf(f"Note the window: this is an uptrend inside a stock still "
                 f"{abs(from_high):.0f}% below its 52-week high. It is a bounce within a "
                 f"larger drawdown, not a recovery of it.")
        )
    out.append(_fact(f"{esc(snap.get('ma_alignment', 'n/a'))}."))

    rsi = snap.get("rsi")
    if rsi is not None:
        out.append(_fact(f"RSI({snap.get('rsi_period', 14)}) at {rsi:.1f} — {rsi_band(rsi)}."))
        if rsi < 30:
            out.append(_inf("Oversold readings mark stretched conditions, not turning points. "
                            "In a downtrend RSI can sit below 30 for weeks."))
        elif rsi > 70:
            out.append(_inf("Overbought readings mark strength as often as they mark exhaustion; "
                            "on their own they are not a reason to fade."))
    if snap.get("gap_pct"):
        out.append(_fact(f"Opened {pct(snap['gap_pct'], 2, sign=True)} against the prior close."))
    return out


def _plan_section(snap: dict) -> list[str]:
    out = ["", "<b>6. 3–6 Day Plan</b>"]
    close = snap["close"]
    support = snap.get("support") or []
    resistance = snap.get("resistance") or []
    implied = snap.get("implied_support")
    atr = snap.get("atr")

    floor = support[0]["level"] if support else (implied["level"] if implied else None)
    ceiling = resistance[0]["level"] if resistance else None
    next_ceiling = resistance[1]["level"] if len(resistance) > 1 else None

    if floor:
        out.append(
            f"• <b>If price holds</b> above <code>{money(floor)}</code> → the level is "
            f"doing its job; base case is a drift back toward "
            f"<code>{money(ceiling)}</code>." if ceiling else
            f"• <b>If price holds</b> above <code>{money(floor)}</code> → the level is "
            f"doing its job and the immediate downside case weakens."
        )
        implied_next = round(floor - atr, 2) if atr else None
        out.append(
            f"• <b>If price breaks</b> <code>{money(floor)}</code> on closing basis → "
            f"the floor is gone; next implied level "
            f"{('<code>' + money(implied_next) + '</code> (1×ATR below)') if implied_next else 'is open air'}."
        )
    else:
        out.append("• <b>If price holds</b> — no reference level below to hold; nothing to anchor to.")

    if ceiling:
        out.append(
            f"• <b>If price bounces toward</b> <code>{money(ceiling)}</code> → watch which "
            f"happens there. Rejection with a lower high keeps the downtrend intact; a close "
            f"above with volume opens "
            f"{('<code>' + money(next_ceiling) + '</code>') if next_ceiling else 'the next leg'}."
        )
    return out


def _sizing_section(snap: dict) -> list[str]:
    out = ["", "<b>7. Sizing Context</b>"]
    atr = snap.get("atr")
    close = snap["close"]
    if not atr:
        out.append(_fact("ATR unavailable — insufficient history."))
        return out

    stop_lo, stop_hi = atr, atr * 1.5
    out.append(
        _fact(f"ATR({snap.get('atr_period', 14)}) is {money(atr)}, "
              f"{snap.get('atr_pct', 0):.2f}% of price.")
    )
    out.append(
        _fact(f"A 1–1.5×ATR stop is {money(stop_lo)}–{money(stop_hi)} of risk per share.")
    )
    out.append(
        _fact(f"At a 1.5:1 minimum target-to-stop ratio, that requires "
              f"{money(stop_lo*1.5)}–{money(stop_hi*1.5)} of expected move to justify entry — "
              f"from {money(close)}, a target of at least "
              f"{money(close + stop_lo*1.5)} long or {money(close - stop_lo*1.5)} short.")
    )
    out.append(
        _inf("This is position arithmetic, not a recommendation. If the nearest level in "
             "your direction sits closer than the required move, the trade does not clear "
             "its own hurdle regardless of how the setup looks.")
    )
    return out


def _bottom_line(snap: dict) -> list[str]:
    out = ["", "<b>8. Bottom Line</b>"]
    move = snap.get("pct_change") or 0
    ratio = snap.get("volume_ratio") or 0
    rsi = snap.get("rsi")
    trend = snap.get("trend", "undetermined")
    bearish_align = "SMA" in (snap.get("ma_alignment") or "") and "<" in (snap.get("ma_alignment") or "")

    aligned = (move < 0 and bearish_align) or (move > 0 and not bearish_align)
    confirmed = ratio >= 1.5

    window = snap.get("trend_window", 90)
    trend = f"{trend} (measured over {window} sessions)"

    if aligned and confirmed:
        read = (
            f"Trend, momentum and volume point the same way: a {trend} with the move "
            f"confirmed by {ratio:.1f}× volume. When all three agree, continuation is "
            f"the base case and the burden falls on whatever would interrupt it."
        )
    elif aligned:
        read = (
            f"Trend and momentum agree — {trend} — but volume at {ratio:.1f}× is not "
            f"confirming. Moves the tape does not participate in tend to retrace."
        )
    else:
        read = (
            f"The session move runs against the prevailing {trend}. That is either the "
            f"start of a turn or a counter-trend bounce, and one session cannot tell "
            f"you which."
        )
    out.append(_inf(read))

    # The counterargument, chosen by rule against whatever the read just claimed.
    counters = []
    if rsi is not None and rsi < 30 and move < 0:
        counters.append(
            f"RSI at {rsi:.0f} is oversold, so the easy downside may already be priced"
        )
    if rsi is not None and rsi > 70 and move > 0:
        counters.append(f"RSI at {rsi:.0f} is overbought, which caps how much further this runs cheaply")
    if (snap.get("support") or snap.get("implied_support")) and move < 0:
        counters.append("price is sitting on identified support, the one place a decline is most likely to pause")
    if snap.get("resistance") and move > 0:
        counters.append("price is running into overhead supply, where advances most often stall")
    if ratio < 1.2:
        counters.append(f"volume at {ratio:.1f}× means few participants agree with this move so far")

    out.append(
        _inf("Strongest counterargument: " + (
            "; ".join(counters[:2]) + "."
            if counters else
            "the read rests on a single session, and one bar is a weak foundation for anything."
        ))
    )
    return out


def technical_note(snap: dict) -> str:
    return _join(
        _support_section(snap)
        + _resistance_section(snap)
        + _position_section(snap)
        + _volume_section(snap)
        + _structure_section(snap)
        + _plan_section(snap)
        + _sizing_section(snap)
        + _bottom_line(snap)
        + [
            "",
            "<i>Every level, average and ratio above was computed from daily OHLCV and "
            "rendered without a language model — nothing here has been paraphrased.</i>",
        ]
    )
