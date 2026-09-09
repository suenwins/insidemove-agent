"""Stage 1 + Stage 2 — the daily scan and the alert it produces.

Run by cron at 15:00 MYT on weekdays. The US session closed at 04:00 MYT the
same morning, so the data is roughly eleven hours old: current enough to act
on or add to a watchlist the same day.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from . import cache, callbacks, data, news, notes, signals, state, telegram_api as tg, universe
from .config import CFG
from .signals import Candidate

log = logging.getLogger(__name__)

TIER_BADGE = {
    "primary": "🎯",
    "secondary": "👀",
}


def _header(session: str, primary: int, secondary: int) -> str:
    return (
        f"<b>🧭 InsideMove Agent — Daily Scan</b>\n"
        f"Session: <b>{session}</b> (most recently completed US session)\n"
        f"Universe: S&amp;P 500 + NASDAQ · trigger ±{CFG.move_threshold_pct:.0f}% "
        f"plus secondary price-action signals\n"
        f"Qualified: {primary} threshold move(s), {secondary} price-action name(s)"
    )


def format_alert(cand: Candidate, meta: dict, catalyst: str, sources: list[dict]) -> str:
    badge = TIER_BADGE.get(cand.tier, "•")
    arrow = "🟢▲" if cand.pct_change >= 0 else "🔴▼"
    name = tg.escape(meta.get("name") or cand.ticker)
    sector = tg.escape(meta.get("sector") or "—")

    label = (
        "Threshold move" if cand.tier == "primary" else "Price-action watch"
    )

    lines = [
        f"{badge} <b>{cand.ticker}</b> — {name}",
        f"<i>{sector}</i> · {label}",
        "",
        f"{arrow} <b>{cand.pct_change:+.2f}%</b>  "
        f"(${cand.prev_close:,.2f} → ${cand.close:,.2f})",
        f"Trailing 24h — prior close vs most recent close. Volume "
        f"{cand.volume_ratio:.1f}× the 20-day average.",
        "",
        f"<b>Why it flagged:</b>",
    ]
    lines += [f"• {tg.escape(r)}" for r in cand.reasons]
    # `catalyst` already arrives as Telegram-safe HTML with its own source
    # attribution — escaping it again would show the tags as literal text.
    lines += ["", f"<b>Read:</b> {catalyst}"]
    return "\n".join(lines)


def process_candidate(cand: Candidate, session_date: str) -> None:
    meta = dict(universe.meta_for(cand.ticker))

    # The NASDAQ constituent file carries no sector, so most small caps arrive
    # blank. Only the handful of names actually being sent are worth a profile
    # lookup, so fill the gap here rather than for 3,600 tickers at scan time.
    if not meta.get("sector"):
        profile = data.company_profile(cand.ticker)
        if profile.get("sector"):
            meta["sector"] = profile["sector"]
        if profile.get("shortName") and len(profile["shortName"]) < len(meta.get("name", "")):
            meta["name"] = profile["shortName"]

    headlines = news.recent_news(cand.ticker, session_date=session_date)
    try:
        catalyst = notes.catalyst_line(cand.ticker, cand.__dict__, headlines)
    except Exception as exc:  # noqa: BLE001
        log.error("catalyst generation failed for %s: %s", cand.ticker, exc)
        catalyst = (
            headlines[0]["headline"] if headlines else news.no_catalyst_line()
        )

    text = format_alert(cand, meta, catalyst, headlines)

    markup = tg.keyboard([[
        ("✅ Approve", callbacks.encode("approve", cand.ticker, session_date, cand.tier)),
        ("❌ Dismiss", callbacks.encode("dismiss", cand.ticker, session_date, cand.tier)),
    ]])

    if CFG.dry_run:
        print("\n" + "=" * 70)
        print(text)
        print("[buttons] ✅ Approve  ❌ Dismiss")
        message_id = None
    else:
        message_id = tg.send_message(text, reply_markup=markup)

    state.record_alert(
        cand.ticker, session_date, message_id, cand.pct_change, cand.tier, cand.reasons
    )


def run_scan(force: bool = False) -> int:
    """Returns the number of alerts sent."""
    state.init()

    now = datetime.now(CFG.tz)
    if now.weekday() >= 5 and not force:
        log.info("weekend (%s) — skipping scan", now.strftime("%A"))
        return 0

    missing = CFG.missing_credentials()
    if missing and not CFG.dry_run:
        raise RuntimeError(f"Missing required credentials: {', '.join(missing)}")

    uni = universe.get_universe()
    log.info("universe: %d tickers", len(uni))

    frames, report = cache.refresh(list(uni))
    log.info(
        "price cache: %d new tickers, %d refreshed, %d restated (split/correction), %d rows added",
        report["cold"], report["warm"], report["restated"], report["added_rows"],
    )
    if not frames:
        raise RuntimeError("No price data available — aborting scan")

    frames, partial = data.drop_incomplete_sessions(frames)
    if partial:
        log.info(
            "dropped an in-progress session bar from %d tickers — the US market "
            "is still open, scanning the last completed session instead", partial,
        )

    session = data.last_session_date(frames)
    if session is None:
        raise RuntimeError("Could not determine the most recent completed session")
    session_ts = pd.Timestamp(session)
    session_date = session.isoformat()
    log.info("scanning session %s across %d tickers", session_date, len(frames))

    ranked = signals.rank(frames, session_ts)

    # Separate "nothing qualified" from "everything that qualified was already
    # sent". Reporting both as a quiet tape is actively misleading: one means
    # the market did nothing, the other means you have already seen it.
    if force:
        picked, repeats = ranked, []
    else:
        repeats = [c for c in ranked if state.already_alerted(c.ticker, session_date)]
        already = {c.ticker for c in repeats}
        picked = [c for c in ranked if c.ticker not in already]

    n_primary = sum(1 for c in picked if c.tier == "primary")
    n_secondary = len(picked) - n_primary

    if not picked:
        if repeats:
            names = ", ".join(f"<b>{c.ticker}</b>" for c in repeats[:5])
            msg = (
                f"<b>🧭 InsideMove Agent — Daily Scan</b>\n"
                f"Session: <b>{session_date}</b>\n\n"
                f"{len(repeats)} name(s) qualified, but all of them were already "
                f"sent to you for this session: {names}.\n\n"
                f"<i>Nothing new — this is a repeat scan of a session you have "
                f"already seen, not a quiet market.</i>"
            )
            log.info(
                "all %d qualifying names for %s were already alerted — nothing new to send",
                len(repeats), session_date,
            )
        else:
            msg = (
                f"<b>🧭 InsideMove Agent — Daily Scan</b>\n"
                f"Session: <b>{session_date}</b>\n\n"
                f"Nothing crossed the ±{CFG.move_threshold_pct:.0f}% threshold and no "
                f"secondary price-action setup met the bar. Genuinely quiet tape — "
                f"{len(frames):,} tickers scanned, none qualified."
            )
        if CFG.dry_run:
            print(msg)
        else:
            tg.send_message(msg)
        return 0

    header = _header(session_date, n_primary, n_secondary)
    if repeats:
        header += (
            f"\n<i>{len(repeats)} already sent earlier for this session — "
            f"suppressed.</i>"
        )
    if CFG.dry_run:
        print(header)
    else:
        tg.send_message(header)

    for cand in picked:
        try:
            process_candidate(cand, session_date)
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to send alert for %s: %s", cand.ticker, exc)

    return len(picked)
