#!/usr/bin/env python3
"""Entry point for one Telegram update, used by the GitHub Actions deployment.

The Cloudflare Worker receives the webhook, answers the callback instantly so
Telegram stops waiting, then fires a `repository_dispatch` carrying the raw
update. This script is what that workflow runs.

    python3 handle_callback.py --update '<json>'
    python3 handle_callback.py --update-file update.json

It is deliberately the same code path the long-polling bot uses, so the VPS
and serverless deployments cannot drift apart.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("insidemove.callback")


def main() -> int:
    parser = argparse.ArgumentParser(description="Handle one Telegram update")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--update", help="the update as a JSON string")
    source.add_argument("--update-file", help="path to a file holding the JSON")
    args = parser.parse_args()

    raw = args.update
    if args.update_file:
        with open(args.update_file, encoding="utf-8") as fh:
            raw = fh.read()

    try:
        update = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("update is not valid JSON: %s", exc)
        return 1

    from insidemove import handlers, state
    from insidemove.config import CFG

    state.init()
    missing = CFG.missing_credentials()
    if missing:
        log.error("missing credentials: %s", ", ".join(missing))
        return 1

    # The Worker verifies Telegram's secret header, but this process can also
    # be reached by anyone able to fire a repository_dispatch, so the chat is
    # re-checked here rather than trusted from upstream.
    def authorised(chat_id) -> bool:
        if not CFG.telegram_chat_id:
            return True
        if str(chat_id) != str(CFG.telegram_chat_id):
            log.warning("ignoring update from unauthorised chat %s", chat_id)
            return False
        return True

    try:
        if "callback_query" in update:
            callback = update["callback_query"]
            chat = (callback.get("message") or {}).get("chat", {}).get("id")
            if not authorised(chat):
                return 0
            handlers.dispatch(callback)
        elif "message" in update:
            import bot

            message = update["message"]
            if not authorised(message.get("chat", {}).get("id")):
                return 0
            bot.handle_message(message)
        else:
            log.info("update carries nothing actionable — ignoring")
    except Exception:
        log.exception("failed to handle update")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
