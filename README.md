# KuCoin Basis Funding Arb

Paper-trading research tooling for KuCoin spot/perp funding arbitrage.

The strategy supports both funding directions:

```text
positive funding: buy spot + short perp
negative funding: short spot + long perp
```

It currently uses public KuCoin REST data only. There are no API keys, authenticated clients, live order placement paths, or margin trading execution paths. The short-spot direction is paper-only modelling.

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

Print a paper summary:

```powershell
python kucoin_basis\print_summary.py
```

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

## Tests

```powershell
python -m py_compile kucoin_basis\*.py core\*.py test_kucoin_basis_strategy.py
python test_kucoin_basis_strategy.py
```

## Current Paper Rules

- Universe is active KuCoin USDT perps with enabled KuCoin spot USDT pairs.
- Entry uses funding benefit minus executable entry slippage, executable exit slippage, taker fees, and safety buffer.
- Entry can occur any time before funding except the final 15 minutes.
- Paper strategy chooses the highest-edge chunk per symbol/direction per scanner timestamp.
- Position adds respect max symbol, total notional, and open-position caps.
- Funding accrues when a stored funding timestamp is crossed.
- After funding is captured, the strategy checks whether the next funding event remains attractive.
- Gentle unwind evaluates available partial-close chunks after exit slippage and estimated exit fees, then chooses the best net PnL percentage chunk.
