#!/usr/bin/env python3
"""Print your Telegram chat id without running the bot.

    1. Send any message to your bot in Telegram (just "hi" is enough)
    2. python scripts/get_chat_id.py

Only TELEGRAM_BOT_TOKEN needs to be set. This exists because the chat id is
needed to configure the bot, and needing a fully configured bot to discover it
would be circular.
"""
from __future__ import annotations

import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from insidemove.config import CFG  # noqa: E402


def main() -> int:
    if not CFG.telegram_token:
        print("TELEGRAM_BOT_TOKEN is not set in .env", file=sys.stderr)
        return 1

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{CFG.telegram_token}/getUpdates",
            params={"limit": 20},
            timeout=30,
        )
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach Telegram: {exc}", file=sys.stderr)
        return 1

    if not payload.get("ok"):
        print(f"Telegram rejected the token: {payload.get('description')}", file=sys.stderr)
        return 1

    chats: dict[str, str] = {}
    for update in payload.get("result", []):
        message = update.get("message") or update.get("callback_query", {}).get("message")
        if not message:
            continue
        chat = message.get("chat", {})
        if chat.get("id") is None:
            continue
        who = chat.get("username") or chat.get("title") or chat.get("first_name") or "?"
        chats[str(chat["id"])] = f"{who} ({chat.get('type', 'chat')})"

    if not chats:
        print(
            "No messages found.\n\n"
            "Telegram only keeps recent updates, and a registered webhook\n"
            "consumes them. Send your bot a message, then run this again.\n"
            "If a webhook is set, remove it first:\n"
            "    python scripts/set_webhook.py --delete"
        )
        return 1

    print("\nFound:\n")
    for cid, who in chats.items():
        print(f"  {cid}   {who}")
    if len(chats) == 1:
        only = next(iter(chats))
        print(f"\nAdd this line to .env:\n\n    TELEGRAM_CHAT_ID={only}\n")
    else:
        print("\nSeveral chats seen — pick your own and add it to .env as TELEGRAM_CHAT_ID.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
