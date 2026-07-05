from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kucoin_basis.config import DEFAULT_CONFIG, KucoinBasisConfig
from kucoin_basis.models import parse_datetime, parse_float
from kucoin_basis.paper_store import PaperStore


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def is_today(row: dict, field: str, now: datetime) -> bool:
    timestamp = parse_datetime(row.get(field))
    return timestamp is not None and timestamp.date() == now.astimezone(timezone.utc).date()


def latest_opportunities(config: KucoinBasisConfig) -> list[dict]:
    files = sorted(config.opportunities_dir.glob("kucoin_basis_opportunities_*.csv"))
    if not files:
        return []
    rows = load_csv(files[-1])
    rows.sort(key=lambda row: parse_float(row.get("expected_edge_pct"), -999) or -999, reverse=True)
    return rows[:10]


def main() -> None:
    parser = argparse.ArgumentParser(description="Print KuCoin basis paper strategy summary.")
    parser.parse_args()

    config = DEFAULT_CONFIG
    store = PaperStore(config)
    now = datetime.now(timezone.utc)
    positions = store.load_all_positions()
    open_positions = [position for position in positions if position.status == "OPEN"]
    fills = load_csv(store.fills_path)
    funding_events = load_csv(store.funding_events_path)
    decisions = load_csv(store.decisions_path)

    by_symbol = defaultdict(float)
    for position in open_positions:
        by_symbol[f"{position.base} {position.direction}"] += position.notional_usd

    realised_funding_today = sum(
        parse_float(row.get("funding_pnl_usd"), 0.0) or 0.0
        for row in funding_events
        if is_today(row, "timestamp_utc", now)
    )
    realised_total_today = sum(
        parse_float(row.get("realised_pnl_usd"), 0.0) or 0.0
        for row in fills
        if is_today(row, "timestamp_utc", now)
    ) + realised_funding_today

    entry_rejections = Counter(
        row.get("reason", "")
        for row in decisions
        if row.get("decision_type") == "ENTRY" and str(row.get("allowed")).lower() == "false"
    )
    exit_reasons = Counter(
        row.get("reason", "")
        for row in decisions
        if row.get("decision_type") == "EXIT"
    )

    print(f"KuCoin basis data: {config.data_dir}")
    print(f"Open positions count: {len(open_positions)}")
    print(f"Total open notional: ${sum(position.notional_usd for position in open_positions):,.2f}")
    print(f"Latest estimated open PnL: ${sum(position.estimated_net_pnl_usd for position in open_positions):,.4f}")
    print(f"Realised funding PnL today: ${realised_funding_today:,.4f}")
    print(f"Realised total PnL today: ${realised_total_today:,.4f}")
    print(f"Funding events captured today: {sum(1 for row in funding_events if is_today(row, 'timestamp_utc', now))}")

    print("\nOpen positions by symbol")
    if not by_symbol:
        print("  none")
    else:
        for key, notional in sorted(by_symbol.items()):
            print(f"  {key}: ${notional:,.2f}")

    print("\nBest current opportunities")
    rows = latest_opportunities(config)
    if not rows:
        print("  none")
    else:
        for row in rows:
            print(
                "  "
                f"{row.get('base')} {row.get('direction', 'LONG_SPOT_SHORT_PERP')} "
                f"${parse_float(row.get('notional_usd'), 0) or 0:,.0f} "
                f"funding={parse_float(row.get('funding_rate_pct'), 0) or 0:.4f}% "
                f"edge={parse_float(row.get('expected_edge_pct'), 0) or 0:.4f}% "
                f"decision={row.get('decision')} reason={row.get('reason')}"
            )

    print("\nMost common entry rejection reasons")
    if not entry_rejections:
        print("  none")
    else:
        for reason, count in entry_rejections.most_common(10):
            print(f"  {reason}: {count}")

    print("\nMost common exit reasons")
    if not exit_reasons:
        print("  none")
    else:
        for reason, count in exit_reasons.most_common(10):
            print(f"  {reason}: {count}")


if __name__ == "__main__":
    main()
