from __future__ import annotations

import csv
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from kucoin_basis.config import DEFAULT_CONFIG, KucoinBasisConfig
from kucoin_basis.execution import ExecutionAdapter
from kucoin_basis.funding import fetch_funding_settlements
from kucoin_basis.kucoin_public_client import KucoinPublicClient
from kucoin_basis.models import OpportunityRow, format_datetime, parse_datetime, utc_now
from kucoin_basis.paper_models import PaperPosition
from kucoin_basis.paper_store import PaperStore


@dataclass(frozen=True)
class ExitEstimate:
    basis_pnl_usd: float
    close_cost_usd: float
    funding_pnl_usd: float
    net_pnl_ex_funding_usd: float
    net_pnl_usd: float
    net_pnl_pct: float


@dataclass(frozen=True)
class ExitValueComparison:
    chunk_notional_usd: float
    risk_adjusted_next_funding_usd: float
    risk_adjusted_exit_redeploy_usd: float
    best_redeployment_edge_pct: float
    basis_giveback_risk_usd: float

    @property
    def hold_is_preferred(self) -> bool:
        return self.risk_adjusted_next_funding_usd > self.risk_adjusted_exit_redeploy_usd


DISCRETIONARY_POST_FUNDING_EXIT_REASONS = {
    "basis_converged_take_profit",
    "basis_near_flat_take_profit",
    "unusually_attractive_all_in_unwind",
    "capital_recycle_profitable_unwind",
}

FORCED_EXIT_REASONS = {
    "pre_funding_reversal_toxic_unwind",
    "post_funding_reversal_toxic_unwind",
    "timed_exit_deadline",
}


def latest_opportunity_file(config: KucoinBasisConfig) -> Path:
    files = sorted(config.opportunities_dir.glob("kucoin_basis_opportunities_*.csv"))
    if not files:
        raise SystemExit(f"No KuCoin basis opportunity files found in {config.opportunities_dir}")
    return files[-1]


def load_opportunities(path: Path) -> list[OpportunityRow]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return [OpportunityRow.from_csv_row(row) for row in csv.DictReader(f)]


def _fresh_opportunities(
    rows: list[OpportunityRow],
    config: KucoinBasisConfig,
    now: datetime,
) -> list[OpportunityRow]:
    if not rows:
        return []
    latest_timestamp = max(row.timestamp_utc for row in rows)
    age_seconds = (now - latest_timestamp).total_seconds()
    if age_seconds > config.max_strategy_row_age_seconds:
        return []
    return [row for row in rows if row.timestamp_utc == latest_timestamp]


def _position_id(base: str, direction: str) -> str:
    return f"KUCOIN_BASIS_{base}_{direction}"


def _entry_group_key(row: OpportunityRow) -> tuple[str, str, str]:
    return (format_datetime(row.timestamp_utc), row.base, row.direction)


def _best_entry_keys_by_group(rows: list[OpportunityRow], processed: set[str]) -> dict[tuple[str, str, str], str]:
    best_by_group: dict[tuple[str, str, str], OpportunityRow] = {}
    for row in rows:
        if row.opportunity_key in processed or row.decision != "ENTER_CANDIDATE":
            continue
        group_key = _entry_group_key(row)
        current = best_by_group.get(group_key)
        row_rank = (row.expected_edge_pct or -999.0, -row.notional_usd)
        current_rank = (
            (current.expected_edge_pct or -999.0, -current.notional_usd)
            if current is not None
            else None
        )
        if current is None or current_rank is None or row_rank > current_rank:
            best_by_group[group_key] = row
    return {group_key: row.opportunity_key for group_key, row in best_by_group.items()}


