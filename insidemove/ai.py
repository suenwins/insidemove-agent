"""Claude API synthesis.

Three calls, all grounded: the model receives pre-computed facts and is told
explicitly that it may not introduce numbers of its own. Stage 3 and Stage 4
label every claim FACT (traceable to the supplied data) or INFERENCE
(reasoned interpretation), so the reader can always tell which is which.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from .config import CFG

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

GROUNDING = (
    "You are a sell-side-grade equity analyst writing for one experienced retail "
    "trader on Telegram.\n"
    "HARD RULES:\n"
    "1. Every number you state must appear in the DATA block. Never estimate, "
    "round differently, or invent a figure. If a figure is absent, say it is unavailable.\n"
    "2. Label every substantive claim as FACT: or INFERENCE:. FACT means it is "
    "directly present in the DATA block. INFERENCE means you reasoned it out — "
    "clearly your interpretation, not a datapoint.\n"
    "3. No investment advice, no 'buy this'. Describe what the tape and the "
    "fundamentals say, and what would confirm or break the read.\n"
    "4. Output Telegram-safe HTML. The ONLY tags permitted are <b>, <i>, <code>. "
    "No headers, no markdown, no tables, no <br>. Use plain newlines and '•' bullets.\n"
    "5. Be dense. No filler, no preamble, no sign-off."
)

ALLOWED_TAGS = ("b", "i", "code", "pre", "u", "s")


def sanitise_html(text: str) -> str:
    """Escape everything, then restore only the tags Telegram accepts.

    A raw '&' or a '<' inside a company name will otherwise make Telegram
    reject the whole message with a 400.
    """
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for tag in ALLOWED_TAGS:
        text = text.replace(f"&lt;{tag}&gt;", f"<{tag}>")
        text = text.replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return text


def _call(prompt: str, system: str, max_tokens: int = 2000, retries: int = 3) -> str:
    if not CFG.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    body = {
        "model": CFG.anthropic_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": CFG.anthropic_api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(API_URL, headers=headers, json=body, timeout=120)
            if resp.status_code in (429, 500, 502, 503, 529):
                wait = 2**attempt
                log.warning("claude %s — retrying in %ds", resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            payload = resp.json()
            return "".join(
                block.get("text", "") for block in payload.get("content", [])
            ).strip()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log.warning("claude call failed (attempt %d/%d): %s", attempt, retries, exc)
            time.sleep(2**attempt)
    raise RuntimeError(f"Claude API failed after {retries} attempts: {last_error}")


def _fmt(data: dict) -> str:
    return json.dumps(data, indent=2, default=str)


# --------------------------------------------------------------------------
# Stage 2 — one-line catalyst
# --------------------------------------------------------------------------

def catalyst_line(ticker: str, candidate: dict, headlines: list[dict]) -> str:
    """One sentence naming the likely catalyst, from same-window news only."""
    if not headlines:
        from .news import no_catalyst_line

        return no_catalyst_line()

    prompt = (
        f"Ticker: {ticker}\n"
        f"Session move: {candidate['pct_change']:+.2f}% "
        f"(${candidate['prev_close']} → ${candidate['close']}), "
        f"volume {candidate['volume_ratio']}× the 20-day average.\n\n"
        f"HEADLINES (all published within the last {CFG.news_window_hours}h):\n"
        f"{_fmt(headlines)}\n\n"
        "Write ONE sentence, max 22 words, naming the most likely catalyst for this "
        "move and tying it to the % change. Use only what the headlines actually say. "
        "If the headlines do not plausibly explain a move of this size, say the move "
        "looks technical or flow-driven instead of forcing a link. "
        "Plain text only — no HTML, no label prefix, no quotes."
    )
    system = (
        "You summarise market catalysts in a single tight sentence. "
        "You never speculate beyond the supplied headlines."
    )
    out = _call(prompt, system, max_tokens=200)
    return re.sub(r"\s+", " ", out).strip().strip('"')


# --------------------------------------------------------------------------
# Stage 3 — equity research note
# --------------------------------------------------------------------------

EQUITY_TEMPLATE = """Produce the equity research note in EXACTLY this six-section \
structure, in this order, each header bolded with <b> tags:

<b>1. Proximate Catalyst</b>
What specifically happened and when. If no catalyst is present in the data, say so \
plainly and treat the move as unexplained.

<b>2. Fundamentals vs. Move</b>
Analyst consensus price target, the current discount/premium to it in both $ and %, \
and whether the move looks justified by fundamentals or like a sentiment overreaction.

<b>3. Insider Activity</b>
Recent Form 4 transactions — who, buy or sell, share count, dollar value, date. \
Distinguish routine compensation activity (grants, option exercises, 10b5-1 style \
scheduled sales) from discretionary open-market decisions. If no filings are in the \
data, say so.

<b>4. Peer Context</b>
How sector peers traded the same session, and therefore whether this is idiosyncratic \
or sector-wide.

<b>5. What to Watch</b>
2-3 concrete near-term checkpoints with a rough timeframe each.

<b>6. Verdict</b>
One paragraph: overreaction vs. justified repricing, and the overall read.

Target 380-550 words total."""


def equity_note(payload: dict) -> str:
    prompt = (
        f"DATA:\n{_fmt(payload)}\n\n{EQUITY_TEMPLATE}"
    )
    return sanitise_html(_call(prompt, GROUNDING, max_tokens=2200))


# --------------------------------------------------------------------------
# Stage 4 — technical write-up
# --------------------------------------------------------------------------

TECHNICAL_TEMPLATE = """Produce the technical analysis in EXACTLY this eight-section \
structure, each header bolded with <b> tags. Every level, average and ratio you cite \
must come from the DATA block verbatim.

<b>1. Support</b>
Explicit structural levels if present. If price is in open air below all references, \
state the ATR-derived implied floor and show the arithmetic exactly as given in \
implied_support.basis, and flag that it is derived, not tested.

<b>2. Resistance (nearest first)</b>
One line per level in the form: <code>LEVEL | CONTEXT | IMPLICATION</code>. Context is \
what the level is (SMA20, SMA50, prior swing high). Implication is what a reclaim or \
rejection there would mean.

<b>3. Price Position</b>
Current price vs SMA20 and SMA50, in both dollars and percent.

<b>4. Volume Read</b>
Does volume confirm the move — distribution or accumulation? State whether a \
capitulation-style exhaustion spike is present or absent, and give the explicit \
threshold that would count as one (e.g. 2.5×+ average volume on a long lower wick).

<b>5. Price Action Structure</b>
Trend classification, the higher-highs/higher-lows or lower-highs/lower-lows read, \
RSI state, moving-average alignment, and any divergence you can support from the data.

<b>6. 3-6 Day Plan</b>
A conditional tree, one branch per line:
• <b>If price holds</b> [level] → implication + target
• <b>If price breaks</b> [level] → implication + next implied level
• <b>If price bounces toward</b> [level] → what to watch for, rejection vs reclaim

<b>7. Sizing Context</b>
ATR-based dollar stop distance, suggested risk as 1-1.5×ATR, and the minimum \
target-to-stop ratio that would justify an entry (≥1.5:1). Frame as arithmetic, \
not as a recommendation.

<b>8. Bottom Line</b>
One paragraph synthesising trend, momentum and volume alignment — and the single \
strongest counterargument to that read.

Target 450-650 words total."""


def technical_note(payload: dict) -> str:
    prompt = f"DATA:\n{_fmt(payload)}\n\n{TECHNICAL_TEMPLATE}"
    return sanitise_html(_call(prompt, GROUNDING, max_tokens=2600))
