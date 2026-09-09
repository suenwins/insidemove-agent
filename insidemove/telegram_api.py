"""Thin Telegram Bot API client.

Deliberately dependency-free (just `requests`) rather than pulling in
python-telegram-bot: the bot needs six endpoints and a long-poll loop, and a
small explicit client is easier to reason about and to run under cron
alongside a separate poller process.
"""
from __future__ import annotations

import html
import logging
import time
from pathlib import Path
from typing import Any

import requests

from .config import CFG

log = logging.getLogger(__name__)

MAX_LEN = 4000  # Telegram's cap is 4096; leave headroom for the split marker.


def _url(method: str) -> str:
    return f"https://api.telegram.org/bot{CFG.telegram_token}/{method}"


def _post(method: str, retries: int = 3, **payload) -> dict[str, Any] | None:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(_url(method), json=payload, timeout=60)
            data = resp.json()
            if data.get("ok"):
                return data["result"]
            desc = data.get("description", "")
            if "retry after" in desc.lower():
                wait = int(data.get("parameters", {}).get("retry_after", 5))
                log.warning("telegram rate limited, sleeping %ds", wait)
                time.sleep(wait)
                continue
            log.error("telegram %s failed: %s", method, desc)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram %s error (attempt %d/%d): %s", method, attempt, retries, exc)
            time.sleep(2**attempt)
    return None


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def _split(text: str) -> list[str]:
    """Split on paragraph boundaries so a section header never lands alone."""
    if len(text) <= MAX_LEN:
        return [text]
    parts, current = [], ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > MAX_LEN:
            if current:
                parts.append(current.strip())
            current = block
            while len(current) > MAX_LEN:
                parts.append(current[:MAX_LEN])
                current = current[MAX_LEN:]
        else:
            current = f"{current}\n\n{block}" if current else block
    if current.strip():
        parts.append(current.strip())
    return parts


def keyboard(buttons: list[list[tuple[str, str]]]) -> dict:
    """buttons: rows of (label, callback_data) pairs."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in buttons
        ]
    }


def send_message(
    text: str,
    chat_id: str | None = None,
    reply_markup: dict | None = None,
    reply_to: int | None = None,
    preview: bool = False,
) -> int | None:
    """Returns the message_id of the last chunk sent."""
    chat_id = chat_id or CFG.telegram_chat_id
    chunks = _split(text)
    last_id = None
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": not preview},
        }
        if reply_to and i == 0:
            payload["reply_to_message_id"] = reply_to
            payload["allow_sending_without_reply"] = True
        if reply_markup and is_last:
            payload["reply_markup"] = reply_markup
        result = _post("sendMessage", **payload)
        if result:
            last_id = result.get("message_id")
        if not is_last:
            time.sleep(0.4)
    return last_id


def send_photo(
    path: Path, caption: str = "", chat_id: str | None = None, reply_to: int | None = None
) -> int | None:
    chat_id = chat_id or CFG.telegram_chat_id
    data = {"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML"}
    if reply_to:
        data["reply_to_message_id"] = str(reply_to)
        data["allow_sending_without_reply"] = "true"
    try:
        with open(path, "rb") as fh:
            resp = requests.post(
                _url("sendPhoto"), data=data, files={"photo": fh}, timeout=120
            )
        payload = resp.json()
        if payload.get("ok"):
            return payload["result"].get("message_id")
        log.error("sendPhoto failed: %s", payload.get("description"))
    except Exception as exc:  # noqa: BLE001
        log.error("sendPhoto error: %s", exc)
    return None


def answer_callback(callback_id: str, text: str = "", alert: bool = False) -> None:
    _post("answerCallbackQuery", callback_query_id=callback_id, text=text[:200], show_alert=alert)


def edit_markup(chat_id: int | str, message_id: int, reply_markup: dict | None = None) -> None:
    _post(
        "editMessageReplyMarkup",
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=reply_markup or {"inline_keyboard": []},
    )


def get_updates(offset: int, timeout: int = 30) -> list[dict]:
    try:
        resp = requests.get(
            _url("getUpdates"),
            params={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": '["callback_query","message"]',
            },
            timeout=timeout + 20,
        )
        data = resp.json()
        return data.get("result", []) if data.get("ok") else []
    except requests.exceptions.Timeout:
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("getUpdates error: %s", exc)
        time.sleep(3)
        return []


def get_me() -> dict | None:
    return _post("getMe")
