import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from kucoin_basis_convergence.config import KucoinBasisConvergenceConfig
from kucoin_basis_convergence.models import ConvergenceOpportunityRow, parse_float
from kucoin_basis_convergence.paper_store import ConvergencePaperStore
from kucoin_basis_convergence.paper_strategy import run_paper_strategy_once


def make_config(root: Path) -> KucoinBasisConvergenceConfig:
    return KucoinBasisConvergenceConfig(
        data_dir=root / "data",
        observations_dir=root / "data" / "observations",
        opportunities_dir=root / "data" / "opportunities",
        paper_dir=root / "data" / "paper",
        archive_dir=root / "data" / "archive",
    )


def make_row(**overrides) -> ConvergenceOpportunityRow:
    now = datetime.now(timezone.utc)
    data = {
        "timestamp_utc": now,
        "base": "TEST",
        "direction": "LONG_SPOT_SHORT_PERP",
        "spot_symbol": "TEST-USDT",
        "perp_symbol": "TESTUSDTM",
        "funding_rate_pct": 0.0,
        "predicted_funding_rate_pct": 0.0,
        "funding_time_utc": now + timedelta(hours=4),
        "funding_interval_hours": 4.0,
        "spot_bid": 99.9,
        "spot_ask": 100.0,
        "perp_bid": 102.0,
        "perp_ask": 102.1,
        "spot_spread_pct": 0.1,
        "perp_spread_pct": 0.098,
        "basis_pct": 2.0,
        "notional_usd": 25.0,
        "spot_entry_slippage_pct": 0.0,
        "perp_entry_slippage_pct": 0.0,
        "spot_exit_slippage_pct": 0.0,
        "perp_exit_slippage_pct": 0.0,
        "entry_cost_pct": 0.16,
        "exit_cost_pct": 0.16,
        "round_trip_cost_pct": 0.37,
        "round_trip_fillable": True,
        "basis_observation_count": 30,
        "basis_mean_pct": 0.0,
        "basis_median_pct": 0.0,
        "basis_std_pct": 0.75,
        "basis_zscore": 2.66,
        "basis_percentile": 95.0,
        "basis_trend_pct": 0.1,
        "basis_change_5m_pct": 0.2,
        "basis_change_15m_pct": 0.5,
        "basis_change_60m_pct": 1.0,
        "basis_target_pct": 0.0,
        "gross_convergence_pct": 2.0,
        "expected_convergence_pct": 1.0,
        "net_edge_pct": 0.63,
        "decision": "ENTER_CANDIDATE",
        "reason": "entry_rules_passed",
        "spot_entry_avg_price": 100.0,
        "perp_entry_avg_price": 102.0,
        "spot_exit_avg_price": 99.9,
        "perp_exit_avg_price": 102.1,
    }
    data.update(overrides)
    return ConvergenceOpportunityRow(**data)


def write_opportunities(path: Path, rows: list[ConvergenceOpportunityRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    csv_rows = [row.to_csv_row() for row in rows]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)


def test_convergence_strategy_opens_then_closes_on_neutralised_profit():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        first_path = config.opportunities_dir / "kucoin_basis_convergence_opportunities_1.csv"
        second_path = config.opportunities_dir / "kucoin_basis_convergence_opportunities_2.csv"
        first = make_row()
        second = make_row(
            timestamp_utc=first.timestamp_utc + timedelta(minutes=1),
            decision="REJECT",
            reason="basis_neutral_watch_row",
            spot_bid=100.0,
            spot_ask=100.1,
            perp_bid=101.0,
            perp_ask=101.0,
            basis_pct=1.0,
            basis_zscore=0.1,
            basis_percentile=50.0,
            net_edge_pct=-0.1,
            spot_exit_avg_price=100.0,
            perp_exit_avg_price=101.0,
        )
        write_opportunities(first_path, [first])
        write_opportunities(second_path, [second])

        first_result = run_paper_strategy_once(config, first_path)
        assert first_result["entries_opened"] == 1
        assert first_result["open_positions"] == 1

        second_result = run_paper_strategy_once(config, second_path)
        assert second_result["open_positions"] == 0

        store = ConvergencePaperStore(config)
        with store.fills_path.open("r", newline="", encoding="utf-8") as f:
            fills = list(csv.DictReader(f))
        assert [row["event_type"] for row in fills] == ["OPEN_POSITION", "CLOSE_POSITION"]
        assert parse_float(fills[-1]["realised_basis_pnl_usd"]) > 0
        assert fills[-1]["reason"] == "basis_improvement_take_profit"


if __name__ == "__main__":
    test_convergence_strategy_opens_then_closes_on_neutralised_profit()
    print("kucoin basis convergence strategy tests passed")
