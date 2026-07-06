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
        try:
            result = run_paper_strategy_once(DEFAULT_CONFIG, opportunity_path=args.opportunities)
        except SystemExit as error:
            if not args.loop:
                raise
            print(f"Paper strategy waiting: {error}", flush=True)
        except Exception as error:
            if not args.loop:
                raise
            print(f"Paper strategy error: {type(error).__name__}: {error}", flush=True)
        else:
            print(
                f"Processed {result['opportunities_seen']} opportunities from {result['opportunity_file']}",
                flush=True,
            )
            print(f"Entries opened: {result['entries_opened']}", flush=True)
            print(f"Open positions: {result['open_positions']}", flush=True)
        if not args.loop:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
