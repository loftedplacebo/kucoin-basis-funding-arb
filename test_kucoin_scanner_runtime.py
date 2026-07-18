from pathlib import Path

import requests

from kucoin_basis.kucoin_public_client import KucoinPublicClient
from kucoin_basis.opportunity_scanner import _spot_hedge_routes
from kucoin_basis.run_scanner import config_for_state_mode


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return self.responses.pop(0)


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_public_client_retries_429_using_retry_after():
    clock = FakeClock()
    session = FakeSession(
        [
            FakeResponse(429, {}, {"Retry-After": "2"}),
            FakeResponse(200, {"code": "200000", "data": [{"symbol": "BTC-USDT"}]}),
        ]
    )
    client = KucoinPublicClient(
        session=session,
        min_request_interval_seconds=0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert client.get_spot_symbols() == [{"symbol": "BTC-USDT"}]
    assert len(session.calls) == 2
    assert clock.sleeps == [2.0]


def test_public_client_caches_catalog_calls():
    clock = FakeClock()
    session = FakeSession(
        [
            FakeResponse(200, {"code": "200000", "data": [{"symbol": "BTC-USDT"}]}),
            FakeResponse(200, {"code": "200000", "data": [{"symbol": "XBTUSDTM"}]}),
        ]
    )
    client = KucoinPublicClient(
        session=session,
        min_request_interval_seconds=0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert client.get_spot_symbols() == client.get_spot_symbols()
    assert client.get_active_contracts() == client.get_active_contracts()
    assert len(session.calls) == 2


def test_public_client_caches_margin_catalogs():
    clock = FakeClock()
    session = FakeSession(
        [
            FakeResponse(200, {"code": "200000", "data": [{"symbol": "BTC-USDT"}]}),
            FakeResponse(200, {"code": "200000", "data": [{"symbol": "ETH-USDT"}]}),
        ]
    )
    client = KucoinPublicClient(
        session=session,
        min_request_interval_seconds=0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert client.get_cross_margin_symbols() == client.get_cross_margin_symbols()
    assert client.get_isolated_margin_symbols() == client.get_isolated_margin_symbols()
    assert len(session.calls) == 2


def test_margin_route_prefers_cross_then_isolated():
    class MarginCatalogClient:
        def get_cross_margin_symbols(self):
            return [
                {
                    "baseCurrency": "BTC",
                    "quoteCurrency": "USDT",
                    "enableTrading": True,
                }
            ]

        def get_isolated_margin_symbols(self):
            return [
                {
                    "baseCurrency": "BTC",
                    "quoteCurrency": "USDT",
                    "tradeEnable": True,
                    "baseBorrowEnable": True,
                },
                {
                    "baseCurrency": "MIRA",
                    "quoteCurrency": "USDT",
                    "tradeEnable": True,
                    "baseBorrowEnable": True,
                },
                {
                    "baseCurrency": "KCS",
                    "quoteCurrency": "USDT",
                    "tradeEnable": True,
                    "baseBorrowEnable": False,
                },
            ]

    assert _spot_hedge_routes(MarginCatalogClient()) == {
        "BTC": "CROSS_OR_ISOLATED",
        "MIRA": "ISOLATED_MARGIN",
    }


def test_rate_limit_exhaustion_does_not_double_load_funding_endpoints():
    clock = FakeClock()
    session = FakeSession([FakeResponse(429, {}) for _ in range(4)])
    client = KucoinPublicClient(
        session=session,
        min_request_interval_seconds=0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    try:
        client.get_current_funding_rate("HOMEUSDTM")
    except requests.HTTPError as exc:
        assert exc.response.status_code == 429
    else:
        raise AssertionError("exhausted rate limit should be reported")

    assert len(session.calls) == 4
    assert all("/api/ua/v1/market/funding-rate" in call[0] for call in session.calls)


def test_dry_run_scanner_uses_dry_run_position_ledger():
    paper = config_for_state_mode("paper")
    dry_run = config_for_state_mode("dry-run")

    assert paper.paper_dir.name == "paper"
    assert dry_run.paper_dir == Path(dry_run.data_dir) / "dry_run"
    assert dry_run.opportunities_dir == paper.opportunities_dir


if __name__ == "__main__":
    test_public_client_retries_429_using_retry_after()
    test_public_client_caches_catalog_calls()
    test_public_client_caches_margin_catalogs()
    test_margin_route_prefers_cross_then_isolated()
    test_rate_limit_exhaustion_does_not_double_load_funding_endpoints()
    test_dry_run_scanner_uses_dry_run_position_ledger()
    print("kucoin scanner runtime tests passed")
