from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from kucoin_basis.basis_history import append_basis_observation, calculate_basis_stats
from kucoin_basis.config import DEFAULT_CONFIG, KucoinBasisConfig
from kucoin_basis.funding import fetch_funding_snapshot
from kucoin_basis.kucoin_public_client import KucoinPublicClient
from kucoin_basis.models import OpportunityRow, SymbolPair, utc_now
from kucoin_basis.orderbook import estimate_basis_round_trip
from kucoin_basis.paper_store import PaperStore
from kucoin_basis.symbols import discover_symbol_pairs, standard_symbol_for_base


OPPORTUNITY_FIELDS = [
    "timestamp_utc",
    "base",
    "direction",
    "spot_hedge_route",
    "spot_symbol",
    "perp_symbol",
    "funding_rate_pct",
    "predicted_funding_rate_pct",
    "funding_time_utc",
    "minutes_to_funding",
    "spot_bid",
    "spot_ask",
    "perp_bid",
    "perp_ask",
    "basis_pct",
    "notional_usd",
    "spot_entry_slippage_pct",
    "perp_entry_slippage_pct",
    "spot_exit_slippage_pct",
    "perp_exit_slippage_pct",
    "expected_edge_pct",
    "round_trip_fillable",
    "decision",
    "reason",
    "spot_entry_avg_price",
    "perp_entry_avg_price",
    "spot_exit_avg_price",
    "perp_exit_avg_price",
    "funding_interval",
    "funding_rate_cap",
    "funding_rate_floor",
    "basis_observation_count",
    "basis_mean_pct",
    "basis_median_pct",
    "basis_std_pct",
    "basis_zscore",
    "basis_percentile",
    "basis_trend_pct",
    "basis_target_pct",
    "basis_convergence_upside_pct",
    "scenario_edge_pct",
]


_FUNDING_CYCLE_OBSERVATIONS: dict[str, tuple[datetime, int]] = {}


def _funding_cycle_confirmed(
    perp_symbol: str,
    funding_time_utc: datetime | None,
    required_observations: int,
) -> bool:
    if funding_time_utc is None:
        return False
    previous = _FUNDING_CYCLE_OBSERVATIONS.get(perp_symbol)
    count = previous[1] + 1 if previous and previous[0] == funding_time_utc else 1
    _FUNDING_CYCLE_OBSERVATIONS[perp_symbol] = (funding_time_utc, count)
    return count >= max(1, required_observations)


def opportunity_file(config: KucoinBasisConfig, now: datetime | None = None) -> Path:
    now = now or utc_now()
    config.opportunities_dir.mkdir(parents=True, exist_ok=True)
    return config.opportunities_dir / f"kucoin_basis_opportunities_{now:%Y%m%d}.csv"


def append_opportunities(path: Path, rows: list[OpportunityRow]) -> None:
    file_exists = path.exists()
    if file_exists:
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_fieldnames = reader.fieldnames or []
        if existing_fieldnames != OPPORTUNITY_FIELDS:
            archive_dir = path.parent.parent / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / f"{path.stem}_schema_mismatch_{utc_now():%H%M%S}{path.suffix}"
            path.replace(archive_path)
            file_exists = False
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OPPORTUNITY_FIELDS)
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow({field: row.to_csv_row().get(field, "") for field in OPPORTUNITY_FIELDS})


def _contracts_by_symbol(client: KucoinPublicClient) -> dict[str, dict]:
    return {
        str(contract.get("symbol")): contract
        for contract in client.get_active_contracts()
        if contract.get("symbol")
    }


