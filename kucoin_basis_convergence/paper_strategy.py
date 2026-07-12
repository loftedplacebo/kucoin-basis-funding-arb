from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from kucoin_basis_convergence.config import DEFAULT_CONFIG, KucoinBasisConvergenceConfig
from kucoin_basis_convergence.models import (
    ConvergenceOpportunityRow,
    format_datetime,
    parse_datetime,
    utc_now,
)
from kucoin_basis_convergence.paper_models import ConvergencePaperPosition
from kucoin_basis_convergence.paper_store import ConvergencePaperStore


@dataclass(frozen=True)
class ExitEstimate:
    basis_pnl_usd: float
    close_cost_usd: float
    funding_pnl_usd: float
    net_pnl_ex_funding_usd: float
    net_pnl_usd: float
    net_pnl_pct: float


def latest_opportunity_file(config: KucoinBasisConvergenceConfig) -> Path:
    files = sorted(config.opportunities_dir.glob("kucoin_basis_convergence_opportunities_*.csv"))
    if not files:
        raise SystemExit(f"No KuCoin convergence opportunity files found in {config.opportunities_dir}")
    return files[-1]


def load_opportunities(path: Path) -> list[ConvergenceOpportunityRow]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return [ConvergenceOpportunityRow.from_csv_row(row) for row in csv.DictReader(f)]


def _fresh_opportunities(
    rows: list[ConvergenceOpportunityRow],
    config: KucoinBasisConvergenceConfig,
    now: datetime,
) -> list[ConvergenceOpportunityRow]:
    if not rows:
        return []
    latest_timestamp = max(row.timestamp_utc for row in rows)
    if (now - latest_timestamp).total_seconds() > config.max_strategy_row_age_seconds:
        return []
    return [row for row in rows if row.timestamp_utc == latest_timestamp]


def _position_id(base: str, direction: str) -> str:
    return f"KUCOIN_CONVERGENCE_{base}_{direction}"


def _entry_group_key(row: ConvergenceOpportunityRow) -> tuple[str, str, str]:
    return (format_datetime(row.timestamp_utc), row.base, row.direction)


def _best_entry_keys_by_group(
    rows: list[ConvergenceOpportunityRow],
    processed: set[str],
) -> dict[tuple[str, str, str], str]:
    best_by_group: dict[tuple[str, str, str], ConvergenceOpportunityRow] = {}
    for row in rows:
        if row.opportunity_key in processed or row.decision != "ENTER_CANDIDATE":
            continue
        group_key = _entry_group_key(row)
        current = best_by_group.get(group_key)
        row_rank = (row.net_edge_pct or -999.0, -row.notional_usd)
        current_rank = (
            (current.net_edge_pct or -999.0, -current.notional_usd)
            if current is not None
            else None
        )
        if current is None or current_rank is None or row_rank > current_rank:
            best_by_group[group_key] = row
    return {group_key: row.opportunity_key for group_key, row in best_by_group.items()}


