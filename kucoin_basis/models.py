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


def minutes_until(value: Optional[datetime], now: datetime) -> Optional[float]:
    if value is None:
        return None
    return (value - now).total_seconds() / 60


@dataclass(frozen=True)
class SymbolPair:
    base: str
    spot_symbol: str
    perp_symbol: str


@dataclass(frozen=True)
class FundingSnapshot:
    base: str
    perp_symbol: str
    funding_rate_pct: Optional[float]
    predicted_funding_rate_pct: Optional[float]
    funding_time_utc: Optional[datetime]
    funding_interval_hours: Optional[float]
    funding_rate_cap: Optional[float]
    funding_rate_floor: Optional[float]
    observed_at_utc: datetime

    def minutes_to_funding(self, now: datetime) -> Optional[float]:
        return minutes_until(self.funding_time_utc, now)


@dataclass(frozen=True)
class OpportunityRow:
    timestamp_utc: datetime
    base: str
    direction: str
    spot_symbol: str
    perp_symbol: str
    funding_rate_pct: Optional[float]
    predicted_funding_rate_pct: Optional[float]
    funding_time_utc: Optional[datetime]
    minutes_to_funding: Optional[float]
    spot_bid: Optional[float]
    spot_ask: Optional[float]
    perp_bid: Optional[float]
    perp_ask: Optional[float]
    basis_pct: Optional[float]
    notional_usd: float
    spot_entry_slippage_pct: Optional[float]
    perp_entry_slippage_pct: Optional[float]
    spot_exit_slippage_pct: Optional[float]
    perp_exit_slippage_pct: Optional[float]
    expected_edge_pct: Optional[float]
    round_trip_fillable: bool
    decision: str
    reason: str
    spot_hedge_route: str = ""
    spot_entry_avg_price: Optional[float] = None
    perp_entry_avg_price: Optional[float] = None
    spot_exit_avg_price: Optional[float] = None
    perp_exit_avg_price: Optional[float] = None
    funding_interval: Optional[float] = None
    funding_rate_cap: Optional[float] = None
    funding_rate_floor: Optional[float] = None
    basis_observation_count: int = 0
    basis_mean_pct: Optional[float] = None
    basis_median_pct: Optional[float] = None
    basis_std_pct: Optional[float] = None
    basis_zscore: Optional[float] = None
    basis_percentile: Optional[float] = None
    basis_trend_pct: Optional[float] = None
    basis_target_pct: Optional[float] = None
    basis_convergence_upside_pct: Optional[float] = None
    scenario_edge_pct: Optional[float] = None

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
    def from_csv_row(cls, row: dict) -> "OpportunityRow":
        timestamp = parse_datetime(row.get("timestamp_utc")) or utc_now()
        return cls(
            timestamp_utc=timestamp,
            base=str(row.get("base", "")).strip(),
            direction=str(row.get("direction", "LONG_SPOT_SHORT_PERP")).strip() or "LONG_SPOT_SHORT_PERP",
            spot_symbol=str(row.get("spot_symbol", "")).strip(),
            perp_symbol=str(row.get("perp_symbol", "")).strip(),
            funding_rate_pct=parse_float(row.get("funding_rate_pct")),
            predicted_funding_rate_pct=parse_float(row.get("predicted_funding_rate_pct")),
            funding_time_utc=parse_datetime(row.get("funding_time_utc")),
            minutes_to_funding=parse_float(row.get("minutes_to_funding")),
            spot_bid=parse_float(row.get("spot_bid")),
            spot_ask=parse_float(row.get("spot_ask")),
            perp_bid=parse_float(row.get("perp_bid")),
            perp_ask=parse_float(row.get("perp_ask")),
            basis_pct=parse_float(row.get("basis_pct")),
            notional_usd=parse_float(row.get("notional_usd"), 0.0) or 0.0,
            spot_entry_slippage_pct=parse_float(row.get("spot_entry_slippage_pct")),
            perp_entry_slippage_pct=parse_float(row.get("perp_entry_slippage_pct")),
            spot_exit_slippage_pct=parse_float(row.get("spot_exit_slippage_pct")),
            perp_exit_slippage_pct=parse_float(row.get("perp_exit_slippage_pct")),
            expected_edge_pct=parse_float(row.get("expected_edge_pct")),
            round_trip_fillable=parse_bool(row.get("round_trip_fillable")),
            decision=str(row.get("decision", "")).strip(),
            reason=str(row.get("reason", "")).strip(),
            spot_hedge_route=str(row.get("spot_hedge_route", "")).strip(),
            spot_entry_avg_price=parse_float(row.get("spot_entry_avg_price")),
            perp_entry_avg_price=parse_float(row.get("perp_entry_avg_price")),
            spot_exit_avg_price=parse_float(row.get("spot_exit_avg_price")),
            perp_exit_avg_price=parse_float(row.get("perp_exit_avg_price")),
            funding_interval=parse_float(row.get("funding_interval")),
            funding_rate_cap=parse_float(row.get("funding_rate_cap")),
            funding_rate_floor=parse_float(row.get("funding_rate_floor")),
            basis_observation_count=parse_int(row.get("basis_observation_count")),
            basis_mean_pct=parse_float(row.get("basis_mean_pct")),
            basis_median_pct=parse_float(row.get("basis_median_pct")),
            basis_std_pct=parse_float(row.get("basis_std_pct")),
            basis_zscore=parse_float(row.get("basis_zscore")),
            basis_percentile=parse_float(row.get("basis_percentile")),
            basis_trend_pct=parse_float(row.get("basis_trend_pct")),
            basis_target_pct=parse_float(row.get("basis_target_pct")),
            basis_convergence_upside_pct=parse_float(row.get("basis_convergence_upside_pct")),
            scenario_edge_pct=parse_float(row.get("scenario_edge_pct")),
        )
