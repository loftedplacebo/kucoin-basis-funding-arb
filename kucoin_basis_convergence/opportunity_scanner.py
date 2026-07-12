from __future__ import annotations

import csv
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from kucoin_basis.funding import fetch_funding_snapshot
from kucoin_basis.kucoin_public_client import KucoinPublicClient
from kucoin_basis.orderbook import estimate_basis_round_trip
from kucoin_basis.symbols import discover_symbol_pairs, standard_symbol_for_base
from kucoin_basis_convergence.basis_history import append_observation, calculate_basis_stats
from kucoin_basis_convergence.config import DEFAULT_CONFIG, KucoinBasisConvergenceConfig
from kucoin_basis_convergence.models import ConvergenceOpportunityRow, utc_now


OPPORTUNITY_FIELDS = [
    "timestamp_utc",
    "base",
    "direction",
    "spot_symbol",
    "perp_symbol",
    "funding_rate_pct",
    "predicted_funding_rate_pct",
    "funding_time_utc",
    "funding_interval_hours",
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
    "entry_cost_pct",
    "exit_cost_pct",
    "round_trip_cost_pct",
    "round_trip_fillable",
    "basis_observation_count",
    "basis_mean_pct",
    "basis_median_pct",
    "basis_std_pct",
    "basis_zscore",
    "basis_percentile",
    "basis_trend_pct",
    "basis_target_pct",
    "gross_convergence_pct",
    "expected_convergence_pct",
    "net_edge_pct",
    "decision",
    "reason",
    "spot_entry_avg_price",
    "perp_entry_avg_price",
    "spot_exit_avg_price",
    "perp_exit_avg_price",
]


def opportunity_file(config: KucoinBasisConvergenceConfig, now: datetime | None = None) -> Path:
    now = now or utc_now()
    config.opportunities_dir.mkdir(parents=True, exist_ok=True)
    return config.opportunities_dir / f"kucoin_basis_convergence_opportunities_{now:%Y%m%d}.csv"


def append_opportunities(path: Path, rows: list[ConvergenceOpportunityRow]) -> None:
    file_exists = path.exists()
    if file_exists:
        with path.open("r", newline="", encoding="utf-8") as f:
            existing_fieldnames = csv.DictReader(f).fieldnames or []
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


def _fixed_entry_cost_pct(config: KucoinBasisConvergenceConfig) -> float:
    return config.estimated_spot_taker_fee_pct + config.estimated_perp_taker_fee_pct


def _target_and_convergence(
    *,
    direction: str,
    basis_pct: float | None,
    basis_median_pct: float | None,
    config: KucoinBasisConvergenceConfig,
) -> tuple[float | None, float | None, float | None]:
    if basis_pct is None or basis_median_pct is None:
        return basis_median_pct, None, None
    target = basis_median_pct
    if direction == "SHORT_SPOT_LONG_PERP":
        gross = max(0.0, target - basis_pct)
    else:
        gross = max(0.0, basis_pct - target)
    expected = gross * config.convergence_haircut
    return target, gross, expected


