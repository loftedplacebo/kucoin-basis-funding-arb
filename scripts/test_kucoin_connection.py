from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlencode

import requests


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name.strip()] = value
    return values


def signed_headers(
    values: dict[str, str],
    method: str,
    endpoint: str,
    body: str = "",
) -> dict[str, str]:
    secret = values["KUCOIN_API_SECRET"]
    timestamp = str(int(time.time() * 1000))
    prehash = f"{timestamp}{method.upper()}{endpoint}{body}"
    signature = base64.b64encode(
        hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()
    encrypted_passphrase = base64.b64encode(
        hmac.new(
            secret.encode(),
            values["KUCOIN_API_PASSPHRASE"].encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    return {
        "KC-API-KEY": values["KUCOIN_API_KEY"],
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": timestamp,
        "KC-API-PASSPHRASE": encrypted_passphrase,
        "KC-API-KEY-VERSION": values["KUCOIN_API_KEY_VERSION"],
        "Content-Type": "application/json",
    }


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | None = None,
) -> dict:
    response = requests.request(
        method,
        url,
        headers=headers,
        data=body,
        timeout=20,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"HTTP {response.status_code}, non-JSON response"
        ) from exc
    if response.status_code != 200 or payload.get("code") != "200000":
        raise RuntimeError(
            f"HTTP {response.status_code}, code={payload.get('code')}, "
            f"message={payload.get('msg', 'unknown')}"
        )
    return payload


def private_request(
    values: dict[str, str],
    method: str,
    base_url: str,
    endpoint: str,
    *,
    params: dict[str, str] | None = None,
    payload: dict | None = None,
) -> dict:
    query = urlencode(params or {})
    signed_endpoint = endpoint + (f"?{query}" if query else "")
    body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    return request_json(
        method,
        base_url + signed_endpoint,
        headers=signed_headers(values, method, signed_endpoint, body),
        body=body or None,
    )


def public_data(base_url: str, endpoint: str, params: dict[str, str] | None = None):
    query = urlencode(params or {})
    suffix = endpoint + (f"?{query}" if query else "")
    return request_json("GET", base_url + suffix).get("data")


def decimal_string(value: Decimal) -> str:
    return format(value, "f")


def floor_to_increment(value: str, increment: str) -> str:
    number = Decimal(value)
    step = Decimal(increment)
    units = (number / step).to_integral_value(rounding=ROUND_DOWN)
    return decimal_string(units * step)


def run_read_only_checks(
    values: dict[str, str], spot_url: str, futures_url: str
) -> None:
    spot_accounts = private_request(
        values, "GET", spot_url, "/api/v1/accounts"
    ).get("data") or []
    account_types = sorted(
        {str(item.get("type")) for item in spot_accounts if item.get("type")}
    )
    currencies = {item.get("currency") for item in spot_accounts if item.get("currency")}
    print(
        "Spot account read: successful "
        f"({len(spot_accounts)} account records, {len(currencies)} currencies, "
        f"types={','.join(account_types) or 'none'})"
    )

    margin_data = private_request(
        values,
        "GET",
        spot_url,
        "/api/v3/margin/accounts",
        params={"quoteCurrency": "USDT", "queryType": "MARGIN"},
    ).get("data") or {}
    margin_accounts = margin_data.get("accounts") or []
    borrow_enabled = sum(
        1 for item in margin_accounts if item.get("borrowEnabled") is True
    )
    debt_present = any(Decimal(str(item.get("liability", "0"))) > 0 for item in margin_accounts)
    print(
        "Cross-margin account read: successful "
        f"({len(margin_accounts)} currency records, borrow-enabled={borrow_enabled}, "
        f"debt-present={'yes' if debt_present else 'no'})"
    )

    borrow_rates = private_request(
        values,
        "GET",
        spot_url,
        "/api/v3/margin/borrowRate",
        params={"currency": "BTC"},
    ).get("data")
    if isinstance(borrow_rates, list):
        borrow_rate_count = len(borrow_rates)
    else:
        borrow_rate_count = 1 if borrow_rates else 0
    print(
        "Margin borrow-rate read: successful "
        f"(BTC rate data available={'yes' if borrow_rate_count else 'no'})"
    )

    futures_data = private_request(
        values,
        "GET",
        futures_url,
        "/api/v1/account-overview",
        params={"currency": "USDT"},
    ).get("data") or {}
    margin_present = Decimal(str(futures_data.get("positionMargin", "0"))) > 0
    print(
        "Futures account read: successful "
        f"(currency={futures_data.get('currency', 'unknown')}, "
        f"position-margin-present={'yes' if margin_present else 'no'})"
    )


def validate_test_orders(
    values: dict[str, str], spot_url: str, futures_url: str
) -> None:
    if values.get("KUCOIN_EXECUTION_MODE", "").strip().lower() != "validate":
        raise RuntimeError(
            "Refusing order validation unless KUCOIN_EXECUTION_MODE=validate"
        )

    spot_symbol = public_data(spot_url, "/api/v2/symbols/BTC-USDT") or {}
    spot_quote = public_data(
        spot_url,
        "/api/v1/market/orderbook/level1",
        {"symbol": "BTC-USDT"},
    ) or {}
    spot_price = floor_to_increment(
        spot_quote.get("bestBid") or spot_quote.get("price"),
        spot_symbol["priceIncrement"],
    )
    spot_size = spot_symbol["baseMinSize"]

    spot_payload = {
        "clientOid": uuid.uuid4().hex,
        "symbol": "BTC-USDT",
        "type": "limit",
        "side": "buy",
        "price": spot_price,
        "size": spot_size,
        "timeInForce": "GTC",
    }
    spot_result = private_request(
        values,
        "POST",
        spot_url,
        "/api/v1/hf/orders/test",
        payload=spot_payload,
    ).get("data") or {}
    print(
        "Spot test order: accepted by non-matching endpoint "
        f"(order-id-returned={'yes' if spot_result.get('orderId') else 'no'})"
    )

    margin_payload = {
        **spot_payload,
        "clientOid": uuid.uuid4().hex,
        "isIsolated": False,
        "autoBorrow": False,
        "autoRepay": False,
    }
    margin_result = private_request(
        values,
        "POST",
        spot_url,
        "/api/v3/hf/margin/order/test",
        payload=margin_payload,
    ).get("data") or {}
    print(
        "Margin test order: accepted by non-matching endpoint "
        f"(order-id-returned={'yes' if margin_result.get('orderId') else 'no'})"
    )

    contract = public_data(futures_url, "/api/v1/contracts/XBTUSDTM") or {}
    futures_price = floor_to_increment(
        str(contract.get("markPrice") or contract.get("lastTradePrice")),
        str(contract["tickSize"]),
    )
    futures_payload = {
        "clientOid": uuid.uuid4().hex,
        "symbol": "XBTUSDTM",
        "type": "limit",
        "side": "buy",
        "price": futures_price,
        "size": 1,
        "leverage": 1,
        "marginMode": "ISOLATED",
        "reduceOnly": False,
        "timeInForce": "GTC",
    }
    futures_result = private_request(
        values,
        "POST",
        futures_url,
        "/api/v1/orders/test",
        payload=futures_payload,
    ).get("data") or {}
    print(
        "Futures test order: accepted by non-matching endpoint "
        f"(order-id-returned={'yes' if futures_result.get('orderId') else 'no'})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Test KuCoin private API authentication.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also read account capabilities and validate non-matching test orders.",
    )
    args = parser.parse_args()

    values = load_env(args.env_file)
    required = {
        "KUCOIN_API_KEY",
        "KUCOIN_API_SECRET",
        "KUCOIN_API_PASSPHRASE",
        "KUCOIN_API_KEY_VERSION",
    }
    missing = sorted(name for name in required if not values.get(name))
    if missing:
        raise SystemExit(f"Missing required environment values: {', '.join(missing)}")

    spot_url = values.get("KUCOIN_SPOT_API_URL", "https://api.kucoin.com").rstrip("/")
    futures_url = values.get(
        "KUCOIN_FUTURES_API_URL", "https://api-futures.kucoin.com"
    ).rstrip("/")
    endpoint = "/api/v1/user/api-key"
    try:
        payload = private_request(values, "GET", spot_url, endpoint)
    except RuntimeError as exc:
        raise SystemExit(f"Authentication test failed: {exc}") from exc

    data = payload.get("data") or {}
    print("KuCoin private API authentication successful")
    print(f"API version: {data.get('apiVersion')}")
    print(f"Permissions: {data.get('permission')}")
    print(f"Site type: {data.get('siteType')}")
    print(f"Master account: {data.get('isMaster')}")
    print(f"KYC status: {data.get('kycStatus')}")

    if args.full:
        print("\nRead-only capability checks")
        try:
            run_read_only_checks(values, spot_url, futures_url)
            print("\nNon-matching order validation")
            validate_test_orders(values, spot_url, futures_url)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise SystemExit(f"Full integration diagnostic failed: {exc}") from exc


if __name__ == "__main__":
    main()
