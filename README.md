# KuCoin Basis Funding Arb

Research and execution-validation tooling for KuCoin spot/perp funding
arbitrage. The strategy runs in public-data paper mode by default and provides
an explicitly selected authenticated dry-run mode.

The complete activation, entry, add, hold, funding, and exit specification is in
[`kucoin_basis/README.md`](kucoin_basis/README.md). That package document is the
authoritative operating description for the funding strategy.

The strategy supports both funding directions:

```text
positive funding: buy spot + short perp
negative funding: short spot + long perp
```

There is no live execution mode. Dry run re-fetches current depth, applies
KuCoin precision and contract rules, checks margin borrow availability, and
validates both hedge legs through non-matching `/test` endpoints. Authenticated
POST requests to any non-test endpoint are refused in code. See
[`docs/dry-run.md`](docs/dry-run.md) for the safety boundary and setup.

## Run Locally

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Run the scanner, paper strategy, and dashboard in separate terminals:

```powershell
python kucoin_basis\run_scanner.py --loop --interval 60
python kucoin_basis\run_paper_strategy.py --loop --interval 60
python kucoin_basis\run_funding_dashboard.py --host 127.0.0.1 --port 8766
```

Open:

```text
http://127.0.0.1:8766/
```

Run the authenticated strategy dry run and its isolated dashboard:

```powershell
python kucoin_basis\run_paper_strategy.py --execution-mode dry-run --loop --interval 60
python kucoin_basis\run_funding_dashboard.py --state-mode dry-run --host 127.0.0.1 --port 8767
```

Verify credentials and all three non-matching test-order endpoints:

```powershell
python scripts\test_kucoin_connection.py --full
```

Print a paper summary:

```powershell
python kucoin_basis\print_summary.py
```

Run the separate basis-convergence paper research loop:

```powershell
python kucoin_basis_convergence\run_scanner.py --loop --interval 60
python kucoin_basis_convergence\run_paper_strategy.py --loop --interval 60
```

The convergence strategy is documented in `kucoin_basis_convergence/STRATEGY.md`.

## VPS Dashboard Access

Clone the repo on the VPS:

```bash
git clone https://github.com/loftedplacebo/kucoin-basis-funding-arb.git
cd kucoin-basis-funding-arb
```

Install and start the `systemd` services:

```bash
bash scripts/install_systemd_services.sh
```

This starts and auto-restarts:

- `kucoin-basis-scanner`
- `kucoin-basis-strategy`
- `kucoin-basis-dashboard`

The dashboard binds to `127.0.0.1:8766` on the VPS.

Check service health:

```bash
bash scripts/vps_status.sh
```

From your local machine, tunnel it:

```powershell
ssh -L 8766:127.0.0.1:8766 your_user@your_vps_ip
```

Then open locally:

```text
http://127.0.0.1:8766/
```

This keeps the dashboard off the public internet.

## Data

Generated paper state and scanner output are written under:

```text
data/kucoin_basis/
```

That directory is intentionally ignored by git.

Useful audit files:

- `data/kucoin_basis/scanner_runs.csv`: one row per scanner pass, including failures and elapsed time.
- `data/kucoin_basis/opportunities/kucoin_basis_opportunities_YYYYMMDD.csv`: every scanner chunk row with `decision` and `reason`.
- `data/kucoin_basis/paper/decisions.csv`: every paper entry/exit decision with allow/deny reason.
- `data/kucoin_basis/paper/fills.csv`: paper opens, adds, partial closes, and closes.
- `data/kucoin_basis/paper/funding_events.csv`: booked funding events.
- `data/kucoin_basis/dry_run/`: isolated dry-run positions, decisions, fills,
  funding events, and execution preflights.
- `data/kucoin_basis/dry_run/execution_attempts.csv`: quantized two-leg order
  plans and KuCoin test-order acceptance results.

The basis-convergence strategy writes separate files under:

```text
data/kucoin_basis_convergence/
```

## Tests

```powershell
python test_kucoin_basis_strategy.py
python test_kucoin_basis_convergence_strategy.py
python test_kucoin_execution.py
```

## Current Strategy Rules

- Universe is active KuCoin USDT perps with enabled KuCoin spot USDT pairs.
- Entry uses funding benefit minus executable entry slippage, executable exit slippage, taker fees, and safety buffer.
- Entry can occur any time before funding except the final 15 minutes.
- Paper strategy chooses the highest-edge chunk per symbol/direction per scanner timestamp.
- Position adds respect max symbol, total notional, and open-position caps.
- Funding accrues when a stored funding timestamp is crossed.
- After funding is captured, the strategy checks whether the next funding event remains attractive.
- Gentle unwind evaluates available partial-close chunks after exit slippage and estimated exit fees, then chooses the best net PnL percentage chunk.
- Dry run gates simulated fills on fresh depth, borrow availability, exchange
  precision, hedge mismatch, and acceptance of both non-matching test orders.
