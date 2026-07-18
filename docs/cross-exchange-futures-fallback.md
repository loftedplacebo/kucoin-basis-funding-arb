# Cross-Exchange Futures Fallback Proposal

Status: design proposal only. This document does not authorize or implement
live orders, new API credentials, transfers, or changes to the existing KuCoin
spot/perpetual strategy.

## 1. Purpose And Scope

The existing strategy should remain the primary route. A cross-exchange trade
may be considered only when all of the following are true:

- The KuCoin opportunity direction is `SHORT_SPOT_LONG_PERP`.
- KuCoin reports `spot_borrow_unavailable`, so neither cross nor isolated
  margin can provide the short-spot hedge.
- The same underlying has a live USDT perpetual on exactly one approved hedge
  venue: Binance, OKX, or MEXC.
- The external contract identity, settlement asset, contract multiplier, API
  availability, and account eligibility have been verified.
- Event-level net funding remains profitable after both venues' funding,
  executable entry and exit slippage, account-specific fees, and safety
  buffers.

The fallback position is:

```text
KuCoin:        long perpetual  -> receives negative KuCoin funding
Hedge venue:  short perpetual -> offsets delta but has its own funding
```

This is not a replacement for every unavailable KuCoin margin pair. It is a
separate futures/futures strategy with additional funding, basis, execution,
venue, collateral, and operational risks.

Do not use an external venue when the KuCoin cash/margin hedge is available.
Do not split one position across multiple external venues in the first version.

## 2. Venue Eligibility And Selection

Build read-only connectors for the three approved venues and refresh their
contract catalogues periodically. A contract is eligible only when:

- Its trading state is live and API order placement is allowed.
- The underlying identity matches KuCoin. Ticker equality alone is not enough;
  compare exchange metadata, index composition where available, and live price
  scale.
- It is a linear USDT contract, or any settlement difference is explicitly
  modeled and approved.
- Minimum size, quantity step, contract multiplier, and leverage permit an
  equal-base-quantity hedge.
- Both entry and exit books meet the depth and staleness rules.
- The account and jurisdiction permit Futures API trading.

Choose one venue per opportunity using this order:

1. Highest conservative all-in edge.
2. Lowest adverse funding over the complete planned holding period.
3. Best executable depth and lowest four-leg slippage.
4. Lowest account-specific API fee.
5. Strongest API health, reconciliation quality, and available collateral.

Binance should generally win a tie because its regular-user taker fee is lower
than MEXC's current API fee and it commonly has deeper books. OKX is a valid
alternative when its funding schedule improves the event-level result. MEXC
should be selected only when its extra coverage or funding advantage more than
compensates for its higher API fees. This preference is not a hard-coded venue
order: measured economics decide.

Useful official references:

- [Binance USD-M Futures API](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/Introduction)
- [MEXC Contract API](https://mexcdevelop.github.io/apidocs/contract_v1_en/)
- [MEXC API Futures fees](https://www.mexc.com/en-GB/announcements/article/updates-to-api-futures-trading-fees-jun-1-2026-17827791535742)
- [OKX API v5](https://www.okx.com/docs-v5/en/)
- [OKX Futures fees](https://www.okx.com/en-gb/help/advance-notice-adjustment-to-vip-tier-and-future-fees)

## 3. Funding Event Model

Never compare only the displayed headline rates. Build a timeline containing
every funding settlement on both venues from proposed entry through proposed
exit.

For each event, store:

- Venue, symbol, settlement timestamp, interval, and observed rate.
- Whether the rate is fixed, predicted, capped, or still changing.
- Position side and expected signed cash flow.
- Observation timestamp and source response identifier.
- Actual settlement amount after the event.

Expected funding for the holding horizon is:

```text
expected funding PnL
  = sum(KuCoin long funding cash flows)
  + sum(external short funding cash flows)
```

With negative funding, the KuCoin long normally receives while the external
short normally pays. The usable funding edge is the difference, not the
KuCoin rate alone.

Funding intervals and timestamps can differ. For example, if OKX settles at
22:00 and 00:00 while KuCoin settles at 00:00, an entry before 22:00 must
include both OKX payments. An entry just after 22:00 may avoid the earlier
payment, but the 00:00 rate must still be known or conservatively bounded.

Timing rules:

- Create the broad shortlist from 60-second scans.
- Refresh funding schedules and rates every minute for shortlisted symbols.
- During the final 15 minutes before any relevant settlement, refresh funding
  and executable books every 5-10 seconds or use WebSocket updates.
- Retain the existing rule that no new position enters within 15 minutes of
  the target KuCoin settlement.
- Reject an entry when an intervening external funding rate is unknown and its
  worst permitted rate could remove the edge.
- Require two consistent observations after a funding-cycle rollover before
  treating a newly displayed rate as stable.
- Use synchronized UTC clocks and stop entries when clock drift exceeds the
  configured tolerance.

After every settlement, reconcile the exchange funding histories before the
position can add, unwind based on banked funding, or count the event in PnL.

## 4. All-In Entry Edge

Evaluate each configured chunk independently. Do not require the entire target
symbol notional to be executable in one order.

For a candidate chunk:

```text
all-in edge
  = expected signed funding across all scheduled events
  - KuCoin entry slippage
  - hedge-venue entry slippage
  - KuCoin modeled exit slippage
  - hedge-venue modeled exit slippage
  - KuCoin round-trip trading fees
  - hedge-venue round-trip trading fees
  - cross-venue basis risk buffer
  - operational safety buffer
```

Use account-specific fees when authenticated fee endpoints are available. Until
then, assume taker execution. Current planning assumptions are:

| Venue | Futures maker | Futures taker |
| --- | ---: | ---: |
| KuCoin | `0.02%` | `0.06%` |
| Binance regular | `0.02%` | `0.05%` |
| OKX regular | `0.02%` | `0.05%` |
| MEXC API | `0.06%` | `0.08%` |

The approximate two-venue taker round trip is therefore `0.22%` with Binance
or OKX and `0.28%` with MEXC, before slippage and safety buffers.

For the initial paper study, require at least `0.20%` all-in edge after every
modeled cost and buffer. This deliberately exceeds the same-venue threshold
because cross-venue execution and basis risk are larger. Calibrate the final
threshold from recorded data rather than promoting this number directly to
live configuration.

Basis convergence may improve PnL but must not be required to make the entry
profitable. A positive convergence estimate is informational or a sizing
modifier, not a substitute for net funding edge.

## 5. Order-Book And Basis Checks

Normalize both perpetual books into base-asset quantity using each contract's
multiplier. For every candidate chunk, simulate:

- Buying the KuCoin perpetual now.
- Selling the external perpetual now.
- Selling the KuCoin perpetual at modeled exit depth.
- Buying the external perpetual at modeled exit depth.

The preflight must:

- Use books no older than one second at order submission.
- Reject crossed, empty, stale, or sequence-broken books.
- Quantize both legs before recalculating edge.
- Require equal base quantity within a maximum 25 basis-point hedge mismatch.
- Require visible depth inside the worst-price limit to exceed the order by a
  configurable safety multiple.
- Cap each leg's slippage and total entry-plus-exit cost.
- Select the most profitable fillable chunk, normally starting at `$100`.
- Add at most one chunk per symbol per scanner tick.

Record cross-venue basis at entry:

```text
cross basis = (external perp mid - KuCoin perp mid) / KuCoin perp mid
```

Maintain rolling 15-minute and 60-minute basis distributions. The entry buffer
should cover a conservative adverse move, such as a high percentile of recent
minute-to-minute changes. Do not assume two perpetual prices will converge at
the same speed as a spot/perpetual pair.

## 6. Two-Venue Execution

Before either order is sent, reserve collateral and persist one execution
intent containing both venue orders and a shared idempotency key.

Use marketable IOC limit orders with explicit worst prices. Submit the two legs
as close together as the APIs permit. The execution state machine must handle:

```text
PLANNED -> PREFLIGHTED -> BOTH_SUBMITTED -> HEDGED -> RECONCILED
                            |                  ^
                            +-> PARTIAL -------+
                            +-> COMPENSATING
```

If only one leg fills:

1. Cancel any unfilled remainder.
2. Hedge the matched base quantity on the other venue within a stricter
   emergency slippage cap.
3. If that fails, flatten the filled leg immediately.
4. Block new entries and alert until both exchanges reconcile.

Persist client order IDs, exchange order IDs, fills, actual fees, quantities,
and timestamps transactionally. The SQLite and execution-state requirements in
`docs/live-next-steps.md` are prerequisites, not optional enhancements.

## 7. Hold And Exit Rules

The first objective is to capture the specifically approved KuCoin funding
event while remaining delta hedged. After settlement:

- Confirm actual funding on both venues.
- Recalculate the next complete funding-event timeline.
- Hold only if the next cycle remains profitable after expected external
  funding, risk buffers, and the opportunity cost of tied-up collateral.
- Do not let a favorable KuCoin rate override a larger adverse external rate.

Use `$100` unwind chunks. Evaluate the next chunk against current books and
cumulative funding allocated to that chunk. Unwind when:

1. The next funding cycle is weak or adverse on an all-venue basis.
2. Cross-venue basis has reached a favorable convergence target.
3. The chunk is all-in profitable after cumulative funding, basis movement,
   fees, and current slippage.
4. Capital recycling is preferable to the risk-adjusted next funding value.
5. Venue, margin, API, delisting, or reconciliation risk requires reduction.

Unlike the same-venue paper strategy, the fallback should not hold indefinitely
during its pilot. Start with a maximum of two KuCoin funding cycles or 24 hours,
whichever is earlier. Between the soft deadline and hard deadline, unwind the
least-loss chunk first using bounded slippage. This limit can be reconsidered
only after measured cross-venue basis and failure-recovery data exist.

## 8. Capital, Leverage, And Rebalancing

Keep pre-funded USDT collateral on KuCoin and each enabled hedge venue. Never
depend on a transfer completing between the two order legs.

- Start at `1x` configured leverage.
- Permit at most `2x` only after dry-run reconciliation and recovery tests.
- Keep effective account leverage below `1.5x` with excess collateral.
- Set independent per-venue, per-symbol, and total notional limits.
- Reserve fees, adverse funding, and a liquidation-distance buffer.
- Block entries when either venue lacks its post-trade reserve.
- Batch treasury rebalancing outside active execution windows.
- Never transfer during an incomplete hedge or unresolved reconciliation.

Automated transfers require a separate approval, state machine, limits, and
kill switch. Reducing order size is preferable to an urgent transfer.

## 9. Proposed Dashboard And Audit Trail

A future cross-exchange page should show:

- KuCoin symbol, hedge venue, hedge symbol, and contract identity status.
- Both directions and normalized base quantities.
- KuCoin funding, external funding, and net event-level funding.
- Every settlement timestamp expected before exit.
- Entry and exit slippage by venue, fee assumptions, basis risk buffer, and
  all-in edge.
- Cross-venue basis at entry and now.
- Fillability, selected chunk, collateral state, and rejection reason.
- Actual funding by venue, actual fees, realised basis PnL, and total PnL.
- Partial-hedge, stale-data, clock-drift, and reconciliation alerts.

Every scan should retain the decision and component values even when rejected.
This allows analysis of whether missing trades were caused by funding, timing,
fees, basis risk, liquidity, account eligibility, or venue availability.

## 10. Validation And Promotion Sequence

1. Build read-only market, funding, fee, and catalogue collectors only.
2. Record all KuCoin `spot_borrow_unavailable` rows and all three external
   venues for at least 14 days.
3. Replay every funding timeline using executable chunk depth and actual
   settlements. Include opportunities that never entered to avoid selection
   bias.
4. Paper trade with independent venue balances, funding ledgers, and
   cross-basis PnL.
5. Add authenticated account checks and non-matching test orders where the
   venue supports them.
6. Exercise partial-fill, stale-book, API-timeout, restart, and reconciliation
   failures before any live pilot.
7. Run one venue, one symbol, minimum size, `1x`, with a strict daily loss and
   unhedged-duration limit.

No implementation should begin until the recorded study shows that net funding
after external funding, fees, slippage, and cross-basis movement is consistently
positive. The July 18 snapshot suggested ESPORTS merited study, while ASP,
ESIM, TOSHI, and HOME did not offer a robust all-in edge at that moment. That
snapshot is illustrative only; venue selection must always use current data.
