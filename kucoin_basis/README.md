# KuCoin Basis Funding Strategy

This package is the canonical KuCoin spot/perpetual funding-arbitrage scanner,
paper and dry-run strategy, ledger, and dashboard. It is maintained in the independent
`kucoin-basis-funding-arb` repository and deployed from
`/opt/kucoin-basis-funding-arb` on the VPS.

Paper mode uses public KuCoin REST APIs only. Authenticated dry-run mode validates
production-shaped Spot, Margin, and Futures orders through KuCoin's non-matching
test endpoints. There is no live execution mode or live-order method.

## Activation

| Process | Command | VPS service | Interval/port |
| --- | --- | --- | --- |
| Scanner | `python kucoin_basis/run_scanner.py --loop --interval 60` | `kucoin-basis-scanner` | 60 seconds |
| Paper strategy | `python kucoin_basis/run_paper_strategy.py --loop --interval 60` | `kucoin-basis-strategy` | 60 seconds |
| Dashboard | `python kucoin_basis/run_funding_dashboard.py --port 8766` | `kucoin-basis-dashboard` | `127.0.0.1:8766` |
| Dry-run scanner | `python kucoin_basis/run_scanner.py --state-mode dry-run --loop --interval 60` | Local only | 60 seconds |
| Dry-run strategy | `python kucoin_basis/run_paper_strategy.py --execution-mode dry-run --loop --interval 60` | Local only | 60 seconds |
| Dry-run dashboard | `python kucoin_basis/run_funding_dashboard.py --state-mode dry-run --port 8767` | Local only | `127.0.0.1:8767` |

A new funding cycle must be observed twice with the same funding timestamp before
it can activate an entry. The first five minutes after a funding rollover are
also quarantined. Once confirmed, a fresh depth-priced scanner row can activate
an entry in the next strategy pass when all remaining entry and risk rules pass.

## Direction And Basis

| KuCoin funding | Paper hedge | Funding benefit |
| --- | --- | --- |
| Positive | Long spot, short perpetual | Receive funding on the short perpetual |
| Negative | Short spot, long perpetual | Receive funding on the long perpetual |

Basis is:

```text
basis_pct = (perpetual_mid / spot_mid - 1) * 100
```

Entry and exit PnL use depth-priced average execution prices. Midpoint basis is
used for basis history and directional percentile statistics.

For `LONG_SPOT_SHORT_PERP`, basis improves when it falls. For
`SHORT_SPOT_LONG_PERP`, basis improves when it rises.

## Universe And Scanner

The default empty whitelist means all pairs satisfying both conditions:

- Active KuCoin USDT-margined perpetual contract.
- Enabled KuCoin `BASE-USDT` spot market.

For each relevant pair, the scanner:

1. Fetches current funding, predicted funding, next funding time, funding
   interval, cap, and floor.
2. Shortlists the funding-receiving direction.
3. Downloads 100 levels of spot and perpetual depth, converting perpetual
   contract sizes to base-asset quantity using the contract multiplier.
4. Prices spot entry, perpetual entry, spot exit, and perpetual exit for each
   configured entry chunk and every open-position watch notional.
5. Appends midpoint basis history and calculates rolling statistics.
6. Classifies the spot hedge as cash, cross margin, isolated margin, or
   unavailable.
7. Writes a decision and reason for every tested row.

Open positions remain on the scanner watchlist even when funding no longer
qualifies for a new entry. This supplies current exit rows for held positions.

## Entry Rules

### 1. Funding and time gate

- Directional funding benefit must be at least `0.03%`.
- At least 15 minutes must remain before funding.
- The same funding timestamp must be observed twice before the cycle is
  confirmed.
- The first five minutes after a funding rollover are quarantined.
- The base must be in `approved_bases` when a whitelist is configured.

The `0.03%` setting is only the first nominal floor. Before downloading depth,
the shortlist also requires:

```text
funding benefit - fixed modeled costs >= 0.02%
```

Fixed modeled costs are:

| Component | Allowance |
| --- | ---: |
| Spot entry taker fee | `0.08%` |
| Perpetual entry taker fee | `0.06%` |
| Combined exit fee | `0.14%` |
| Safety buffer | `0.03%` |
| Total before slippage | `0.31%` |

Therefore, absent special fee changes, the practical pre-depth funding benefit
must be at least approximately `0.33%`. Four measured slippage components are
then subtracted as well.

### 2. Hedgeability gate

- `LONG_SPOT_SHORT_PERP` uses owned cash spot and does not require base-asset
  borrowing.
- `SHORT_SPOT_LONG_PERP` requires either an enabled KuCoin cross-margin pair or
  an isolated-margin pair with base borrowing enabled.