def _open_notional_by_base(positions: dict[str, PaperPosition]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for position in positions.values():
        totals[position.base] = totals.get(position.base, 0.0) + position.notional_usd
    return totals


def _repair_missing_next_funding_times(
    positions: dict[str, PaperPosition],
    store: PaperStore,
    config: KucoinBasisConfig,
) -> None:
    if not store.funding_events_path.exists():
        return
    latest_funding_time_by_position: dict[str, datetime] = {}
    with store.funding_events_path.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            position_id = row.get("position_id", "")
            funding_time = parse_datetime(row.get("funding_time_utc"))
            if not position_id or funding_time is None:
                continue
            current = latest_funding_time_by_position.get(position_id)
            if current is None or funding_time > current:
                latest_funding_time_by_position[position_id] = funding_time

    now = utc_now()
    for position in positions.values():
        if position.next_funding_time is not None:
            continue
        funding_time = latest_funding_time_by_position.get(position.position_id)
        if funding_time is None:
            continue
        next_time = funding_time
        while next_time <= now:
            next_time += timedelta(hours=config.fallback_funding_interval_hours)
        position.next_funding_time = next_time
        position.updated_at = now


def _exit_slippage_cost_pct(row: OpportunityRow, config: KucoinBasisConfig) -> float:
    return (
        (row.spot_exit_slippage_pct or 0.0)
        + (row.perp_exit_slippage_pct or 0.0)
        + config.estimated_exit_fee_pct
    )


def _estimate_exit_chunk(
    position: PaperPosition,
    row: OpportunityRow,
    config: KucoinBasisConfig,
    chunk_notional_usd: float,
) -> ExitEstimate | None:
    if position.notional_usd <= 0 or chunk_notional_usd <= 0:
        return None
    fraction = min(1.0, chunk_notional_usd / position.notional_usd)
    spot_qty = position.spot_qty * fraction
    perp_qty = position.perp_qty * fraction
    if position.direction == "SHORT_SPOT_LONG_PERP":
        if not row.spot_ask or not row.perp_bid:
            return None
        spot_pnl = chunk_notional_usd - (spot_qty * row.spot_ask)
        perp_pnl = (perp_qty * row.perp_bid) - chunk_notional_usd
    else:
        if not row.spot_bid or not row.perp_ask:
            return None
        spot_pnl = (spot_qty * row.spot_bid) - chunk_notional_usd
        perp_pnl = chunk_notional_usd - (perp_qty * row.perp_ask)

    basis_pnl = spot_pnl + perp_pnl
    close_cost = chunk_notional_usd * _exit_slippage_cost_pct(row, config) / 100
    funding_pnl = position.realised_funding_pnl_usd * fraction
    net_pnl_ex_funding = basis_pnl - close_cost
    net_pnl = funding_pnl + net_pnl_ex_funding
    return ExitEstimate(
        basis_pnl_usd=basis_pnl,
        close_cost_usd=close_cost,
        funding_pnl_usd=funding_pnl,
        net_pnl_ex_funding_usd=net_pnl_ex_funding,
        net_pnl_usd=net_pnl,
        net_pnl_pct=(net_pnl / chunk_notional_usd) * 100,
    )


def _mark_position(position: PaperPosition, row: OpportunityRow, config: KucoinBasisConfig) -> PaperPosition:
    now = utc_now()
    if row.funding_interval is not None:
        position.funding_interval_hours = row.funding_interval
    if (
        position.next_funding_time is None
        and row.funding_time_utc is not None
        and row.funding_time_utc > now
    ):
        position.next_funding_time = row.funding_time_utc

    estimate = _estimate_exit_chunk(position, row, config, position.notional_usd)
    if estimate is not None:
        position.unrealised_basis_pnl_usd = estimate.basis_pnl_usd
        position.estimated_close_cost_usd = estimate.close_cost_usd
        position.estimated_net_pnl_usd = estimate.net_pnl_usd
    if position.direction == "SHORT_SPOT_LONG_PERP" and row.spot_ask and row.perp_bid:
        position.current_basis_pct = row.basis_pct if row.basis_pct is not None else position.current_basis_pct
    elif row.spot_bid and row.perp_ask:
        position.current_basis_pct = row.basis_pct if row.basis_pct is not None else position.current_basis_pct

    position.updated_at = now
    return position


def _funding_interval_hours(
    position: PaperPosition,
    row: OpportunityRow | None,
    config: KucoinBasisConfig,
) -> float:
    interval = (
        (row.funding_interval if row is not None else None)
        or position.funding_interval_hours
        or config.fallback_funding_interval_hours
    )
    return interval if interval > 0 else config.fallback_funding_interval_hours


def _accrue_funding_if_crossed(
    position: PaperPosition,
    row: OpportunityRow | None,
    store: PaperStore,
    config: KucoinBasisConfig,
    funding_client: KucoinPublicClient | None = None,
) -> None:
    now = utc_now()
    funding_time = position.next_funding_time
    if funding_time is None or funding_time > now:
        return
    if row is not None and row.funding_interval is not None:
        position.funding_interval_hours = row.funding_interval
    if funding_client is None:
        return
    try:
        settlements = fetch_funding_settlements(
            funding_client,
            position.perp_symbol,
            funding_time,
            now,
        )
    except Exception as error:
        print(
            f"Funding settlement pending for {position.perp_symbol} at "
            f"{format_datetime(funding_time)}: {type(error).__name__}: {error}",
            flush=True,
        )
        return
    while funding_time is not None and funding_time <= now:
        raw_funding_rate_pct = settlements.get(funding_time)
        if raw_funding_rate_pct is None:
            # KuCoin can publish the final settlement shortly after the boundary.
            # Keep it pending and retry instead of applying the next cycle's rate.
            break
        funding_benefit_pct = (
            raw_funding_rate_pct
            if position.direction == "LONG_SPOT_SHORT_PERP"
            else -raw_funding_rate_pct
        )
        funding_pnl = position.notional_usd * funding_benefit_pct / 100
        position.realised_funding_pnl_usd += funding_pnl
        position.funding_events_captured += 1
        store.append_funding_event(
            {
                "timestamp_utc": format_datetime(now),
                "position_id": position.position_id,
                "base": position.base,
                "direction": position.direction,
                "perp_symbol": position.perp_symbol,
                "funding_time_utc": format_datetime(funding_time),
                "funding_rate_pct": f"{raw_funding_rate_pct:.8f}",
                "notional_usd": f"{position.notional_usd:.8f}",
                "funding_pnl_usd": f"{funding_pnl:.8f}",
            }
        )
        funding_time = _next_funding_time_after(funding_time, position, row, config)
    position.next_funding_time = funding_time
    position.updated_at = now


def _next_funding_time_after(
    funding_time: datetime,
    position: PaperPosition,
    row: OpportunityRow | None,
    config: KucoinBasisConfig,
):
    interval_hours = _funding_interval_hours(position, row, config)
    return funding_time + timedelta(hours=interval_hours)


def _basis_improvement_pct(position: PaperPosition) -> float:
    if position.direction == "SHORT_SPOT_LONG_PERP":
        return position.current_basis_pct - position.entry_basis_pct
    return position.entry_basis_pct - position.current_basis_pct


def _basis_moved_adversely(position: PaperPosition, config: KucoinBasisConfig) -> bool:
    return _basis_improvement_pct(position) < -config.max_basis_adverse_move_pct


def _funding_benefit_pct(position: PaperPosition, row: OpportunityRow) -> float | None:
    if row.funding_rate_pct is None:
        return None
    if position.direction == "LONG_SPOT_SHORT_PERP":
        return row.funding_rate_pct
    return -row.funding_rate_pct


TOXIC_UNWIND_REASONS = {
    "pre_funding_reversal_toxic_unwind",
    "post_funding_reversal_toxic_unwind",
    "timed_exit_unwind",
    "timed_exit_deadline",
}


def _position_age_hours(position: PaperPosition, now: datetime) -> float:
    return max(0.0, (now - position.created_at).total_seconds() / 3600)


def _update_adverse_funding_state(
    position: PaperPosition,
    row: OpportunityRow,
    config: KucoinBasisConfig,
    now: datetime,
) -> None:
    funding_benefit_pct = _funding_benefit_pct(position, row)
    if funding_benefit_pct is None:
        return
    if funding_benefit_pct < config.toxic_adverse_funding_threshold_pct:
        position.adverse_funding_since = position.adverse_funding_since or now
    else:
        position.adverse_funding_since = None


def _forced_unwind_reason(
    position: PaperPosition,
    row: OpportunityRow,
    config: KucoinBasisConfig,
    now: datetime,
) -> str | None:
    age_hours = _position_age_hours(position, now)
    if config.timed_exit_enabled and age_hours >= config.timed_exit_deadline_hours:
        return "timed_exit_deadline"
    if config.timed_exit_enabled and age_hours >= config.timed_exit_start_hours:
        return "timed_exit_unwind"
    if not config.toxic_unwind_enabled:
        return None

    funding_benefit_pct = _funding_benefit_pct(position, row)
    if (
        funding_benefit_pct is None
        or funding_benefit_pct >= config.toxic_adverse_funding_threshold_pct
        or position.adverse_funding_since is None
    ):
        return None

    adverse_minutes = (now - position.adverse_funding_since).total_seconds() / 60
    funding_time = row.funding_time_utc or position.next_funding_time
    minutes_to_funding = (
        (funding_time - now).total_seconds() / 60
        if funding_time is not None
        else None
    )
    confirmed = adverse_minutes >= config.toxic_funding_confirmation_minutes
    deadline_near = (
        minutes_to_funding is not None
        and minutes_to_funding <= config.toxic_unwind_start_minutes_before_funding
    )
    if not confirmed and not deadline_near:
        return None
    return (
        "pre_funding_reversal_toxic_unwind"
        if position.funding_events_captured <= 0
        else "post_funding_reversal_toxic_unwind"
    )


def _forced_unwind_deadline(
    position: PaperPosition,
    row: OpportunityRow,
    reason: str,
    config: KucoinBasisConfig,
) -> datetime:
    if reason in {"timed_exit_unwind", "timed_exit_deadline"}:
        return position.created_at + timedelta(hours=config.timed_exit_deadline_hours)
    funding_time = row.funding_time_utc or position.next_funding_time
    if funding_time is not None:
        return funding_time - timedelta(minutes=config.min_minutes_before_funding)
    return utc_now() + timedelta(minutes=config.toxic_funding_confirmation_minutes)


def _choose_forced_unwind_close(
    rows: list[OpportunityRow],
    *,
    base: str,
    direction: str,
    position: PaperPosition,
    reason: str,
    config: KucoinBasisConfig,
    now: datetime,
) -> tuple[float, OpportunityRow, ExitEstimate] | None:
    matching_rows = [
        row for row in rows if row.base == base and row.direction == direction
    ]
    reference_row = max(matching_rows, key=lambda row: row.timestamp_utc) if matching_rows else None
    if reference_row is None:
        return None
    deadline = _forced_unwind_deadline(position, reference_row, reason, config)
    remaining_seconds = (deadline - now).total_seconds()
    deadline_reached = remaining_seconds <= 0 or reason == "timed_exit_deadline"
    if deadline_reached:
        required_chunk = position.notional_usd
        max_exit_cost_pct = None
    else:
        cycles_remaining = max(
            1,
            int(remaining_seconds // max(1.0, config.orderbook_monitor_interval_seconds)),
        )
        chunks_remaining = max(
            1,
            math.ceil(position.notional_usd / max(0.01, config.toxic_unwind_chunk_usd)),
        )
        pace_buffer_minutes = (
            config.timed_exit_pace_buffer_minutes
            if reason == "timed_exit_unwind"
            else config.toxic_unwind_pace_buffer_minutes
        )
        pace_buffer_cycles = int(
            pace_buffer_minutes * 60 / max(1.0, config.orderbook_monitor_interval_seconds)
        )
        forced_pace = cycles_remaining <= chunks_remaining + pace_buffer_cycles
        required_chunk = max(
            config.toxic_unwind_chunk_usd,
            position.notional_usd / cycles_remaining,
        )
        max_exit_cost_pct = config.toxic_max_exit_cost_pct
        if reason == "timed_exit_unwind":
            age_progress = (
                _position_age_hours(position, now) - config.timed_exit_start_hours
            ) / max(0.01, config.timed_exit_deadline_hours - config.timed_exit_start_hours)
            max_exit_cost_pct *= 1 + min(1.0, max(0.0, age_progress))

    chunk_sizes = {
        min(position.notional_usd, config.toxic_unwind_chunk_usd),
        position.notional_usd,
        *(
            min(position.notional_usd, chunk)
            for chunk in config.gentle_unwind_chunk_ladder_usd
        ),
    }
    candidates: list[tuple[float, OpportunityRow, ExitEstimate]] = []
    for chunk in sorted(chunk_sizes):
        if chunk <= 0:
            continue
        exit_row = _choose_full_close_row(
            rows,
            base=base,
            direction=direction,
            notional_usd=chunk,
        )
        if exit_row is None:
            continue
        estimate = _estimate_exit_chunk(position, exit_row, config, chunk)
        if estimate is None:
            continue
        if (
            max_exit_cost_pct is not None
            and _exit_slippage_cost_pct(exit_row, config) > max_exit_cost_pct
        ):
            continue
        candidates.append((chunk, exit_row, estimate))
    if not candidates:
        return None

    if not deadline_reached and not forced_pace:
        if reason == "timed_exit_unwind":
            return None
        adverse_funding_pct = max(
            0.0,
            -(_funding_benefit_pct(position, reference_row) or 0.0),
        )
        economically_preferred = [
            candidate
            for candidate in candidates
            if max(0.0, -candidate[2].net_pnl_usd)
            <= candidate[0] * adverse_funding_pct / 100
        ]
        if not economically_preferred:
            return None
        candidates = economically_preferred

    on_pace = [candidate for candidate in candidates if candidate[0] + 1e-8 >= required_chunk]
    if not on_pace:
        return max(candidates, key=lambda candidate: candidate[0])
    if deadline_reached:
        return max(on_pace, key=lambda candidate: (candidate[0], candidate[2].net_pnl_pct))
    return max(
        on_pace,
        key=lambda candidate: (
            round(candidate[2].net_pnl_pct, 10),
            candidate[2].net_pnl_usd,
            -candidate[0],
        ),
    )


def _basis_target_reason(position: PaperPosition, config: KucoinBasisConfig) -> str | None:
    basis_improvement = _basis_improvement_pct(position)
    if basis_improvement >= config.basis_take_profit_improvement_pct:
        return "basis_converged_take_profit"
    if basis_improvement >= 0 and abs(position.current_basis_pct) <= config.basis_near_flat_exit_abs_pct:
        return "basis_near_flat_take_profit"
    return None


def _row_basis_too_volatile(row: OpportunityRow, config: KucoinBasisConfig) -> bool:
    return (
        (
            row.basis_std_pct is not None
            and row.basis_std_pct > config.max_basis_adverse_move_pct
        )
        or (
            row.basis_trend_pct is not None
            and abs(row.basis_trend_pct) > config.max_basis_adverse_move_pct
        )
    )


def _cooldown_key(base: str, direction: str) -> tuple[str, str]:
    return (base, direction)


def _ensure_cooldown(
    store: PaperStore,
    active_cooldowns: dict[tuple[str, str], dict],
    *,
    base: str,
    direction: str,
    reason: str,
    config: KucoinBasisConfig,
    duration_minutes: float | None = None,
) -> None:
    key = _cooldown_key(base, direction)
    if key in active_cooldowns:
        return
    now = utc_now()
    expires_at = now + timedelta(minutes=duration_minutes or config.volatility_cooldown_minutes)
    row = {
        "timestamp_utc": format_datetime(now),
        "base": base,
        "direction": direction,
        "reason": reason,
        "expires_at_utc": format_datetime(expires_at),
    }
    store.append_cooldown(row)
    active_cooldowns[key] = row


def _row_age_seconds(row: OpportunityRow, now: datetime) -> float:
    return (now - row.timestamp_utc).total_seconds()


def _should_exit(position: PaperPosition, row: OpportunityRow, config: KucoinBasisConfig) -> tuple[bool, str]:
    funding_captured = position.funding_events_captured > 0
    funding_benefit_pct = _funding_benefit_pct(position, row)
    next_funding_weak = funding_benefit_pct is not None and funding_benefit_pct < config.min_hold_funding_rate_pct
    basis_improvement = _basis_improvement_pct(position)
    if not funding_captured:
        if basis_improvement < -config.max_basis_adverse_move_pct:
            return False, "hold_basis_moved_adversely"
        return False, "hold_until_first_funding"

    if (
        funding_benefit_pct is not None
        and funding_benefit_pct >= config.juicy_hold_funding_rate_pct
    ):
        return False, "hold_for_juicy_next_funding"
    if basis_improvement < -config.max_basis_adverse_move_pct and next_funding_weak:
        return True, "funding_weak_basis_adverse_try_unwind"
    if next_funding_weak:
        return True, "funding_captured_next_funding_below_threshold"

    basis_target_reason = _basis_target_reason(position, config)
    if basis_target_reason is not None:
        return True, basis_target_reason

    if (
        funding_benefit_pct is not None
        and funding_benefit_pct >= config.min_hold_funding_rate_pct
    ):
        return False, "hold_for_next_funding_and_basis"

    if row.expected_edge_pct is not None and row.expected_edge_pct < config.min_expected_edge_pct:
        return True, "funding_captured_holding_edge_weak"
    return False, "hold"


def _choose_full_close_row(
    rows: list[OpportunityRow],
    *,
    base: str,
    direction: str,
    notional_usd: float,
) -> OpportunityRow | None:
    candidates = [
        row
        for row in rows
        if row.base == base
        and row.direction == direction
        and row.notional_usd + 1e-8 >= notional_usd
        and row.spot_exit_avg_price is not None
        and row.perp_exit_avg_price is not None
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda row: (row.notional_usd, -(row.expected_edge_pct or -999)))


def _choose_partial_close(
    rows: list[OpportunityRow],
    *,
    base: str,
    direction: str,
    position: PaperPosition,
    position_notional_usd: float,
    config: KucoinBasisConfig,
    require_profitable: bool = True,
    require_ex_funding_profit: bool = False,
    min_profit_usd: float = 0.0,
    max_exit_cost_pct: float | None = None,
) -> tuple[float, OpportunityRow, ExitEstimate] | None:
    candidates: list[tuple[float, OpportunityRow, ExitEstimate]] = []
    for chunk in config.gentle_unwind_chunk_ladder_usd:
        if chunk > position_notional_usd + 1e-8:
            continue
        row = _choose_full_close_row(
            rows,
            base=base,
            direction=direction,
            notional_usd=chunk,
        )
        if row is None:
            continue
        estimate = _estimate_exit_chunk(position, row, config, chunk)
        if estimate is None:
            continue
        if max_exit_cost_pct is not None and _exit_slippage_cost_pct(row, config) > max_exit_cost_pct:
            continue
        profit_to_test = estimate.net_pnl_ex_funding_usd if require_ex_funding_profit else estimate.net_pnl_usd
        if require_profitable and profit_to_test <= 0:
            continue
        if profit_to_test < min_profit_usd:
            continue
        candidates.append((chunk, row, estimate))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda candidate: (
            candidate[0],
            candidate[2].net_pnl_usd,
        ),
    )


def _choose_pre_funding_take_profit_close(
    rows: list[OpportunityRow],
    *,
    base: str,
    direction: str,
    position: PaperPosition,
    config: KucoinBasisConfig,
) -> tuple[float, OpportunityRow, ExitEstimate, float] | None:
    if not config.pre_funding_take_profit_enabled or position.funding_events_captured > 0:
        return None
    if _basis_improvement_pct(position) < config.pre_funding_take_profit_min_basis_improvement_pct:
        return None

    candidates: list[tuple[float, OpportunityRow, ExitEstimate, float]] = []
    for chunk in config.gentle_unwind_chunk_ladder_usd:
        if chunk >= position.notional_usd - 1e-8:
            continue
        row = _choose_full_close_row(rows, base=base, direction=direction, notional_usd=chunk)
        if row is None:
            continue
        estimate = _estimate_exit_chunk(position, row, config, chunk)
        if estimate is None or estimate.net_pnl_ex_funding_usd <= 0:
            continue
        funding_benefit_pct = max(0.0, _funding_benefit_pct(position, row) or 0.0)
        foregone_funding_usd = chunk * funding_benefit_pct / 100
        profit_hurdle_usd = max(
            config.pre_funding_take_profit_min_profit_usd,
            foregone_funding_usd * config.pre_funding_take_profit_funding_multiplier,
        )
        if estimate.net_pnl_ex_funding_usd < profit_hurdle_usd:
            continue
        candidates.append((chunk, row, estimate, foregone_funding_usd))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda candidate: (
            candidate[2].net_pnl_ex_funding_usd,
            candidate[2].net_pnl_ex_funding_usd / candidate[0],
            -candidate[0],
        ),
    )


