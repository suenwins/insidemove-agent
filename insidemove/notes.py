"""Note-engine dispatcher.

One seam between the app and whatever writes the prose. `NOTE_ENGINE`
selects it:

    template   deterministic rendering, no API, no key, no cost  (default)
    anthropic  Claude API — better Stage 3 synthesis, ~$6/month

Stage 2 and Stage 4 are near-lossless under `template`, because the numbers
they report were already computed deterministically before either engine was
called. Stage 3 is where a model genuinely adds something: weighing a
catalyst against analyst targets, insider behaviour and peer moves is
synthesis, and the template can only apply fixed rules to it.

`STAGE3_ENGINE` therefore overrides the engine for Stage 3 alone, so paying
for the one call that is actually hard — and nothing else — is a supported
configuration rather than an all-or-nothing switch.
"""
from __future__ import annotations

import logging

from . import templates
from .config import CFG  # also guarantees the .env / settings.env load has run

log = logging.getLogger(__name__)

ENGINE = CFG.note_engine
STAGE3_ENGINE = CFG.stage3_engine

VALID = {"template", "anthropic"}


def _engine(name: str) -> str:
    if name not in VALID:
        log.warning("unknown note engine %r — falling back to template", name)
        return "template"
    return name


def _claude():
    from . import ai

    return ai


def catalyst_line(ticker: str, candidate: dict, headlines: list[dict], rejected: int = 0) -> str:
    """Returns Telegram-safe HTML carrying its own source attribution, so the
    caller can drop it straight into a message without escaping it again."""
    if _engine(ENGINE) == "anthropic":
        try:
            line = templates.esc(_claude().catalyst_line(ticker, candidate, headlines))
            if headlines:
                top = headlines[0]
                src = templates.esc(top.get("source") or "unknown source")
                when = top.get("when")
                stamp = f"{src}, {templates.esc(when)}" if when else src
                line += f" <i>({stamp})</i>"
            return line
        except Exception as exc:  # noqa: BLE001
            log.warning("claude catalyst failed for %s (%s) — using template", ticker, exc)
    return templates.catalyst_line(ticker, candidate, headlines, rejected)


def equity_note(payload: dict) -> str:
    if _engine(STAGE3_ENGINE) == "anthropic":
        try:
            return _claude().equity_note(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("claude equity note failed (%s) — using template", exc)
    return templates.equity_note(payload)


def technical_note(snapshot: dict) -> str:
    if _engine(ENGINE) == "anthropic":
        try:
            return _claude().technical_note(snapshot)
        except Exception as exc:  # noqa: BLE001
            log.warning("claude technical note failed (%s) — using template", exc)
    return templates.technical_note(snapshot)


def describe() -> str:
    """Human-readable summary for /status."""
    if ENGINE == STAGE3_ENGINE:
        return ENGINE
    return f"{ENGINE} (Stage 3: {STAGE3_ENGINE})"


def requires_anthropic_key() -> bool:
    return "anthropic" in {ENGINE, STAGE3_ENGINE}
