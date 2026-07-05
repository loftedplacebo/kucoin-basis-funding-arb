from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional


Exchange = Literal["binance", "kucoin", "mexc", "bitget", "hyperliquid"]
MarketType = Literal["spot", "futures"]
TradeSide = Literal["buy", "sell"]
OpportunityType = Literal["spot_futures", "futures_futures"]


@dataclass(frozen=True)
class MarketSymbol:
    standard_symbol: str          # e.g. BTCUSDT
    base_asset: str               # e.g. BTC
    quote_asset: str              # e.g. USDT
    exchange: Exchange
    market_type: MarketType
    exchange_symbol: str          # e.g. BTCUSDT, BTC_USDT, BTC-USDT, XBTUSDTM


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    quantity: float


@dataclass(frozen=True)
class OrderBook:
    exchange: Exchange
    market_type: MarketType
    standard_symbol: str
    exchange_symbol: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    observed_at_utc: datetime


@dataclass(frozen=True)
class ExecutionEstimate:
    exchange: Exchange
    market_type: MarketType
    standard_symbol: str
    side: TradeSide
    notional_usdt: float
    best_price: float
    average_price: float
    filled_quantity: float
    filled_notional: float
    slippage_pct: float
    is_fillable: bool


@dataclass(frozen=True)
class FundingInfo:
    exchange: Exchange
    standard_symbol: str
    exchange_symbol: str
    funding_rate: Optional[float]
    next_funding_time_utc: Optional[datetime]
    funding_interval_hours: Optional[int]
    observed_at_utc: datetime
    stability_score: Optional[float] = None


@dataclass(frozen=True)
class SpotFutureOpportunity:
    standard_symbol: str
    exchange: Exchange
    spot_exchange_symbol: str
    futures_exchange_symbol: str
    spot_ask: float
    futures_bid: float
    gross_basis_pct: float
    funding_rate: Optional[float]
    estimated_fees_pct: float
    estimated_slippage_pct: float
    net_edge_pct: float
    notional_usdt: float
    observed_at_utc: datetime


@dataclass(frozen=True)
class FuturesFuturesOpportunity:
    standard_symbol: str
    long_exchange: Exchange
    short_exchange: Exchange
    long_exchange_symbol: str
    short_exchange_symbol: str
    long_ask: float
    short_bid: float
    gross_spread_pct: float
    long_funding_rate: Optional[float]
    short_funding_rate: Optional[float]
    funding_diff_pct: Optional[float]
    estimated_fees_pct: float
    estimated_slippage_pct: float
    net_edge_pct: float
    notional_usdt: float
    observed_at_utc: datetime