- Rows without either route remain visible as `UNHEDGEABLE` but cannot enter.
- Cross and isolated borrow availability is rechecked through the authenticated
  account immediately before a dry-run test order.

### 3. Depth and expected-edge gate

- All four round-trip legs must be fillable for the tested notional.
- Expected edge after four slippage components and all modeled costs must be at
  least `0.02%`.
- Exit slippage plus modeled exit fee must not exceed `1.00%`.
- The strategy processes only the newest scanner timestamp.
- A row older than 180 seconds is stale and cannot activate an entry or exit.

### 4. Basis percentile gate

The latest 15 midpoint basis observations are retained. Once five observations
exist:

- `LONG_SPOT_SHORT_PERP` requires basis at or above the 75th percentile.
- `SHORT_SPOT_LONG_PERP` requires basis at or below the 25th percentile.

The directional rule favors an elevated perpetual premium before shorting the
perpetual and a depressed basis before buying the perpetual.

### 4. Chunk selection and activation

- Tested entry chunks are `$100`, `$250`, `$500`, and `$1,000`.
- Maximum entry chunk is `$1,000`.
- For each base/direction/scanner timestamp, only one row can enter.
- The selected row has the highest expected edge; a smaller notional wins an
  exact edge tie.
- Every opportunity key is processed once and written to the processed ledger.
- A fresh scanner tick can add another best chunk to an existing position.
- The chunk ladder is a menu, not a finite sequence. Repeated adds may continue
  until an exposure or cooldown rule blocks them.

### 5. Add and portfolio gates

- Adds are aggregated into one deterministic `base + direction` position.
- Maximum symbol notional is `$5,000`.
- Maximum total open notional is `$50,000`.
- Maximum open positions is 100.
- A basis standard deviation above `5.00%`, or absolute basis trend above
  `5.00%`, blocks entry and starts a 60-minute cooldown.
- If an existing position's basis has moved adversely by more than `5.00%`, the
  position is held but new adds are blocked for a 60-minute cooldown.
- Any partial or full exit starts a 60-minute re-entry cooldown for that
  base/direction.

The `5.00%` adverse threshold is an add/cooldown control, not a stop-loss.

## Hold Rules

Before the first captured funding event, the normal rule is to hold. Two narrow
exceptions can close part of the hedge:

- Exceptional basis convergence can take a partial profit when basis has
  improved by at least `2.00%`, trade PnL after exit costs is at least `$5.00`,
  and that profit is at least `1.25x` the funding forgone on the chunk.
- A confirmed funding reversal can activate the toxic unwind described below.

An adverse basis move by itself never closes the position. A move beyond
`5.00%` blocks adds and starts the no-add cooldown.

After funding has been captured, normal hold/exit priority is:

1. Directional next funding at or above `0.75%` holds for another event and
   blocks discretionary basis, attractive-profit, and capital-recycling exits.
2. Next funding below `0.30%` requests an unwind.
3. Basis improvement of at least `0.50%`, or non-adverse basis within `0.50%`
   of flat, requests a convergence exit.
4. Funding from `0.30%` to below `0.75%` otherwise holds for the next funding
   event while waiting for a better exit.
5. An unusually attractive all-in `$100` chunk or a capital-recycling trigger
   can request an unwind when the juicy-funding override is not active.
6. For discretionary exits, the strategy can still hold when risk-adjusted next
   funding is worth more than exiting and redeploying the chunk.

Toxic funding reversal and the 40-to-48-hour timed exit are forced lifecycle
controls and take precedence over the normal juicy-funding hold.

## Exit Rules

### Normal profitable unwind

- A complete exit is preferred only when the full notional is depth-priced and
  trade PnL excluding funding is at least `0.02%` of position notional.
- Otherwise the strategy evaluates `$100`, `$250`, and `$500` partial chunks.
- Normal gentle unwind requires positive trade PnL excluding funding and ranks
  by net percentage, then net dollars, then the smaller chunk.
- A `$100` funding-harvest exit may tolerate negative trade PnL when allocated
  cumulative funding makes all-in chunk PnL at least `$0.25`.
- Only one selected chunk is removed per strategy pass.

### Opportunistic and capital-recycling exits

- An all-in `$100` chunk is unusually attractive when it earns at least the
  lower of `$1.00` and `0.75%` of chunk notional.
- A position at or above 80% of the `$5,000` symbol cap can recycle a profitable
  chunk when next funding is below `0.50%` or unavailable.
- Discretionary convergence, attractive-profit, and recycling exits compare a
  90%-haircut next-funding value with a 75%-haircut redeployment edge, estimated
  basis giveback risk, and a `$0.05` hold margin. The position stays open when
  holding has the higher risk-adjusted value.

### Toxic funding reversal

- Directional funding below `0.00%` is adverse because the held perpetual leg
  would pay rather than receive.
