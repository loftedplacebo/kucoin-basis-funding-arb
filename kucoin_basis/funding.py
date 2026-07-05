from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from kucoin_basis.kucoin_public_client import KucoinPublicClient
from kucoin_basis.models import FundingSnapshot, SymbolPair, utc_now


def _as_float(data: dict, *keys: str) -> Optional[float]:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _as_datetime_from_ms(data: dict, *keys: str) -> Optional[datetime]:
    for key in keys:
        value = data.get(key)
        if value in (None, ""):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 10_000_000_000:
            numeric = numeric / 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    return None


def _decimal_rate_to_pct(value: Optional[float]) -> Optional[float]:
    return None if value is None else value * 100


def fetch_funding_snapshot(
    client: KucoinPublicClient,
    pair: SymbolPair,
    contracts_by_symbol: dict[str, dict],
) -> FundingSnapshot:
    contract = contracts_by_symbol.get(pair.perp_symbol, {})
    if contract.get("fundingFeeRate") not in (None, ""):
        data = {
            "fundingRate": contract.get("fundingFeeRate"),
            "predictedFundingRate": contract.get("predictedFundingFeeRate"),
            "fundingTime": contract.get("nextFundingRateDateTime"),
            "granularity": (
                contract.get("currentFundingRateGranularity")
                or contract.get("fundingRateGranularity")
            ),
            "fundingRateCap": contract.get("fundingRateCap"),
            "fundingRateFloor": contract.get("fundingRateFloor"),
        }
    else:
        data = client.get_current_funding_rate(pair.perp_symbol)

    funding_rate = _as_float(
        data,
        "nextFundingRate",
        "fundingRate",
        "fundingFeeRate",
        "value",
    )
    predicted = _as_float(
        data,
        "predictedFundingRate",
        "predictedValue",
        "nextFundingRate",
    )
    funding_time = _as_datetime_from_ms(
        data,
        "fundingTime",
        "nextFundingTime",
        "timePoint",
        "timepoint",
    )

    interval_hours = _as_float(data, "fundingIntervalHours", "fundingInterval")
    if interval_hours is None:
        interval_ms = _as_float(data, "granularity")
        interval_hours = None if interval_ms is None else interval_ms / 1000 / 60 / 60
    if interval_hours is None:
        interval_hours = _as_float(contract, "fundingInterval") or 8.0

    return FundingSnapshot(
        base=pair.base,
        perp_symbol=pair.perp_symbol,
        funding_rate_pct=_decimal_rate_to_pct(funding_rate),
        predicted_funding_rate_pct=_decimal_rate_to_pct(predicted),
        funding_time_utc=funding_time,
        funding_interval_hours=interval_hours,
        funding_rate_cap=_decimal_rate_to_pct(_as_float(data, "fundingRateCap", "maxFundingRate")),
        funding_rate_floor=_decimal_rate_to_pct(_as_float(data, "fundingRateFloor", "minFundingRate")),
        observed_at_utc=utc_now(),
    )
