from __future__ import annotations

import csv
from pathlib import Path

from kucoin_basis.config import KucoinBasisConfig
from kucoin_basis.models import format_datetime, parse_datetime, utc_now
from kucoin_basis.paper_models import PaperPosition


POSITION_FIELDS = [
    "position_id",
    "base",
    "direction",
    "spot_symbol",
    "perp_symbol",
    "notional_usd",
    "spot_qty",
    "perp_qty",
    "spot_entry_price",
    "perp_entry_price",
    "entry_basis_pct",
    "current_basis_pct",
    "funding_rate_pct_at_entry",
    "expected_funding_pct",
    "realised_funding_pnl_usd",
    "unrealised_basis_pnl_usd",
    "estimated_close_cost_usd",
    "estimated_net_pnl_usd",
    "created_at",
    "updated_at",
    "next_funding_time",
    "funding_events_captured",
    "funding_interval_hours",
    "adverse_funding_since",
    "status",
]

FILL_FIELDS = [
    "timestamp_utc",
    "event_type",
    "position_id",
    "base",
    "direction",
    "spot_symbol",
    "perp_symbol",
    "notional_usd",
    "spot_price",
    "perp_price",
    "fees_usd",
    "realised_pnl_usd",
    "realised_basis_pnl_usd",
    "realised_funding_pnl_usd",
    "reason",
]

FUNDING_EVENT_FIELDS = [
    "timestamp_utc",
    "position_id",
    "base",
    "direction",
    "perp_symbol",
    "funding_time_utc",
    "funding_rate_pct",
    "notional_usd",
    "funding_pnl_usd",
]

DECISION_FIELDS = [
    "timestamp_utc",
    "decision_type",
    "base",
    "direction",
    "position_id",
    "opportunity_key",
    "allowed",
    "reason",
    "notional_usd",
    "expected_edge_pct",
    "estimated_net_pnl_usd",
    "row_timestamp_utc",
    "row_age_seconds",
    "entry_basis_pct",
    "current_basis_pct",
    "basis_improvement_pct",
    "exit_mode",
    "expected_next_funding_usd",
    "pre_funding_exit_profit_usd",
    "basis_target_reached",
    "all_in_chunk_profit_usd",
    "capital_recycle_triggered",
    "foregone_funding_usd",
    "economic_hold_applied",
    "economic_comparison_chunk_usd",
    "risk_adjusted_next_funding_usd",
    "risk_adjusted_exit_redeploy_usd",
    "best_redeployment_edge_pct",
    "basis_giveback_risk_usd",
]

PROCESSED_FIELDS = ["opportunity_key", "timestamp_utc", "source_file", "processed_at_utc"]

COOLDOWN_FIELDS = [
    "timestamp_utc",
    "base",
    "direction",
    "reason",
    "expires_at_utc",
]

EXECUTION_ATTEMPT_FIELDS = [
    "timestamp_utc",
    "mode",
    "action",
    "base",
    "direction",
    "requested_notional_usd",
    "executable_notional_usd",
    "accepted",
    "reason",
    "spot_venue",
    "spot_side",
    "spot_size",
    "spot_average_price",
    "spot_limit_price",
    "spot_slippage_pct",
    "perp_side",
    "perp_contracts",
    "perp_base_quantity",
    "perp_average_price",
    "perp_limit_price",
    "perp_slippage_pct",
    "hedge_mismatch_bps",
    "spot_test_accepted",
    "perp_test_accepted",
]


class PaperStore:
    def __init__(self, config: KucoinBasisConfig):
        self.config = config
        self.data_dir = Path(config.paper_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.positions_path = self.data_dir / "positions.csv"
        self.fills_path = self.data_dir / "fills.csv"
        self.funding_events_path = self.data_dir / "funding_events.csv"
        self.decisions_path = self.data_dir / "decisions.csv"
        self.processed_opportunities_path = self.data_dir / "processed_opportunities.csv"
        self.cooldowns_path = self.data_dir / "cooldowns.csv"
        self.execution_attempts_path = self.data_dir / "execution_attempts.csv"

    def load_all_positions(self) -> list[PaperPosition]:
        if not self.positions_path.exists():
            return []
        with self.positions_path.open("r", newline="", encoding="utf-8") as f:
            return [PaperPosition.from_csv_row(row) for row in csv.DictReader(f)]

    def load_open_positions(self) -> dict[str, PaperPosition]:
        return {
            position.position_id: position
            for position in self.load_all_positions()
            if position.status == "OPEN"
        }

    def write_positions(self, positions: dict[str, PaperPosition]) -> None:
        closed = [
            position
            for position in self.load_all_positions()
            if position.status != "OPEN" and position.position_id not in positions
        ]
        with self.positions_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=POSITION_FIELDS)
            writer.writeheader()
            for position in closed + list(positions.values()):
                writer.writerow(position.to_csv_row())

    def append_fill(self, row: dict) -> None:
        self._append_row(self.fills_path, FILL_FIELDS, row)

    def append_funding_event(self, row: dict) -> None:
        self._append_row(self.funding_events_path, FUNDING_EVENT_FIELDS, row)

    def append_decision(self, row: dict) -> None:
        self._append_row(self.decisions_path, DECISION_FIELDS, row)

    def append_cooldown(self, row: dict) -> None:
        self._append_row(self.cooldowns_path, COOLDOWN_FIELDS, row)

    def append_execution_attempt(self, row: dict) -> None:
        self._append_row(
            self.execution_attempts_path,
            EXECUTION_ATTEMPT_FIELDS,
            row,
        )

    def load_active_cooldowns(self, now=None) -> dict[tuple[str, str], dict]:
        now = now or utc_now()
        if not self.cooldowns_path.exists():
            return {}
        active = {}
        with self.cooldowns_path.open("r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                expires_at = parse_datetime(row.get("expires_at_utc"))
                if expires_at is None or expires_at <= now:
                    continue
                key = (row.get("base", ""), row.get("direction", ""))
                current = active.get(key)
                current_expires = parse_datetime(current.get("expires_at_utc")) if current else None
                if current is None or current_expires is None or expires_at > current_expires:
                    active[key] = row
        return active

    def load_processed_opportunities(self) -> set[str]:
        if not self.processed_opportunities_path.exists():
            return set()
        with self.processed_opportunities_path.open("r", newline="", encoding="utf-8") as f:
            return {row.get("opportunity_key", "") for row in csv.DictReader(f)}

    def mark_processed(self, opportunity_key: str, timestamp_utc: str, source_file: Path) -> None:
        self._append_row(
            self.processed_opportunities_path,
            PROCESSED_FIELDS,
            {
                "opportunity_key": opportunity_key,
                "timestamp_utc": timestamp_utc,
                "source_file": str(source_file),
                "processed_at_utc": format_datetime(utc_now()),
            },
        )

    @staticmethod
    def _append_row(path: Path, fieldnames: list[str], row: dict) -> None:
        file_exists = path.exists()
        if file_exists:
            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                existing_fieldnames = reader.fieldnames or []
                if existing_fieldnames != fieldnames:
                    existing_rows = list(reader)
                    with path.open("w", newline="", encoding="utf-8") as rewrite:
                        writer = csv.DictWriter(rewrite, fieldnames=fieldnames)
                        writer.writeheader()
                        for existing_row in existing_rows:
                            writer.writerow({
                                field: existing_row.get(field, "")
                                for field in fieldnames
                            })
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in fieldnames})