- A toxic unwind activates after 60 minutes of confirmed adverse funding or
  once the next settlement is within 90 minutes.
- Before forced pacing is needed, a chunk exits only when its loss is no worse
  than the adverse funding avoided.
- The strategy aims to finish 15 minutes before funding, normally in `$100`
  increments, with exit cost capped at `1.00%` while time permits.
- As the deadline approaches it prioritizes completing the unwind; at the
  deadline it can close the full executable remainder even at a loss.
- This rule applies both before the first funding event and after funding has
  already been captured.

### Timed lifecycle exit

- At 40 hours, adds stop and a paced unwind window begins.
- The strategy initially waits for better prices, then increases urgency so the
  position is closed by 48 hours.
- The exit-cost allowance rises from `1.00%` toward `2.00%` over the window.
- At 48 hours the full executable remainder is selected regardless of profit or
  exit-cost cap.

Every exit still requires a fresh depth-priced row. When no acceptable or
currently paced chunk exists, the decision log records why the position remains
open and the next strategy pass retries.

## Funding Accounting

- Funding accrues after a stored settlement timestamp is crossed.
- The actual settled rate is applied to current open notional and direction.
- The next timestamp advances using the exchange interval, with an eight-hour
  fallback.
- Multiple missed settlements can be accrued sequentially after restart.
- Partial exits reduce quantities, notional, and accrued funding proportionally.
- Funding events are stored independently from entry and exit fills.

## Position And Cost Accounting

- Position quantities use depth-priced average entry prices.
- Adds recalculate weighted spot price, perpetual price, basis, funding estimate,
  quantities, and notional.
- Exit PnL uses the stored quantities and current executable opposite book sides.
- Entry fees, close costs, basis PnL, funding PnL, net excluding funding, and
  total net PnL are tracked separately.

## Data And Audit Files

All primary strategy state is under `data/kucoin_basis/`:

| File | Purpose |
| --- | --- |
| `scanner_runs.csv` | Scanner health, elapsed time, row count, and failures |
| `basis_history.csv` | Midpoint basis history and funding context |
| `opportunities/kucoin_basis_opportunities_YYYYMMDD.csv` | Depth-priced decision rows |
| `paper/positions.csv` | Aggregated paper positions |
| `paper/fills.csv` | Opens, adds, partial exits, and full exits |
| `paper/decisions.csv` | Entry and exit allow/reject reasons |
| `paper/funding_events.csv` | Actual funding accruals |
| `paper/processed_opportunities.csv` | One-time opportunity processing ledger |
| `paper/cooldowns.csv` | Volatility, adverse-add, and post-exit cooldowns |
| `dry_run/positions.csv` | Isolated dry-run positions using quantized hedge quantities |
| `dry_run/decisions.csv` | Dry-run strategy and execution-gate decisions |
| `dry_run/fills.csv` | Simulated fills accepted by both test-order preflights |
| `dry_run/execution_attempts.csv` | Fresh depth, precision, hedge, and test-order audit |

The separate `kucoin_basis_convergence` research strategy writes to its own data
root and is not part of these funding-harvest rules.

## Dry Run And Live Limitations

- Paper mode uses KuCoin's public cross/isolated margin catalogues. Dry run also
  checks account-level borrowing and selects the matching cross or isolated
  test-order route, but it does not create a loan.
- Borrow cost, recalls, margin liquidation, account-specific fee tier, and
  actual balance reservation are not included in PnL.
- Dry run applies current order precision, minimum size, contract multiplier,
  fresh depth, and a 25-basis-point maximum hedge-quantity mismatch.
- A requested dollar chunk can quantize down to the nearest valid Futures
  contract; the dry-run ledger records the resulting executable notional and
  equal-base hedge quantities.
- KuCoin test orders validate payloads but do not enter the matching engine, so
  they cannot reproduce fills, partial fills, latency, fees, funding, or leg
  risk.
- Independent REST snapshots cannot guarantee atomic two-leg execution.
- The authenticated client refuses POST requests to non-test endpoints. There
  is no live execution mode.

The full dry-run setup and safety boundary are documented in
[`../docs/dry-run.md`](../docs/dry-run.md).

## Commands

```bash
python kucoin_basis/run_scanner.py --state-mode dry-run
python kucoin_basis/run_paper_strategy.py
python kucoin_basis/run_paper_strategy.py --execution-mode dry-run
python kucoin_basis/run_funding_dashboard.py --port 8766
python kucoin_basis/run_funding_dashboard.py --state-mode dry-run --port 8767
python kucoin_basis/print_summary.py
python test_kucoin_basis_strategy.py
python test_kucoin_basis_convergence_strategy.py
python test_kucoin_execution.py
```
