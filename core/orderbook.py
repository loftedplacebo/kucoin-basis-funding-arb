from __future__ import annotations

from core.models import ExecutionEstimate, OrderBook, OrderBookLevel, TradeSide


def estimate_execution_from_orderbook(
    orderbook: OrderBook,
    side: TradeSide,
    notional_usdt: float,
) -> ExecutionEstimate:
    """
    Estimate execution price and slippage for a marketable order.

    side='buy'  consumes asks.
    side='sell' consumes bids.
    """
    if notional_usdt <= 0:
        raise ValueError("notional_usdt must be positive")

    levels = orderbook.asks if side == "buy" else orderbook.bids

    if not levels:
        raise ValueError("Order book has no levels")

    best_price = levels[0].price
    remaining_notional = notional_usdt
    filled_quantity = 0.0
    filled_notional = 0.0

    for level in levels:
        level_notional = level.price * level.quantity
        take_notional = min(remaining_notional, level_notional)
        take_quantity = take_notional / level.price

        filled_quantity += take_quantity
        filled_notional += take_notional
        remaining_notional -= take_notional

        if remaining_notional <= 1e-9:
            break

    is_fillable = remaining_notional <= 1e-9

    if filled_quantity <= 0:
        average_price = 0.0
        slippage_pct = 0.0
    else:
        average_price = filled_notional / filled_quantity

        if side == "buy":
            slippage_pct = ((average_price / best_price) - 1) * 100
        else:
            slippage_pct = ((best_price / average_price) - 1) * 100

    return ExecutionEstimate(
        exchange=orderbook.exchange,
        market_type=orderbook.market_type,
        standard_symbol=orderbook.standard_symbol,
        side=side,
        notional_usdt=notional_usdt,
        best_price=best_price,
        average_price=average_price,
        filled_quantity=filled_quantity,
        filled_notional=filled_notional,
        slippage_pct=slippage_pct,
        is_fillable=is_fillable,
    )


def parse_orderbook_levels(raw_levels: list, max_levels: int = 50) -> list[OrderBookLevel]:
    """
    Parse exchange order book levels into standard objects.

    Accepts levels like:
        [["100.1", "2.5"], ...]
        [{"price": "100.1", "size": "2.5"}, ...]
    """
    parsed: list[OrderBookLevel] = []

    for level in raw_levels[:max_levels]:
        if isinstance(level, dict):
            price = float(level.get("price") or level.get("p") or level.get("px"))
            quantity = float(
                level.get("quantity")
                or level.get("qty")
                or level.get("size")
                or level.get("sz")
            )
        else:
            price = float(level[0])
            quantity = float(level[1])

        parsed.append(OrderBookLevel(price=price, quantity=quantity))

    return parsed