def _entry_decision(
    *,
    config: KucoinBasisConvergenceConfig,
    direction: str,
    basis_pct: float | None,
    basis_observation_count: int,
    basis_std_pct: float | None,
    basis_zscore: float | None,
    basis_percentile: float | None,
    basis_trend_pct: float | None,
    expected_convergence_pct: float | None,
    net_edge_pct: float | None,
    round_trip_cost_pct: float | None,
    exit_cost_pct: float | None,
    round_trip_fillable: bool,
) -> tuple[str, str]:
    if basis_pct is None:
        return "REJECT", "basis_missing"
    if abs(basis_pct) < config.min_abs_basis_pct:
        return "REJECT", "basis_too_small"
    if basis_observation_count < config.min_basis_observations_for_stats:
        return "REJECT", "basis_stats_warming_up"
    if basis_std_pct is None or basis_zscore is None or basis_percentile is None:
        return "REJECT", "basis_stats_missing"
    if basis_std_pct > config.max_basis_std_pct:
        return "REJECT", "basis_std_too_high"
    if basis_trend_pct is not None and abs(basis_trend_pct) > config.max_basis_trend_abs_pct:
        return "REJECT", "basis_trend_too_fast"
    if not round_trip_fillable:
        return "REJECT", "round_trip_not_fillable"
    if exit_cost_pct is None or exit_cost_pct > config.max_exit_cost_pct:
        return "REJECT", "exit_cost_too_high"
    if round_trip_cost_pct is None or round_trip_cost_pct > config.max_round_trip_cost_pct:
        return "REJECT", "round_trip_cost_too_high"
    if expected_convergence_pct is None or expected_convergence_pct < config.min_expected_convergence_pct:
        return "REJECT", "convergence_too_small"
    if net_edge_pct is None or net_edge_pct < config.min_net_edge_pct:
        return "REJECT", "net_edge_below_threshold"
    if direction == "SHORT_SPOT_LONG_PERP":
        if basis_zscore > -config.entry_zscore_abs:
            return "REJECT", "basis_zscore_not_cheap_enough"
        if basis_percentile > config.cheap_entry_max_percentile:
            return "REJECT", "basis_percentile_not_cheap_enough"
    else:
        if basis_zscore < config.entry_zscore_abs:
            return "REJECT", "basis_zscore_not_rich_enough"
        if basis_percentile < config.rich_entry_min_percentile:
            return "REJECT", "basis_percentile_not_rich_enough"
    return "ENTER_CANDIDATE", "entry_rules_passed"


def scan_pair(
    client: KucoinPublicClient,
    config: KucoinBasisConvergenceConfig,
    pair,
    contracts_by_symbol: dict[str, dict],
    now: datetime,
) -> list[ConvergenceOpportunityRow]:
    funding = fetch_funding_snapshot(client, pair, contracts_by_symbol)
    standard_symbol = standard_symbol_for_base(pair.base)
    spot_book = client.get_spot_orderbook(standard_symbol, pair.spot_symbol, limit=100)
    perp_book = client.get_futures_orderbook(standard_symbol, pair.perp_symbol, limit=100)

    spot_bid = spot_book.bids[0].price if spot_book.bids else None
    spot_ask = spot_book.asks[0].price if spot_book.asks else None
    perp_bid = perp_book.bids[0].price if perp_book.bids else None
    perp_ask = perp_book.asks[0].price if perp_book.asks else None
    spot_mid = (spot_bid + spot_ask) / 2 if spot_bid and spot_ask else None
    perp_mid = (perp_bid + perp_ask) / 2 if perp_bid and perp_ask else None
    basis_pct = ((perp_mid / spot_mid) - 1) * 100 if spot_mid and perp_mid and spot_mid > 0 else None

    append_observation(
        config=config,
        base=pair.base,
        spot_symbol=pair.spot_symbol,
        perp_symbol=pair.perp_symbol,
        spot_mid=spot_mid,
        perp_mid=perp_mid,
        basis_pct=basis_pct,
        funding_rate_pct=funding.funding_rate_pct,
        predicted_funding_rate_pct=funding.predicted_funding_rate_pct,
        funding_time_utc=funding.funding_time_utc,
        funding_interval_hours=funding.funding_interval_hours,
        now=now,
    )
    basis_stats = calculate_basis_stats(config=config, base=pair.base, current_basis_pct=basis_pct)

    rows: list[ConvergenceOpportunityRow] = []
    for notional in config.chunk_ladder_usd:
        if notional > config.max_chunk_notional_usd:
            continue
        for direction in ("LONG_SPOT_SHORT_PERP", "SHORT_SPOT_LONG_PERP"):
            estimate = estimate_basis_round_trip(
                direction=direction,
                spot_book=spot_book,
                perp_book=perp_book,
                notional_usd=notional,
            )
            entry_cost_pct = (
                estimate.spot_entry.slippage_pct
                + estimate.perp_entry.slippage_pct
                + _fixed_entry_cost_pct(config)
            )
            exit_cost_pct = (
                estimate.spot_exit.slippage_pct
                + estimate.perp_exit.slippage_pct
                + config.estimated_exit_fee_pct
            )
            round_trip_cost_pct = entry_cost_pct + exit_cost_pct + config.safety_buffer_pct
            basis_target_pct, gross_convergence_pct, expected_convergence_pct = _target_and_convergence(
                direction=direction,
                basis_pct=basis_pct,
                basis_median_pct=basis_stats.median_pct,
                config=config,
            )
            net_edge_pct = (
                None
                if expected_convergence_pct is None
                else expected_convergence_pct - round_trip_cost_pct
            )
            decision, reason = _entry_decision(
                config=config,
                direction=direction,
                basis_pct=basis_pct,
                basis_observation_count=basis_stats.observation_count,
                basis_std_pct=basis_stats.std_pct,
                basis_zscore=basis_stats.zscore,
                basis_percentile=basis_stats.percentile,
                basis_trend_pct=basis_stats.trend_pct,
                expected_convergence_pct=expected_convergence_pct,
                net_edge_pct=net_edge_pct,
                round_trip_cost_pct=round_trip_cost_pct,
                exit_cost_pct=exit_cost_pct,
                round_trip_fillable=estimate.round_trip_fillable,
            )
            rows.append(
                ConvergenceOpportunityRow(
                    timestamp_utc=now,
                    base=pair.base,
                    direction=direction,
                    spot_symbol=pair.spot_symbol,
                    perp_symbol=pair.perp_symbol,
                    funding_rate_pct=funding.funding_rate_pct,
                    predicted_funding_rate_pct=funding.predicted_funding_rate_pct,
                    funding_time_utc=funding.funding_time_utc,
                    funding_interval_hours=funding.funding_interval_hours,
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
                    entry_cost_pct=entry_cost_pct,
                    exit_cost_pct=exit_cost_pct,
                    round_trip_cost_pct=round_trip_cost_pct,
                    round_trip_fillable=estimate.round_trip_fillable,
                    basis_observation_count=basis_stats.observation_count,
                    basis_mean_pct=basis_stats.mean_pct,
                    basis_median_pct=basis_stats.median_pct,
                    basis_std_pct=basis_stats.std_pct,
                    basis_zscore=basis_stats.zscore,
                    basis_percentile=basis_stats.percentile,
                    basis_trend_pct=basis_stats.trend_pct,
                    basis_target_pct=basis_target_pct,
                    gross_convergence_pct=gross_convergence_pct,
                    expected_convergence_pct=expected_convergence_pct,
                    net_edge_pct=net_edge_pct,
                    decision=decision,
                    reason=reason,
                    spot_entry_avg_price=estimate.spot_entry.average_price,
                    perp_entry_avg_price=estimate.perp_entry.average_price,
                    spot_exit_avg_price=estimate.spot_exit.average_price,
                    perp_exit_avg_price=estimate.perp_exit.average_price,
                )
            )
    return rows


