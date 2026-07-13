from __future__ import annotations

import csv
import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from kucoin_basis_convergence.config import KucoinBasisConvergenceConfig
from kucoin_basis_convergence.models import format_datetime, parse_float, utc_now


_HISTORY_LOCK = threading.Lock()

OBSERVATION_FIELDS = [
    "timestamp_utc",
    "base",
    "spot_symbol",
    "perp_symbol",
    "spot_bid",
    "spot_ask",
    "spot_spread_pct",
    "perp_bid",
    "perp_ask",
    "perp_spread_pct",
    "spot_mid",
    "perp_mid",
    "basis_pct",
    "funding_rate_pct",
    "predicted_funding_rate_pct",
    "funding_time_utc",
    "funding_interval_hours",
]


@dataclass(frozen=True)
class BasisStats:
    observation_count: int
    mean_pct: float | None
    median_pct: float | None
    std_pct: float | None
    zscore: float | None
    percentile: float | None
    min_pct: float | None
    max_pct: float | None
    trend_pct: float | None


def observation_file(config: KucoinBasisConvergenceConfig, now=None) -> Path:
    now = now or utc_now()
    config.observations_dir.mkdir(parents=True, exist_ok=True)
    return config.observations_dir / f"kucoin_basis_convergence_observations_{now:%Y%m%d}.csv"


def append_observation(
    *,
    config: KucoinBasisConvergenceConfig,
    base: str,
    spot_symbol: str,
    perp_symbol: str,
    spot_mid: float | None,
    perp_mid: float | None,
    basis_pct: float | None,
    funding_rate_pct: float | None,
    predicted_funding_rate_pct: float | None,
    funding_time_utc,
    funding_interval_hours: float | None,
    spot_bid: float | None = None,
    spot_ask: float | None = None,
    spot_spread_pct: float | None = None,
    perp_bid: float | None = None,
    perp_ask: float | None = None,
    perp_spread_pct: float | None = None,
    now=None,
) -> None:
    if spot_mid is None or perp_mid is None or basis_pct is None:
        return
    path = observation_file(config, now)
    with _HISTORY_LOCK:
        file_exists = path.exists()
        if file_exists:
            with path.open("r", newline="", encoding="utf-8") as f:
                existing_fieldnames = csv.DictReader(f).fieldnames or []
            if existing_fieldnames != OBSERVATION_FIELDS:
                config.archive_dir.mkdir(parents=True, exist_ok=True)
                archive_path = config.archive_dir / f"{path.stem}_schema_mismatch_{utc_now():%H%M%S}{path.suffix}"
                path.replace(archive_path)
                file_exists = False
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=OBSERVATION_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(
                {
                    "timestamp_utc": format_datetime(now or utc_now()),
                    "base": base,
                    "spot_symbol": spot_symbol,
                    "perp_symbol": perp_symbol,
                    "spot_bid": "" if spot_bid is None else f"{spot_bid:.12f}",
                    "spot_ask": "" if spot_ask is None else f"{spot_ask:.12f}",
                    "spot_spread_pct": "" if spot_spread_pct is None else f"{spot_spread_pct:.8f}",
                    "perp_bid": "" if perp_bid is None else f"{perp_bid:.12f}",
                    "perp_ask": "" if perp_ask is None else f"{perp_ask:.12f}",
                    "perp_spread_pct": "" if perp_spread_pct is None else f"{perp_spread_pct:.8f}",
                    "spot_mid": f"{spot_mid:.12f}",
                    "perp_mid": f"{perp_mid:.12f}",
                    "basis_pct": f"{basis_pct:.8f}",
                    "funding_rate_pct": "" if funding_rate_pct is None else f"{funding_rate_pct:.8f}",
                    "predicted_funding_rate_pct": ""
                    if predicted_funding_rate_pct is None
                    else f"{predicted_funding_rate_pct:.8f}",
                    "funding_time_utc": format_datetime(funding_time_utc),
                    "funding_interval_hours": ""
                    if funding_interval_hours is None
                    else f"{funding_interval_hours:.8f}",
                }
            )


