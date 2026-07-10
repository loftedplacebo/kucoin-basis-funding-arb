from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from kucoin_basis.config import DEFAULT_CONFIG, KucoinBasisConfig
from kucoin_basis.models import OpportunityRow, format_datetime, parse_datetime, utc_now
from kucoin_basis.paper_models import PaperPosition
from kucoin_basis.paper_store import PaperStore


@dataclass(frozen=True)
class ExitEstimate:
    basis_pnl_usd: float
    close_cost_usd: float
    net_pnl_usd: float
    net_pnl_pct: float


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
    net_pnl = funding_pnl + basis_pnl - close_cost
    return ExitEstimate(
        basis_pnl_usd=basis_pnl,
        close_cost_usd=close_cost,
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


def _raw_funding_rate_pct(position: PaperPosition, row: OpportunityRow | None) -> float:
    if row is not None and row.funding_rate_pct is not None:
        return row.funding_rate_pct
    return position.funding_rate_pct_at_entry


def _accrue_funding_if_crossed(
    position: PaperPosition,
    row: OpportunityRow | None,
    store: PaperStore,
    config: KucoinBasisConfig,
) -> None:
    now = utc_now()
    funding_time = position.next_funding_time
    if funding_time is None or funding_time > now:
        return
    if row is not None and row.funding_interval is not None:
        position.funding_interval_hours = row.funding_interval
    raw_funding_rate_pct = _raw_funding_rate_pct(position, row)
    while funding_time is not None and funding_time <= now:
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
) -> None:
    key = _cooldown_key(base, direction)
    if key in active_cooldowns:
        return
    now = utc_now()
    expires_at = now + timedelta(minutes=config.volatility_cooldown_minutes)
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
    funding_benefit_pct = None
    if row.funding_rate_pct is not None:
        funding_benefit_pct = (
            row.funding_rate_pct
            if position.direction == "LONG_SPOT_SHORT_PERP"
            else -row.funding_rate_pct
        )
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
    if (
        funding_benefit_pct is not None
        and funding_benefit_pct >= config.min_hold_funding_rate_pct
    ):
        return True, "funding_captured_try_profitable_unwind"

    basis_converged = basis_improvement >= config.basis_take_profit_improvement_pct
    basis_near_flat = abs(position.current_basis_pct) <= config.basis_near_flat_exit_abs_pct
    if basis_converged and position.estimated_net_pnl_usd >= 0:
        return True, "basis_converged_take_profit"
    if basis_near_flat and position.estimated_net_pnl_usd >= 0:
        return True, "basis_near_flat_take_profit"

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
        if require_profitable and estimate.net_pnl_usd <= 0:
            continue
        candidates.append((chunk, row, estimate))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda candidate: (
            candidate[2].net_pnl_pct,
            candidate[2].net_pnl_usd,
            -candidate[0],
        ),
    )


def _reduce_position(position: PaperPosition, chunk_notional_usd: float) -> None:
    if chunk_notional_usd >= position.notional_usd - 1e-8:
        position.notional_usd = 0.0
        position.spot_qty = 0.0
        position.perp_qty = 0.0
        position.status = "CLOSED"
        position.updated_at = utc_now()
        return

    fraction = chunk_notional_usd / position.notional_usd
    position.notional_usd -= chunk_notional_usd
    position.spot_qty *= 1 - fraction
    position.perp_qty *= 1 - fraction
    position.updated_at = utc_now()


def _add_to_position(position: PaperPosition, row: OpportunityRow) -> PaperPosition:
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
    position.updated_at = utc_now()
    return position


def run_paper_strategy_once(
    config: KucoinBasisConfig = DEFAULT_CONFIG,
    opportunity_path: Path | None = None,
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
        should_exit, reason = _should_exit(position, row, config)
        if reason == "hold_basis_moved_adversely":
            _ensure_cooldown(
                store,
                active_cooldowns,
                base=position.base,
                direction=position.direction,
                reason=reason,
                config=config,
            )
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
            }
        )
        if should_exit:
            exit_chunk = None
            exit_row = row
            force_gentle_unwind = reason == "funding_captured_try_profitable_unwind"
            full_exit = (
                not force_gentle_unwind
                and row.notional_usd + 1e-8 >= position.notional_usd
                and row.spot_exit_avg_price is not None
                and row.perp_exit_avg_price is not None
                and (
                    not config.gentle_unwind_enabled
                    or position.estimated_net_pnl_usd >= position.notional_usd * config.min_profit_to_full_exit_pct / 100
                )
            )
            if full_exit:
                exit_chunk = position.notional_usd
            elif config.gentle_unwind_enabled:
                partial = _choose_partial_close(
                    opportunities,
                    base=position.base,
                    direction=position.direction,
                    position=position,
                    position_notional_usd=position.notional_usd,
                    config=config,
                    require_profitable=True,
                )
                if partial is not None:
                    exit_chunk, exit_row, exit_estimate = partial
                    realised_pnl = exit_estimate.net_pnl_usd
                    close_cost = exit_estimate.close_cost_usd

            if exit_chunk is None:
                store.append_decision(
                    {
                        "timestamp_utc": format_datetime(utc_now()),
                        "decision_type": "EXIT",
                        "base": position.base,
                        "direction": position.direction,
                        "position_id": position.position_id,
                        "opportunity_key": row.opportunity_key,
                        "allowed": "False",
                        "reason": "exit_wanted_no_profitable_chunk",
                        "notional_usd": f"{position.notional_usd:.8f}",
                        "expected_edge_pct": "" if row.expected_edge_pct is None else f"{row.expected_edge_pct:.8f}",
                        "estimated_net_pnl_usd": f"{position.estimated_net_pnl_usd:.8f}",
                        "row_timestamp_utc": format_datetime(row.timestamp_utc),
                        "row_age_seconds": f"{_row_age_seconds(row, utc_now()):.3f}",
                        "entry_basis_pct": f"{position.entry_basis_pct:.8f}",
                        "current_basis_pct": f"{position.current_basis_pct:.8f}",
                        "basis_improvement_pct": f"{_basis_improvement_pct(position):.8f}",
                    }
                )
                continue

            fraction = 1.0 if position.notional_usd <= 0 else min(1.0, exit_chunk / position.notional_usd)
            if full_exit:
                realised_pnl = position.estimated_net_pnl_usd * fraction
                close_cost = position.estimated_close_cost_usd * fraction
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
                    "fees_usd": f"{close_cost:.8f}",
                    "realised_pnl_usd": f"{realised_pnl:.8f}",
                    "reason": reason,
                }
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
            reason = "volatility_cooldown"
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
        elif existing_position is None and len(positions) >= config.max_open_positions:
            allowed = False
            reason = "max_open_positions"
        elif total_open + row.notional_usd > config.max_total_notional_usd:
            allowed = False
            reason = "max_total_exposure"
        elif new_symbol_notional > config.max_symbol_notional_usd:
            allowed = False
            reason = "max_symbol_exposure"

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

        spot_entry_price = row.spot_entry_avg_price or row.spot_ask or 0.0
        perp_entry_price = row.perp_entry_avg_price or row.perp_bid or 0.0
        if spot_entry_price <= 0 or perp_entry_price <= 0:
            continue

        if existing_position is not None:
            position = _add_to_position(existing_position, row)
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
                spot_qty=row.notional_usd / spot_entry_price,
                perp_qty=row.notional_usd / perp_entry_price,
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
