# KuCoin Dry Run

Dry-run mode runs the existing funding strategy against live KuCoin public data
and validates every simulated entry and exit with KuCoin's authenticated,
non-matching test-order endpoints. It cannot place a live order.

## Safety Boundary

- `paper` remains the default strategy mode and requires no credentials.
- `dry-run` is the only authenticated strategy mode.
- The private client refuses every authenticated `POST` whose endpoint does not
  end in `/test`.
- There is no `live` execution mode or live-order method in the codebase.
- Dry-run state is isolated under `data/kucoin_basis/dry_run/`.

## Environment

Create `.env` in the repository root with:

```text
KUCOIN_API_KEY=...
KUCOIN_API_SECRET=...
KUCOIN_API_PASSPHRASE=...
KUCOIN_API_KEY_VERSION=3
KUCOIN_SPOT_API_URL=https://api.kucoin.com
KUCOIN_FUTURES_API_URL=https://api-futures.kucoin.com
KUCOIN_EXECUTION_MODE=dry_run
```

`KUCOIN_EXECUTION_MODE=validate` is also accepted for compatibility with the
connection diagnostic. The `.env` file is ignored by git.

Use an API key with General, Spot, Margin, and Futures permissions. Disable
withdrawal permission and apply an IP whitelist before any future live work.

## Run

Keep the public scanner running as normal:

```powershell
python kucoin_basis\run_scanner.py --loop --interval 60
```

Run one dry pass:

```powershell
python kucoin_basis\run_paper_strategy.py --execution-mode dry-run
```

Run continuously:

```powershell
python kucoin_basis\run_paper_strategy.py --execution-mode dry-run --loop --interval 60
```

View the isolated dry-run ledger:

```powershell
python kucoin_basis\run_funding_dashboard.py --state-mode dry-run --host 127.0.0.1 --port 8767
```

## Preflight Checks

Before a strategy-approved fill reaches the dry-run ledger, the adapter:

1. Fetches fresh Spot and Futures order books.
2. Converts Futures depth from contracts to base-asset quantity.
3. Quantizes Spot size, Spot price, Futures contracts, and Futures price using
   current exchange metadata.
4. Builds equal-base-quantity hedge legs and enforces a maximum quantity
   mismatch of 25 basis points.
5. Checks margin borrow availability before a short-spot entry.
6. Builds marketable IOC limit orders at the worst consumed book price.
7. Validates both legs through KuCoin's Spot/Margin and Futures `/test`
   endpoints.
8. Records the simulated fill only when both test orders are accepted.

Every attempt is written to:

```text
data/kucoin_basis/dry_run/execution_attempts.csv
```

The log includes requested and executable notional, fresh average and limit
prices, slippage, quantized sizes, hedge mismatch, and each leg's acceptance
state. It does not store API secrets or returned order IDs.

## Limitations

KuCoin test orders do not enter the matching engine. They validate credentials,
permissions, endpoint payloads, precision, and order parameters, but they do
not produce fills, positions, borrowing, fees, funding, latency, partial fills,
or leg risk. The paper ledger still models those outcomes from order-book depth.

A future live adapter needs a persistent two-leg execution state machine,
idempotent client order IDs, fill reconciliation, first-leg failure recovery,
borrow and balance reservations, exchange-position reconciliation, and a manual
kill switch. None of those live capabilities are enabled by this dry-run mode.
