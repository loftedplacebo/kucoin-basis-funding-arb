from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from core.models import OrderBook, OrderBookLevel
from kucoin_basis.execution import ExecutionResult, KucoinDryRunExecutor
from kucoin_basis.kucoin_private_client import (
    KucoinCredentials,
    KucoinPrivateClient,
    KucoinSafetyError,
)
from kucoin_basis.kucoin_public_client import KucoinPublicClient
from kucoin_basis.paper_store import PaperStore
from kucoin_basis.paper_strategy import run_paper_strategy_once
from test_kucoin_basis_strategy import (
    make_config,
    make_row,
    replace_row,
    write_opportunities,
)


class FakePrivateClient:
    def __init__(self, borrow_enabled: bool = True):
        self.credentials = SimpleNamespace(execution_mode="validate")
        self.borrow_enabled = borrow_enabled
        self.spot_orders = []
        self.margin_orders = []
        self.futures_orders = []

    def get_cross_margin_account(self):
        return {
            "accounts": [
                {"currency": "MIRA", "borrowEnabled": self.borrow_enabled}
            ]
        }

    def test_spot_order(self, payload):
        self.spot_orders.append(payload)
        return {"orderId": "redacted"}

    def test_margin_order(self, payload):
        self.margin_orders.append(payload)
        return {"orderId": "redacted"}

    def test_futures_order(self, payload):
        self.futures_orders.append(payload)
        return {"orderId": "redacted"}


class FakePublicClient:
    def get_spot_orderbook(self, standard_symbol, exchange_symbol, limit=100):
        return OrderBook(
            exchange="kucoin",
            market_type="spot",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            bids=[OrderBookLevel(99.9, 100.0)],
            asks=[OrderBookLevel(100.0, 100.0)],
            observed_at_utc=datetime.now(timezone.utc),
        )

    def get_futures_orderbook(self, standard_symbol, exchange_symbol, limit=100):
        return OrderBook(
            exchange="kucoin",
            market_type="futures",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            bids=[OrderBookLevel(100.1, 100.0)],
            asks=[OrderBookLevel(100.2, 100.0)],
            observed_at_utc=datetime.now(timezone.utc),
        )

    def get_spot_symbol(self, exchange_symbol):
        return {
            "baseIncrement": "0.001",
            "baseMinSize": "0.001",
            "priceIncrement": "0.01",
            "minFunds": "0.1",
        }

    def get_contract(self, exchange_symbol):
        return {"multiplier": "0.1", "lotSize": "1", "tickSize": "0.01"}


class StubExecutionAdapter:
    mode = "dry_run"

    def __init__(self, accepted: bool):
        self.accepted = accepted

    def execute(self, action, row, notional_usd, target_base_quantity=None):
        executable_notional = 90.0 if self.accepted else notional_usd
        return ExecutionResult(
            timestamp_utc=datetime.now(timezone.utc),
            mode=self.mode,
            action=action,
            base=row.base,
            direction=row.direction,
            requested_notional_usd=notional_usd,
            executable_notional_usd=executable_notional,
            accepted=self.accepted,
            reason="test_orders_accepted" if self.accepted else "forced_test_rejection",
            spot_size=0.9 if self.accepted else 0.0,
            spot_average_price=100.0 if self.accepted else 0.0,
            perp_base_quantity=0.9 if self.accepted else 0.0,
            perp_average_price=100.0 if self.accepted else 0.0,
        )


def test_short_spot_entry_validates_auto_borrow_and_long_perp():
    private = FakePrivateClient()
    executor = KucoinDryRunExecutor(private, FakePublicClient())
    row = make_row(100.0, 0.01, 0.01)

    result = executor.execute("ENTRY", row, 100.0)

    assert result.accepted
    assert result.spot_venue == "margin"
    assert result.perp_contracts == 9
    assert result.spot_size == result.perp_base_quantity
    assert result.hedge_mismatch_bps == 0
    assert private.margin_orders[0]["side"] == "sell"
    assert private.margin_orders[0]["autoBorrow"] is True
    assert private.margin_orders[0]["autoRepay"] is False
    assert private.futures_orders[0]["side"] == "buy"
    assert private.futures_orders[0]["reduceOnly"] is False
    assert private.margin_orders[0]["timeInForce"] == "IOC"


def test_short_spot_exit_validates_auto_repay_and_reduce_only_perp():
    private = FakePrivateClient()
    executor = KucoinDryRunExecutor(private, FakePublicClient())
    row = make_row(100.0, 0.01, 0.01)

    result = executor.execute("EXIT", row, 100.0)

    assert result.accepted
    assert private.margin_orders[0]["side"] == "buy"
    assert private.margin_orders[0]["autoBorrow"] is False
    assert private.margin_orders[0]["autoRepay"] is True
    assert private.futures_orders[0]["side"] == "sell"
    assert private.futures_orders[0]["reduceOnly"] is True


