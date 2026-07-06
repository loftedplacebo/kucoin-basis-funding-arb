from __future__ import annotations

import argparse
import csv
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kucoin_basis.config import DEFAULT_CONFIG
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
    return parser.parse_args()


def _append_scanner_run(row: dict) -> None:
    DEFAULT_CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
    path = DEFAULT_CONFIG.data_dir / "scanner_runs.csv"
    file_exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SCANNER_RUN_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in SCANNER_RUN_FIELDS})


def _run_once() -> None:
    started = time.monotonic()
    timestamp = datetime.now(timezone.utc).isoformat()
    path, rows, errors = scan_once(DEFAULT_CONFIG)
    candidates = sum(1 for row in rows if row.decision == "ENTER_CANDIDATE")
    elapsed = time.monotonic() - started
    _append_scanner_run(
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


def _log_exception(error: Exception, started: float) -> None:
    elapsed = time.monotonic() - started
    summary = f"{type(error).__name__}: {error}"
    _append_scanner_run(
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
    while True:
        started = time.monotonic()
        try:
            _run_once()
        except Exception as error:
            _log_exception(error, started)
            if not args.loop:
                raise
        if not args.loop:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