def scan_once(
    config: KucoinBasisConvergenceConfig = DEFAULT_CONFIG,
    client: KucoinPublicClient | None = None,
) -> tuple[Path, list[ConvergenceOpportunityRow], list[str]]:
    provided_client = client is not None
    client = client or KucoinPublicClient()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.observations_dir.mkdir(parents=True, exist_ok=True)
    config.opportunities_dir.mkdir(parents=True, exist_ok=True)
    config.paper_dir.mkdir(parents=True, exist_ok=True)
    config.archive_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    contracts = _contracts_by_symbol(client)
    pairs = discover_symbol_pairs(client, config)
    rows: list[ConvergenceOpportunityRow] = []
    errors: list[str] = []

    if provided_client:
        for pair in pairs:
            try:
                rows.extend(scan_pair(client, config, pair, contracts, now))
            except Exception as exc:
                errors.append(f"{pair.base}: {exc}")
    else:
        thread_local = threading.local()

        def worker(pair):
            if not hasattr(thread_local, "client"):
                thread_local.client = KucoinPublicClient()
            return scan_pair(thread_local.client, config, pair, contracts, now)

        with ThreadPoolExecutor(max_workers=max(1, config.scan_max_workers)) as executor:
            future_by_pair = {executor.submit(worker, pair): pair for pair in pairs}
            for future in as_completed(future_by_pair):
                pair = future_by_pair[future]
                try:
                    rows.extend(future.result())
                except Exception as exc:
                    errors.append(f"{pair.base}: {exc}")

    output_path = opportunity_file(config, now)
    append_opportunities(output_path, rows)
    return output_path, rows, errors