def test_exit_uses_stored_base_quantity_instead_of_recalculating_from_notional():
    private = FakePrivateClient()
    executor = KucoinDryRunExecutor(private, FakePublicClient())
    row = make_row(50.0, 0.01, 0.01)

    result = executor.execute(
        "EXIT",
        row,
        50.0,
        target_base_quantity=1.0,
    )

    assert result.accepted
    assert result.spot_size == 1.0
    assert result.perp_base_quantity == 1.0
    assert result.perp_contracts == 10
    assert result.executable_notional_usd > 50.0


def test_long_spot_entry_uses_spot_account_and_short_perp():
    private = FakePrivateClient()
    executor = KucoinDryRunExecutor(private, FakePublicClient())
    row = replace_row(make_row(100.0, 0.01, 0.01), direction="LONG_SPOT_SHORT_PERP")

    result = executor.execute("ENTRY", row, 100.0)

    assert result.accepted
    assert len(private.spot_orders) == 1
    assert not private.margin_orders
    assert private.spot_orders[0]["side"] == "buy"
    assert private.futures_orders[0]["side"] == "sell"


def test_short_spot_entry_rejects_when_base_cannot_be_borrowed():
    private = FakePrivateClient(borrow_enabled=False)
    executor = KucoinDryRunExecutor(private, FakePublicClient())

    result = executor.execute("ENTRY", make_row(100.0, 0.01, 0.01), 100.0)

    assert not result.accepted
    assert result.reason == "spot_margin_borrow_not_enabled"
    assert not private.margin_orders
    assert not private.futures_orders


def test_private_client_refuses_every_non_test_post():
    credentials = KucoinCredentials(
        api_key="key",
        api_secret="secret",
        api_passphrase="passphrase",
        api_key_version="3",
        execution_mode="validate",
    )
    client = KucoinPrivateClient(credentials)

    try:
        client._request(
            "POST",
            credentials.spot_url,
            "/api/v1/hf/orders",
            payload={"symbol": "BTC-USDT"},
        )
    except KucoinSafetyError as exc:
        assert "non-test endpoint" in str(exc)
    else:
        raise AssertionError("live order endpoint was not refused")


def test_futures_orderbook_contract_sizes_are_converted_to_base_quantity():
    client = KucoinPublicClient()

    def fake_get(base_url, path, params=None):
        if path == "/api/v1/level2/snapshot":
            return {"bids": [["100", "10"]], "asks": [["101", "20"]]}
        if path == "/api/v1/contracts/MIRAUSDTM":
            return {"symbol": "MIRAUSDTM", "multiplier": "0.1"}
        raise AssertionError(path)

    client._get = fake_get
    book = client.get_futures_orderbook("MIRAUSDT", "MIRAUSDTM")

    assert book.bids[0].quantity == 1.0
    assert book.asks[0].quantity == 2.0


def test_strategy_rejected_preflight_cannot_create_paper_fill():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        row = replace_row(
            make_row(100.0, 0.01, 0.01),
            decision="ENTER_CANDIDATE",
            reason="entry_rules_passed",
        )
        path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(path, [row])

        result = run_paper_strategy_once(
            config,
            path,
            execution_adapter=StubExecutionAdapter(False),
        )
        store = PaperStore(config)

        assert result["entries_opened"] == 0
        assert result["execution_attempts"] == 1
        assert result["execution_rejections"] == 1
        assert not store.fills_path.exists()
        assert store.execution_attempts_path.exists()


def test_strategy_accepted_preflight_records_simulated_fill():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        row = replace_row(
            make_row(100.0, 0.01, 0.01),
            decision="ENTER_CANDIDATE",
            reason="entry_rules_passed",
        )
        path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(path, [row])

        result = run_paper_strategy_once(
            config,
            path,
            execution_adapter=StubExecutionAdapter(True),
        )
        store = PaperStore(config)

        assert result["entries_opened"] == 1
        assert result["execution_attempts"] == 1
        assert result["execution_rejections"] == 0
        assert store.fills_path.exists()
        position = next(iter(store.load_open_positions().values()))
        assert position.notional_usd == 90.0
        assert position.spot_qty == 0.9
        assert position.perp_qty == 0.9


if __name__ == "__main__":
    test_short_spot_entry_validates_auto_borrow_and_long_perp()
    test_short_spot_exit_validates_auto_repay_and_reduce_only_perp()
    test_exit_uses_stored_base_quantity_instead_of_recalculating_from_notional()
    test_long_spot_entry_uses_spot_account_and_short_perp()
    test_short_spot_entry_rejects_when_base_cannot_be_borrowed()
    test_private_client_refuses_every_non_test_post()
    test_futures_orderbook_contract_sizes_are_converted_to_base_quantity()
    test_strategy_rejected_preflight_cannot_create_paper_fill()
    test_strategy_accepted_preflight_records_simulated_fill()
    print("kucoin execution tests passed")
