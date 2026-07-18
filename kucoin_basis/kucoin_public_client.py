from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import requests

from core.models import OrderBook, OrderBookLevel
from core.orderbook import parse_orderbook_levels


SPOT_BASE_URL = "https://api.kucoin.com"
FUTURES_BASE_URL = "https://api-futures.kucoin.com"


class KucoinPublicClient:
    """Unauthenticated KuCoin REST client for research and paper trading."""

    def __init__(self):
        self.session = requests.Session()
        self._spot_symbol_cache: dict[str, dict] = {}
        self._contract_cache: dict[str, dict] = {}

    def _get(self, base_url: str, path: str, params: Optional[dict] = None):
        response = self.session.get(f"{base_url}{path}", params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "200000":
            raise RuntimeError(f"KuCoin public API error for {path}: {payload}")
        return payload.get("data")

    def get_spot_symbols(self) -> list[dict]:
        data = self._get(SPOT_BASE_URL, "/api/v1/symbols")
        return data if isinstance(data, list) else []

    def get_active_contracts(self) -> list[dict]:
        data = self._get(FUTURES_BASE_URL, "/api/v1/contracts/active")
        contracts = data if isinstance(data, list) else []
        for contract in contracts:
            symbol = str(contract.get("symbol", ""))
            if symbol:
                self._contract_cache[symbol] = contract
        return contracts

    def get_spot_symbol(self, exchange_symbol: str) -> dict:
        cached = self._spot_symbol_cache.get(exchange_symbol)
        if cached is not None:
            return cached
        data = self._get(SPOT_BASE_URL, f"/api/v2/symbols/{exchange_symbol}")
        symbol = data if isinstance(data, dict) else {}
        self._spot_symbol_cache[exchange_symbol] = symbol
        return symbol

    def get_contract(self, exchange_symbol: str) -> dict:
        cached = self._contract_cache.get(exchange_symbol)
        if cached is not None:
            return cached
        data = self._get(FUTURES_BASE_URL, f"/api/v1/contracts/{exchange_symbol}")
        contract = data if isinstance(data, dict) else {}
        self._contract_cache[exchange_symbol] = contract
        return contract

    def get_spot_orderbook(self, standard_symbol: str, exchange_symbol: str, limit: int = 100) -> OrderBook:
        path = "/api/v1/market/orderbook/level2_100"
        if limit <= 20:
            path = "/api/v1/market/orderbook/level2_20"
        data = self._get(SPOT_BASE_URL, path, params={"symbol": exchange_symbol})
        return OrderBook(
            exchange="kucoin",
            market_type="spot",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            bids=parse_orderbook_levels(data.get("bids", []), max_levels=limit),
            asks=parse_orderbook_levels(data.get("asks", []), max_levels=limit),
            observed_at_utc=datetime.now(timezone.utc),
        )

    def get_futures_orderbook(self, standard_symbol: str, exchange_symbol: str, limit: int = 100) -> OrderBook:
        data = self._get(
            FUTURES_BASE_URL,
            "/api/v1/level2/snapshot",
            params={"symbol": exchange_symbol},
        )
        multiplier = float(self.get_contract(exchange_symbol).get("multiplier") or 1.0)
        bids = parse_orderbook_levels(data.get("bids", []), max_levels=limit)
        asks = parse_orderbook_levels(data.get("asks", []), max_levels=limit)
        return OrderBook(
            exchange="kucoin",
            market_type="futures",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            bids=[
                OrderBookLevel(price=level.price, quantity=level.quantity * multiplier)
                for level in bids
            ],
            asks=[
                OrderBookLevel(price=level.price, quantity=level.quantity * multiplier)
                for level in asks
            ],
            observed_at_utc=datetime.now(timezone.utc),
        )

    def get_current_funding_rate(self, exchange_symbol: str) -> dict:
        try:
            data = self._get(
                SPOT_BASE_URL,
                "/api/ua/v1/market/funding-rate",
                params={"symbol": exchange_symbol},
            )
            return data if isinstance(data, dict) else {}
        except Exception:
            data = self._get(FUTURES_BASE_URL, f"/api/v1/funding-rate/{exchange_symbol}/current")
            return data if isinstance(data, dict) else {}

    def get_public_funding_history(
        self,
        exchange_symbol: str,
        from_ms: int,
        to_ms: int,
    ) -> list[dict]:
        data = self._get(
            FUTURES_BASE_URL,
            "/api/v1/contract/funding-rates",
            params={"symbol": exchange_symbol, "from": from_ms, "to": to_ms},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("dataList", "items"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return rows
        return []
