from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kucoin_basis.config import DEFAULT_CONFIG
from kucoin_basis.execution import KucoinDryRunExecutor
from kucoin_basis.paper_strategy import run_paper_strategy_once


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the KuCoin basis paper strategy.")
    parser.add_argument("--opportunities", type=Path, default=None)
    parser.add_argument(
        "--execution-mode",
        choices=("paper", "dry-run"),
        default="paper",
        help="paper uses public data only; dry-run validates both legs with KuCoin test orders.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPO_ROOT / ".env",
        help="Credentials file used only in dry-run mode.",
    )
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
    config = DEFAULT_CONFIG
    execution_adapter = None
    if args.execution_mode == "dry-run":
        config = replace(
            DEFAULT_CONFIG,
            paper_dir=DEFAULT_CONFIG.data_dir / "dry_run",
        )
        execution_adapter = KucoinDryRunExecutor.from_env_file(
            args.env_file,
            max_hedge_mismatch_bps=config.dry_run_max_hedge_mismatch_bps,
            max_entry_leg_slippage_pct=config.max_entry_leg_slippage_pct,
            max_exit_leg_slippage_pct=config.max_exit_leg_slippage_pct,
            max_combined_entry_slippage_pct=config.max_combined_entry_slippage_pct,
            max_combined_exit_slippage_pct=config.max_combined_exit_slippage_pct,
        )
        print(
            "DRY RUN: KuCoin non-matching test orders enabled; live order endpoints are unavailable.",
            flush=True,
        )
    while True:
        try:
            result = run_paper_strategy_once(
                config,
                opportunity_path=args.opportunities,
                execution_adapter=execution_adapter,
            )
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
            if args.execution_mode == "dry-run":
                print(
                    "Execution preflights: "
                    f"{result['execution_attempts']} "
                    f"({result['execution_rejections']} rejected)",
                    flush=True,
                )
        if not args.loop:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
