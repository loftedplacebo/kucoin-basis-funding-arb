from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class KucoinBasisConfig:
    # Empty means all active KuCoin USDT perps with enabled BASE-USDT spot.
    approved_bases: tuple[str, ...] = ()

    min_funding_rate_pct: float = 0.03
    min_hold_funding_rate_pct: float = 0.30
    min_expected_edge_pct: float = 0.02
    min_minutes_before_funding: float = 15.0
    orderbook_monitor_interval_seconds: float = 60.0
    max_strategy_row_age_seconds: float = 180.0
    volatility_cooldown_minutes: float = 60.0

    max_total_notional_usd: float = 50_000.0
    max_symbol_notional_usd: float = 5_000.0
    max_open_positions: int = 100

    chunk_ladder_usd: tuple[float, ...] = (100.0, 250.0, 500.0, 1_000.0)
    max_chunk_notional_usd: float = 1_000.0

    estimated_spot_taker_fee_pct: float = 0.10
    estimated_perp_taker_fee_pct: float = 0.06
    estimated_exit_fee_pct: float = 0.16
    safety_buffer_pct: float = 0.03
    max_entry_exit_cost_pct: float = 1.00

    max_orderbook_age_ms: int = 1_000
    max_basis_adverse_move_pct: float = 5.00
    basis_history_lookback: int = 15
    min_basis_observations_for_stats: int = 5
    short_spot_entry_max_basis_percentile: float = 25.0
    long_spot_entry_min_basis_percentile: float = 75.0
    short_spot_exit_min_basis_percentile: float = 50.0
    long_spot_exit_max_basis_percentile: float = 50.0
    basis_take_profit_improvement_pct: float = 0.50
    basis_near_flat_exit_abs_pct: float = 0.50
    basis_convergence_haircut: float = 0.50
    gentle_unwind_enabled: bool = True
    gentle_unwind_chunk_ladder_usd: tuple[float, ...] = (100.0, 250.0, 500.0)
    min_profit_to_full_exit_pct: float = 0.02
    juicy_hold_funding_rate_pct: float = 1.00
    post_close_reentry_cooldown_minutes: float = 60.0
    fallback_funding_interval_hours: float = 8.0

    data_dir: Path = REPO_ROOT / "data" / "kucoin_basis"
    opportunities_dir: Path = REPO_ROOT / "data" / "kucoin_basis" / "opportunities"
    paper_dir: Path = REPO_ROOT / "data" / "kucoin_basis" / "paper"
    archive_dir: Path = REPO_ROOT / "data" / "kucoin_basis" / "archive"


DEFAULT_CONFIG = KucoinBasisConfig()
