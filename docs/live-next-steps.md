# Live Trading Next Steps

This document defines the work required before the strategy can place live
orders. CSV remains useful for exports and analysis, but it must not be the live
source of truth.

## 1. Transactional State

Use SQLite in WAL mode on the single VPS. Migrate existing CSV data and retain
CSV as a read-only export.

The database must store:

- Positions and position lots.
- Strategy decisions and processed opportunities.
- Execution intents, both order legs, exchange order IDs, fills, and fees.
- Funding settlements, margin loans, repayments, cooldowns, and transfers.
- Account snapshots and reconciliation results.

Use transactions and unique constraints for opportunity keys, client order IDs,
fills, and `position + funding timestamp`. A restart must never duplicate an
order or funding event.

## 2. Two-Leg Execution

Add a persistent state machine covering:

```text
PLANNED -> PREFLIGHTED -> FIRST_LEG_SUBMITTED -> FIRST_LEG_FILLED
        -> HEDGE_SUBMITTED -> HEDGED -> RECONCILED
```

It must also handle partial fills, rejection, timeout, cancellation, and a
`COMPENSATING` path that neutralises an unmatched first leg. Set a strict
maximum unhedged notional and maximum unhedged duration. A restart resumes the
stored state instead of creating a new trade.

## 3. Leverage Policy

- Start the live pilot at `1x` Futures leverage.
- Permit `2x` only after reconciliation and failure-recovery tests pass; hard
  cap configured leverage at `2x` initially.
- Keep effective account leverage below `1.5x` by maintaining excess collateral.
- Require a configurable liquidation-distance buffer before every entry or add.
- Block adds when Futures margin utilisation, cross-margin debt ratio, or
  collateral concentration exceeds its limit.
- Never increase leverage to rescue an adverse position.

Leverage limits must be checked from current exchange account state, not only
from locally recorded positions.

## 4. Liquidity Protection

Every entry, add, and exit needs a fresh preflight immediately before order
submission:

- Reject stale or crossed books and re-fetch when either book is older than one
  second.
- Convert Futures contracts to base quantity and quantize both legs before the
  profitability check.
- Use marketable IOC limit orders with an explicit worst price; do not send an
  unbounded market order.
- Cap per-leg slippage, combined slippage plus fees, hedge mismatch, and order
  size as a percentage of visible depth.
- Require depth inside the price limit to exceed the order by a configurable
  safety multiple.
- Recalculate expected edge after quantization and current borrow cost.
- Reduce the chunk or wait when liquidity fails; forced lifecycle exits may
  relax limits gradually but must remain bounded and logged.

Initial limits should be conservative and calibrated from dry-run telemetry,
not guessed into the live adapter.

## 5. Funds And Collateral Rebalancing

Build a treasury controller for Spot/Trading, cross-margin, and Futures funds.
It should calculate target balances from open positions, approved execution
intents, margin requirements, expected fees, and a reserve buffer.

Rebalancing rules:

- Maintain independent minimum USDT buffers for Spot/Margin and Futures.
- Reserve collateral for every approved but incomplete hedge before considering
  funds available elsewhere.
- Use upper and lower target bands so funds move only after a material deficit
  or surplus; this hysteresis prevents transfer ping-pong.
- Batch transfers and enforce minimum transfer size, cooldown, and maximum
  amount per hour/day.
- Never transfer during an incomplete hedge, active compensation, stale account
  snapshot, or failed reconciliation.
- Never transfer borrowed assets. Repay liabilities before releasing excess
  margin collateral.
- Prefer reducing new order size when a safe transfer cannot complete.
- Record transfer intent, exchange transfer ID, status, balance before/after,
  and reconciliation result in the database.

The controller should propose transfers in dry run first. Automated transfers
are enabled separately from automated orders and have their own kill switch.

## 6. Reconciliation And Risk Controls

At startup and on a schedule, compare local state with KuCoin balances, margin
debt, open orders, fills, transfers, and Futures positions. Stop new entries on
any unexplained difference.

Required controls:

- Manual global kill switch and entries-only pause.
- Maximum symbol, total, borrowed, and unhedged exposure.
- Daily realised-loss and execution-error limits.
- API/data staleness, rate-limit, and repeated-rejection circuit breakers.
- Alerts for partial hedges, reconciliation failures, margin pressure, transfer
  failures, and positions approaching the timed-exit deadline.

## Promotion Sequence

1. SQLite migration with CSV parity and restart tests.
2. Persistent execution state machine exercised entirely with mocked failures.
3. Dry-run order and transfer proposals recorded for at least one full strategy
   cycle across all relevant funding intervals.
4. Reconciliation dashboard shows zero unexplained differences.
5. Manual live pilot at minimum valid size, `1x`, one symbol, and a strict total
   loss cap.
6. Increase symbols or permit `2x` only after reviewing fills, fees, borrow cost,
   slippage, transfer behavior, and recovery events.

No live-order endpoint should be added until steps 1 through 4 pass.