def _decision_for_row(
    *,
    pair: SymbolPair,
    config: KucoinBasisConfig,
    direction: str,
    funding_benefit_pct: float | None,
    minutes_to_funding: float | None,
    funding_interval_hours: float | None,
    funding_cycle_confirmed: bool,
    expected_edge_pct: float | None,
    round_trip_fillable: bool,
    basis_observation_count: int,
    basis_percentile: float | None,
    exit_cost_pct: float | None,
    spot_hedge_route: str = "CROSS_MARGIN",
    spot_entry_slippage_pct: float | None = None,
    perp_entry_slippage_pct: float | None = None,
    spot_exit_slippage_pct: float | None = None,
    perp_exit_slippage_pct: float | None = None,
    notional_usd: float | None = None,
    basis_trend_pct: float | None = None,
) -> tuple[str, str]:
    if config.approved_bases and pair.base not in config.approved_bases:
        return "REJECT", "base_not_whitelisted"
    if direction == "SHORT_SPOT_LONG_PERP":
        if spot_hedge_route == "NONE":
            return "UNHEDGEABLE", "spot_borrow_unavailable"
        if spot_hedge_route == "UNKNOWN":
            return "REJECT", "spot_borrow_status_unavailable"
    if funding_benefit_pct is None:
        return "REJECT", "funding_rate_missing"
    if funding_benefit_pct < config.min_funding_rate_pct:
        return "REJECT", "funding_below_threshold"
    if minutes_to_funding is None:
        return "REJECT", "funding_time_missing"
    if minutes_to_funding < config.min_minutes_before_funding:
        return "REJECT", "too_close_to_funding"
    interval_minutes = (funding_interval_hours or config.fallback_funding_interval_hours) * 60
    minutes_since_previous_funding = interval_minutes - minutes_to_funding
    if minutes_since_previous_funding < 0:
        return "REJECT", "funding_cycle_time_mismatch"
    if minutes_since_previous_funding < config.post_funding_entry_quarantine_minutes:
        return "REJECT", "post_funding_rollover_quarantine"
    # Entry timing is evaluated per chunk. Existing positions can still appear
    # on the watchlist, while new capital is constrained to the tested windows.
    if notional_usd is not None:
        if minutes_to_funding > config.max_entry_window_minutes:
            return "REJECT", "entry_too_early"
        if minutes_to_funding > config.preferred_entry_window_minutes:
            if direction == "SHORT_SPOT_LONG_PERP":
                return "REJECT", "short_spot_entry_window_expired"
            if not any(
                abs(notional_usd - chunk) < 1e-9
                for chunk in config.reduced_late_entry_chunk_ladder_usd
            ):
                return "REJECT", "entry_size_restricted_by_timing"
            if config.late_entry_requires_favourable_basis_trend and (
                basis_trend_pct is None or basis_trend_pct >= 0.0
            ):
                return "REJECT", "basis_trend_not_favourable_for_late_entry"
    if not funding_cycle_confirmed:
        return "REJECT", "funding_cycle_unconfirmed"
    if expected_edge_pct is None or expected_edge_pct < config.min_expected_edge_pct:
        return "REJECT", "expected_edge_below_threshold"
    if not round_trip_fillable:
        return "REJECT", "round_trip_not_fillable"
    entry_slippage = (spot_entry_slippage_pct or 0.0) + (perp_entry_slippage_pct or 0.0)
    if (
        (spot_entry_slippage_pct or 0.0) > config.max_entry_leg_slippage_pct
        or (perp_entry_slippage_pct or 0.0) > config.max_entry_leg_slippage_pct
        or entry_slippage > config.max_combined_entry_slippage_pct
    ):
        return "REJECT", "entry_slippage_too_high"
    exit_slippage = (spot_exit_slippage_pct or 0.0) + (perp_exit_slippage_pct or 0.0)
    if (
        (spot_exit_slippage_pct or 0.0) > config.max_exit_leg_slippage_pct
        or (perp_exit_slippage_pct or 0.0) > config.max_exit_leg_slippage_pct
        or exit_slippage > config.max_combined_exit_slippage_pct
    ):
        return "REJECT", "exit_slippage_too_high"
    if exit_cost_pct is not None and exit_cost_pct > config.max_entry_exit_cost_pct:
        return "REJECT", "exit_cost_too_high"
    if basis_observation_count >= config.min_basis_observations_for_stats:
        if pair.base and basis_percentile is None:
            return "REJECT", "basis_stats_missing"
        if (
            direction == "SHORT_SPOT_LONG_PERP"
            and basis_percentile is not None
            and basis_percentile > config.short_spot_entry_max_basis_percentile
        ):
            return "REJECT", "basis_not_low_enough_for_short_spot"
        if (
            direction == "LONG_SPOT_SHORT_PERP"
            and basis_percentile is not None
            and basis_percentile < config.long_spot_entry_min_basis_percentile
        ):
            return "REJECT", "basis_not_high_enough_for_long_spot"
    return "ENTER_CANDIDATE", "entry_rules_passed"


def _fixed_cost_pct(config: KucoinBasisConfig) -> float:
    return (
        config.estimated_spot_taker_fee_pct
        + config.estimated_perp_taker_fee_pct
        + config.estimated_exit_fee_pct
        + config.safety_buffer_pct
    )


