from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_datetime(value: Optional[datetime]) -> str:
    return "" if value is None else value.astimezone(timezone.utc).isoformat()


def parse_datetime(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_float(value, default: Optional[float] = None) -> Optional[float]:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value, default: int = 0) -> int:
    parsed = parse_float(value)
    return default if parsed is None else int(parsed)


def parse_bool(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


@dataclass(frozen=True)
class ConvergenceOpportunityRow:
    timestamp_utc: datetime
    base: str
    direction: str
    spot_symbol: str
    perp_symbol: str
    funding_rate_pct: Optional[float]
    predicted_funding_rate_pct: Optional[float]
    funding_time_utc: Optional[datetime]
    funding_interval_hours: Optional[float]
    spot_bid: Optional[float]
    spot_ask: Optional[float]
    perp_bid: Optional[float]
    perp_ask: Optional[float]
    spot_spread_pct: Optional[float]
    perp_spread_pct: Optional[float]
    basis_pct: Optional[float]
    notional_usd: float
    spot_entry_slippage_pct: Optional[float]
    perp_entry_slippage_pct: Optional[float]
    spot_exit_slippage_pct: Optional[float]
    perp_exit_slippage_pct: Optional[float]
    entry_cost_pct: Optional[float]
    exit_cost_pct: Optional[float]
    round_trip_cost_pct: Optional[float]
    round_trip_fillable: bool
    basis_observation_count: int
    basis_mean_pct: Optional[float]
    basis_median_pct: Optional[float]
    basis_std_pct: Optional[float]
    basis_zscore: Optional[float]
    basis_percentile: Optional[float]
    basis_trend_pct: Optional[float]
    basis_change_5m_pct: Optional[float]
    basis_change_15m_pct: Optional[float]
    basis_change_60m_pct: Optional[float]
    basis_target_pct: Optional[float]
    gross_convergence_pct: Optional[float]
    expected_convergence_pct: Optional[float]
    net_edge_pct: Optional[float]
    decision: str
    reason: str
    spot_entry_avg_price: Optional[float] = None
    perp_entry_avg_price: Optional[float] = None
    spot_exit_avg_price: Optional[float] = None
    perp_exit_avg_price: Optional[float] = None

    @property
    def opportunity_key(self) -> str:
        return f"{self.timestamp_utc.isoformat()}|{self.base}|{self.direction}|{int(self.notional_usd)}"

    def to_csv_row(self) -> dict:
        row = asdict(self)
        row["timestamp_utc"] = format_datetime(self.timestamp_utc)
        row["funding_time_utc"] = format_datetime(self.funding_time_utc)
        row["round_trip_fillable"] = str(self.round_trip_fillable)
        return row

    @classmethod
    def from_csv_row(cls, row: dict) -> "ConvergenceOpportunityRow":
        return cls(
            timestamp_utc=parse_datetime(row.get("timestamp_utc")) or utc_now(),
            base=str(row.get("base", "")).strip(),
            direction=str(row.get("direction", "LONG_SPOT_SHORT_PERP")).strip() or "LONG_SPOT_SHORT_PERP",
            spot_symbol=str(row.get("spot_symbol", "")).strip(),
            perp_symbol=str(row.get("perp_symbol", "")).strip(),
            funding_rate_pct=parse_float(row.get("funding_rate_pct")),
            predicted_funding_rate_pct=parse_float(row.get("predicted_funding_rate_pct")),
            funding_time_utc=parse_datetime(row.get("funding_time_utc")),
            funding_interval_hours=parse_float(row.get("funding_interval_hours")),
            spot_bid=parse_float(row.get("spot_bid")),
            spot_ask=parse_float(row.get("spot_ask")),
            perp_bid=parse_float(row.get("perp_bid")),
            perp_ask=parse_float(row.get("perp_ask")),
            spot_spread_pct=parse_float(row.get("spot_spread_pct")),
            perp_spread_pct=parse_float(row.get("perp_spread_pct")),
            basis_pct=parse_float(row.get("basis_pct")),
            notional_usd=parse_float(row.get("notional_usd"), 0.0) or 0.0,
            spot_entry_slippage_pct=parse_float(row.get("spot_entry_slippage_pct")),
            perp_entry_slippage_pct=parse_float(row.get("perp_entry_slippage_pct")),
            spot_exit_slippage_pct=parse_float(row.get("spot_exit_slippage_pct")),
            perp_exit_slippage_pct=parse_float(row.get("perp_exit_slippage_pct")),
            entry_cost_pct=parse_float(row.get("entry_cost_pct")),
            exit_cost_pct=parse_float(row.get("exit_cost_pct")),
            round_trip_cost_pct=parse_float(row.get("round_trip_cost_pct")),
            round_trip_fillable=parse_bool(row.get("round_trip_fillable")),
            basis_observation_count=parse_int(row.get("basis_observation_count")),
            basis_mean_pct=parse_float(row.get("basis_mean_pct")),
            basis_median_pct=parse_float(row.get("basis_median_pct")),
            basis_std_pct=parse_float(row.get("basis_std_pct")),
            basis_zscore=parse_float(row.get("basis_zscore")),
            basis_percentile=parse_float(row.get("basis_percentile")),
            basis_trend_pct=parse_float(row.get("basis_trend_pct")),
            basis_change_5m_pct=parse_float(row.get("basis_change_5m_pct")),
            basis_change_15m_pct=parse_float(row.get("basis_change_15m_pct")),
            basis_change_60m_pct=parse_float(row.get("basis_change_60m_pct")),
            basis_target_pct=parse_float(row.get("basis_target_pct")),
            gross_convergence_pct=parse_float(row.get("gross_convergence_pct")),
            expected_convergence_pct=parse_float(row.get("expected_convergence_pct")),
            net_edge_pct=parse_float(row.get("net_edge_pct")),
            decision=str(row.get("decision", "")).strip(),
            reason=str(row.get("reason", "")).strip(),
            spot_entry_avg_price=parse_float(row.get("spot_entry_avg_price")),
            perp_entry_avg_price=parse_float(row.get("perp_entry_avg_price")),
            spot_exit_avg_price=parse_float(row.get("spot_exit_avg_price")),
            perp_exit_avg_price=parse_float(row.get("perp_exit_avg_price")),
        )
