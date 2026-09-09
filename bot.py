#!/usr/bin/env python3
"""Long-polling listener: the process that answers button taps.

Runs continuously under systemd/Docker. The scan itself is a separate cron
process (run_scan.py) so a crash in one never takes the other down, and the
scan does not have to keep a socket open for 23 hours a day.
"""
from __future__ import annotations

import logging
import signal
import sys
import time

from insidemove import cache, handlers, notes, state, telegram_api as tg
from insidemove.config import CFG
from insidemove.scanner import run_scan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("insidemove.bot")

_running = True


def _stop(signum, _frame):
    global _running
    log.info("received signal %s — shutting down", signum)
    _running = False


HELP = (
    "<b>🧭 InsideMove Agent</b>\n\n"
    "I scan the S&amp;P 500 + NASDAQ after every US close and surface the day's "
    "most significant price action.\n\n"
    "<b>The funnel</b>\n"
    "1️⃣ Daily scan alert — ticker, sector, trailing-24h move, why it flagged\n"
    "2️⃣ ✅ Approve → AI equity research note (catalyst, fundamentals, insiders, peers)\n"
    "3️⃣ 📊 PTC → chart + AI technical analysis with a 3-6 day conditional plan\n\n"
    "<b>Commands</b>\n"
    "/scan — run the scan now, off-schedule\n"
    "/status — configuration and last scan\n"
    "/help — this message\n\n"
    f"<i>Scheduled daily at {CFG.scan_hour:02d}:{CFG.scan_minute:02d} "
    f"{CFG.tz.key}, weekdays.</i>"
)


def _status() -> str:
    recent = state.last_session()
    uni = state.kv_get("universe") or {}
    if recent:
        session_date, alerts = recent
        last = f"{session_date} — {len(alerts)} alert(s)"
    else:
        last = "no scans recorded yet"
    stats = cache.PriceCache().stats()
    return (
        "<b>Status</b>\n"
        f"• Universe: {len(uni)} tickers\n"
        f"• Threshold: ±{CFG.move_threshold_pct:.0f}% ({CFG.direction})\n"
        f"• Daily cap: {CFG.max_alerts_per_day} "
        f"(max {CFG.max_secondary_slots} price-action slots)\n"
        f"• News window: {CFG.news_window_hours}h\n"
        f"• Note engine: {notes.describe()}"
        + (f" ({CFG.anthropic_model})" if notes.requires_anthropic_key() else " — no AI, no cost")
        + "\n"
        f"• Schedule: {CFG.scan_hour:02d}:{CFG.scan_minute:02d} {CFG.tz.key}, weekdays\n"
        f"• Price cache: {stats['tickers']} tickers, {stats['rows']:,} bars"
        + (f", through {stats['newest']}" if stats["newest"] else " (empty)")
        + "\n"
        f"• Last scan: {last}"
    )


SETUP_ONLY = (
    "⚙️ <b>Setup mode</b>\n\n"
    "I don't know which chat I belong to yet, so I'm only answering "
    "<code>/chatid</code> until that's fixed.\n\n"
    "Send <code>/chatid</code>, put the number into <code>TELEGRAM_CHAT_ID</code> "
    "in your <code>.env</code>, and restart me."
)


def handle_message(msg: dict) -> None:
    chat_id = str(msg.get("chat", {}).get("id"))
    text = (msg.get("text") or "").strip().lower()

    if CFG.telegram_chat_id and chat_id != str(CFG.telegram_chat_id):
        log.warning("ignoring message from unauthorised chat %s", chat_id)
        return

    # Unlocked because no chat id is configured yet. Anyone who finds the bot
    # can talk to it in this state, so answer only what is needed to escape it
    # — never /scan, which would burn real work for a stranger.
    if not CFG.telegram_chat_id and not text.startswith(("/chatid", "/start", "/help")):
        tg.send_message(SETUP_ONLY, chat_id=chat_id)
        return

    if text.startswith(("/start", "/help")):
        tg.send_message(
            HELP + ("\n\n" + SETUP_ONLY if not CFG.telegram_chat_id else ""),
            chat_id=chat_id,
        )
    elif text.startswith("/status"):
        tg.send_message(_status(), chat_id=chat_id)
    elif text.startswith("/scan"):
        tg.send_message("🔍 Running an off-schedule scan — this takes a few minutes…",
                        chat_id=chat_id)
        try:
            n = run_scan(force=True)
            if n == 0:
                tg.send_message("Scan complete — nothing met the bar.", chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("manual scan failed")
            tg.send_message(f"❌ Scan failed: {tg.escape(str(exc))}", chat_id=chat_id)
    elif text.startswith("/chatid"):
        tg.send_message(
            f"This chat id is <code>{chat_id}</code>\n\n"
            f"Put it in <code>.env</code> as:\n"
            f"<code>TELEGRAM_CHAT_ID={chat_id}</code>\n\n"
            f"then restart me. That also locks me to this chat.",
            chat_id=chat_id,
        )


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    state.init()

    # Only the token is needed to START. Requiring the chat id here was a
    # deadlock: the id is obtained by running the bot and sending /chatid, so
    # demanding it up front made it impossible to ever get one.
    if not CFG.telegram_token:
        log.error("TELEGRAM_BOT_TOKEN is not set — nothing to connect with")
        return 1

    me = tg.get_me()
    if not me:
        log.error("could not reach Telegram — check TELEGRAM_BOT_TOKEN")
        return 1
    log.info("connected as @%s — polling for updates", me.get("username"))

    if not CFG.telegram_chat_id:
        log.warning(
            "TELEGRAM_CHAT_ID is not set — running in setup mode. Send /chatid "
            "in Telegram, put the number in .env, then restart. Until then only "
            "/start, /help and /chatid are answered."
        )

    offset = state.get_offset()
    while _running:
        updates = tg.get_updates(offset, timeout=30)
        for update in updates:
            offset = update["update_id"] + 1
            state.set_offset(offset)
            try:
                if "callback_query" in update:
                    handlers.dispatch(update["callback_query"])
                elif "message" in update:
                    handle_message(update["message"])
            except Exception:  # noqa: BLE001
                log.exception("failed to process update %s", update.get("update_id"))
        if not updates:
            time.sleep(1)

    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
