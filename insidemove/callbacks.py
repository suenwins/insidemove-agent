"""Self-contained inline-button payloads.

Telegram caps `callback_data` at 64 bytes, which is why the first build
stored a random token per button and looked the ticker up in SQLite. That
was over-engineering: the payload only ever needed a verb, a ticker and a
session date.

    a:AAPL:20260904        15 bytes
    p:BRK-B:20260904:s     18 bytes

Nothing to look up, nothing to expire, and no database — which is what lets
the whole bot run on a platform with no persistent disk. A button tapped
three weeks later still says exactly what it means.

The trade-off, stated honestly: the scan's list of *reasons* no longer rides
along to Stage 3. Those are recomputed from price data when the button is
pressed, which is marginally more work and slightly fresher.
"""
from __future__ import annotations

import re

VERBS = {"a": "approve", "p": "ptc", "d": "dismiss"}
REVERSE = {v: k for k, v in VERBS.items()}

TIERS = {"p": "primary", "s": "secondary", "z": "zone"}
TIERS_REVERSE = {v: k for k, v in TIERS.items()}

# Yahoo tickers: letters, digits, dot, dash, caret.
_TICKER = re.compile(r"^[A-Z0-9.\-^]{1,12}$")
MAX_BYTES = 64


class BadCallback(ValueError):
    pass


def encode(verb: str, ticker: str, session_date: str, tier: str = "") -> str:
    """session_date is ISO (2026-09-04); it is packed as 20260904."""
    short = REVERSE.get(verb, verb)
    if short not in VERBS:
        raise BadCallback(f"unknown verb {verb!r}")

    ticker = ticker.upper().strip()
    if not _TICKER.match(ticker):
        raise BadCallback(f"unusable ticker {ticker!r}")

    compact = session_date.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise BadCallback(f"unusable session date {session_date!r}")

    parts = [short, ticker, compact]
    if tier:
        parts.append(TIERS_REVERSE.get(tier, tier[:1]))

    payload = ":".join(parts)
    if len(payload.encode("utf-8")) > MAX_BYTES:
        raise BadCallback(f"payload too long: {payload!r}")
    return payload


def decode(payload: str) -> dict:
    parts = (payload or "").split(":")
    if len(parts) < 3:
        raise BadCallback(f"malformed payload {payload!r}")

    short, ticker, compact = parts[0], parts[1].upper(), parts[2]
    if short not in VERBS:
        raise BadCallback(f"unknown verb {short!r}")
    if not _TICKER.match(ticker):
        raise BadCallback(f"unusable ticker {ticker!r}")
    if len(compact) != 8 or not compact.isdigit():
        raise BadCallback(f"unusable session date {compact!r}")

    tier = ""
    if len(parts) > 3 and parts[3]:
        tier = TIERS.get(parts[3], parts[3])

    return {
        "verb": VERBS[short],
        "ticker": ticker,
        "session_date": f"{compact[:4]}-{compact[4:6]}-{compact[6:]}",
        "tier": tier,
    }
