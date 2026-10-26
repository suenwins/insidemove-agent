"""Central configuration for InsideMove Agent.

Every knob the operator is likely to touch lives here and can be overridden
with an environment variable, so the container image never needs rebuilding
just to change a threshold.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Precedence, strongest first:
#   1. real environment variables  — GitHub Actions `env:` blocks, or a value
#      exported for a single run
#   2. .env                        — your machine, gitignored, holds secrets
#   3. settings.env                — committed tuning knobs, no secrets
#
# python-dotenv never overwrites a variable that is already set, so loading in
# this order gives exactly that. The point is that a threshold committed to
# settings.env applies identically on your laptop and on the deployed bot,
# instead of drifting between the two.
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "settings.env")


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # ---- identity -------------------------------------------------------
    bot_name: str = "InsideMove Agent"

    # ---- credentials ----------------------------------------------------
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    finnhub_api_key: str = os.getenv("FINNHUB_API_KEY", "")
    # SEC asks automated clients to identify themselves in the User-Agent.
    # Optional: an email here makes EDGAR less likely to throttle the bot.
    sec_contact: str = os.getenv("SEC_CONTACT", "")
    sec_filings_enabled: bool = _b("SEC_FILINGS", True)
    free_feeds_enabled: bool = _b("FREE_FEEDS", True)

    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    # Which engine writes the notes: "template" (deterministic, free) or
    # "anthropic". STAGE3_ENGINE can override it for the equity note alone.
    note_engine: str = os.getenv("NOTE_ENGINE", "template").strip().lower()
    stage3_engine: str = (
        os.getenv("STAGE3_ENGINE", "").strip().lower()
        or os.getenv("NOTE_ENGINE", "template").strip().lower()
    )

    # ---- scheduling -----------------------------------------------------
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(os.getenv("TZ_NAME", "Asia/Kuala_Lumpur")))
    scan_hour: int = _i("SCAN_HOUR", 15)
    scan_minute: int = _i("SCAN_MINUTE", 0)

    # ---- Stage 1: primary trigger --------------------------------------
    # Absolute single-session % move that always qualifies a ticker.
    move_threshold_pct: float = _f("MOVE_THRESHOLD_PCT", 7.0)
    # "both" | "up" | "down"
    direction: str = os.getenv("DIRECTION", "both")

    # ---- Stage 1: secondary "interesting price action" signals ---------
    # Each secondary signal needs a minimum move so we do not surface noise.
    secondary_min_move_pct: float = _f("SECONDARY_MIN_MOVE_PCT", 3.0)
    volume_spike_x: float = _f("VOLUME_SPIKE_X", 3.0)
    gap_pct: float = _f("GAP_PCT", 4.0)
    range_expansion_x: float = _f("RANGE_EXPANSION_X", 2.5)
    wick_ratio: float = _f("WICK_RATIO", 0.6)
    wick_volume_x: float = _f("WICK_VOLUME_X", 2.0)

    max_alerts_per_day: int = _i("MAX_ALERTS_PER_DAY", 5)
    # Cap on how many of the daily slots secondary-only names may take.
    max_secondary_slots: int = _i("MAX_SECONDARY_SLOTS", 2)

    # ---- data windows ---------------------------------------------------
    lookback_days: int = _i("LOOKBACK_DAYS", 200)   # fetched history
    chart_days: int = _i("CHART_DAYS", 90)          # Stage 4 chart window
    news_window_hours: int = _i("NEWS_WINDOW_HOURS", 24)
    min_price: float = _f("MIN_PRICE", 3.0)
    min_avg_dollar_volume: float = _f("MIN_AVG_DOLLAR_VOLUME", 5_000_000)

    # ---- indicator params ----------------------------------------------
    sma_fast: int = _i("SMA_FAST", 20)
    sma_slow: int = _i("SMA_SLOW", 50)
    atr_period: int = _i("ATR_PERIOD", 14)
    rsi_period: int = _i("RSI_PERIOD", 14)

    # ---- paths ----------------------------------------------------------
    data_dir: Path = Path(os.getenv("DATA_DIR", str(ROOT / "data")))

    # ---- behaviour ------------------------------------------------------
    dry_run: bool = _b("DRY_RUN", False)
    universe_refresh_days: int = _i("UNIVERSE_REFRESH_DAYS", 7)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "insidemove.sqlite3"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def missing_credentials(self) -> list[str]:
        missing = []
        if not self.telegram_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        # Only required when an engine actually calls Claude. Under the
        # default template engine the bot needs no AI key at all.
        if "anthropic" in {self.note_engine, self.stage3_engine} and not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        return missing


CFG = Config()
CFG.ensure_dirs()
