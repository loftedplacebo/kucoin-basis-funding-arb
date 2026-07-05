from __future__ import annotations

import re
from typing import Optional


COMMON_QUOTES = ["USDT", "USDC", "USD", "BTC", "ETH"]


def normalise_symbol(symbol: str) -> str:
    """
    Convert exchange-specific symbols into a standard compact format.

    Examples:
        BTCUSDT   -> BTCUSDT
        BTC_USDT  -> BTCUSDT
        BTC-USDT  -> BTCUSDT
        BTC/USDT  -> BTCUSDT
        XBTUSDTM  -> BTCUSDT

    Important:
        Do not globally replace XBT with BTC, because symbols like AIXBTUSDT
        are valid Binance symbols and should remain unchanged.
    """
    if not symbol:
        raise ValueError("Symbol cannot be empty")

    s = symbol.upper().strip()
    s = s.replace("-", "").replace("_", "").replace("/", "")

    # KuCoin futures uses XBT for BTC, but only when XBT is the base asset.
    if s.startswith("XBT"):
        s = "BTC" + s[3:]

    for suffix in ["PERP", "SWAP"]:
        if s.endswith(suffix):
            s = s[: -len(suffix)]

    # KuCoin futures symbols normally end in M, e.g. XBTUSDTM.
    # Only strip this when it follows a quote asset.
    if s.endswith("USDTM"):
        s = s[:-1]
    elif s.endswith("USDCM"):
        s = s[:-1]

    return s


def split_standard_symbol(symbol: str) -> tuple[str, str]:
    """
    Split standard symbol into base and quote.

    Example:
        BTCUSDT -> BTC, USDT
    """
    s = normalise_symbol(symbol)

    for quote in COMMON_QUOTES:
        if s.endswith(quote):
            base = s[: -len(quote)]
            if base:
                return base, quote

    raise ValueError(f"Could not split symbol into base/quote: {symbol}")


def to_mexc_futures_symbol(standard_symbol: str) -> str:
    base, quote = split_standard_symbol(standard_symbol)
    return f"{base}_{quote}"


def to_mexc_spot_symbol(standard_symbol: str) -> str:
    return normalise_symbol(standard_symbol)


def to_bitget_symbol(standard_symbol: str) -> str:
    return normalise_symbol(standard_symbol)


def to_binance_symbol(standard_symbol: str) -> str:
    return normalise_symbol(standard_symbol)


def to_kucoin_spot_symbol(standard_symbol: str) -> str:
    base, quote = split_standard_symbol(standard_symbol)
    return f"{base}-{quote}"


def to_kucoin_futures_symbol(standard_symbol: str) -> str:
    base, quote = split_standard_symbol(standard_symbol)

    # KuCoin uses XBTUSDTM for BTC perpetual.
    if base == "BTC" and quote == "USDT":
        return "XBTUSDTM"

    return f"{base}{quote}M"


def to_hyperliquid_symbol(standard_symbol: str) -> str:
    base, quote = split_standard_symbol(standard_symbol)
    if quote not in {"USDT", "USDC", "USD"}:
        raise ValueError(f"Unsupported Hyperliquid quote asset: {quote}")
    return base


def standard_to_exchange_symbol(
    standard_symbol: str,
    exchange: str,
    market_type: str,
) -> str:
    exchange = exchange.lower()
    market_type = market_type.lower()

    if exchange == "binance":
        return to_binance_symbol(standard_symbol)

    if exchange == "bitget":
        return to_bitget_symbol(standard_symbol)

    if exchange == "mexc":
        if market_type == "futures":
            return to_mexc_futures_symbol(standard_symbol)
        return to_mexc_spot_symbol(standard_symbol)

    if exchange == "kucoin":
        if market_type == "futures":
            return to_kucoin_futures_symbol(standard_symbol)
        return to_kucoin_spot_symbol(standard_symbol)

    if exchange == "hyperliquid":
        if market_type != "futures":
            raise ValueError("Hyperliquid adapter only supports futures")
        return to_hyperliquid_symbol(standard_symbol)

    raise ValueError(f"Unsupported exchange: {exchange}")
