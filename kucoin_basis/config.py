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
    preferred_entry_window_minutes: float = 120.0
    max_entry_window_minutes: float = 240.0
    reduced_late_entry_chunk_ladder_usd: tuple[float, ...] = (50.0, 75.0)
    late_entry_requires_favourable_basis_trend: bool = True
    post_funding_entry_quarantine_minutes: float = 5.0
    funding_cycle_confirmation_observations: int = 2
    orderbook_monitor_interval_seconds: float = 60.0
    max_strategy_row_age_seconds: float = 180.0
    volatility_cooldown_minutes: float = 60.0

    max_total_notional_usd: float = 50_000.0
    max_symbol_notional_usd: float = 5_000.0
    max_open_positions: int = 100
    dry_run_max_hedge_mismatch_bps: float = 25.0

    chunk_ladder_usd: tuple[float, ...] = (100.0, 250.0, 500.0, 1_000.0)
    max_chunk_notional_usd: float = 1_000.0

    estimated_spot_taker_fee_pct: float = 0.08
    estimated_perp_taker_fee_pct: float = 0.06
    estimated_exit_fee_pct: float = 0.14
    safety_buffer_pct: float = 0.03
    max_entry_exit_cost_pct: float = 1.00
    max_entry_leg_slippage_pct: float = 0.75
    max_exit_leg_slippage_pct: float = 0.75
    max_combined_entry_slippage_pct: float = 1.25
    max_combined_exit_slippage_pct: float = 1.25
    adverse_basis_exit_enabled: bool = True
    adverse_basis_exit_loss_multiplier: float = 1.00
    adverse_basis_exit_buffer_usd: float = 0.25

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
    gentle_unwind_chunk_ladder_usd: tuple[float, ...] = (100.0, 75.0, 50.0)
    funding_harvest_unwind_chunk_usd: float = 100.0
    min_funding_harvest_unwind_profit_usd: float = 0.25
    toxic_unwind_enabled: bool = True
    toxic_unwind_chunk_usd: float = 100.0
    toxic_adverse_funding_threshold_pct: float = 0.0
    toxic_funding_confirmation_minutes: float = 60.0
    toxic_unwind_start_minutes_before_funding: float = 90.0
    toxic_unwind_pace_buffer_minutes: float = 15.0
    toxic_max_exit_cost_pct: float = 1.00
    timed_exit_enabled: bool = True
    timed_exit_start_hours: float = 40.0
    timed_exit_deadline_hours: float = 48.0
    timed_exit_pace_buffer_minutes: float = 60.0
    pre_funding_take_profit_enabled: bool = True
    pre_funding_take_profit_min_basis_improvement_pct: float = 2.00
    pre_funding_take_profit_min_profit_usd: float = 5.00
    pre_funding_take_profit_funding_multiplier: float = 1.25
    unusually_attractive_unwind_profit_usd: float = 1.00
    unusually_attractive_unwind_profit_pct: float = 0.75
    capital_recycle_funding_rate_pct: float = 0.50
    capital_recycle_min_symbol_exposure_fraction: float = 0.80
    min_profit_to_full_exit_pct: float = 0.02
    juicy_hold_funding_rate_pct: float = 0.75
    economic_funding_hold_enabled: bool = True
    next_funding_value_haircut: float = 0.90
    redeployment_edge_haircut: float = 0.75
    basis_giveback_risk_std_multiplier: float = 0.50
    basis_giveback_risk_improvement_fraction: float = 0.25
    economic_hold_min_advantage_usd: float = 0.05
    post_close_reentry_cooldown_minutes: float = 60.0
    fallback_funding_interval_hours: float = 8.0

    data_dir: Path = REPO_ROOT / "data" / "kucoin_basis"
    opportunities_dir: Path = REPO_ROOT / "data" / "kucoin_basis" / "opportunities"
    paper_dir: Path = REPO_ROOT / "data" / "kucoin_basis" / "paper"
    archive_dir: Path = REPO_ROOT / "data" / "kucoin_basis" / "archive"


DEFAULT_CONFIG = KucoinBasisConfig()
