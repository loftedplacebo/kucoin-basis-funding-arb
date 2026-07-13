from __future__ import annotations

from core.symbols import normalise_symbol, split_standard_symbol

from kucoin_basis.config import KucoinBasisConfig
from kucoin_basis.kucoin_public_client import KucoinPublicClient
from kucoin_basis.models import SymbolPair


def standard_symbol_for_base(base: str) -> str:
    return f"{base.upper()}USDT"


def build_symbol_pairs(
    spot_symbols: list[dict],
    contracts: list[dict],
    config: KucoinBasisConfig,
) -> list[SymbolPair]:
    active_spot_symbols = {}
    for item in spot_symbols:
        if item.get("quoteCurrency") != "USDT":
            continue
        if str(item.get("enableTrading", "true")).lower() == "false":
            continue
        base = str(item.get("baseCurrency") or "").upper()
        symbol = item.get("symbol")
        if base and symbol:
            active_spot_symbols[base] = str(symbol)

    active_perp_symbols = {}
    for contract in contracts:
        symbol = contract.get("symbol")
        quote = contract.get("quoteCurrency")
        status = contract.get("status")
        if not symbol or quote != "USDT" or status != "Open":
            continue
        base, _ = split_standard_symbol(normalise_symbol(str(symbol)))
        active_perp_symbols[base] = str(symbol)

    pairs = []
    allowed_bases = set(config.approved_bases)
    tradeable_bases = sorted(set(active_spot_symbols) & set(active_perp_symbols))
    for base in tradeable_bases:
        if allowed_bases and base not in allowed_bases:
            continue
        pairs.append(
            SymbolPair(
                base=base,
                spot_symbol=active_spot_symbols[base],
                perp_symbol=active_perp_symbols[base],
            )
        )
    return pairs


def discover_symbol_pairs(
    client: KucoinPublicClient,
    config: KucoinBasisConfig,
) -> list[SymbolPair]:
    spot_symbols = client.get_spot_symbols()
    contracts = client.get_active_contracts()
    return build_symbol_pairs(spot_symbols, contracts, config)
