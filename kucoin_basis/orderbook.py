from __future__ import annotations

from dataclasses import dataclass

from core.models import ExecutionEstimate, OrderBook
from core.orderbook import estimate_execution_from_orderbook


@dataclass(frozen=True)
class RoundTripEstimate:
    notional_usd: float
    spot_entry: ExecutionEstimate
    perp_entry: ExecutionEstimate
    spot_exit: ExecutionEstimate
    perp_exit: ExecutionEstimate

    @property
    def round_trip_fillable(self) -> bool:
        return all(
            item.is_fillable
            for item in [self.spot_entry, self.perp_entry, self.spot_exit, self.perp_exit]
        )

    @property
    def total_slippage_pct(self) -> float:
        return (
            self.spot_entry.slippage_pct
            + self.perp_entry.slippage_pct
            + self.spot_exit.slippage_pct
            + self.perp_exit.slippage_pct
        )


def estimate_long_spot_short_perp_round_trip(
    *,
    spot_book: OrderBook,
    perp_book: OrderBook,
    notional_usd: float,
) -> RoundTripEstimate:
    return RoundTripEstimate(
        notional_usd=notional_usd,
        spot_entry=estimate_execution_from_orderbook(spot_book, "buy", notional_usd),
        perp_entry=estimate_execution_from_orderbook(perp_book, "sell", notional_usd),
        spot_exit=estimate_execution_from_orderbook(spot_book, "sell", notional_usd),
        perp_exit=estimate_execution_from_orderbook(perp_book, "buy", notional_usd),
    )


def estimate_short_spot_long_perp_round_trip(
    *,
    spot_book: OrderBook,
    perp_book: OrderBook,
    notional_usd: float,
) -> RoundTripEstimate:
    return RoundTripEstimate(
        notional_usd=notional_usd,
        spot_entry=estimate_execution_from_orderbook(spot_book, "sell", notional_usd),
        perp_entry=estimate_execution_from_orderbook(perp_book, "buy", notional_usd),
        spot_exit=estimate_execution_from_orderbook(spot_book, "buy", notional_usd),
        perp_exit=estimate_execution_from_orderbook(perp_book, "sell", notional_usd),
    )


def estimate_basis_round_trip(
    *,
    direction: str,
    spot_book: OrderBook,
    perp_book: OrderBook,
    notional_usd: float,
) -> RoundTripEstimate:
    if direction == "SHORT_SPOT_LONG_PERP":
        return estimate_short_spot_long_perp_round_trip(
            spot_book=spot_book,
            perp_book=perp_book,
            notional_usd=notional_usd,
        )
    return estimate_long_spot_short_perp_round_trip(
        spot_book=spot_book,
        perp_book=perp_book,
        notional_usd=notional_usd,
    )