def _choose_unusually_attractive_close(
    rows: list[OpportunityRow],
    *,
    base: str,
    direction: str,
    position: PaperPosition,
    config: KucoinBasisConfig,
) -> tuple[float, OpportunityRow, ExitEstimate] | None:
    chunk = min(config.funding_harvest_unwind_chunk_usd, position.notional_usd)
    if chunk <= 0:
        return None
    row = _choose_full_close_row(rows, base=base, direction=direction, notional_usd=chunk)
    if row is None:
        return None
    estimate = _estimate_exit_chunk(position, row, config, chunk)
    if estimate is None:
        return None
    hurdle_usd = min(
        config.unusually_attractive_unwind_profit_usd,
        chunk * config.unusually_attractive_unwind_profit_pct / 100,
    )
    if estimate.net_pnl_usd < hurdle_usd:
        return None
    return chunk, row, estimate


def _capital_recycle_is_preferred(
    position: PaperPosition,
    row: OpportunityRow,
    config: KucoinBasisConfig,
) -> bool:
    min_notional = config.max_symbol_notional_usd * config.capital_recycle_min_symbol_exposure_fraction
    if position.notional_usd < min_notional:
        return False
    funding_benefit_pct = _funding_benefit_pct(position, row)
    return funding_benefit_pct is None or funding_benefit_pct < config.capital_recycle_funding_rate_pct


