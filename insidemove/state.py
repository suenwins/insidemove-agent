"""Small persistent state, as plain JSON files.

This replaces the SQLite store. Two reasons.

First, the callback tokens it existed for are gone: Telegram caps
`callback_data` at 64 bytes, and `a:AAPL:20260904` is fifteen. The ticker and
session fit directly in the button, so there is nothing to look up.

Second, the whole point of the GitHub Actions deployment is that no machine
is kept running, which means no persistent disk either. What survives between
runs is whatever the Actions cache restores — and a couple of small JSON
files restore cleanly, where a WAL-mode SQLite database restored mid-write
does not.

Everything here is small: which alerts went out on which session, the ticker
universe, and the long-poll offset for the VPS deployment.
"""
from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CFG

log = logging.getLogger(__name__)


def _path(name: str) -> Path:
    CFG.data_dir.mkdir(parents=True, exist_ok=True)
    return CFG.data_dir / f"{name}.json"


def _read(name: str, default: Any) -> Any:
    path = _path(name)
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("state file %s unreadable (%s) — using default", path.name, exc)
        return default


def _write(name: str, value: Any) -> None:
    """Write via a temp file and rename, so an interrupted run can never
    leave a half-written file that fails to parse on the next one."""
    path = _path(name)
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=f".{name}.", suffix=".tmp",
            delete=False, encoding="utf-8",
        ) as tmp:
            json.dump(value, tmp, ensure_ascii=False)
            temp_name = tmp.name
        Path(temp_name).replace(path)
    except Exception as exc:  # noqa: BLE001
        log.error("could not write state file %s: %s", path.name, exc)


def init() -> None:
    CFG.data_dir.mkdir(parents=True, exist_ok=True)
    CFG.cache_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# key/value — each key gets its own file so the large universe blob is never
# rewritten just to bump a counter
# ---------------------------------------------------------------------------

def kv_get(key: str) -> Any | None:
    return _read(f"kv_{key}", None)


def kv_set(key: str, value: Any) -> None:
    _write(f"kv_{key}", value)


# ---------------------------------------------------------------------------
# alert de-duplication
# ---------------------------------------------------------------------------

def _alerts() -> dict:
    return _read("alerts", {})


def already_alerted(ticker: str, session_date: str) -> bool:
    return ticker in _alerts().get(session_date, {})


def record_alert(
    ticker: str,
    session_date: str,
    message_id: int | None = None,
    pct_change: float | None = None,
    tier: str = "",
    reasons: list[str] | None = None,
) -> None:
    alerts = _alerts()
    alerts.setdefault(session_date, {})[ticker] = {
        "message_id": message_id,
        "pct_change": pct_change,
        "tier": tier,
        "reasons": reasons or [],
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # Keep the last 60 sessions; the file stays a few tens of KB forever.
    for old in sorted(alerts)[:-60]:
        alerts.pop(old, None)
    _write("alerts", alerts)


def last_session() -> tuple[str, dict] | None:
    alerts = _alerts()
    if not alerts:
        return None
    newest = max(alerts)
    return newest, alerts[newest]


# ---------------------------------------------------------------------------
# long-poll offset (VPS deployment only; unused under the webhook)
# ---------------------------------------------------------------------------

def get_offset() -> int:
    return int(_read("offset", {"value": 0}).get("value", 0))


def set_offset(value: int) -> None:
    _write("offset", {"value": int(value)})