def _funding_benefit_pct(direction: str, funding_rate_pct: float | None) -> float | None:
    if funding_rate_pct is None:
        return None
    if direction == "LONG_SPOT_SHORT_PERP":
        return funding_rate_pct
    return -funding_rate_pct


def _basis_convergence_scenario(
    *,
    config: KucoinBasisConfig,
    direction: str,
    basis_pct: float | None,
    expected_edge_pct: float | None,
) -> tuple[float | None, float | None, float | None]:
    if basis_pct is None:
        return None, None, expected_edge_pct

    target_abs = config.basis_near_flat_exit_abs_pct
    if direction == "SHORT_SPOT_LONG_PERP":
        target = -target_abs
        raw_upside = max(0.0, target - basis_pct)
    else:
        target = target_abs
        raw_upside = max(0.0, basis_pct - target)

    upside = raw_upside * config.basis_convergence_haircut
    scenario_edge = None if expected_edge_pct is None else expected_edge_pct + upside
    return target, upside, scenario_edge


def _open_position_watchlist(config: KucoinBasisConfig) -> dict[str, dict[str, set[float]]]:
    watchlist: dict[str, dict[str, set[float]]] = {}
    for position in PaperStore(config).load_open_positions().values():
        by_direction = watchlist.setdefault(position.base, {})
        notionals = by_direction.setdefault(position.direction, set())
        notionals.add(position.notional_usd)
        notionals.update(config.gentle_unwind_chunk_ladder_usd)
    return watchlist


def _spot_hedge_routes(client: KucoinPublicClient) -> dict[str, str]:
    cross_bases = {
        str(item.get("baseCurrency", "")).upper()
        for item in client.get_cross_margin_symbols()
        if item.get("quoteCurrency") == "USDT"
        and str(item.get("enableTrading", "true")).lower() != "false"
    }
    isolated_bases = {
        str(item.get("baseCurrency", "")).upper()
        for item in client.get_isolated_margin_symbols()
        if item.get("quoteCurrency") == "USDT"
        and item.get("tradeEnable") is True
        and item.get("baseBorrowEnable") is True
    }
    routes = {}
    for base in cross_bases | isolated_bases:
        if base in cross_bases and base in isolated_bases:
            routes[base] = "CROSS_OR_ISOLATED"
        elif base in cross_bases:
            routes[base] = "CROSS_MARGIN"
        else:
            routes[base] = "ISOLATED_MARGIN"
    return routes


def _spot_hedge_route(
    direction: str,
    base: str,
    routes: dict[str, str] | None,
) -> str:
    if direction == "LONG_SPOT_SHORT_PERP":
        return "CASH_SPOT"
    if routes is None:
        return "CROSS_MARGIN"
    return routes.get(base, "NONE")


def _shortlist_directions(
    *,
    config: KucoinBasisConfig,
    funding_rate_pct: float | None,
    minutes_to_funding: float | None,
) -> list[str]:
    directions = []
    for direction in ("LONG_SPOT_SHORT_PERP", "SHORT_SPOT_LONG_PERP"):
        benefit = _funding_benefit_pct(direction, funding_rate_pct)
        if benefit is None or benefit < config.min_funding_rate_pct:
            continue
        if minutes_to_funding is None or minutes_to_funding < config.min_minutes_before_funding:
            continue
        if benefit - _fixed_cost_pct(config) < config.min_expected_edge_pct:
            continue
        directions.append(direction)
    return directions