def _best_redeployment_edge_pct(
    rows: list[OpportunityRow],
    *,
    position: PaperPosition,
    positions: dict[str, PaperPosition],
    chunk_notional_usd: float,
    config: KucoinBasisConfig,
) -> float:
    open_notional_by_base = _open_notional_by_base(positions)
    candidates: list[float] = []
    for candidate in rows:
        if (
            candidate.base == position.base
            or candidate.decision != "ENTER_CANDIDATE"
            or not candidate.round_trip_fillable
            or candidate.expected_edge_pct is None
            or candidate.expected_edge_pct < config.min_expected_edge_pct
            or candidate.notional_usd + 1e-8 < chunk_notional_usd
        ):
            continue
        candidate_position_id = _position_id(candidate.base, candidate.direction)
        if (
            candidate_position_id not in positions
            and len(positions) >= config.max_open_positions
        ):
            continue
        if (
            open_notional_by_base.get(candidate.base, 0.0) + chunk_notional_usd
            > config.max_symbol_notional_usd + 1e-8
        ):
            continue
        candidates.append(candidate.expected_edge_pct)
    return max(candidates, default=0.0)


def _compare_hold_with_exit_and_redeployment(
    rows: list[OpportunityRow],
    *,
    position: PaperPosition,
    reference_row: OpportunityRow,
    positions: dict[str, PaperPosition],
    config: KucoinBasisConfig,
) -> ExitValueComparison | None:
    if not config.economic_funding_hold_enabled:
        return None
    funding_benefit_pct = _funding_benefit_pct(position, reference_row)
    if funding_benefit_pct is None or funding_benefit_pct <= 0:
        return None

    chunk = min(config.funding_harvest_unwind_chunk_usd, position.notional_usd)
    if chunk <= 0:
        return None
    best_redeployment_edge_pct = _best_redeployment_edge_pct(
        rows,
        position=position,
        positions=positions,
        chunk_notional_usd=chunk,
        config=config,
    )
    basis_improvement_pct = max(0.0, _basis_improvement_pct(position))
    statistical_risk_pct = max(0.0, reference_row.basis_std_pct or 0.0) * (
        config.basis_giveback_risk_std_multiplier
    )
    fallback_risk_pct = (
        basis_improvement_pct * config.basis_giveback_risk_improvement_fraction
    )
    basis_giveback_risk_pct = min(
        basis_improvement_pct,
        max(statistical_risk_pct, fallback_risk_pct),
    )

    risk_adjusted_next_funding_usd = (
        chunk * funding_benefit_pct / 100 * config.next_funding_value_haircut
    )
    risk_adjusted_redeployment_usd = (
        chunk
        * best_redeployment_edge_pct
        / 100
        * config.redeployment_edge_haircut
    )
    basis_giveback_risk_usd = chunk * basis_giveback_risk_pct / 100
    risk_adjusted_exit_redeploy_usd = (
        risk_adjusted_redeployment_usd
        + basis_giveback_risk_usd
        + config.economic_hold_min_advantage_usd
    )
    return ExitValueComparison(
        chunk_notional_usd=chunk,
        risk_adjusted_next_funding_usd=risk_adjusted_next_funding_usd,
        risk_adjusted_exit_redeploy_usd=risk_adjusted_exit_redeploy_usd,
        best_redeployment_edge_pct=best_redeployment_edge_pct,
        basis_giveback_risk_usd=basis_giveback_risk_usd,
    )


def _choose_funding_harvest_close(
    rows: list[OpportunityRow],
    *,
    base: str,
    direction: str,
    position: PaperPosition,
    config: KucoinBasisConfig,
) -> tuple[float, OpportunityRow, ExitEstimate] | None:
    if position.funding_events_captured <= 0 or position.realised_funding_pnl_usd <= 0:
        return None
    return _choose_partial_close(
        rows,
        base=base,
        direction=direction,
        position=position,
        position_notional_usd=min(
            position.notional_usd, config.funding_harvest_unwind_chunk_usd
        ),
        config=config,
        require_profitable=True,
        require_ex_funding_profit=False,
        min_profit_usd=config.min_funding_harvest_unwind_profit_usd,
        max_exit_cost_pct=config.max_entry_exit_cost_pct,
    )


