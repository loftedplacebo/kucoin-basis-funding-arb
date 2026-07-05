from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kucoin_basis.config import DEFAULT_CONFIG
from kucoin_basis.opportunity_scanner import scan_once


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan KuCoin spot/perp funding opportunities.")
    parser.add_argument("--loop", action="store_true", help="Run continuously.")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_CONFIG.orderbook_monitor_interval_seconds,
        help="Seconds between loop scans.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    while True:
        path, rows, errors = scan_once(DEFAULT_CONFIG)
        candidates = sum(1 for row in rows if row.decision == "ENTER_CANDIDATE")
        print(f"Wrote {len(rows)} KuCoin basis rows to {path}")
        print(f"Entry candidates: {candidates}")
        if errors:
            print("Errors:")
            for error in errors:
                print(f"  {error}")
        if not args.loop:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
