from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests


class KucoinApiError(RuntimeError):
    pass


class KucoinSafetyError(RuntimeError):
    pass


def load_env_file(path: Path) -> dict[str, str]:
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


@dataclass(frozen=True)
class KucoinCredentials:
    api_key: str
    api_secret: str
    api_passphrase: str
    api_key_version: str
    spot_url: str = "https://api.kucoin.com"
    futures_url: str = "https://api-futures.kucoin.com"
    execution_mode: str = ""

    @classmethod
    def from_env_file(cls, path: Path) -> "KucoinCredentials":
        values = load_env_file(path)
        required = (
            "KUCOIN_API_KEY",
            "KUCOIN_API_SECRET",
            "KUCOIN_API_PASSPHRASE",
            "KUCOIN_API_KEY_VERSION",
        )
        missing = [name for name in required if not values.get(name)]
        if missing:
            raise KucoinApiError(
                f"Missing required environment values: {', '.join(missing)}"
            )
        return cls(
            api_key=values["KUCOIN_API_KEY"],
            api_secret=values["KUCOIN_API_SECRET"],
            api_passphrase=values["KUCOIN_API_PASSPHRASE"],
            api_key_version=values["KUCOIN_API_KEY_VERSION"],
            spot_url=values.get("KUCOIN_SPOT_API_URL", "https://api.kucoin.com").rstrip("/"),
            futures_url=values.get(
                "KUCOIN_FUTURES_API_URL", "https://api-futures.kucoin.com"
            ).rstrip("/"),
            execution_mode=values.get("KUCOIN_EXECUTION_MODE", ""),
        )


class KucoinPrivateClient:
    """Authenticated KuCoin client intentionally limited to reads and test orders."""

    def __init__(
        self,
        credentials: KucoinCredentials,
        session: requests.Session | None = None,
    ):
        self.credentials = credentials
        self.session = session or requests.Session()

    def _headers(self, method: str, endpoint: str, body: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        secret = self.credentials.api_secret.encode()
        prehash = f"{timestamp}{method.upper()}{endpoint}{body}".encode()
        signature = base64.b64encode(
            hmac.new(secret, prehash, hashlib.sha256).digest()
        ).decode()
        encrypted_passphrase = base64.b64encode(
            hmac.new(
                secret,
                self.credentials.api_passphrase.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        return {
            "KC-API-KEY": self.credentials.api_key,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": encrypted_passphrase,
            "KC-API-KEY-VERSION": self.credentials.api_key_version,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        base_url: str,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict | None = None,
    ):
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise KucoinSafetyError(f"Unsupported private API method: {method}")
        if method == "POST" and not endpoint.endswith("/test"):
            raise KucoinSafetyError(
                f"Refusing authenticated POST to non-test endpoint: {endpoint}"
            )
        query = urlencode(params or {})
        signed_endpoint = endpoint + (f"?{query}" if query else "")
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        try:
            response = self.session.request(
                method,
                base_url + signed_endpoint,
                headers=self._headers(method, signed_endpoint, body),
                data=body or None,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise KucoinApiError(f"KuCoin request failed: {exc}") from exc
        try:
            response_payload = response.json()
        except ValueError as exc:
            raise KucoinApiError(
                f"KuCoin returned HTTP {response.status_code} with a non-JSON body"
            ) from exc
        if response.status_code != 200 or response_payload.get("code") != "200000":
            raise KucoinApiError(
                f"HTTP {response.status_code}, code={response_payload.get('code')}, "
                f"message={response_payload.get('msg', 'unknown')}"
            )
        return response_payload.get("data")

    def get_api_key_info(self) -> dict:
        return self._request(
            "GET", self.credentials.spot_url, "/api/v1/user/api-key"
        ) or {}

    def get_spot_accounts(self) -> list[dict]:
        data = self._request(
            "GET", self.credentials.spot_url, "/api/v1/accounts"
        )
        return data if isinstance(data, list) else []

    def get_cross_margin_account(self) -> dict:
        return self._request(
            "GET",
            self.credentials.spot_url,
            "/api/v3/margin/accounts",
            params={"quoteCurrency": "USDT", "queryType": "MARGIN"},
        ) or {}

    def get_futures_account(self, currency: str = "USDT") -> dict:
        return self._request(
            "GET",
            self.credentials.futures_url,
            "/api/v1/account-overview",
            params={"currency": currency},
        ) or {}

    def test_spot_order(self, payload: dict) -> dict:
        return self._request(
            "POST",
            self.credentials.spot_url,
            "/api/v1/hf/orders/test",
            payload=payload,
        ) or {}

    def test_margin_order(self, payload: dict) -> dict:
        return self._request(
            "POST",
            self.credentials.spot_url,
            "/api/v3/hf/margin/order/test",
            payload=payload,
        ) or {}

    def test_futures_order(self, payload: dict) -> dict:
        return self._request(
            "POST",
            self.credentials.futures_url,
            "/api/v1/orders/test",
            payload=payload,
        ) or {}