def _reduce_position(position: PaperPosition, chunk_notional_usd: float) -> None:
    if chunk_notional_usd >= position.notional_usd - 1e-8:
        position.notional_usd = 0.0
        position.spot_qty = 0.0
        position.perp_qty = 0.0
        position.realised_funding_pnl_usd = 0.0
        position.status = "CLOSED"
        position.updated_at = utc_now()
        return

    fraction = chunk_notional_usd / position.notional_usd
    position.notional_usd -= chunk_notional_usd
    position.spot_qty *= 1 - fraction
    position.perp_qty *= 1 - fraction
    position.realised_funding_pnl_usd *= 1 - fraction
    position.updated_at = utc_now()


def _add_to_position(
    position: PaperPosition,
    row: OpportunityRow,
    spot_quantity: float | None = None,
    perp_quantity: float | None = None,
) -> PaperPosition:
    old_notional = position.notional_usd
    new_notional = old_notional + row.notional_usd
    spot_entry_price = row.spot_entry_avg_price or 0.0
    perp_entry_price = row.perp_entry_avg_price or 0.0
    if spot_entry_price <= 0 or perp_entry_price <= 0:
        return position

    added_spot_qty = spot_quantity or row.notional_usd / spot_entry_price
    added_perp_qty = perp_quantity or row.notional_usd / perp_entry_price
    old_spot_cost = position.spot_qty * position.spot_entry_price
    old_perp_cost = position.perp_qty * position.perp_entry_price
    position.spot_qty += added_spot_qty
    position.perp_qty += added_perp_qty
    position.notional_usd = new_notional
    position.spot_entry_price = (
        old_spot_cost + added_spot_qty * spot_entry_price
    ) / position.spot_qty
    position.perp_entry_price = (
        old_perp_cost + added_perp_qty * perp_entry_price
    ) / position.perp_qty
    position.entry_basis_pct = (
        (position.entry_basis_pct * old_notional) + ((row.basis_pct or 0.0) * row.notional_usd)
    ) / new_notional
    position.current_basis_pct = row.basis_pct if row.basis_pct is not None else position.current_basis_pct
    position.funding_rate_pct_at_entry = (
        (position.funding_rate_pct_at_entry * old_notional)
        + ((row.funding_rate_pct or 0.0) * row.notional_usd)
    ) / new_notional
    funding_benefit_pct = (
        row.funding_rate_pct
        if position.direction == "LONG_SPOT_SHORT_PERP"
        else -(row.funding_rate_pct or 0.0)
    )
    position.expected_funding_pct = (
        (position.expected_funding_pct * old_notional)
        + ((funding_benefit_pct or 0.0) * row.notional_usd)
    ) / new_notional
    position.next_funding_time = row.funding_time_utc or position.next_funding_time
    position.funding_interval_hours = row.funding_interval or position.funding_interval_hours
    position.spot_hedge_route = position.spot_hedge_route or row.spot_hedge_route
    position.updated_at = utc_now()
    return position


