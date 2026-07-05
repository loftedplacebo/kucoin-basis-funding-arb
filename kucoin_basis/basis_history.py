from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from kucoin_basis.config import KucoinBasisConfig
from kucoin_basis.models import format_datetime, parse_float, utc_now


BASIS_HISTORY_FIELDS = [
    "timestamp_utc",
    "base",
    "spot_symbol",
    "perp_symbol",
    "spot_mid",
    "perp_mid",
    "basis_pct",
    "funding_rate_pct",
    "minutes_to_funding",
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


def basis_history_path(config: KucoinBasisConfig) -> Path:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    return config.data_dir / "basis_history.csv"


def append_basis_observation(
    *,
    config: KucoinBasisConfig,
    base: str,
    spot_symbol: str,
    perp_symbol: str,
    spot_mid: float | None,
    perp_mid: float | None,
    basis_pct: float | None,
    funding_rate_pct: float | None,
    minutes_to_funding: float | None,
) -> None:
    if spot_mid is None or perp_mid is None or basis_pct is None:
        return
    path = basis_history_path(config)
    file_exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=BASIS_HISTORY_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp_utc": format_datetime(utc_now()),
                "base": base,
                "spot_symbol": spot_symbol,
                "perp_symbol": perp_symbol,
                "spot_mid": f"{spot_mid:.12f}",
                "perp_mid": f"{perp_mid:.12f}",
                "basis_pct": f"{basis_pct:.8f}",
                "funding_rate_pct": "" if funding_rate_pct is None else f"{funding_rate_pct:.8f}",
                "minutes_to_funding": "" if minutes_to_funding is None else f"{minutes_to_funding:.8f}",
            }
        )


def load_recent_basis_values(
    *,
    config: KucoinBasisConfig,
    base: str,
    limit: int | None = None,
) -> list[float]:
    path = basis_history_path(config)
    if not path.exists():
        return []
    values = []
    with path.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("base") != base:
                continue
            value = parse_float(row.get("basis_pct"))
            if value is not None:
                values.append(value)
    lookback = limit or config.basis_history_lookback
    return values[-lookback:]


def calculate_basis_stats(
    *,
    config: KucoinBasisConfig,
    base: str,
    current_basis_pct: float | None,
) -> BasisStats:
    values = load_recent_basis_values(config=config, base=base, limit=config.basis_history_lookback)
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
