# KuCoin Basis Convergence Paper Strategy

This strategy is separate from the funding-first paper strategy. It removes funding as an entry gate and treats funding as a logged carry cost/benefit only.

## Core Recommendation

Scan every active KuCoin spot USDT pair that also has an active USDT perpetual, every 60 seconds to start. The scanner uses a conservative worker pool so the full universe can finish close to the target cadence without immediately hammering KuCoin public APIs. I would not start at 30 seconds for the full universe until the VPS has proven API headroom, because every scan consumes two order book requests per symbol plus contract/symbol discovery. A 60 second scan is enough for paper discovery and avoids training the strategy around microstructure noise.

If 60 seconds is stable for a few days, test 30 seconds on a whitelist of liquid/high-volatility bases rather than the full universe.

## Entry Logic

For every pair, the scanner records one raw basis observation:

```text
basis_pct = (perp_mid / spot_mid - 1) * 100
```

Then it builds candidate rows for both directions and each small notional chunk.

Directions:

```text
perp rich / high basis: LONG_SPOT_SHORT_PERP
perp cheap / low basis: SHORT_SPOT_LONG_PERP
```

Default entry rules:

- At least 30 rolling observations.
- Absolute basis at least 0.50%.
- Cheap/rich tail confirmed by both z-score and percentile:
  - cheap perp: z-score <= -2.0 and percentile <= 10
  - rich perp: z-score >= 2.0 and percentile >= 90
- Expected convergence is current basis back toward the rolling median, haircutted by 50%.
- Net edge is expected convergence minus entry slippage, exit slippage, taker fees, and buffer.
- Net edge must be at least 0.15%.
- Exit cost must be no more than 0.80%.
- Full round-trip cost must be no more than 1.50%.
- Round trip must be fillable at the selected notional.

The point is not to predict a full 20% snapback. The point is to clip small, repeatable dislocations only when the order book says the trade is executable.

## Position Sizing

Defaults are deliberately small:

```text
chunks: 25, 50, 100, 250 USDT
max symbol exposure: 1,000 USDT
max total exposure: 10,000 USDT
```

This is the right shape for research. Many small trades give better statistics than a few large trades, and they expose symbols where the displayed basis is fake because the book is too thin.

## Exit Logic

The initial exit rules are intentionally simple and auditable:

- Take profit if basis improves by at least 0.35% and executable PnL excluding funding is positive.
- Take profit if executable PnL excluding funding reaches 0.20%.
- Take profit if basis neutralises, meaning z-score is within +/-0.35 or percentile is between 40 and 60, and executable PnL excluding funding is positive.
- Exit if the trade moves adversely by 1.50% in basis terms.
- Exit if held 12 hours, even if it is not profitable.
- After 6 hours, exit only if executable PnL excluding funding is non-negative.
- Do not spend accumulated funding to hide a bad basis exit. The decision engine checks basis PnL excluding funding.

The strategy supports partial closes via 25/50/100 USDT chunks. This is important because the close book is often thinner than the entry book.

## What To Analyse

Use the CSVs to answer these before changing thresholds:

- Which bases produce positive realised basis PnL excluding funding?
- Does z-score or percentile do a better job of identifying mean reversion?
- How often does the book look profitable at 25 USDT but fail at 100 or 250 USDT?
- What is the distribution of time-to-profit?
- Are losses mostly from adverse continuation, stale observations, or exit slippage?
- Does funding help or hurt when ignored as an entry condition?
- Are the best opportunities concentrated in only a few very thin symbols?

## CSV Outputs

All files live under:

```text
data/kucoin_basis_convergence/
```

Important files:

- `observations/kucoin_basis_convergence_observations_YYYYMMDD.csv`: one raw basis observation per pair per scan.
- `opportunities/kucoin_basis_convergence_opportunities_YYYYMMDD.csv`: chunk-level candidate rows with costs, stats, decisions, and reasons.
- The research schema intentionally records market-quality and momentum context on every row: spot/perp top-of-book spreads, 5m/15m/60m basis changes, rolling mean/median/std, z-score, percentile, trend, executable entry/exit prices, slippage, round-trip costs, fillability, decision, and rejection reason.
- `paper/positions.csv`: open and closed paper positions.
- `paper/fills.csv`: opens, adds, partial closes, and closes.
- `paper/decisions.csv`: every entry/exit decision, including rejections.
- `paper/funding_events.csv`: funding booked during a convergence trade.
- `paper/cooldowns.csv`: post-close and volatility cooldowns.
- `scanner_runs.csv`: scan health, row count, candidate count, and elapsed time.

## Best Practice Notes

Treat the first week as data generation, not strategy validation. The first threshold set should be conservative, because crypto spot/perp basis tails are often caused by bad spot liquidity, stale prints, borrow/short constraints, or forced perp flow that can continue longer than expected.

The most important metric is not raw basis range. It is realised basis PnL after executable entry and exit costs, excluding funding. If that number is not positive across many small trades, the apparent basis volatility is not monetisable.
