#!/usr/bin/env python3
"""Cron entrypoint for the daily scan.

    0 15 * * 1-5  cd /opt/insidemove-agent && python3 run_scan.py >> data/scan.log 2>&1

Flags:
    --force     ignore the weekday guard and the already-alerted check
    --dry-run   print the alerts to stdout instead of sending them
"""
from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="InsideMove Agent daily scan")
    parser.add_argument("--force", action="store_true", help="run regardless of weekday/dedupe")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    parser.add_argument("--refresh-universe", action="store_true", help="force a ticker-list refresh")
    args = parser.parse_args()

    if args.dry_run:
        import os

        os.environ["DRY_RUN"] = "1"

    from insidemove.config import CFG  # imported after DRY_RUN is set
    from insidemove import universe
    from insidemove.scanner import run_scan

    if args.refresh_universe:
        universe.get_universe(force=True)

    try:
        sent = run_scan(force=args.force)
    except Exception:
        logging.exception("scan failed")
        return 1

    logging.info("scan finished — %d alert(s) sent", sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