def load_recent_basis_values(
    *,
    config: KucoinBasisConvergenceConfig,
    base: str,
    limit: int | None = None,
) -> list[float]:
    values: list[float] = []
    files = sorted(config.observations_dir.glob("kucoin_basis_convergence_observations_*.csv"))
    with _HISTORY_LOCK:
        for path in files[-3:]:
            with path.open("r", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("base") != base:
                        continue
                    value = parse_float(row.get("basis_pct"))
                    if value is not None:
                        values.append(value)
    lookback = limit or config.basis_history_lookback
    return values[-lookback:]


def load_basis_history_points_by_base(
    *,
    config: KucoinBasisConvergenceConfig,
    limit: int | None = None,
) -> dict[str, list[tuple[datetime, float]]]:
    lookback = limit or config.basis_history_lookback
    values_by_base: dict[str, deque[tuple[datetime, float]]] = {}
    files = sorted(config.observations_dir.glob("kucoin_basis_convergence_observations_*.csv"))
    with _HISTORY_LOCK:
        for path in files[-3:]:
            with path.open("r", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    base = row.get("base")
                    timestamp = row.get("timestamp_utc")
                    value = parse_float(row.get("basis_pct"))
                    if not base or not timestamp or value is None:
                        continue
                    try:
                        parsed_timestamp = datetime.fromisoformat(str(timestamp))
                    except ValueError:
                        continue
                    if parsed_timestamp.tzinfo is None:
                        parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
                    values = values_by_base.setdefault(base, deque(maxlen=lookback))
                    values.append((parsed_timestamp, value))
    return {base: list(values) for base, values in values_by_base.items()}


def load_basis_history_by_base(
    *,
    config: KucoinBasisConvergenceConfig,
    limit: int | None = None,
) -> dict[str, list[float]]:
    lookback = limit or config.basis_history_lookback
    values_by_base: dict[str, deque[float]] = {}
    files = sorted(config.observations_dir.glob("kucoin_basis_convergence_observations_*.csv"))
    with _HISTORY_LOCK:
        for path in files[-3:]:
            with path.open("r", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    base = row.get("base")
                    value = parse_float(row.get("basis_pct"))
                    if not base or value is None:
                        continue
                    values = values_by_base.setdefault(base, deque(maxlen=lookback))
                    values.append(value)
    return {base: list(values) for base, values in values_by_base.items()}


def calculate_basis_stats_from_values(
    values: list[float],
    current_basis_pct: float | None,
) -> BasisStats:
    count = len(values)
    if count == 0:
        return BasisStats(count, None, None, None, None, None, None, None, None)

    mean_pct = sum(values) / count
    median_pct = median(values)
    min_pct = min(values)
    max_pct = max(values)
    variance = sum((value - mean_pct) ** 2 for value in values) / count
    std_pct = math.sqrt(variance)
    zscore = None
    if current_basis_pct is not None and std_pct > 1e-12:
        zscore = (current_basis_pct - mean_pct) / std_pct

    percentile = None
    if current_basis_pct is not None:
        below_or_equal = sum(1 for value in values if value <= current_basis_pct)
        percentile = below_or_equal / count * 100

    trend_pct = None
    if count >= 2:
        trend_pct = values[-1] - values[0]

    return BasisStats(
        observation_count=count,
        mean_pct=mean_pct,
        median_pct=median_pct,
        std_pct=std_pct,
        zscore=zscore,
        percentile=percentile,
        min_pct=min_pct,
        max_pct=max_pct,
        trend_pct=trend_pct,
    )


def calculate_basis_stats(
    *,
    config: KucoinBasisConvergenceConfig,
    base: str,
    current_basis_pct: float | None,
) -> BasisStats:
    values = load_recent_basis_values(config=config, base=base, limit=config.basis_history_lookback)
    return calculate_basis_stats_from_values(values, current_basis_pct)
