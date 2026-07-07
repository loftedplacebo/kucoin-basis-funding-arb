from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from kucoin_basis.models import format_datetime, parse_datetime, parse_float, parse_int, utc_now


@dataclass
class PaperPosition:
    position_id: str
    base: str
    direction: str
    spot_symbol: str
    perp_symbol: str
    notional_usd: float
    spot_qty: float
    perp_qty: float
    spot_entry_price: float
    perp_entry_price: float
    entry_basis_pct: float
    current_basis_pct: float
    funding_rate_pct_at_entry: float
    expected_funding_pct: float
    realised_funding_pnl_usd: float
    unrealised_basis_pnl_usd: float
    estimated_close_cost_usd: float
    estimated_net_pnl_usd: float
    created_at: datetime
    updated_at: datetime
    next_funding_time: Optional[datetime]
    funding_events_captured: int
    funding_interval_hours: Optional[float] = None
    status: str = "OPEN"

    def to_csv_row(self) -> dict:
        row = asdict(self)
        row["created_at"] = format_datetime(self.created_at)
        row["updated_at"] = format_datetime(self.updated_at)
        row["next_funding_time"] = format_datetime(self.next_funding_time)
        return row

    @classmethod
    def from_csv_row(cls, row: dict) -> "PaperPosition":
        return cls(
            position_id=str(row.get("position_id", "")),
            base=str(row.get("base", "")),
            direction=str(row.get("direction", "LONG_SPOT_SHORT_PERP")) or "LONG_SPOT_SHORT_PERP",
            spot_symbol=str(row.get("spot_symbol", "")),
            perp_symbol=str(row.get("perp_symbol", "")),
            notional_usd=parse_float(row.get("notional_usd"), 0.0) or 0.0,
            spot_qty=parse_float(row.get("spot_qty"), 0.0) or 0.0,
            perp_qty=parse_float(row.get("perp_qty"), 0.0) or 0.0,
            spot_entry_price=parse_float(row.get("spot_entry_price"), 0.0) or 0.0,
            perp_entry_price=parse_float(row.get("perp_entry_price"), 0.0) or 0.0,
            entry_basis_pct=parse_float(row.get("entry_basis_pct"), 0.0) or 0.0,
            current_basis_pct=parse_float(row.get("current_basis_pct"), 0.0) or 0.0,
            funding_rate_pct_at_entry=parse_float(row.get("funding_rate_pct_at_entry"), 0.0) or 0.0,
            expected_funding_pct=parse_float(row.get("expected_funding_pct"), 0.0) or 0.0,
            realised_funding_pnl_usd=parse_float(row.get("realised_funding_pnl_usd"), 0.0) or 0.0,
            unrealised_basis_pnl_usd=parse_float(row.get("unrealised_basis_pnl_usd"), 0.0) or 0.0,
            estimated_close_cost_usd=parse_float(row.get("estimated_close_cost_usd"), 0.0) or 0.0,
            estimated_net_pnl_usd=parse_float(row.get("estimated_net_pnl_usd"), 0.0) or 0.0,
            created_at=parse_datetime(row.get("created_at")) or utc_now(),
            updated_at=parse_datetime(row.get("updated_at")) or utc_now(),
            next_funding_time=parse_datetime(row.get("next_funding_time")),
            funding_interval_hours=parse_float(row.get("funding_interval_hours")),
            funding_events_captured=parse_int(row.get("funding_events_captured")),
            status=str(row.get("status", "OPEN")),
        )