def scan_pair(
    client: KucoinPublicClient,
    config: KucoinBasisConfig,
    pair: SymbolPair,
    contracts_by_symbol: dict[str, dict],
    now: datetime,
    watchlist: dict[str, dict[str, set[float]]] | None = None,
    spot_hedge_routes: dict[str, str] | None = None,
) -> list[OpportunityRow]:
    # Use the bulk contract feed only to identify symbols worth inspecting. Any
    # symbol that can enter or is already open is then verified atomically.
    funding = fetch_funding_snapshot(
        client,
        pair,
        contracts_by_symbol,
        atomic=False,
    )
    minutes = funding.minutes_to_funding(now)
    shortlisted_directions = _shortlist_directions(
        config=config,
        funding_rate_pct=funding.funding_rate_pct,
        minutes_to_funding=minutes,
    )
    watched_by_direction = (watchlist or {}).get(pair.base, {})
    directions = sorted(set(shortlisted_directions) | set(watched_by_direction))
    if not directions:
        return []

    funding = fetch_funding_snapshot(client, pair, contracts_by_symbol, atomic=True)
    minutes = funding.minutes_to_funding(now)
    funding_cycle_confirmed = _funding_cycle_confirmed(
        pair.perp_symbol,
        funding.funding_time_utc,
        config.funding_cycle_confirmation_observations,
    )
    shortlisted_directions = _shortlist_directions(
        config=config,
        funding_rate_pct=funding.funding_rate_pct,
        minutes_to_funding=minutes,
    )
    directions = sorted(set(shortlisted_directions) | set(watched_by_direction))
    if not directions:
        return []

    standard_symbol = standard_symbol_for_base(pair.base)
    spot_book = client.get_spot_orderbook(standard_symbol, pair.spot_symbol, limit=100)
    perp_book = client.get_futures_orderbook(standard_symbol, pair.perp_symbol, limit=100)

    spot_bid = spot_book.bids[0].price if spot_book.bids else None
    spot_ask = spot_book.asks[0].price if spot_book.asks else None
    perp_bid = perp_book.bids[0].price if perp_book.bids else None
    perp_ask = perp_book.asks[0].price if perp_book.asks else None
    basis_pct = None
    spot_mid = None
    perp_mid = None
    if spot_bid and spot_ask and perp_bid and perp_ask:
        spot_mid = (spot_bid + spot_ask) / 2
        perp_mid = (perp_bid + perp_ask) / 2
        if spot_mid > 0:
            basis_pct = ((perp_mid / spot_mid) - 1) * 100

    append_basis_observation(
        config=config,
        base=pair.base,
        spot_symbol=pair.spot_symbol,
        perp_symbol=pair.perp_symbol,
        spot_mid=spot_mid,
        perp_mid=perp_mid,
        basis_pct=basis_pct,
        funding_rate_pct=funding.funding_rate_pct,
        minutes_to_funding=minutes,
    )
    basis_stats = calculate_basis_stats(
        config=config,
        base=pair.base,
        current_basis_pct=basis_pct,
    )

    rows = []
    notionals = set(config.chunk_ladder_usd)
    notionals.update(config.reduced_late_entry_chunk_ladder_usd)
    for direction_notionals in watched_by_direction.values():
        notionals.update(direction_notionals)
    for notional in sorted(notionals):
        if notional <= 0:
            continue
        for direction in directions:
            spot_hedge_route = _spot_hedge_route(
                direction, pair.base, spot_hedge_routes
            )
            is_entry_chunk = (
                notional in (
                    set(config.chunk_ladder_usd)
                    | set(config.reduced_late_entry_chunk_ladder_usd)
                )
                and notional <= config.max_chunk_notional_usd
            )
            is_entry_direction = direction in shortlisted_directions
            is_watch_row = direction in watched_by_direction and notional in watched_by_direction[direction]
            if not is_entry_chunk and not is_watch_row:
                continue
            estimate = estimate_basis_round_trip(
                direction=direction,
                spot_book=spot_book,
                perp_book=perp_book,
                notional_usd=notional,
            )
            expected_edge_pct = None
            funding_benefit_pct = _funding_benefit_pct(direction, funding.funding_rate_pct)
            if funding_benefit_pct is not None:
                expected_edge_pct = (
                    funding_benefit_pct
                    - estimate.spot_entry.slippage_pct
                    - estimate.perp_entry.slippage_pct
                    - estimate.spot_exit.slippage_pct
                    - estimate.perp_exit.slippage_pct
                    - config.estimated_spot_taker_fee_pct
                    - config.estimated_perp_taker_fee_pct
                    - config.estimated_exit_fee_pct
                    - config.safety_buffer_pct
                )
            exit_cost_pct = (
                estimate.spot_exit.slippage_pct
                + estimate.perp_exit.slippage_pct
                + config.estimated_exit_fee_pct
            )
            basis_target_pct, basis_convergence_upside_pct, scenario_edge_pct = _basis_convergence_scenario(
                config=config,
                direction=direction,
                basis_pct=basis_pct,
                expected_edge_pct=expected_edge_pct,
            )
            if is_entry_chunk and is_entry_direction:
                decision, reason = _decision_for_row(
                    pair=pair,
                    config=config,
                    direction=direction,
                    funding_benefit_pct=funding_benefit_pct,
                    minutes_to_funding=minutes,
                    funding_interval_hours=funding.funding_interval_hours,
                    funding_cycle_confirmed=funding_cycle_confirmed,
                    expected_edge_pct=expected_edge_pct,
                    round_trip_fillable=estimate.round_trip_fillable,
                    basis_observation_count=basis_stats.observation_count,
                    basis_percentile=basis_stats.percentile,
                    exit_cost_pct=exit_cost_pct,
                    spot_hedge_route=spot_hedge_route,
                    spot_entry_slippage_pct=estimate.spot_entry.slippage_pct,
                    perp_entry_slippage_pct=estimate.perp_entry.slippage_pct,
                    spot_exit_slippage_pct=estimate.spot_exit.slippage_pct,
                    perp_exit_slippage_pct=estimate.perp_exit.slippage_pct,
                    notional_usd=notional,
                    basis_trend_pct=basis_stats.trend_pct,
                )
            else:
                decision, reason = "REJECT", "open_position_watchlist"
            rows.append(
                OpportunityRow(
                    timestamp_utc=now,
                    base=pair.base,
                    direction=direction,
                    spot_hedge_route=spot_hedge_route,
                    spot_symbol=pair.spot_symbol,
                    perp_symbol=pair.perp_symbol,
                    funding_rate_pct=funding.funding_rate_pct,
                    predicted_funding_rate_pct=funding.predicted_funding_rate_pct,
                    funding_time_utc=funding.funding_time_utc,
                    minutes_to_funding=minutes,
                    spot_bid=spot_bid,
                    spot_ask=spot_ask,
                    perp_bid=perp_bid,
                    perp_ask=perp_ask,
                    basis_pct=basis_pct,
                    notional_usd=notional,
                    spot_entry_slippage_pct=estimate.spot_entry.slippage_pct,
                    perp_entry_slippage_pct=estimate.perp_entry.slippage_pct,
                    spot_exit_slippage_pct=estimate.spot_exit.slippage_pct,
                    perp_exit_slippage_pct=estimate.perp_exit.slippage_pct,
                    expected_edge_pct=expected_edge_pct,
                    round_trip_fillable=estimate.round_trip_fillable,
                    decision=decision,
                    reason=reason,
                    spot_entry_avg_price=estimate.spot_entry.average_price,
                    perp_entry_avg_price=estimate.perp_entry.average_price,
                    spot_exit_avg_price=estimate.spot_exit.average_price,
                    perp_exit_avg_price=estimate.perp_exit.average_price,
                    funding_interval=funding.funding_interval_hours,
                    funding_rate_cap=funding.funding_rate_cap,
                    funding_rate_floor=funding.funding_rate_floor,
                    basis_observation_count=basis_stats.observation_count,
                    basis_mean_pct=basis_stats.mean_pct,
                    basis_median_pct=basis_stats.median_pct,
                    basis_std_pct=basis_stats.std_pct,
                    basis_zscore=basis_stats.zscore,
                    basis_percentile=basis_stats.percentile,
                    basis_trend_pct=basis_stats.trend_pct,
                    basis_target_pct=basis_target_pct,
                    basis_convergence_upside_pct=basis_convergence_upside_pct,
                    scenario_edge_pct=scenario_edge_pct,
                )
            )
    return rows


def scan_once(
    config: KucoinBasisConfig = DEFAULT_CONFIG,
    client: KucoinPublicClient | None = None,
) -> tuple[Path, list[OpportunityRow], list[str]]:
    client = client or KucoinPublicClient()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.opportunities_dir.mkdir(parents=True, exist_ok=True)
    config.paper_dir.mkdir(parents=True, exist_ok=True)
    config.archive_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    errors: list[str] = []
    contracts = _contracts_by_symbol(client)
    pairs = discover_symbol_pairs(client, config)
    watchlist = _open_position_watchlist(config)
    try:
        spot_hedge_routes = _spot_hedge_routes(client)
    except Exception as exc:
        spot_hedge_routes = {
            pair.base: "UNKNOWN"
            for pair in pairs
        }
        errors.append(f"margin support: {exc}")
    rows: list[OpportunityRow] = []

    for pair in pairs:
        try:
            rows.extend(
                scan_pair(
                    client,
                    config,
                    pair,
                    contracts,
                    now,
                    watchlist,
                    spot_hedge_routes,
                )
            )
        except Exception as exc:
            errors.append(f"{pair.base}: {exc}")

    output_path = opportunity_file(config, now)
    append_opportunities(output_path, rows)
    return output_path, rows, errors
