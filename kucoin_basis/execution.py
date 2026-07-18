from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from pathlib import Path
from typing import Literal, Protocol

from core.models import OrderBook, OrderBookLevel
from kucoin_basis.kucoin_private_client import (
    KucoinApiError,
    KucoinCredentials,
    KucoinPrivateClient,
    KucoinSafetyError,
)
from kucoin_basis.kucoin_public_client import KucoinPublicClient
from kucoin_basis.models import OpportunityRow, format_datetime


ExecutionAction = Literal["ENTRY", "EXIT"]


@dataclass(frozen=True)
class QuantityExecution:
    average_price: float
    worst_price: float
    notional_usd: float
    slippage_pct: float


@dataclass(frozen=True)
class ExecutionResult:
    timestamp_utc: datetime
    mode: str
    action: str
    base: str
    direction: str
    requested_notional_usd: float
    executable_notional_usd: float
    accepted: bool
    reason: str
    spot_venue: str = ""
    spot_side: str = ""
    spot_size: float = 0.0
    spot_average_price: float = 0.0
    spot_limit_price: float = 0.0
    spot_slippage_pct: float = 0.0
    perp_side: str = ""
    perp_contracts: int = 0
    perp_base_quantity: float = 0.0
    perp_average_price: float = 0.0
    perp_limit_price: float = 0.0
    perp_slippage_pct: float = 0.0
    hedge_mismatch_bps: float = 0.0
    spot_test_accepted: bool = False
    perp_test_accepted: bool = False

    def to_csv_row(self) -> dict:
        row = asdict(self)
        row["timestamp_utc"] = format_datetime(self.timestamp_utc)
        return row


class ExecutionAdapter(Protocol):
    mode: str

    def execute(
        self,
        action: ExecutionAction,
        row: OpportunityRow,
        notional_usd: float,
        target_base_quantity: float | None = None,
    ) -> ExecutionResult:
        ...


def _decimal_string(value: Decimal) -> str:
    return format(value, "f")


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _price_to_step(value: float, step: Decimal, side: str) -> Decimal:
    rounding = ROUND_CEILING if side == "buy" else ROUND_DOWN
    return (Decimal(str(value)) / step).to_integral_value(rounding=rounding) * step


def _execution_for_quantity(
    levels: list[OrderBookLevel], quantity: float, side: str
) -> QuantityExecution | None:
    if quantity <= 0 or not levels:
        return None
    remaining = quantity
    filled = 0.0
    notional = 0.0
    worst_price = 0.0
    for level in levels:
        take = min(remaining, level.quantity)
        filled += take
        notional += take * level.price
        remaining -= take
        if take > 0:
            worst_price = level.price
        if remaining <= 1e-12:
            break
    if remaining > 1e-9 or filled <= 0:
        return None
    average = notional / filled
    best = levels[0].price
    if side == "buy":
        slippage = ((average / best) - 1) * 100
    else:
        slippage = ((best / average) - 1) * 100
    return QuantityExecution(average, worst_price, notional, slippage)


def _sides(direction: str, action: ExecutionAction) -> tuple[str, str, str]:
    if direction == "LONG_SPOT_SHORT_PERP":
        return ("spot", "buy", "sell") if action == "ENTRY" else ("spot", "sell", "buy")
    if direction == "SHORT_SPOT_LONG_PERP":
        return ("margin", "sell", "buy") if action == "ENTRY" else ("margin", "buy", "sell")
    raise ValueError(f"Unsupported strategy direction: {direction}")