def _open_notional_by_base(positions: dict[str, ConvergencePaperPosition]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for position in positions.values():
        totals[position.base] = totals.get(position.base, 0.0) + position.notional_usd
    return totals


def _basis_improvement_pct(position: ConvergencePaperPosition) -> float:
    if position.direction == "SHORT_SPOT_LONG_PERP":
        return position.current_basis_pct - position.entry_basis_pct
    return position.entry_basis_pct - position.current_basis_pct


def _row_age_seconds(row: ConvergenceOpportunityRow, now: datetime) -> float:
    return (now - row.timestamp_utc).total_seconds()


def _exit_slippage_cost_pct(row: ConvergenceOpportunityRow, config: KucoinBasisConvergenceConfig) -> float:
    return (
        (row.spot_exit_slippage_pct or 0.0)
        + (row.perp_exit_slippage_pct or 0.0)
        + config.estimated_exit_fee_pct
    )


def _estimate_exit_chunk(
    position: ConvergencePaperPosition,
    row: ConvergenceOpportunityRow,
    config: KucoinBasisConvergenceConfig,
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


def _funding_benefit_pct(position: ConvergencePaperPosition, raw_funding_rate_pct: float) -> float:
    if position.direction == "LONG_SPOT_SHORT_PERP":
        return raw_funding_rate_pct
    return -raw_funding_rate_pct


def _next_funding_time_after(
    funding_time: datetime,
    position: ConvergencePaperPosition,
    row: ConvergenceOpportunityRow | None,
    config: KucoinBasisConvergenceConfig,
) -> datetime:
    interval = (
        (row.funding_interval_hours if row is not None else None)
        or position.funding_interval_hours
        or config.fallback_funding_interval_hours
    )
    return funding_time + timedelta(hours=interval if interval > 0 else config.fallback_funding_interval_hours)


def _accrue_funding_if_crossed(
    position: ConvergencePaperPosition,
    row: ConvergenceOpportunityRow | None,
    store: ConvergencePaperStore,
    config: KucoinBasisConvergenceConfig,
) -> None:
    now = utc_now()
    funding_time = position.next_funding_time
    if funding_time is None or funding_time > now:
        return
    raw_funding_rate_pct = (
        row.funding_rate_pct
        if row is not None and row.funding_rate_pct is not None
        else position.funding_rate_pct_at_entry
    )
    while funding_time is not None and funding_time <= now:
        funding_pnl = position.notional_usd * _funding_benefit_pct(position, raw_funding_rate_pct) / 100
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


def _mark_position(
    position: ConvergencePaperPosition,
    row: ConvergenceOpportunityRow,
    config: KucoinBasisConvergenceConfig,
) -> None:
    position.current_basis_pct = row.basis_pct if row.basis_pct is not None else position.current_basis_pct
    position.current_zscore = row.basis_zscore if row.basis_zscore is not None else position.current_zscore
    position.current_percentile = (
        row.basis_percentile if row.basis_percentile is not None else position.current_percentile
    )
    position.target_basis_pct = row.basis_target_pct if row.basis_target_pct is not None else position.target_basis_pct
    position.funding_interval_hours = row.funding_interval_hours or position.funding_interval_hours
    if position.next_funding_time is None and row.funding_time_utc is not None:
        position.next_funding_time = row.funding_time_utc
    estimate = _estimate_exit_chunk(position, row, config, position.notional_usd)
    if estimate is not None:
        position.unrealised_basis_pnl_usd = estimate.basis_pnl_usd
        position.estimated_close_cost_usd = estimate.close_cost_usd
        position.estimated_net_pnl_ex_funding_usd = estimate.net_pnl_ex_funding_usd
        position.estimated_net_pnl_usd = estimate.net_pnl_usd
    position.updated_at = utc_now()


def _is_neutralised(position: ConvergencePaperPosition, config: KucoinBasisConvergenceConfig) -> bool:
    if abs(position.current_zscore) <= config.neutral_zscore_abs:
        return True
    return config.neutral_percentile_low <= position.current_percentile <= config.neutral_percentile_high


def _should_exit(
    position: ConvergencePaperPosition,
    row: ConvergenceOpportunityRow,
    config: KucoinBasisConvergenceConfig,
) -> tuple[bool, str]:
    age = utc_now() - position.created_at
    improvement = _basis_improvement_pct(position)
    net_pct = (
        position.estimated_net_pnl_ex_funding_usd / position.notional_usd * 100
        if position.notional_usd > 0
        else 0.0
    )
    if improvement <= -config.max_adverse_basis_move_pct:
        return True, "basis_stop_loss"
    if age >= timedelta(hours=config.hard_max_hold_hours):
        return True, "hard_max_hold_time"
    if improvement >= config.take_profit_basis_improvement_pct and net_pct > 0:
        return True, "basis_improvement_take_profit"
    if net_pct >= config.take_profit_net_pct:
        return True, "net_profit_take_profit"
    if _is_neutralised(position, config) and net_pct > 0:
        return True, "basis_neutralised_take_profit"
    if age >= timedelta(hours=config.max_hold_hours) and net_pct >= 0:
        return True, "max_hold_profitable_exit"
    if row.net_edge_pct is not None and row.net_edge_pct < 0 and net_pct > 0:
        return True, "remaining_edge_gone_take_profit"
    return False, "hold"


def _choose_full_close_row(
    rows: list[ConvergenceOpportunityRow],
    *,
    base: str,
    direction: str,
    notional_usd: float,
) -> ConvergenceOpportunityRow | None:
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
    return min(candidates, key=lambda row: (row.notional_usd, -(row.net_edge_pct or -999)))


def _choose_partial_close(
    rows: list[ConvergenceOpportunityRow],
    *,
    base: str,
    direction: str,
    position: ConvergencePaperPosition,
    position_notional_usd: float,
    config: KucoinBasisConvergenceConfig,
    require_profitable: bool,
) -> tuple[float, ConvergenceOpportunityRow, ExitEstimate] | None:
    candidates: list[tuple[float, ConvergenceOpportunityRow, ExitEstimate]] = []
    for chunk in config.gentle_unwind_chunk_ladder_usd:
        if chunk > position_notional_usd + 1e-8:
            continue
        row = _choose_full_close_row(rows, base=base, direction=direction, notional_usd=chunk)
        if row is None:
            continue
        estimate = _estimate_exit_chunk(position, row, config, chunk)
        if estimate is None:
            continue
        if require_profitable and estimate.net_pnl_ex_funding_usd <= 0:
            continue
        candidates.append((chunk, row, estimate))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda candidate: (
            candidate[2].net_pnl_ex_funding_usd / candidate[0] * 100,
            candidate[2].net_pnl_ex_funding_usd,
            -candidate[0],
        ),
    )


def _reduce_position(position: ConvergencePaperPosition, chunk_notional_usd: float) -> None:
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


def _add_to_position(position: ConvergencePaperPosition, row: ConvergenceOpportunityRow) -> ConvergencePaperPosition:
    old_notional = position.notional_usd
    new_notional = old_notional + row.notional_usd
    spot_entry_price = row.spot_entry_avg_price or 0.0
    perp_entry_price = row.perp_entry_avg_price or 0.0
    if spot_entry_price <= 0 or perp_entry_price <= 0:
        return position
    position.spot_qty += row.notional_usd / spot_entry_price
    position.perp_qty += row.notional_usd / perp_entry_price
    position.notional_usd = new_notional
    position.spot_entry_price = new_notional / position.spot_qty
    position.perp_entry_price = new_notional / position.perp_qty
    position.entry_basis_pct = (
        (position.entry_basis_pct * old_notional) + ((row.basis_pct or 0.0) * row.notional_usd)
    ) / new_notional
    position.entry_zscore = (
        (position.entry_zscore * old_notional) + ((row.basis_zscore or 0.0) * row.notional_usd)
    ) / new_notional
    position.entry_percentile = (
        (position.entry_percentile * old_notional) + ((row.basis_percentile or 0.0) * row.notional_usd)
    ) / new_notional
    position.entry_net_edge_pct = (
        (position.entry_net_edge_pct * old_notional) + ((row.net_edge_pct or 0.0) * row.notional_usd)
    ) / new_notional
    position.current_basis_pct = row.basis_pct if row.basis_pct is not None else position.current_basis_pct
    position.current_zscore = row.basis_zscore if row.basis_zscore is not None else position.current_zscore
    position.current_percentile = row.basis_percentile if row.basis_percentile is not None else position.current_percentile
    position.target_basis_pct = row.basis_target_pct if row.basis_target_pct is not None else position.target_basis_pct
    position.funding_rate_pct_at_entry = (
        (position.funding_rate_pct_at_entry * old_notional)
        + ((row.funding_rate_pct or 0.0) * row.notional_usd)
    ) / new_notional
    position.next_funding_time = row.funding_time_utc or position.next_funding_time
    position.funding_interval_hours = row.funding_interval_hours or position.funding_interval_hours
    position.updated_at = utc_now()
    return position


def _ensure_cooldown(
    store: ConvergencePaperStore,
    active_cooldowns: dict[tuple[str, str], dict],
    *,
    base: str,
    direction: str,
    reason: str,
    config: KucoinBasisConvergenceConfig,
    duration_minutes: float | None = None,
) -> None:
    key = (base, direction)
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


def _append_decision(
    store: ConvergencePaperStore,
    *,
    decision_type: str,
    base: str,
    direction: str,
    position_id: str,
    row: ConvergenceOpportunityRow | None,
    allowed: bool,
    reason: str,
    notional_usd: float,
    estimated_net_pnl_usd: float | None,
    entry_basis_pct: float | None = None,
    current_basis_pct: float | None = None,
    basis_improvement_pct: float | None = None,
) -> None:
    now = utc_now()
    store.append_decision(
        {
            "timestamp_utc": format_datetime(now),
            "decision_type": decision_type,
            "base": base,
            "direction": direction,
            "position_id": position_id,
            "opportunity_key": "" if row is None else row.opportunity_key,
            "allowed": str(allowed),
            "reason": reason,
            "notional_usd": f"{notional_usd:.8f}",
            "net_edge_pct": "" if row is None or row.net_edge_pct is None else f"{row.net_edge_pct:.8f}",
            "estimated_net_pnl_usd": "" if estimated_net_pnl_usd is None else f"{estimated_net_pnl_usd:.8f}",
            "row_timestamp_utc": "" if row is None else format_datetime(row.timestamp_utc),
            "row_age_seconds": "" if row is None else f"{_row_age_seconds(row, now):.3f}",
            "entry_basis_pct": "" if entry_basis_pct is None else f"{entry_basis_pct:.8f}",
            "current_basis_pct": "" if current_basis_pct is None else f"{current_basis_pct:.8f}",
            "basis_improvement_pct": ""
            if basis_improvement_pct is None
            else f"{basis_improvement_pct:.8f}",
            "basis_zscore": "" if row is None or row.basis_zscore is None else f"{row.basis_zscore:.8f}",
            "basis_percentile": ""
            if row is None or row.basis_percentile is None
            else f"{row.basis_percentile:.8f}",
        }
    )


def run_paper_strategy_once(
    config: KucoinBasisConvergenceConfig = DEFAULT_CONFIG,
    opportunity_path: Path | None = None,
) -> dict:
    store = ConvergencePaperStore(config)
    now = utc_now()
    opportunity_path = opportunity_path or latest_opportunity_file(config)
    loaded_opportunities = load_opportunities(opportunity_path)
    opportunities = _fresh_opportunities(loaded_opportunities, config, now)
    processed = store.load_processed_opportunities()
    positions = store.load_open_positions()
    active_cooldowns = store.load_active_cooldowns(now)

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
        _accrue_funding_if_crossed(position, row, store, config)
        if row is None:
            _append_decision(
                store,
                decision_type="EXIT",
                base=position.base,
                direction=position.direction,
                position_id=position.position_id,
                row=None,
                allowed=False,
                reason="no_fresh_market_row",
                notional_usd=position.notional_usd,
                estimated_net_pnl_usd=position.estimated_net_pnl_usd,
                entry_basis_pct=position.entry_basis_pct,
                current_basis_pct=position.current_basis_pct,
                basis_improvement_pct=_basis_improvement_pct(position),
            )
            continue
        _mark_position(position, row, config)
        should_exit, reason = _should_exit(position, row, config)
        _append_decision(
            store,
            decision_type="EXIT",
            base=position.base,
            direction=position.direction,
            position_id=position.position_id,
            row=row,
            allowed=should_exit,
            reason=reason,
            notional_usd=position.notional_usd,
            estimated_net_pnl_usd=position.estimated_net_pnl_usd,
            entry_basis_pct=position.entry_basis_pct,
            current_basis_pct=position.current_basis_pct,
            basis_improvement_pct=_basis_improvement_pct(position),
        )
        if not should_exit:
            continue

        exit_chunk = None
        exit_row = row
        exit_estimate = None
        force_exit = reason in {"basis_stop_loss", "hard_max_hold_time"}
        full_exit_estimate = _estimate_exit_chunk(position, row, config, position.notional_usd)
        if full_exit_estimate is not None and (force_exit or full_exit_estimate.net_pnl_ex_funding_usd > 0):
            exit_chunk = position.notional_usd
            exit_estimate = full_exit_estimate
        elif config.gentle_unwind_enabled:
            partial = _choose_partial_close(
                opportunities,
                base=position.base,
                direction=position.direction,
                position=position,
                position_notional_usd=position.notional_usd,
                config=config,
                require_profitable=not force_exit,
            )
            if partial is not None:
                exit_chunk, exit_row, exit_estimate = partial

        if exit_chunk is None or exit_estimate is None:
            _append_decision(
                store,
                decision_type="EXIT",
                base=position.base,
                direction=position.direction,
                position_id=position.position_id,
                row=row,
                allowed=False,
                reason="exit_wanted_no_executable_chunk",
                notional_usd=position.notional_usd,
                estimated_net_pnl_usd=position.estimated_net_pnl_usd,
                entry_basis_pct=position.entry_basis_pct,
                current_basis_pct=position.current_basis_pct,
                basis_improvement_pct=_basis_improvement_pct(position),
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
                "spot_price": "" if exit_row.spot_exit_avg_price is None else f"{exit_row.spot_exit_avg_price:.8f}",
                "perp_price": "" if exit_row.perp_exit_avg_price is None else f"{exit_row.perp_exit_avg_price:.8f}",
                "fees_usd": f"{exit_estimate.close_cost_usd:.8f}",
                "realised_pnl_usd": f"{exit_estimate.net_pnl_usd:.8f}",
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
    for row in sorted(opportunities, key=lambda item: (item.net_edge_pct or -999, -item.notional_usd), reverse=True):
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
        cooldown = active_cooldowns.get((row.base, row.direction))
        if allowed and cooldown is not None:
            allowed = False
            reason = cooldown.get("reason") or "cooldown"
        elif existing_position is None and len(positions) >= config.max_open_positions:
            allowed = False
            reason = "max_open_positions"
        elif total_open + row.notional_usd > config.max_total_notional_usd:
            allowed = False
            reason = "max_total_exposure"
        elif new_symbol_notional > config.max_symbol_notional_usd:
            allowed = False
            reason = "max_symbol_exposure"
        elif allowed and existing_position is not None and _basis_improvement_pct(existing_position) < 0:
            allowed = False
            reason = "no_add_while_existing_trade_adverse"

        _append_decision(
            store,
            decision_type="ENTRY",
            base=row.base,
            direction=row.direction,
            position_id=position_id,
            row=row,
            allowed=allowed,
            reason=reason,
            notional_usd=row.notional_usd,
            estimated_net_pnl_usd=None,
            current_basis_pct=row.basis_pct,
        )
        if not allowed:
            continue

        spot_entry_price = row.spot_entry_avg_price or 0.0
        perp_entry_price = row.perp_entry_avg_price or 0.0
        if spot_entry_price <= 0 or perp_entry_price <= 0:
            continue

        if existing_position is not None:
            position = _add_to_position(existing_position, row)
            event_type = "ADD_POSITION"
        else:
            position = ConvergencePaperPosition(
                position_id=position_id,
                base=row.base,
                direction=row.direction,
                spot_symbol=row.spot_symbol,
                perp_symbol=row.perp_symbol,
                notional_usd=row.notional_usd,
                spot_qty=row.notional_usd / spot_entry_price,
                perp_qty=row.notional_usd / perp_entry_price,
                spot_entry_price=spot_entry_price,
                perp_entry_price=perp_entry_price,
                entry_basis_pct=row.basis_pct or 0.0,
                current_basis_pct=row.basis_pct or 0.0,
                entry_zscore=row.basis_zscore or 0.0,
                current_zscore=row.basis_zscore or 0.0,
                entry_percentile=row.basis_percentile or 0.0,
                current_percentile=row.basis_percentile or 0.0,
                target_basis_pct=row.basis_target_pct or 0.0,
                entry_net_edge_pct=row.net_edge_pct or 0.0,
                funding_rate_pct_at_entry=row.funding_rate_pct or 0.0,
                realised_funding_pnl_usd=0.0,
                unrealised_basis_pnl_usd=0.0,
                estimated_close_cost_usd=0.0,
                estimated_net_pnl_ex_funding_usd=0.0,
                estimated_net_pnl_usd=0.0,
                created_at=utc_now(),
                updated_at=utc_now(),
                next_funding_time=row.funding_time_utc,
                funding_events_captured=0,
                funding_interval_hours=row.funding_interval_hours or config.fallback_funding_interval_hours,
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
                "spot_price": f"{spot_entry_price:.8f}",
                "perp_price": f"{perp_entry_price:.8f}",
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
    }

