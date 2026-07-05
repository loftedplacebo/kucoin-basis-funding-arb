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
from kucoin_basis.symbols import discover_symbol_pairs, standard_symbol_for_base


OPPORTUNITY_FIELDS = [
    "timestamp_utc",
    "base",
    "direction",
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
    expected_edge_pct: float | None,
    round_trip_fillable: bool,
    basis_observation_count: int,
    basis_percentile: float | None,
) -> tuple[str, str]:
    if config.approved_bases and pair.base not in config.approved_bases:
        return "REJECT", "base_not_whitelisted"
    if funding_benefit_pct is None:
        return "REJECT", "funding_rate_missing"
    if funding_benefit_pct < config.min_funding_rate_pct:
        return "REJECT", "funding_below_threshold"
    if minutes_to_funding is None:
        return "REJECT", "funding_time_missing"
    if minutes_to_funding < config.min_minutes_before_funding:
        return "REJECT", "too_close_to_funding"
    if expected_edge_pct is None or expected_edge_pct < config.min_expected_edge_pct:
        return "REJECT", "expected_edge_below_threshold"
    if not round_trip_fillable:
        return "REJECT", "round_trip_not_fillable"
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
) -> list[OpportunityRow]:
    funding = fetch_funding_snapshot(client, pair, contracts_by_symbol)
    minutes = funding.minutes_to_funding(now)
    shortlisted_directions = _shortlist_directions(
        config=config,
        funding_rate_pct=funding.funding_rate_pct,
        minutes_to_funding=minutes,
    )
    if not shortlisted_directions:
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
    for notional in config.chunk_ladder_usd:
        if notional > config.max_chunk_notional_usd:
            continue
        for direction in shortlisted_directions:
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
            basis_target_pct, basis_convergence_upside_pct, scenario_edge_pct = _basis_convergence_scenario(
                config=config,
                direction=direction,
                basis_pct=basis_pct,
                expected_edge_pct=expected_edge_pct,
            )
            decision, reason = _decision_for_row(
                pair=pair,
                config=config,
                direction=direction,
                funding_benefit_pct=funding_benefit_pct,
                minutes_to_funding=minutes,
                expected_edge_pct=expected_edge_pct,
                round_trip_fillable=estimate.round_trip_fillable,
                basis_observation_count=basis_stats.observation_count,
                basis_percentile=basis_stats.percentile,
            )
            rows.append(
                OpportunityRow(
                    timestamp_utc=now,
                    base=pair.base,
                    direction=direction,
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
    contracts = _contracts_by_symbol(client)
    pairs = discover_symbol_pairs(client, config)
    rows: list[OpportunityRow] = []
    errors: list[str] = []

    for pair in pairs:
        try:
            rows.extend(scan_pair(client, config, pair, contracts, now))
        except Exception as exc:
            errors.append(f"{pair.base}: {exc}")

    output_path = opportunity_file(config, now)
    append_opportunities(output_path, rows)
    return output_path, rows, errors
