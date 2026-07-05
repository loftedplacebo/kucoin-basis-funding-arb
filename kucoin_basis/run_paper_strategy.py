from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kucoin_basis.config import DEFAULT_CONFIG
from kucoin_basis.paper_strategy import run_paper_strategy_once


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the KuCoin basis paper strategy.")
    parser.add_argument("--opportunities", type=Path, default=None)
    parser.add_argument("--loop", action="store_true", help="Run continuously.")
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between paper strategy passes when looping.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    while True:
        result = run_paper_strategy_once(DEFAULT_CONFIG, opportunity_path=args.opportunities)
        print(f"Processed {result['opportunities_seen']} opportunities from {result['opportunity_file']}")
        print(f"Entries opened: {result['entries_opened']}")
        print(f"Open positions: {result['open_positions']}")
        if not args.loop:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
