"""Incremental price history cache.

The first run downloads a year of daily bars for the whole universe. Every
run after that downloads only the handful of sessions it is missing and
appends them, which is the difference between an eight-minute job and a
forty-second one.

Stored as a single long-format parquet (ticker, date, OHLCV) rather than one
file per ticker: 3,500 small files are slow to open and awkward to cache,
whereas one compressed file is a few tens of megabytes and restores in
seconds on a CI runner.

**Corporate actions are the reason this is not a trivial append.** Yahoo
serves unadjusted prices, so the morning after a 4-for-1 split every
historical bar it returns is a quarter of what we cached yesterday. Blindly
appending would leave the cache holding pre-split prices next to post-split
ones, and every percentage, average and level computed from it would be
quietly wrong. So each refresh re-fetches a short overlapping window and
compares closes on the dates it already holds; a ticker whose history has
been restated is dropped and re-downloaded in full.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd

from . import data
from .config import CFG

log = logging.getLogger(__name__)

COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

# How far back a refresh re-fetches. Must comfortably exceed the longest
# market closure (and any weekend plus holiday run) so no session is skipped,
# and must overlap the cache by several bars so splits are detectable.
REFRESH_PERIOD = "1mo"
OVERLAP_BARS = 5

# A close differing by more than this on a date we already hold means the
# history was restated (split, reverse split, or a data correction).
RESTATEMENT_TOLERANCE = 0.005  # 0.5%

# Keep a little over a year of calendar days so 252 trading bars survive.
RETAIN_DAYS = 400


class PriceCache:
    def __init__(self, path=None):
        self.path = path or (CFG.data_dir / "prices.parquet")
        self._frame: pd.DataFrame | None = None

    # -- persistence -------------------------------------------------------

    def load(self) -> pd.DataFrame:
        if self._frame is not None:
            return self._frame
        if not self.path.exists():
            self._frame = pd.DataFrame(columns=["ticker", "date"] + COLUMNS)
        else:
            try:
                self._frame = pd.read_parquet(self.path)
            except Exception as exc:  # noqa: BLE001
                log.warning("cache unreadable (%s) — starting fresh", exc)
                self._frame = pd.DataFrame(columns=["ticker", "date"] + COLUMNS)
        if not self._frame.empty:
            self._frame["date"] = pd.to_datetime(self._frame["date"])
        return self._frame

    def save(self) -> None:
        if self._frame is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._frame.to_parquet(self.path, index=False, compression="zstd")
        log.info(
            "cache saved: %d rows, %d tickers, %.1f MB",
            len(self._frame),
            self._frame["ticker"].nunique() if not self._frame.empty else 0,
            self.path.stat().st_size / 1e6,
        )

    # -- inspection --------------------------------------------------------

    def coverage(self) -> dict[str, pd.Timestamp]:
        """Last cached session per ticker."""
        frame = self.load()
        if frame.empty:
            return {}
        return frame.groupby("ticker")["date"].max().to_dict()

    def stats(self) -> dict:
        frame = self.load()
        if frame.empty:
            return {"tickers": 0, "rows": 0, "oldest": None, "newest": None}
        return {
            "tickers": int(frame["ticker"].nunique()),
            "rows": int(len(frame)),
            "oldest": frame["date"].min().date().isoformat(),
            "newest": frame["date"].max().date().isoformat(),
        }

    # -- the update path ---------------------------------------------------

    @staticmethod
    def _to_long(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
        rows = []
        for ticker, df in frames.items():
            block = df[COLUMNS].copy()
            block["ticker"] = ticker
            block["date"] = pd.to_datetime(df.index)
            rows.append(block.reset_index(drop=True))
        if not rows:
            return pd.DataFrame(columns=["ticker", "date"] + COLUMNS)
        return pd.concat(rows, ignore_index=True)[["ticker", "date"] + COLUMNS]

    def _restated(self, ticker: str, cached: pd.DataFrame, fresh: pd.DataFrame) -> bool:
        """True if the freshly fetched bars disagree with what we hold on
        dates common to both — the fingerprint of a split or a correction."""
        overlap = cached.merge(
            fresh, on="date", suffixes=("_old", "_new"), how="inner"
        ).tail(OVERLAP_BARS)
        if overlap.empty:
            return False
        old, new = overlap["Close_old"], overlap["Close_new"]
        drift = ((old - new).abs() / old.replace(0, pd.NA)).max()
        if pd.notna(drift) and drift > RESTATEMENT_TOLERANCE:
            log.info(
                "%s: history restated (max close drift %.2f%% over %d overlapping bars)"
                " — refetching in full",
                ticker, drift * 100, len(overlap),
            )
            return True
        return False

    def update(self, tickers: list[str], full_period: str = "1y") -> dict:
        """Bring the cache up to date. Returns a small report."""
        tickers = sorted(set(t for t in tickers if t))
        frame = self.load()
        coverage = self.coverage()

        # Staleness is measured against the last COMPLETED session, not against
        # today. Measuring against today is subtly broken: a run during US
        # market hours caches an in-progress bar dated today, which then looks
        # current tomorrow as well — so the next real session never gets
        # fetched and the cache silently rots a day behind.
        ceiling = pd.Timestamp(data.latest_completed_session())

        cold = [t for t in tickers if t not in coverage]
        warm = [t for t in tickers if t in coverage and coverage[t] < ceiling]

        report = {"cold": len(cold), "warm": len(warm), "restated": 0, "added_rows": 0}
        log.info(
            "cache update: %d new tickers (full history), %d to refresh, %d already current",
            len(cold), len(warm), len(tickers) - len(cold) - len(warm),
        )

        new_blocks: list[pd.DataFrame] = []
        drop_tickers: set[str] = set()

        # --- warm path: short window, verified against what we hold --------
        if warm:
            fetched = data.fetch_history(warm, period=REFRESH_PERIOD)
            for ticker, fresh in fetched.items():
                fresh = fresh[COLUMNS].copy()
                fresh["date"] = pd.to_datetime(fresh.index)
                fresh = fresh.reset_index(drop=True)

                cached = frame[frame["ticker"] == ticker][["date"] + COLUMNS]
                if self._restated(ticker, cached, fresh):
                    drop_tickers.add(ticker)
                    cold.append(ticker)
                    report["restated"] += 1
                    continue

                known = set(cached["date"])
                addition = fresh[~fresh["date"].isin(known)].copy()
                if addition.empty:
                    continue
                addition["ticker"] = ticker
                new_blocks.append(addition[["ticker", "date"] + COLUMNS])

        # --- cold path: full history --------------------------------------
        if cold:
            fetched = data.fetch_history(cold, period=full_period)
            new_blocks.append(self._to_long(fetched))
            drop_tickers.update(fetched.keys())

        if drop_tickers and not frame.empty:
            frame = frame[~frame["ticker"].isin(drop_tickers)]

        blocks = [b for b in ([frame] if not frame.empty else []) + new_blocks if not b.empty]
        merged = pd.concat(blocks, ignore_index=True) if blocks else frame

        if not merged.empty:
            # Belt and braces: even if a partial bar was fetched, it never
            # reaches the stored cache.
            merged = merged[merged["date"] <= ceiling]
            report["added_rows"] = len(merged) - len(self.load())
            merged = merged.drop_duplicates(subset=["ticker", "date"], keep="last")
            cutoff = pd.Timestamp(datetime.now() - timedelta(days=RETAIN_DAYS))
            merged = merged[merged["date"] >= cutoff]
            merged = merged.sort_values(["ticker", "date"]).reset_index(drop=True)

        self._frame = merged
        return report

    # -- reading back ------------------------------------------------------

    def frames(self, tickers: list[str] | None = None, min_bars: int = 30) -> dict[str, pd.DataFrame]:
        """Per-ticker OHLCV frames, in the shape the rest of the app expects."""
        frame = self.load()
        if frame.empty:
            return {}
        if tickers is not None:
            frame = frame[frame["ticker"].isin(set(tickers))]

        out: dict[str, pd.DataFrame] = {}
        for ticker, block in frame.groupby("ticker", sort=False):
            if len(block) < min_bars:
                continue
            df = block.sort_values("date").set_index("date")[COLUMNS]
            df.index.name = None
            out[str(ticker)] = df
        return out


def refresh(tickers: list[str], full_period: str = "1y") -> tuple[dict[str, pd.DataFrame], dict]:
    """Convenience: update the cache and hand back usable frames."""
    cache = PriceCache()
    report = cache.update(tickers, full_period=full_period)
    cache.save()
    return cache.frames(tickers), report
