#!/usr/bin/env python3
"""Point Telegram at the Cloudflare Worker (or unhook it again).

    python3 scripts/set_webhook.py --url https://insidemove-webhook.<you>.workers.dev
    python3 scripts/set_webhook.py --status
    python3 scripts/set_webhook.py --delete     # back to long-polling

The secret it registers must match the Worker's TELEGRAM_SECRET; Telegram
sends it back on every request and the Worker rejects anything without it.
Pass --secret or set TELEGRAM_SECRET in the environment.

Webhook and long-polling are mutually exclusive: registering a webhook stops
getUpdates from returning anything, so run --delete before going back to
running bot.py.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from insidemove.config import CFG  # noqa: E402


def call(method: str, **payload):
    resp = requests.post(
        f"https://api.telegram.org/bot{CFG.telegram_token}/{method}",
        json=payload,
        timeout=30,
    )
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the Telegram webhook")
    parser.add_argument("--url", help="Worker URL to register")
    parser.add_argument("--secret", help="shared secret (generated if omitted)")
    parser.add_argument("--delete", action="store_true", help="remove the webhook")
    parser.add_argument("--status", action="store_true", help="show current webhook info")
    args = parser.parse_args()

    if not CFG.telegram_token:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 1

    if args.status:
        print(json.dumps(call("getWebhookInfo"), indent=2))
        return 0

    if args.delete:
        result = call("deleteWebhook", drop_pending_updates=False)
        print(json.dumps(result, indent=2))
        print("\nWebhook removed — bot.py long-polling will work again.")
        return 0 if result.get("ok") else 1

    if not args.url:
        parser.error("give --url, or one of --status / --delete")

    secret = args.secret or os.getenv("TELEGRAM_SECRET") or secrets.token_urlsafe(24)
    result = call(
        "setWebhook",
        url=args.url,
        secret_token=secret,
        allowed_updates=["callback_query", "message"],
        max_connections=4,
    )
    print(json.dumps(result, indent=2))

    if result.get("ok"):
        print("\nWebhook registered.")
        if not args.secret and not os.getenv("TELEGRAM_SECRET"):
            print("\nA secret was generated. Give the Worker the same value:")
            print(f"\n    wrangler secret put TELEGRAM_SECRET\n    {secret}\n")
            print("Nothing will work until the Worker holds this exact string.")
        return 0

    print("\nsetWebhook failed — check the URL is reachable over HTTPS.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
