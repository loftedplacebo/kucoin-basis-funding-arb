from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class KucoinBasisConvergenceConfig:
    # Empty means all active KuCoin USDT perps with enabled BASE-USDT spot.
    approved_bases: tuple[str, ...] = ()

    orderbook_monitor_interval_seconds: float = 60.0
    scan_max_workers: int = 8
    max_strategy_row_age_seconds: float = 600.0

    # Small-trade defaults. The goal is to learn across many independent events
    # instead of betting on one large basis snapback.
    max_total_notional_usd: float = 10_000.0
    max_symbol_notional_usd: float = 1_000.0
    max_open_positions: int = 100
    chunk_ladder_usd: tuple[float, ...] = (25.0, 50.0, 100.0, 250.0)
    max_chunk_notional_usd: float = 250.0

    estimated_spot_taker_fee_pct: float = 0.08
    estimated_perp_taker_fee_pct: float = 0.06
    estimated_exit_fee_pct: float = 0.14
    safety_buffer_pct: float = 0.05
    max_exit_cost_pct: float = 0.80
    max_round_trip_cost_pct: float = 1.50

    # Basis signal settings.
    basis_history_lookback: int = 120
    min_basis_observations_for_stats: int = 30
    entry_zscore_abs: float = 2.0
    cheap_entry_max_percentile: float = 10.0
    rich_entry_min_percentile: float = 90.0
    min_abs_basis_pct: float = 0.50
    min_expected_convergence_pct: float = 0.35
    min_net_edge_pct: float = 0.15
    convergence_haircut: float = 0.50
    max_basis_std_pct: float = 6.00
    max_basis_trend_abs_pct: float = 8.00

    # Exit and risk settings.
    take_profit_basis_improvement_pct: float = 0.35
    take_profit_net_pct: float = 0.20
    neutral_zscore_abs: float = 0.35
    neutral_percentile_low: float = 40.0
    neutral_percentile_high: float = 60.0
    max_adverse_basis_move_pct: float = 1.50
    max_hold_hours: float = 6.0
    hard_max_hold_hours: float = 12.0
    gentle_unwind_enabled: bool = True
    gentle_unwind_chunk_ladder_usd: tuple[float, ...] = (25.0, 50.0, 100.0)
    post_close_reentry_cooldown_minutes: float = 30.0
    volatility_cooldown_minutes: float = 60.0
    fallback_funding_interval_hours: float = 8.0

    data_dir: Path = REPO_ROOT / "data" / "kucoin_basis_convergence"
    observations_dir: Path = REPO_ROOT / "data" / "kucoin_basis_convergence" / "observations"
    opportunities_dir: Path = REPO_ROOT / "data" / "kucoin_basis_convergence" / "opportunities"
    paper_dir: Path = REPO_ROOT / "data" / "kucoin_basis_convergence" / "paper"
    archive_dir: Path = REPO_ROOT / "data" / "kucoin_basis_convergence" / "archive"


DEFAULT_CONFIG = KucoinBasisConvergenceConfig()