class KucoinDryRunExecutor:
    """Validates production-shaped hedge orders without entering a matching engine."""

    mode = "dry_run"

    def __init__(
        self,
        private_client: KucoinPrivateClient,
        public_client: KucoinPublicClient | None = None,
        max_hedge_mismatch_bps: float = 25.0,
    ):
        mode = private_client.credentials.execution_mode.strip().lower().replace("-", "_")
        if mode not in {"validate", "dry_run"}:
            raise KucoinSafetyError(
                "Dry run requires KUCOIN_EXECUTION_MODE=validate or dry_run"
            )
        self.private_client = private_client
        self.public_client = public_client or KucoinPublicClient()
        self.max_hedge_mismatch_bps = max_hedge_mismatch_bps
        self._borrow_enabled: dict[str, bool] | None = None

    @classmethod
    def from_env_file(
        cls,
        path: Path,
        max_hedge_mismatch_bps: float = 25.0,
    ) -> "KucoinDryRunExecutor":
        credentials = KucoinCredentials.from_env_file(path)
        return cls(
            KucoinPrivateClient(credentials),
            max_hedge_mismatch_bps=max_hedge_mismatch_bps,
        )

    def _margin_borrow_enabled(self, currency: str) -> bool:
        if self._borrow_enabled is None:
            account = self.private_client.get_cross_margin_account()
            self._borrow_enabled = {
                str(item.get("currency", "")): item.get("borrowEnabled") is True
                for item in account.get("accounts") or []
            }
        return self._borrow_enabled.get(currency, False)

    def _rejected(
        self,
        action: ExecutionAction,
        row: OpportunityRow,
        notional_usd: float,
        reason: str,
        **fields,
    ) -> ExecutionResult:
        return ExecutionResult(
            timestamp_utc=datetime.now(timezone.utc),
            mode=self.mode,
            action=action,
            base=row.base,
            direction=row.direction,
            requested_notional_usd=notional_usd,
            executable_notional_usd=float(fields.pop("executable_notional_usd", 0.0)),
            accepted=False,
            reason=reason,
            **fields,
        )

    def execute(
        self,
        action: ExecutionAction,
        row: OpportunityRow,
        notional_usd: float,
        target_base_quantity: float | None = None,
    ) -> ExecutionResult:
        try:
            return self._execute(
                action,
                row,
                notional_usd,
                target_base_quantity=target_base_quantity,
            )
        except (KeyError, TypeError, ValueError, KucoinApiError, KucoinSafetyError) as exc:
            return self._rejected(
                action,
                row,
                notional_usd,
                f"{type(exc).__name__}: {exc}",
            )

    def _execute(
        self,
        action: ExecutionAction,
        row: OpportunityRow,
        notional_usd: float,
        target_base_quantity: float | None = None,
    ) -> ExecutionResult:
        if notional_usd <= 0:
            raise ValueError("notional must be positive")
        spot_venue, spot_side, perp_side = _sides(row.direction, action)
        if spot_venue == "margin" and action == "ENTRY" and not self._margin_borrow_enabled(row.base):
            return self._rejected(
                action, row, notional_usd, "spot_margin_borrow_not_enabled"
            )

        standard_symbol = f"{row.base}USDT"
        spot_book = self.public_client.get_spot_orderbook(
            standard_symbol, row.spot_symbol, limit=100
        )
        perp_book = self.public_client.get_futures_orderbook(
            standard_symbol, row.perp_symbol, limit=100
        )
        spot_levels = spot_book.asks if spot_side == "buy" else spot_book.bids
        perp_levels = perp_book.asks if perp_side == "buy" else perp_book.bids
        if not spot_levels or not perp_levels:
            return self._rejected(action, row, notional_usd, "empty_orderbook")

        spot_symbol = self.public_client.get_spot_symbol(row.spot_symbol)
        contract = self.public_client.get_contract(row.perp_symbol)
        spot_step = Decimal(str(spot_symbol["baseIncrement"]))
        spot_min = Decimal(str(spot_symbol["baseMinSize"]))
        min_funds = Decimal(str(spot_symbol.get("minFunds") or "0"))
        spot_tick = Decimal(str(spot_symbol["priceIncrement"]))
        multiplier = Decimal(str(contract["multiplier"]))
        contract_lot = Decimal(str(contract.get("lotSize") or "1"))
        contract_tick = Decimal(str(contract["tickSize"]))

        reference_price = max(spot_levels[0].price, perp_levels[0].price)
        target_base = Decimal(
            str(
                target_base_quantity
                if target_base_quantity is not None
                else notional_usd / reference_price
            )
        )
        contracts = _floor_to_step(target_base / multiplier, contract_lot)
        if contracts <= 0:
            return self._rejected(action, row, notional_usd, "below_futures_minimum")
        perp_base = contracts * multiplier
        spot_size = _floor_to_step(perp_base, spot_step)
        if spot_size < spot_min:
            return self._rejected(action, row, notional_usd, "below_spot_minimum")

        spot_execution = _execution_for_quantity(spot_levels, float(spot_size), spot_side)
        perp_execution = _execution_for_quantity(perp_levels, float(perp_base), perp_side)
        if spot_execution is None or perp_execution is None:
            return self._rejected(action, row, notional_usd, "fresh_depth_not_fillable")
        if Decimal(str(spot_execution.notional_usd)) < min_funds:
            return self._rejected(action, row, notional_usd, "below_spot_min_funds")

        mismatch_bps = float(abs(spot_size - perp_base) / perp_base * Decimal("10000"))
        if mismatch_bps > self.max_hedge_mismatch_bps:
            return self._rejected(
                action,
                row,
                notional_usd,
                "hedge_quantity_mismatch",
                hedge_mismatch_bps=mismatch_bps,
            )

        spot_limit = _price_to_step(spot_execution.worst_price, spot_tick, spot_side)
        perp_limit = _price_to_step(perp_execution.worst_price, contract_tick, perp_side)
        executable_notional = min(
            spot_execution.notional_usd, perp_execution.notional_usd
        )
        common_fields = {
            "executable_notional_usd": executable_notional,
            "spot_venue": spot_venue,
            "spot_side": spot_side,
            "spot_size": float(spot_size),
            "spot_average_price": spot_execution.average_price,
            "spot_limit_price": float(spot_limit),
            "spot_slippage_pct": spot_execution.slippage_pct,
            "perp_side": perp_side,
            "perp_contracts": int(contracts),
            "perp_base_quantity": float(perp_base),
            "perp_average_price": perp_execution.average_price,
            "perp_limit_price": float(perp_limit),
            "perp_slippage_pct": perp_execution.slippage_pct,
            "hedge_mismatch_bps": mismatch_bps,
        }

        spot_payload = {
            "clientOid": uuid.uuid4().hex,
            "symbol": row.spot_symbol,
            "type": "limit",
            "side": spot_side,
            "price": _decimal_string(spot_limit),
            "size": _decimal_string(spot_size),
            "timeInForce": "IOC",
        }
        if spot_venue == "margin":
            spot_payload.update(
                {
                    "isIsolated": False,
                    "autoBorrow": action == "ENTRY",
                    "autoRepay": action == "EXIT",
                }
            )
            spot_response = self.private_client.test_margin_order(spot_payload)
        else:
            spot_response = self.private_client.test_spot_order(spot_payload)
        spot_accepted = bool(spot_response.get("orderId"))
        if not spot_accepted:
            return self._rejected(
                action,
                row,
                notional_usd,
                "spot_test_order_not_accepted",
                spot_test_accepted=False,
                **common_fields,
            )

        futures_payload = {
            "clientOid": uuid.uuid4().hex,
            "symbol": row.perp_symbol,
            "type": "limit",
            "side": perp_side,
            "price": _decimal_string(perp_limit),
            "size": int(contracts),
            "leverage": 1,
            "marginMode": "ISOLATED",
            "reduceOnly": action == "EXIT",
            "timeInForce": "IOC",
        }
        futures_response = self.private_client.test_futures_order(futures_payload)
        perp_accepted = bool(futures_response.get("orderId"))
        if not perp_accepted:
            return self._rejected(
                action,
                row,
                notional_usd,
                "futures_test_order_not_accepted",
                spot_test_accepted=True,
                perp_test_accepted=False,
                **common_fields,
            )

        return ExecutionResult(
            timestamp_utc=datetime.now(timezone.utc),
            mode=self.mode,
            action=action,
            base=row.base,
            direction=row.direction,
            requested_notional_usd=notional_usd,
            accepted=True,
            reason="test_orders_accepted",
            spot_test_accepted=True,
            perp_test_accepted=True,
            **common_fields,
        )
