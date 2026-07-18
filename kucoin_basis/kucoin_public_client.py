from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from core.models import OrderBook, OrderBookLevel
from core.orderbook import parse_orderbook_levels


SPOT_BASE_URL = "https://api.kucoin.com"
FUTURES_BASE_URL = "https://api-futures.kucoin.com"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class KucoinPublicClient:
    """Unauthenticated KuCoin REST client for research and paper trading."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        min_request_interval_seconds: float = 0.10,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        spot_symbols_cache_seconds: float = 300.0,
        active_contracts_cache_seconds: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.session = session or requests.Session()
        self.min_request_interval_seconds = max(0.0, min_request_interval_seconds)
        self.max_retries = max(0, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.spot_symbols_cache_seconds = max(0.0, spot_symbols_cache_seconds)
        self.active_contracts_cache_seconds = max(0.0, active_contracts_cache_seconds)
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None
        self._spot_symbols: tuple[float, list[dict]] | None = None
        self._active_contracts: tuple[float, list[dict]] | None = None
        self._cross_margin_symbols: tuple[float, list[dict]] | None = None
        self._isolated_margin_symbols: tuple[float, list[dict]] | None = None
        self._spot_symbol_cache: dict[str, dict] = {}
        self._contract_cache: dict[str, dict] = {}

    def _pace_request(self) -> None:
        if self._last_request_at is not None:
            elapsed = self._monotonic() - self._last_request_at
            delay = self.min_request_interval_seconds - elapsed
            if delay > 0:
                self._sleep(delay)
        self._last_request_at = self._monotonic()

    def _retry_delay(self, response: requests.Response | None, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            try:
                return min(30.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(30.0, self.retry_backoff_seconds * (2**attempt))

    def _get(self, base_url: str, path: str, params: Optional[dict] = None):
        response: requests.Response | None = None
        for attempt in range(self.max_retries + 1):
            self._pace_request()
            try:
                response = self.session.get(
                    f"{base_url}{path}", params=params, timeout=20
                )
            except (requests.ConnectionError, requests.Timeout):
                if attempt >= self.max_retries:
                    raise
                self._sleep(self._retry_delay(None, attempt))
                continue

            if (
                response.status_code in RETRYABLE_STATUS_CODES
                and attempt < self.max_retries
            ):
                self._sleep(self._retry_delay(response, attempt))
                continue

            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != "200000":
                raise RuntimeError(f"KuCoin public API error for {path}: {payload}")
            return payload.get("data")
        raise RuntimeError(f"KuCoin public API retry loop exhausted for {path}")

    def _cached(
        self, item: tuple[float, list[dict]] | None, ttl: float
    ) -> list[dict] | None:
        if item is None or self._monotonic() - item[0] >= ttl:
            return None
        return item[1]

    def get_spot_symbols(self) -> list[dict]:
        cached = self._cached(self._spot_symbols, self.spot_symbols_cache_seconds)
        if cached is not None:
            return cached
        data = self._get(SPOT_BASE_URL, "/api/v1/symbols")
        symbols = data if isinstance(data, list) else []
        self._spot_symbols = (self._monotonic(), symbols)
        return symbols

    def get_active_contracts(self) -> list[dict]:
        cached = self._cached(self._active_contracts, self.active_contracts_cache_seconds)
        if cached is not None:
            return cached
        data = self._get(FUTURES_BASE_URL, "/api/v1/contracts/active")
        contracts = data if isinstance(data, list) else []
        self._active_contracts = (self._monotonic(), contracts)
        for contract in contracts:
            symbol = str(contract.get("symbol", ""))
            if symbol:
                self._contract_cache[symbol] = contract
        return contracts

    def get_cross_margin_symbols(self) -> list[dict]:
        cached = self._cached(
            self._cross_margin_symbols, self.spot_symbols_cache_seconds
        )
        if cached is not None:
            return cached
        data = self._get(SPOT_BASE_URL, "/api/v3/margin/symbols")
        symbols = data if isinstance(data, list) else []
        self._cross_margin_symbols = (self._monotonic(), symbols)
        return symbols

    def get_isolated_margin_symbols(self) -> list[dict]:
        cached = self._cached(
            self._isolated_margin_symbols, self.spot_symbols_cache_seconds
        )
        if cached is not None:
            return cached
        data = self._get(SPOT_BASE_URL, "/api/v1/isolated/symbols")
        symbols = data if isinstance(data, list) else []
        self._isolated_margin_symbols = (self._monotonic(), symbols)
        return symbols

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
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code in RETRYABLE_STATUS_CODES:
                raise
        except (requests.ConnectionError, requests.Timeout):
            raise
        except Exception:
            pass
        data = self._get(
            FUTURES_BASE_URL,
            f"/api/v1/funding-rate/{exchange_symbol}/current",
        )
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
