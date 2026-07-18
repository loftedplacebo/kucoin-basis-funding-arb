from __future__ import annotations

import argparse
import csv
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kucoin_basis.config import DEFAULT_CONFIG, KucoinBasisConfig
from kucoin_basis.kucoin_public_client import KucoinPublicClient
from kucoin_basis.opportunity_scanner import scan_once


SCANNER_RUN_FIELDS = [
    "timestamp_utc",
    "status",
    "opportunity_file",
    "rows",
    "entry_candidates",
    "errors",
    "exception",
    "elapsed_seconds",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan KuCoin spot/perp funding opportunities.")
    parser.add_argument("--loop", action="store_true", help="Run continuously.")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_CONFIG.orderbook_monitor_interval_seconds,
        help="Seconds between loop scans.",
    )
    parser.add_argument(
        "--state-mode",
        choices=("paper", "dry-run"),
        default="paper",
        help="Position ledger used to keep open symbols on the scanner watchlist.",
    )
    return parser.parse_args()


def config_for_state_mode(state_mode: str) -> KucoinBasisConfig:
    if state_mode == "dry-run":
        return replace(DEFAULT_CONFIG, paper_dir=DEFAULT_CONFIG.data_dir / "dry_run")
    if state_mode == "paper":
        return DEFAULT_CONFIG
    raise ValueError(f"Unsupported scanner state mode: {state_mode}")


def _append_scanner_run(config: KucoinBasisConfig, row: dict) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    path = config.data_dir / "scanner_runs.csv"
    file_exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SCANNER_RUN_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in SCANNER_RUN_FIELDS})


def _run_once(config: KucoinBasisConfig, client: KucoinPublicClient) -> None:
    started = time.monotonic()
    timestamp = datetime.now(timezone.utc).isoformat()
    path, rows, errors = scan_once(config, client)
    candidates = sum(1 for row in rows if row.decision == "ENTER_CANDIDATE")
    elapsed = time.monotonic() - started
    _append_scanner_run(
        config,
        {
            "timestamp_utc": timestamp,
            "status": "OK" if not errors else "OK_WITH_ERRORS",
            "opportunity_file": str(path),
            "rows": len(rows),
            "entry_candidates": candidates,
            "errors": " | ".join(errors),
            "elapsed_seconds": f"{elapsed:.3f}",
        }
    )
    print(f"Wrote {len(rows)} KuCoin basis rows to {path}", flush=True)
    print(f"Entry candidates: {candidates}", flush=True)
    if errors:
        print("Errors:", flush=True)
        for error in errors:
            print(f"  {error}", flush=True)


def _log_exception(config: KucoinBasisConfig, error: Exception, started: float) -> None:
    elapsed = time.monotonic() - started
    summary = f"{type(error).__name__}: {error}"
    _append_scanner_run(
        config,
        {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "status": "ERROR",
            "exception": summary,
            "errors": traceback.format_exc(limit=8).replace("\n", "\\n"),
            "elapsed_seconds": f"{elapsed:.3f}",
        }
    )
    print(f"Scanner error: {summary}", flush=True)


def main() -> None:
    args = parse_args()
    config = config_for_state_mode(args.state_mode)
    client = KucoinPublicClient()
    print(f"Scanner position watchlist: {args.state_mode} ({config.paper_dir})", flush=True)
    while True:
        started = time.monotonic()
        try:
            _run_once(config, client)
        except Exception as error:
            _log_exception(config, error, started)
            if not args.loop:
                raise
        if not args.loop:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