def run_paper_strategy_once(
    config: KucoinBasisConfig = DEFAULT_CONFIG,
    opportunity_path: Path | None = None,
    funding_client: KucoinPublicClient | None = None,
    execution_adapter: ExecutionAdapter | None = None,
) -> dict:
    store = PaperStore(config)
    now = utc_now()
    opportunity_path = opportunity_path or latest_opportunity_file(config)
    loaded_opportunities = load_opportunities(opportunity_path)
    opportunities = _fresh_opportunities(loaded_opportunities, config, now)
    processed = store.load_processed_opportunities()
    positions = store.load_open_positions()
    active_cooldowns = store.load_active_cooldowns(now)
    _repair_missing_next_funding_times(positions, store, config)
    by_base = _open_notional_by_base(positions)
    funding_client = funding_client or KucoinPublicClient()
    execution_attempts = 0
    execution_rejections = 0

    latest_by_position = {}
    for row in opportunities:
        latest_by_position[(row.base, row.direction)] = row

    for position in list(positions.values()):
        row = _choose_full_close_row(
            opportunities,
            base=position.base,
            direction=position.direction,
            notional_usd=position.notional_usd,
        ) or latest_by_position.get((position.base, position.direction))
        _accrue_funding_if_crossed(position, row, store, config, funding_client)
        if row is None:
            store.append_decision(
                {
                    "timestamp_utc": format_datetime(utc_now()),
                    "decision_type": "EXIT",
                    "base": position.base,
                    "direction": position.direction,
                    "position_id": position.position_id,
                    "opportunity_key": "",
                    "allowed": "False",
                    "reason": "no_fresh_market_row",
                    "notional_usd": f"{position.notional_usd:.8f}",
                    "expected_edge_pct": "",
                    "estimated_net_pnl_usd": f"{position.estimated_net_pnl_usd:.8f}",
                    "row_timestamp_utc": "",
                    "row_age_seconds": "",
                    "entry_basis_pct": f"{position.entry_basis_pct:.8f}",
                    "current_basis_pct": f"{position.current_basis_pct:.8f}",
                    "basis_improvement_pct": f"{_basis_improvement_pct(position):.8f}",
                }
            )
            continue
        _mark_position(position, row, config)
        position_now = utc_now()
        _update_adverse_funding_state(position, row, config, position_now)
        should_exit, reason = _should_exit(position, row, config)
        forced_unwind_reason = _forced_unwind_reason(
            position,
            row,
            config,
            position_now,
        )
        if forced_unwind_reason is not None:
            should_exit = True
            reason = forced_unwind_reason
        selected_exit: tuple[float, OpportunityRow, ExitEstimate] | None = None
        foregone_funding_usd: float | None = None
        pre_funding_exit_profit_usd: float | None = None
        capital_recycle_triggered = False
        exit_value_comparison: ExitValueComparison | None = None

        if position.funding_events_captured <= 0:
            pre_funding_exit = _choose_pre_funding_take_profit_close(
                opportunities,
                base=position.base,
                direction=position.direction,
                position=position,
                config=config,
            )
            if pre_funding_exit is not None:
                chunk, exit_row, exit_estimate, foregone_funding_usd = pre_funding_exit
                selected_exit = (chunk, exit_row, exit_estimate)
                pre_funding_exit_profit_usd = exit_estimate.net_pnl_ex_funding_usd
                should_exit = True
                reason = "pre_funding_exceptional_take_profit"
        elif not should_exit and reason != "hold_for_juicy_next_funding":
            attractive_exit = _choose_unusually_attractive_close(
                opportunities,
                base=position.base,
                direction=position.direction,
                position=position,
                config=config,
            )
            if attractive_exit is not None:
                selected_exit = attractive_exit
                should_exit = True
                reason = "unusually_attractive_all_in_unwind"
            elif _capital_recycle_is_preferred(position, row, config):
                capital_exit = _choose_partial_close(
                    opportunities,
                    base=position.base,
                    direction=position.direction,
                    position=position,
                    position_notional_usd=position.notional_usd,
                    config=config,
                    require_profitable=True,
                    require_ex_funding_profit=False,
                    min_profit_usd=config.min_funding_harvest_unwind_profit_usd,
                    max_exit_cost_pct=config.max_entry_exit_cost_pct,
                )
                if capital_exit is not None:
                    selected_exit = capital_exit
                    should_exit = True
                    reason = "capital_recycle_profitable_unwind"
                    capital_recycle_triggered = True

        if reason in DISCRETIONARY_POST_FUNDING_EXIT_REASONS:
            exit_value_comparison = _compare_hold_with_exit_and_redeployment(
                opportunities,
                position=position,
                reference_row=row,
                positions=positions,
                config=config,
            )
            if (
                exit_value_comparison is not None
                and exit_value_comparison.hold_is_preferred
            ):
                should_exit = False
                selected_exit = None
                capital_recycle_triggered = False
                reason = "hold_for_superior_next_funding_value"

        if reason == "hold_basis_moved_adversely":
            _ensure_cooldown(
                store,
                active_cooldowns,
                base=position.base,
                direction=position.direction,
                reason=reason,
                config=config,
            )
        funding_benefit_pct = _funding_benefit_pct(position, row)
        expected_next_funding_usd = (
            position.notional_usd * funding_benefit_pct / 100
            if funding_benefit_pct is not None
            else None
        )
        basis_target_reached = _basis_target_reason(position, config) is not None
        all_in_chunk_profit_usd = selected_exit[2].net_pnl_usd if selected_exit is not None else None
        if reason == "pre_funding_exceptional_take_profit":
            exit_mode = "pre_funding_take_profit"
        elif reason in {
            "funding_captured_next_funding_below_threshold",
            "funding_weak_basis_adverse_try_unwind",
            "funding_captured_holding_edge_weak",
        }:
            exit_mode = "weak_funding"
        elif reason in {"basis_converged_take_profit", "basis_near_flat_take_profit"}:
            exit_mode = "basis_target"
        elif reason == "unusually_attractive_all_in_unwind":
            exit_mode = "attractive_all_in"
        elif reason == "capital_recycle_profitable_unwind":
            exit_mode = "capital_recycle"
        elif reason in {
            "pre_funding_reversal_toxic_unwind",
            "post_funding_reversal_toxic_unwind",
        }:
            exit_mode = "toxic_unwind"
        elif reason in {"timed_exit_unwind", "timed_exit_deadline"}:
            exit_mode = "timed_exit"
        else:
            exit_mode = "hold"
        exit_audit_fields = {
            "exit_mode": exit_mode,
            "expected_next_funding_usd": (
                "" if expected_next_funding_usd is None else f"{expected_next_funding_usd:.8f}"
            ),
            "pre_funding_exit_profit_usd": (
                "" if pre_funding_exit_profit_usd is None else f"{pre_funding_exit_profit_usd:.8f}"
            ),
            "basis_target_reached": str(basis_target_reached),
            "all_in_chunk_profit_usd": (
                "" if all_in_chunk_profit_usd is None else f"{all_in_chunk_profit_usd:.8f}"
            ),
            "capital_recycle_triggered": str(capital_recycle_triggered),
            "foregone_funding_usd": (
                "" if foregone_funding_usd is None else f"{foregone_funding_usd:.8f}"
            ),
            "economic_hold_applied": str(
                exit_value_comparison is not None
                and exit_value_comparison.hold_is_preferred
            ),
            "economic_comparison_chunk_usd": (
                ""
                if exit_value_comparison is None
                else f"{exit_value_comparison.chunk_notional_usd:.8f}"
            ),
            "risk_adjusted_next_funding_usd": (
                ""
                if exit_value_comparison is None
                else f"{exit_value_comparison.risk_adjusted_next_funding_usd:.8f}"
            ),
            "risk_adjusted_exit_redeploy_usd": (
                ""
                if exit_value_comparison is None
                else f"{exit_value_comparison.risk_adjusted_exit_redeploy_usd:.8f}"
            ),
            "best_redeployment_edge_pct": (
                ""
                if exit_value_comparison is None
                else f"{exit_value_comparison.best_redeployment_edge_pct:.8f}"
            ),
            "basis_giveback_risk_usd": (
                ""
                if exit_value_comparison is None
                else f"{exit_value_comparison.basis_giveback_risk_usd:.8f}"
            ),
        }
        decision_now = utc_now()
        store.append_decision(
            {
                "timestamp_utc": format_datetime(decision_now),
                "decision_type": "EXIT",
                "base": position.base,
                "direction": position.direction,
                "position_id": position.position_id,
                "opportunity_key": row.opportunity_key,
                "allowed": str(should_exit),
                "reason": reason,
                "notional_usd": f"{position.notional_usd:.8f}",
                "expected_edge_pct": "" if row.expected_edge_pct is None else f"{row.expected_edge_pct:.8f}",
                "estimated_net_pnl_usd": f"{position.estimated_net_pnl_usd:.8f}",
                "row_timestamp_utc": format_datetime(row.timestamp_utc),
                "row_age_seconds": f"{_row_age_seconds(row, decision_now):.3f}",
                "entry_basis_pct": f"{position.entry_basis_pct:.8f}",
                "current_basis_pct": f"{position.current_basis_pct:.8f}",
                "basis_improvement_pct": f"{_basis_improvement_pct(position):.8f}",
                **exit_audit_fields,
            }
        )
        if should_exit:
            exit_chunk = None
            exit_row = row
            exit_estimate = None
            if selected_exit is not None:
                exit_chunk, exit_row, exit_estimate = selected_exit
            full_exit_estimate = None
            full_exit_min_profit_usd = (
                position.notional_usd * config.min_profit_to_full_exit_pct / 100
                if config.gentle_unwind_enabled
                else 0.0
            )
            full_exit = False
            if (
                selected_exit is None
                and row.notional_usd + 1e-8 >= position.notional_usd
                and row.spot_exit_avg_price is not None
                and row.perp_exit_avg_price is not None
            ):
                full_exit_estimate = _estimate_exit_chunk(position, row, config, position.notional_usd)
                full_exit = (
                    full_exit_estimate is not None
                    and full_exit_estimate.net_pnl_ex_funding_usd >= full_exit_min_profit_usd
                )
            if selected_exit is None and full_exit:
                exit_chunk = position.notional_usd
                exit_estimate = full_exit_estimate
            elif selected_exit is None and config.gentle_unwind_enabled:
                partial = _choose_partial_close(
                    opportunities,
                    base=position.base,
                    direction=position.direction,
                    position=position,
                    position_notional_usd=position.notional_usd,
                    config=config,
                    require_profitable=True,
                    require_ex_funding_profit=True,
                )
                if partial is not None:
                    exit_chunk, exit_row, exit_estimate = partial
                elif reason in {
                    "funding_captured_next_funding_below_threshold",
                    "funding_weak_basis_adverse_try_unwind",
                    "funding_captured_holding_edge_weak",
                    "basis_converged_take_profit",
                    "basis_near_flat_take_profit",
                    *TOXIC_UNWIND_REASONS,
                }:
                    harvest = _choose_funding_harvest_close(
                        opportunities,
                        base=position.base,
                        direction=position.direction,
                        position=position,
                        config=config,
                    )
                    if harvest is not None:
                        exit_chunk, exit_row, exit_estimate = harvest
                        reason = "funding_harvest_profitable_unwind"

                if exit_chunk is None and reason in TOXIC_UNWIND_REASONS:
                    forced_close = _choose_forced_unwind_close(
                        opportunities,
                        base=position.base,
                        direction=position.direction,
                        position=position,
                        reason=reason,
                        config=config,
                        now=utc_now(),
                    )
                    if forced_close is not None:
                        exit_chunk, exit_row, exit_estimate = forced_close

            if exit_chunk is None or exit_estimate is None:
                if reason == "timed_exit_unwind":
                    no_exit_reason = "timed_exit_waiting_for_better_price"
                elif reason in {
                    "pre_funding_reversal_toxic_unwind",
                    "post_funding_reversal_toxic_unwind",
                }:
                    no_exit_reason = "toxic_unwind_waiting_for_price_or_pace"
                else:
                    no_exit_reason = "exit_wanted_no_profitable_chunk"
                store.append_decision(
                    {
                        "timestamp_utc": format_datetime(utc_now()),
                        "decision_type": "EXIT",
                        "base": position.base,
                        "direction": position.direction,
                        "position_id": position.position_id,
                        "opportunity_key": row.opportunity_key,
                        "allowed": "False",
                        "reason": no_exit_reason,
                        "notional_usd": f"{position.notional_usd:.8f}",
                        "expected_edge_pct": "" if row.expected_edge_pct is None else f"{row.expected_edge_pct:.8f}",
                        "estimated_net_pnl_usd": f"{position.estimated_net_pnl_usd:.8f}",
                        "row_timestamp_utc": format_datetime(row.timestamp_utc),
                        "row_age_seconds": f"{_row_age_seconds(row, utc_now()):.3f}",
                        "entry_basis_pct": f"{position.entry_basis_pct:.8f}",
                        "current_basis_pct": f"{position.current_basis_pct:.8f}",
                        "basis_improvement_pct": f"{_basis_improvement_pct(position):.8f}",
                        **exit_audit_fields,
                    }
                )
                continue

            if execution_adapter is not None:
                exit_fraction = min(1.0, exit_chunk / position.notional_usd)
                target_base_quantity = min(
                    position.spot_qty * exit_fraction,
                    position.perp_qty * exit_fraction,
                )
                execution_result = execution_adapter.execute(
                    "EXIT",
                    replace(
                        exit_row,
                        spot_hedge_route=(
                            position.spot_hedge_route
                            or exit_row.spot_hedge_route
                            or (
                                "CROSS_MARGIN"
                                if position.direction == "SHORT_SPOT_LONG_PERP"
                                else "CASH_SPOT"
                            )
                        ),
                    ),
                    exit_chunk,
                    target_base_quantity=target_base_quantity,
                )
                execution_attempts += 1
                store.append_execution_attempt(execution_result.to_csv_row())
                if not execution_result.accepted:
                    execution_rejections += 1
                    store.append_decision(
                        {
                            "timestamp_utc": format_datetime(utc_now()),
                            "decision_type": "EXIT",
                            "base": position.base,
                            "direction": position.direction,
                            "position_id": position.position_id,
                            "opportunity_key": exit_row.opportunity_key,
                            "allowed": "False",
                            "reason": f"dry_run_preflight_rejected: {execution_result.reason}",
                            "notional_usd": f"{exit_chunk:.8f}",
                            "expected_edge_pct": (
                                ""
                                if exit_row.expected_edge_pct is None
                                else f"{exit_row.expected_edge_pct:.8f}"
                            ),
                            "estimated_net_pnl_usd": f"{exit_estimate.net_pnl_usd:.8f}",
                            "row_timestamp_utc": format_datetime(exit_row.timestamp_utc),
                            "row_age_seconds": f"{_row_age_seconds(exit_row, utc_now()):.3f}",
                            "entry_basis_pct": f"{position.entry_basis_pct:.8f}",
                            "current_basis_pct": f"{position.current_basis_pct:.8f}",
                            "basis_improvement_pct": f"{_basis_improvement_pct(position):.8f}",
                            **exit_audit_fields,
                        }
                    )
                    continue
                exit_row = replace(
                    exit_row,
                    notional_usd=exit_chunk,
                    spot_exit_avg_price=execution_result.spot_average_price,
                    perp_exit_avg_price=execution_result.perp_average_price,
                    spot_exit_slippage_pct=execution_result.spot_slippage_pct,
                    perp_exit_slippage_pct=execution_result.perp_slippage_pct,
                    spot_ask=(
                        execution_result.spot_average_price
                        if position.direction == "SHORT_SPOT_LONG_PERP"
                        else exit_row.spot_ask
                    ),
                    perp_bid=(
                        execution_result.perp_average_price
                        if position.direction == "SHORT_SPOT_LONG_PERP"
                        else exit_row.perp_bid
                    ),
                    spot_bid=(
                        execution_result.spot_average_price
                        if position.direction == "LONG_SPOT_SHORT_PERP"
                        else exit_row.spot_bid
                    ),
                    perp_ask=(
                        execution_result.perp_average_price
                        if position.direction == "LONG_SPOT_SHORT_PERP"
                        else exit_row.perp_ask
                    ),
                )
                exit_estimate = _estimate_exit_chunk(
                    position,
                    exit_row,
                    config,
                    exit_chunk,
                )
                if exit_estimate is None:
                    continue

                # The adapter has just repriced both legs from fresh depth. A
                # stale scanner row must not turn a previously profitable exit
                # into a realised loss, except for explicit risk/deadline exits.
                if exit_estimate.net_pnl_usd <= 0 and reason not in FORCED_EXIT_REASONS:
                    store.append_decision(
                        {
                            "timestamp_utc": format_datetime(utc_now()),
                            "decision_type": "EXIT",
                            "base": position.base,
                            "direction": position.direction,
                            "position_id": position.position_id,
                            "opportunity_key": exit_row.opportunity_key,
                            "allowed": "False",
                            "reason": "fresh_exit_unprofitable",
                            "notional_usd": f"{exit_chunk:.8f}",
                            "expected_edge_pct": (
                                ""
                                if exit_row.expected_edge_pct is None
                                else f"{exit_row.expected_edge_pct:.8f}"
                            ),
                            "estimated_net_pnl_usd": f"{exit_estimate.net_pnl_usd:.8f}",
                            "row_timestamp_utc": format_datetime(exit_row.timestamp_utc),
                            "row_age_seconds": f"{_row_age_seconds(exit_row, utc_now()):.3f}",
                            "entry_basis_pct": f"{position.entry_basis_pct:.8f}",
                            "current_basis_pct": f"{position.current_basis_pct:.8f}",
                            "basis_improvement_pct": f"{_basis_improvement_pct(position):.8f}",
                        }
                    )
                    continue

            event_type = "CLOSE_POSITION" if exit_chunk >= position.notional_usd - 1e-8 else "PARTIAL_CLOSE"
            store.append_fill(
                {
                    "timestamp_utc": format_datetime(utc_now()),
                    "event_type": event_type,
                    "position_id": position.position_id,
                    "base": position.base,
                    "direction": position.direction,
                    "spot_symbol": position.spot_symbol,
                    "perp_symbol": position.perp_symbol,
                    "notional_usd": f"{exit_chunk:.8f}",
                    "spot_price": exit_row.spot_exit_avg_price or "",
                    "perp_price": exit_row.perp_exit_avg_price or "",
                    "fees_usd": f"{exit_estimate.close_cost_usd:.8f}",
                    "realised_pnl_usd": f"{exit_estimate.net_pnl_ex_funding_usd:.8f}",
                    "realised_basis_pnl_usd": f"{exit_estimate.basis_pnl_usd:.8f}",
                    "realised_funding_pnl_usd": f"{exit_estimate.funding_pnl_usd:.8f}",
                    "reason": reason,
                }
            )
            _ensure_cooldown(
                store,
                active_cooldowns,
                base=position.base,
                direction=position.direction,
                reason="post_close_reentry_cooldown",
                config=config,
                duration_minutes=config.post_close_reentry_cooldown_minutes,
            )
            _reduce_position(position, exit_chunk)
            if position.status == "CLOSED":
                positions.pop(position.position_id, None)

    entries = 0
    by_base = _open_notional_by_base(positions)
    total_open = sum(position.notional_usd for position in positions.values())
    best_entry_keys_by_group = _best_entry_keys_by_group(opportunities, processed)
    for row in sorted(opportunities, key=lambda item: (item.expected_edge_pct or -999, -item.notional_usd), reverse=True):
        if row.opportunity_key in processed:
            continue
        store.mark_processed(row.opportunity_key, format_datetime(row.timestamp_utc), opportunity_path)

        allowed = row.decision == "ENTER_CANDIDATE"
        reason = row.reason
        if allowed and best_entry_keys_by_group.get(_entry_group_key(row)) != row.opportunity_key:
            allowed = False
            reason = "lower_ranked_chunk_same_tick"
        position_id = _position_id(row.base, row.direction)
        existing_position = positions.get(position_id)
        new_symbol_notional = by_base.get(row.base, 0.0) + row.notional_usd
        cooldown = active_cooldowns.get(_cooldown_key(row.base, row.direction))
        if allowed and cooldown is not None:
            allowed = False
            reason = cooldown.get("reason") or "volatility_cooldown"
        elif allowed and _row_basis_too_volatile(row, config):
            allowed = False
            reason = "basis_too_volatile_no_entry"
            _ensure_cooldown(
                store,
                active_cooldowns,
                base=row.base,
                direction=row.direction,
                reason=reason,
                config=config,
            )
        elif allowed and existing_position is not None and _basis_moved_adversely(existing_position, config):
            allowed = False
            reason = "basis_too_volatile_no_add"
            _ensure_cooldown(
                store,
                active_cooldowns,
                base=row.base,
                direction=row.direction,
                reason=reason,
                config=config,
            )
        elif allowed and existing_position is not None and (
            existing_position.adverse_funding_since is not None
            or _position_age_hours(existing_position, utc_now()) >= config.timed_exit_start_hours
        ):
            allowed = False
            reason = "toxic_or_timed_unwind_no_add"
        elif existing_position is None and len(positions) >= config.max_open_positions:
            allowed = False
            reason = "max_open_positions"
        elif total_open + row.notional_usd > config.max_total_notional_usd:
            allowed = False
            reason = "max_total_exposure"
        elif new_symbol_notional > config.max_symbol_notional_usd:
            allowed = False
            reason = "max_symbol_exposure"

        execution_result = None
        execution_row = row
        if allowed and execution_adapter is not None:
            execution_result = execution_adapter.execute(
                "ENTRY", row, row.notional_usd
            )
            execution_attempts += 1
            store.append_execution_attempt(execution_result.to_csv_row())
            if not execution_result.accepted:
                execution_rejections += 1
                allowed = False
                reason = f"dry_run_preflight_rejected: {execution_result.reason}"
            else:
                execution_row = replace(
                    row,
                    notional_usd=execution_result.executable_notional_usd,
                    spot_hedge_route=(
                        "ISOLATED_MARGIN"
                        if execution_result.spot_venue == "margin_isolated"
                        else (
                            "CROSS_MARGIN"
                            if execution_result.spot_venue == "margin_cross"
                            else row.spot_hedge_route
                        )
                    ),
                    spot_entry_avg_price=execution_result.spot_average_price,
                    perp_entry_avg_price=execution_result.perp_average_price,
                    spot_entry_slippage_pct=execution_result.spot_slippage_pct,
                    perp_entry_slippage_pct=execution_result.perp_slippage_pct,
                )

        store.append_decision(
            {
                "timestamp_utc": format_datetime(utc_now()),
                "decision_type": "ENTRY",
                "base": row.base,
                "direction": row.direction,
                "position_id": position_id,
                "opportunity_key": row.opportunity_key,
                "allowed": str(allowed),
                "reason": reason,
                "notional_usd": f"{row.notional_usd:.8f}",
                "expected_edge_pct": "" if row.expected_edge_pct is None else f"{row.expected_edge_pct:.8f}",
                "estimated_net_pnl_usd": "",
                "row_timestamp_utc": format_datetime(row.timestamp_utc),
                "row_age_seconds": f"{_row_age_seconds(row, utc_now()):.3f}",
                "entry_basis_pct": "",
                "current_basis_pct": "" if row.basis_pct is None else f"{row.basis_pct:.8f}",
                "basis_improvement_pct": "",
            }
        )
        if not allowed:
            continue

        row = execution_row
        new_symbol_notional = by_base.get(row.base, 0.0) + row.notional_usd

        spot_entry_price = row.spot_entry_avg_price or row.spot_ask or 0.0
        perp_entry_price = row.perp_entry_avg_price or row.perp_bid or 0.0
        if spot_entry_price <= 0 or perp_entry_price <= 0:
            continue

        if existing_position is not None:
            position = _add_to_position(
                existing_position,
                row,
                spot_quantity=(execution_result.spot_size if execution_result else None),
                perp_quantity=(
                    execution_result.perp_base_quantity if execution_result else None
                ),
            )
            event_type = "ADD_POSITION"
        else:
            funding_benefit_pct = (
                row.funding_rate_pct
                if row.direction == "LONG_SPOT_SHORT_PERP"
                else -(row.funding_rate_pct or 0.0)
            )
            position = PaperPosition(
                position_id=position_id,
                base=row.base,
                direction=row.direction,
                spot_symbol=row.spot_symbol,
                perp_symbol=row.perp_symbol,
                notional_usd=row.notional_usd,
                spot_qty=(
                    execution_result.spot_size
                    if execution_result and execution_result.spot_size > 0
                    else row.notional_usd / spot_entry_price
                ),
                perp_qty=(
                    execution_result.perp_base_quantity
                    if execution_result and execution_result.perp_base_quantity > 0
                    else row.notional_usd / perp_entry_price
                ),
                spot_entry_price=spot_entry_price,
                perp_entry_price=perp_entry_price,
                entry_basis_pct=row.basis_pct or 0.0,
                current_basis_pct=row.basis_pct or 0.0,
                funding_rate_pct_at_entry=row.funding_rate_pct or 0.0,
                expected_funding_pct=funding_benefit_pct or 0.0,
                realised_funding_pnl_usd=0.0,
                unrealised_basis_pnl_usd=0.0,
                estimated_close_cost_usd=0.0,
                estimated_net_pnl_usd=0.0,
                created_at=utc_now(),
                updated_at=utc_now(),
                next_funding_time=row.funding_time_utc,
                funding_events_captured=0,
                funding_interval_hours=row.funding_interval or config.fallback_funding_interval_hours,
                spot_hedge_route=row.spot_hedge_route,
            )
            event_type = "OPEN_POSITION"
        positions[position.position_id] = position
        by_base[row.base] = new_symbol_notional
        total_open += row.notional_usd
        entries += 1
        store.append_fill(
            {
                "timestamp_utc": format_datetime(utc_now()),
                "event_type": event_type,
                "position_id": position.position_id,
                "base": position.base,
                "direction": position.direction,
                "spot_symbol": position.spot_symbol,
                "perp_symbol": position.perp_symbol,
                "notional_usd": f"{row.notional_usd:.8f}",
                "spot_price": "" if row.spot_entry_avg_price is None else f"{row.spot_entry_avg_price:.8f}",
                "perp_price": "" if row.perp_entry_avg_price is None else f"{row.perp_entry_avg_price:.8f}",
                "fees_usd": f"{row.notional_usd * (config.estimated_spot_taker_fee_pct + config.estimated_perp_taker_fee_pct) / 100:.8f}",
                "realised_pnl_usd": "0.00000000",
                "realised_basis_pnl_usd": "",
                "realised_funding_pnl_usd": "",
                "reason": row.reason,
            }
        )

    store.write_positions(positions)
    return {
        "opportunity_file": str(opportunity_path),
        "opportunities_seen": len(loaded_opportunities),
        "fresh_opportunities_seen": len(opportunities),
        "entries_opened": entries,
        "open_positions": len(positions),
        "execution_attempts": execution_attempts,
        "execution_rejections": execution_rejections,
    }
