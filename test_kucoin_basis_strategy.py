import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta, timezone

from core.models import OrderBook, OrderBookLevel
from kucoin_basis.config import KucoinBasisConfig
from kucoin_basis.models import OpportunityRow, SymbolPair, parse_float
from kucoin_basis.funding_dashboard import _position_unwind_estimates, load_summary_payload
from kucoin_basis.opportunity_scanner import scan_pair
from kucoin_basis.paper_models import PaperPosition
from kucoin_basis.paper_store import DECISION_FIELDS, PaperStore
from kucoin_basis.paper_strategy import _accrue_funding_if_crossed, _choose_partial_close, _should_exit, run_paper_strategy_once


def make_position(**overrides):
    now = datetime.now(timezone.utc)
    data = {
        "position_id": "KUCOIN_BASIS_MIRA_SHORT_SPOT_LONG_PERP",
        "base": "MIRA",
        "direction": "SHORT_SPOT_LONG_PERP",
        "spot_symbol": "MIRA-USDT",
        "perp_symbol": "MIRAUSDTM",
        "notional_usd": 500.0,
        "spot_qty": 5.0,
        "perp_qty": 5.0,
        "spot_entry_price": 100.0,
        "perp_entry_price": 100.0,
        "entry_basis_pct": -1.0,
        "current_basis_pct": -0.8,
        "funding_rate_pct_at_entry": -0.5,
        "expected_funding_pct": 0.5,
        "realised_funding_pnl_usd": 0.0,
        "unrealised_basis_pnl_usd": 0.0,
        "estimated_close_cost_usd": 0.0,
        "estimated_net_pnl_usd": 0.0,
        "created_at": now - timedelta(hours=1),
        "updated_at": now,
        "next_funding_time": now + timedelta(hours=1),
        "funding_events_captured": 1,
        "status": "OPEN",
    }
    data.update(overrides)
    return PaperPosition(**data)


def make_row(notional_usd: float, spot_exit_slippage_pct: float, perp_exit_slippage_pct: float) -> OpportunityRow:
    now = datetime.now(timezone.utc)
    return OpportunityRow(
        timestamp_utc=now,
        base="MIRA",
        direction="SHORT_SPOT_LONG_PERP",
        spot_symbol="MIRA-USDT",
        perp_symbol="MIRAUSDTM",
        funding_rate_pct=-0.5,
        predicted_funding_rate_pct=-0.5,
        funding_time_utc=now + timedelta(hours=1),
        minutes_to_funding=60.0,
        spot_bid=99.8,
        spot_ask=99.9,
        perp_bid=100.1,
        perp_ask=100.2,
        basis_pct=-0.8,
        notional_usd=notional_usd,
        spot_entry_slippage_pct=0.0,
        perp_entry_slippage_pct=0.0,
        spot_exit_slippage_pct=spot_exit_slippage_pct,
        perp_exit_slippage_pct=perp_exit_slippage_pct,
        expected_edge_pct=0.1,
        round_trip_fillable=True,
        decision="ENTER_CANDIDATE",
        reason="entry_rules_passed",
        spot_entry_avg_price=100.0,
        perp_entry_avg_price=100.0,
        spot_exit_avg_price=99.9,
        perp_exit_avg_price=100.1,
        funding_interval=1.0,
    )


def make_config(root: Path) -> KucoinBasisConfig:
    return KucoinBasisConfig(
        data_dir=root / "data",
        opportunities_dir=root / "data" / "opportunities",
        paper_dir=root / "data" / "paper",
        archive_dir=root / "data" / "archive",
    )


def write_opportunities(path: Path, rows: list[OpportunityRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    csv_rows = [row.to_csv_row() for row in rows]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)


def replace_row(row: OpportunityRow, **overrides) -> OpportunityRow:
    return OpportunityRow(**{**row.__dict__, **overrides})


class DummyKucoinClient:
    def get_spot_orderbook(self, standard_symbol: str, exchange_symbol: str, limit: int = 100) -> OrderBook:
        now = datetime.now(timezone.utc)
        return OrderBook(
            exchange="kucoin",
            market_type="spot",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            bids=[OrderBookLevel(99.9, 1000.0)],
            asks=[OrderBookLevel(100.0, 1000.0)],
            observed_at_utc=now,
        )

    def get_futures_orderbook(self, standard_symbol: str, exchange_symbol: str, limit: int = 100) -> OrderBook:
        now = datetime.now(timezone.utc)
        return OrderBook(
            exchange="kucoin",
            market_type="futures",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            bids=[OrderBookLevel(100.1, 1000.0)],
            asks=[OrderBookLevel(100.2, 1000.0)],
            observed_at_utc=now,
        )


def test_gentle_unwind_chooses_best_net_pnl_pct_after_exit_slippage():
    config = KucoinBasisConfig(
        gentle_unwind_chunk_ladder_usd=(100.0, 500.0),
        estimated_exit_fee_pct=0.0,
    )
    position = make_position()
    clean_small_chunk = make_row(100.0, 0.02, 0.02)
    worse_large_chunk = make_row(500.0, 0.05, 0.05)

    selected = _choose_partial_close(
        [worse_large_chunk, clean_small_chunk],
        base="MIRA",
        direction="SHORT_SPOT_LONG_PERP",
        position=position,
        position_notional_usd=position.notional_usd,
        config=config,
    )

    assert selected is not None
    chunk, row, estimate = selected
    assert chunk == 100.0
    assert row.notional_usd == 100.0
    assert estimate.net_pnl_pct > 0


def test_funding_accrues_without_current_opportunity_row():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(
            next_funding_time=datetime.now(timezone.utc) - timedelta(minutes=90),
            funding_interval_hours=1.0,
            funding_events_captured=0,
        )

        _accrue_funding_if_crossed(position, None, store, config)

        assert position.funding_events_captured == 2
        assert position.next_funding_time is not None
        assert position.next_funding_time > datetime.now(timezone.utc)
        with store.funding_events_path.open("r", newline="", encoding="utf-8") as f:
            events = list(csv.DictReader(f))
        assert len(events) == 2
        assert sum(parse_float(row["funding_pnl_usd"], 0.0) or 0.0 for row in events) == 5.0


def test_add_position_fill_logs_added_chunk_not_running_total():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        first_path = config.opportunities_dir / "kucoin_basis_opportunities_1.csv"
        second_path = config.opportunities_dir / "kucoin_basis_opportunities_2.csv"
        first_row = make_row(100.0, 0.01, 0.01)
        second_row = make_row(250.0, 0.01, 0.01)
        second_row = OpportunityRow(
            **{
                **second_row.__dict__,
                "timestamp_utc": second_row.timestamp_utc + timedelta(minutes=1),
            }
        )
        write_opportunities(first_path, [first_row])
        write_opportunities(second_path, [second_row])

        run_paper_strategy_once(config, first_path)
        run_paper_strategy_once(config, second_path)

        store = PaperStore(config)
        with store.fills_path.open("r", newline="", encoding="utf-8") as f:
            fills = list(csv.DictReader(f))
        assert [row["event_type"] for row in fills] == ["OPEN_POSITION", "ADD_POSITION"]
        assert parse_float(fills[0]["notional_usd"]) == 100.0
        assert parse_float(fills[1]["notional_usd"]) == 250.0


def test_stale_close_row_is_not_used_for_exit_decision():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        now = datetime.now(timezone.utc)
        position = make_position(
            entry_basis_pct=-5.0,
            current_basis_pct=-5.0,
            funding_events_captured=0,
            next_funding_time=now + timedelta(hours=1),
        )
        store.write_positions({position.position_id: position})

        stale_row = replace_row(
            make_row(500.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(hours=5),
            basis_pct=-21.0,
            decision="REJECT",
            reason="basis_not_low_enough_for_short_spot",
            spot_exit_avg_price=11.345276256058835,
            perp_exit_avg_price=8.953380744056894,
        )
        fresh_row = replace_row(
            make_row(500.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            basis_pct=-5.5,
            decision="REJECT",
            reason="expected_edge_below_threshold",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [stale_row, fresh_row])

        run_paper_strategy_once(config, opportunity_path)

        positions = store.load_open_positions()
        assert position.position_id in positions
        fills = []
        if store.fills_path.exists():
            with store.fills_path.open("r", newline="", encoding="utf-8") as f:
                fills = list(csv.DictReader(f))
        assert not any(row["event_type"] == "CLOSE_POSITION" for row in fills)
        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            decisions = list(csv.DictReader(f))
        exit_decisions = [row for row in decisions if row["decision_type"] == "EXIT"]
        assert exit_decisions
        assert exit_decisions[-1]["opportunity_key"] == fresh_row.opportunity_key
        assert exit_decisions[-1]["reason"] == "hold_until_first_funding"


def test_adverse_basis_holds_instead_of_hard_exit():
    config = KucoinBasisConfig(max_basis_adverse_move_pct=5.0)
    position = make_position(
        entry_basis_pct=-5.0,
        current_basis_pct=-11.1,
        funding_events_captured=0,
    )

    should_exit, reason = _should_exit(position, make_row(500.0, 0.01, 0.01), config)

    assert should_exit is False
    assert reason == "hold_basis_moved_adversely"


def test_very_juicy_next_funding_overrides_basis_take_profit():
    config = KucoinBasisConfig(min_hold_funding_rate_pct=0.30, juicy_hold_funding_rate_pct=1.0)
    position = make_position(
        entry_basis_pct=-5.0,
        current_basis_pct=-4.0,
        funding_events_captured=1,
        estimated_net_pnl_usd=10.0,
    )
    row = replace_row(make_row(500.0, 0.01, 0.01), funding_rate_pct=-1.5)

    should_exit, reason = _should_exit(position, row, config)

    assert should_exit is False
    assert reason == "hold_for_juicy_next_funding"


def test_profitable_next_funding_holds_until_an_exit_trigger_after_funding():
    config = KucoinBasisConfig(min_hold_funding_rate_pct=0.30, juicy_hold_funding_rate_pct=1.0)
    position = make_position(
        entry_basis_pct=-5.0,
        current_basis_pct=-4.8,
        funding_events_captured=1,
        estimated_net_pnl_usd=10.0,
    )
    row = replace_row(make_row(500.0, 0.01, 0.01), funding_rate_pct=-0.5)

    should_exit, reason = _should_exit(position, row, config)

    assert should_exit is False
    assert reason == "hold_for_next_funding_and_basis"


def test_exceptional_pre_funding_profit_takes_best_partial_chunk():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(
            notional_usd=1_000.0,
            spot_qty=10.0,
            perp_qty=10.0,
            entry_basis_pct=-3.0,
            current_basis_pct=-3.0,
            funding_events_captured=0,
        )
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        row = replace_row(
            make_row(500.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            basis_pct=-0.5,
            spot_ask=99.0,
            perp_bid=101.0,
            spot_exit_avg_price=99.0,
            perp_exit_avg_price=101.0,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [row])

        run_paper_strategy_once(config, opportunity_path)

        positions = store.load_open_positions()
        assert positions[position.position_id].notional_usd == 500.0
        with store.fills_path.open("r", newline="", encoding="utf-8") as f:
            fills = list(csv.DictReader(f))
        assert fills[-1]["event_type"] == "PARTIAL_CLOSE"
        assert parse_float(fills[-1]["notional_usd"]) == 500.0
        assert fills[-1]["reason"] == "pre_funding_exceptional_take_profit"
        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            decisions = list(csv.DictReader(f))
        exit_decision = [item for item in decisions if item["decision_type"] == "EXIT"][-1]
        assert exit_decision["exit_mode"] == "pre_funding_take_profit"
        assert (parse_float(exit_decision["pre_funding_exit_profit_usd"], 0.0) or 0.0) >= 5.0
        assert (parse_float(exit_decision["foregone_funding_usd"], 0.0) or 0.0) > 0


def test_pre_funding_profit_still_holds_below_basis_improvement_threshold():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(
            notional_usd=1_000.0,
            spot_qty=10.0,
            perp_qty=10.0,
            entry_basis_pct=-3.0,
            current_basis_pct=-3.0,
            funding_events_captured=0,
        )
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        row = replace_row(
            make_row(500.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            basis_pct=-1.1,
            spot_ask=99.0,
            perp_bid=101.0,
            spot_exit_avg_price=99.0,
            perp_exit_avg_price=101.0,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [row])

        run_paper_strategy_once(config, opportunity_path)

        assert store.load_open_positions()[position.position_id].notional_usd == 1_000.0
        assert not store.fills_path.exists()
        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            decisions = list(csv.DictReader(f))
        exit_decisions = [item for item in decisions if item["decision_type"] == "EXIT"]
        assert exit_decisions[-1]["reason"] == "hold_until_first_funding"


def test_pre_funding_profit_must_beat_foregone_funding_hurdle():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(
            notional_usd=1_000.0,
            spot_qty=10.0,
            perp_qty=10.0,
            entry_basis_pct=-3.0,
            current_basis_pct=-3.0,
            funding_events_captured=0,
        )
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        row = replace_row(
            make_row(500.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            funding_rate_pct=-5.0,
            basis_pct=-0.5,
            spot_ask=99.0,
            perp_bid=101.0,
            spot_exit_avg_price=99.0,
            perp_exit_avg_price=101.0,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [row])

        run_paper_strategy_once(config, opportunity_path)

        assert store.load_open_positions()[position.position_id].notional_usd == 1_000.0
        assert not store.fills_path.exists()


def test_moderate_funding_holds_when_no_post_funding_exit_trigger_is_met():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(realised_funding_pnl_usd=0.0, funding_events_captured=1)
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        row = replace_row(
            make_row(100.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            funding_rate_pct=-0.5,
            basis_pct=-0.8,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [row])

        run_paper_strategy_once(config, opportunity_path)

        assert store.load_open_positions()[position.position_id].notional_usd == 500.0
        assert not store.fills_path.exists()
        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            decisions = list(csv.DictReader(f))
        exit_decisions = [item for item in decisions if item["decision_type"] == "EXIT"]
        assert exit_decisions[-1]["reason"] == "hold_for_next_funding_and_basis"


def test_unusually_attractive_all_in_chunk_can_unwind_with_moderate_funding():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(realised_funding_pnl_usd=4.0, funding_events_captured=1)
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        row = replace_row(
            make_row(100.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            funding_rate_pct=-0.5,
            basis_pct=-0.8,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [row])

        run_paper_strategy_once(config, opportunity_path)

        assert store.load_open_positions()[position.position_id].notional_usd == 400.0
        with store.fills_path.open("r", newline="", encoding="utf-8") as f:
            fills = list(csv.DictReader(f))
        assert fills[-1]["reason"] == "unusually_attractive_all_in_unwind"
        assert (parse_float(fills[-1]["realised_funding_pnl_usd"], 0.0) or 0.0) == 0.8


def test_large_position_can_recycle_profitable_chunk_when_funding_is_only_moderate():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(
            notional_usd=4_000.0,
            spot_qty=40.0,
            perp_qty=40.0,
            realised_funding_pnl_usd=20.0,
            funding_events_captured=1,
        )
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        row = replace_row(
            make_row(100.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            funding_rate_pct=-0.4,
            basis_pct=-0.8,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [row])

        run_paper_strategy_once(config, opportunity_path)

        assert store.load_open_positions()[position.position_id].notional_usd == 3_900.0
        with store.fills_path.open("r", newline="", encoding="utf-8") as f:
            fills = list(csv.DictReader(f))
        assert fills[-1]["reason"] == "capital_recycle_profitable_unwind"
        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            decisions = list(csv.DictReader(f))
        exit_decisions = [item for item in decisions if item["decision_type"] == "EXIT"]
        assert exit_decisions[-1]["capital_recycle_triggered"] == "True"


def test_capital_recycle_rejects_excessive_exit_cost():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(
            notional_usd=4_000.0,
            spot_qty=40.0,
            perp_qty=40.0,
            realised_funding_pnl_usd=52.0,
            funding_events_captured=1,
        )
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        row = replace_row(
            make_row(100.0, 0.5, 0.5),
            timestamp_utc=now - timedelta(seconds=30),
            funding_rate_pct=-0.4,
            basis_pct=-0.8,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [row])

        run_paper_strategy_once(config, opportunity_path)

        assert store.load_open_positions()[position.position_id].notional_usd == 4_000.0
        assert not store.fills_path.exists()


def test_juicy_funding_blocks_even_an_unusually_attractive_exit():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(realised_funding_pnl_usd=50.0, funding_events_captured=1)
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        row = replace_row(
            make_row(100.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            funding_rate_pct=-1.2,
            basis_pct=-0.2,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [row])

        run_paper_strategy_once(config, opportunity_path)

        assert store.load_open_positions()[position.position_id].notional_usd == 500.0
        assert not store.fills_path.exists()
        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            decisions = list(csv.DictReader(f))
        exit_decisions = [item for item in decisions if item["decision_type"] == "EXIT"]
        assert exit_decisions[-1]["reason"] == "hold_for_juicy_next_funding"


def test_dashboard_unwind_status_matches_moderate_funding_triggers():
    config = KucoinBasisConfig()
    row = replace_row(make_row(100.0, 0.01, 0.01), funding_rate_pct=-0.5, basis_pct=-0.8)

    holding = _position_unwind_estimates(
        make_position(realised_funding_pnl_usd=0.0, funding_events_captured=1),
        [row],
        config,
    )
    attractive = _position_unwind_estimates(
        make_position(realised_funding_pnl_usd=4.0, funding_events_captured=1),
        [row],
        config,
    )

    assert holding["next_unwind_status"] == "hold for funding/basis"
    assert attractive["next_unwind_status"] == "strong all-in unwind"


def test_profitable_post_funding_unwind_closes_best_chunk_only():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(
            notional_usd=500.0,
            realised_funding_pnl_usd=5.0,
            funding_events_captured=1,
        )
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        small = replace_row(
            make_row(100.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            basis_pct=-0.3,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        large = replace_row(
            make_row(500.0, 0.05, 0.05),
            timestamp_utc=now - timedelta(seconds=30),
            basis_pct=-0.3,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [large, small])

        run_paper_strategy_once(config, opportunity_path)

        positions = store.load_open_positions()
        assert positions[position.position_id].notional_usd == 400.0
        with store.fills_path.open("r", newline="", encoding="utf-8") as f:
            fills = list(csv.DictReader(f))
        assert fills[-1]["event_type"] == "PARTIAL_CLOSE"
        assert parse_float(fills[-1]["notional_usd"]) == 100.0
        assert fills[-1]["reason"] == "basis_converged_take_profit"
        assert parse_float(fills[-1]["realised_basis_pnl_usd"]) > 0
        assert parse_float(fills[-1]["realised_funding_pnl_usd"]) == 1.0
        assert 0 < parse_float(fills[-1]["realised_pnl_usd"]) < parse_float(fills[-1]["realised_funding_pnl_usd"])
        assert positions[position.position_id].realised_funding_pnl_usd == 4.0


def test_post_close_cooldown_blocks_same_loop_reentry():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(
            notional_usd=500.0,
            realised_funding_pnl_usd=5.0,
            funding_events_captured=1,
        )
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        row = replace_row(
            make_row(100.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            funding_rate_pct=-0.1,
            decision="ENTER_CANDIDATE",
            reason="entry_rules_passed",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [row])

        run_paper_strategy_once(config, opportunity_path)

        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            decisions = list(csv.DictReader(f))
        entry_decisions = [item for item in decisions if item["decision_type"] == "ENTRY"]
        assert entry_decisions
        assert entry_decisions[-1]["allowed"] == "False"
        assert entry_decisions[-1]["reason"] == "post_close_reentry_cooldown"


def test_profitable_carry_unwind_requires_profit_excluding_funding():
    config = KucoinBasisConfig(min_hold_funding_rate_pct=0.30, juicy_hold_funding_rate_pct=1.0)
    position = make_position(
        notional_usd=500.0,
        realised_funding_pnl_usd=50.0,
        funding_events_captured=1,
        spot_qty=5.0,
        perp_qty=5.0,
    )
    lossy_row = replace_row(
        make_row(100.0, 0.01, 0.01),
        spot_exit_avg_price=100.1,
        perp_exit_avg_price=99.9,
        spot_ask=100.1,
        perp_bid=99.9,
        expected_edge_pct=0.5,
    )

    selected = _choose_partial_close(
        [lossy_row],
        base="MIRA",
        direction="SHORT_SPOT_LONG_PERP",
        position=position,
        position_notional_usd=position.notional_usd,
        config=config,
        require_ex_funding_profit=True,
    )

    assert selected is None


def test_weak_funding_exit_harvests_profitable_100_usd_chunk_all_in():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(
            notional_usd=500.0,
            realised_funding_pnl_usd=50.0,
            funding_events_captured=1,
            spot_qty=5.0,
            perp_qty=5.0,
        )
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        lossy_full_row = replace_row(
            make_row(500.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            funding_rate_pct=-0.1,
            spot_exit_avg_price=101.0,
            perp_exit_avg_price=99.0,
            spot_ask=101.0,
            perp_bid=99.0,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [lossy_full_row])

        run_paper_strategy_once(config, opportunity_path)

        positions = store.load_open_positions()
        assert positions[position.position_id].notional_usd == 400.0
        assert positions[position.position_id].realised_funding_pnl_usd == 40.0
        with store.fills_path.open("r", newline="", encoding="utf-8") as f:
            fills = list(csv.DictReader(f))
        assert fills[-1]["event_type"] == "PARTIAL_CLOSE"
        assert parse_float(fills[-1]["notional_usd"]) == 100.0
        assert fills[-1]["reason"] == "funding_harvest_profitable_unwind"
        assert parse_float(fills[-1]["realised_pnl_usd"]) < 0
        assert parse_float(fills[-1]["realised_funding_pnl_usd"]) == 10.0
        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            decisions = list(csv.DictReader(f))
        exit_decisions = [row for row in decisions if row["decision_type"] == "EXIT"]
        assert exit_decisions[-1]["allowed"] == "True"
        assert exit_decisions[-1]["reason"] == "funding_captured_next_funding_below_threshold"


def test_basis_target_can_harvest_funding_when_trade_pnl_is_still_negative():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(
            notional_usd=500.0,
            realised_funding_pnl_usd=50.0,
            funding_events_captured=1,
            spot_qty=5.0,
            perp_qty=5.0,
            entry_basis_pct=-1.0,
            current_basis_pct=-1.0,
        )
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        lossy_row = replace_row(
            make_row(100.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            funding_rate_pct=-0.5,
            basis_pct=-0.3,
            spot_exit_avg_price=101.0,
            perp_exit_avg_price=99.0,
            spot_ask=101.0,
            perp_bid=99.0,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [lossy_row])

        run_paper_strategy_once(config, opportunity_path)

        positions = store.load_open_positions()
        assert positions[position.position_id].notional_usd == 400.0
        with store.fills_path.open("r", newline="", encoding="utf-8") as f:
            fills = list(csv.DictReader(f))
        assert fills[-1]["reason"] == "funding_harvest_profitable_unwind"
        assert (parse_float(fills[-1]["realised_pnl_usd"], 0.0) or 0.0) < 0
        assert (parse_float(fills[-1]["realised_funding_pnl_usd"], 0.0) or 0.0) == 10.0


def test_weak_funding_exit_still_holds_when_harvest_chunk_not_profitable_all_in():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        position = make_position(
            notional_usd=500.0,
            realised_funding_pnl_usd=1.0,
            funding_events_captured=1,
            spot_qty=5.0,
            perp_qty=5.0,
        )
        store.write_positions({position.position_id: position})
        now = datetime.now(timezone.utc)
        lossy_full_row = replace_row(
            make_row(500.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            funding_rate_pct=-0.1,
            spot_exit_avg_price=101.0,
            perp_exit_avg_price=99.0,
            spot_ask=101.0,
            perp_bid=99.0,
            decision="REJECT",
            reason="open_position_watchlist",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [lossy_full_row])

        run_paper_strategy_once(config, opportunity_path)

        positions = store.load_open_positions()
        assert positions[position.position_id].notional_usd == 500.0
        assert not store.fills_path.exists()
        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            decisions = list(csv.DictReader(f))
        exit_decisions = [row for row in decisions if row["decision_type"] == "EXIT"]
        assert exit_decisions[-1]["allowed"] == "False"
        assert exit_decisions[-1]["reason"] == "exit_wanted_no_profitable_chunk"


def test_summary_totals_do_not_double_count_open_funding():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        now = datetime.now(timezone.utc)
        position = make_position(
            notional_usd=100.0,
            realised_funding_pnl_usd=5.0,
            unrealised_basis_pnl_usd=-3.0,
            estimated_close_cost_usd=1.0,
            estimated_net_pnl_usd=1.0,
            funding_events_captured=1,
        )
        store.write_positions({position.position_id: position})
        store.append_funding_event(
            {
                "timestamp_utc": now.isoformat(),
                "position_id": position.position_id,
                "base": position.base,
                "direction": position.direction,
                "perp_symbol": position.perp_symbol,
                "funding_time_utc": now.isoformat(),
                "funding_rate_pct": "-5.00000000",
                "notional_usd": "100.00000000",
                "funding_pnl_usd": "5.00000000",
            }
        )
        store.append_fill(
            {
                "timestamp_utc": now.isoformat(),
                "event_type": "PARTIAL_CLOSE",
                "position_id": position.position_id,
                "base": position.base,
                "direction": position.direction,
                "spot_symbol": position.spot_symbol,
                "perp_symbol": position.perp_symbol,
                "notional_usd": "100.00000000",
                "spot_price": "1",
                "perp_price": "1",
                "fees_usd": "1.00000000",
                "realised_pnl_usd": "-2.00000000",
                "realised_basis_pnl_usd": "-1.00000000",
                "realised_funding_pnl_usd": "5.00000000",
                "reason": "test",
            }
        )

        payload = load_summary_payload(config)

        assert payload["estimatedOpenPnlExFundingUsd"] == -4.0
        assert payload["totalRealisedPnlUsd"] == 3.0
        assert payload["totalPnlInclOpenUsd"] == -1.0


def test_decision_schema_upgrade_preserves_legacy_rows():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        store.data_dir.mkdir(parents=True, exist_ok=True)
        legacy_fields = DECISION_FIELDS[:16]
        with store.decisions_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=legacy_fields)
            writer.writeheader()
            writer.writerow({
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "decision_type": "EXIT",
                "base": "MIRA",
                "direction": "SHORT_SPOT_LONG_PERP",
                "reason": "legacy_reason",
            })

        store.append_decision({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "decision_type": "EXIT",
            "base": "MIRA",
            "direction": "SHORT_SPOT_LONG_PERP",
            "reason": "new_reason",
            "exit_mode": "basis_target",
            "basis_target_reached": "True",
        })

        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert reader.fieldnames == DECISION_FIELDS
        assert [row["reason"] for row in rows] == ["legacy_reason", "new_reason"]
        assert rows[0]["exit_mode"] == ""
        assert rows[1]["exit_mode"] == "basis_target"


def test_open_position_watchlist_rows_are_scanned_even_without_entry_shortlist():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        pair = SymbolPair(base="MIRA", spot_symbol="MIRA-USDT", perp_symbol="MIRAUSDTM")
        now = datetime.now(timezone.utc)
        contracts = {
            "MIRAUSDTM": {
                "symbol": "MIRAUSDTM",
                "fundingFeeRate": "0.001",
                "predictedFundingFeeRate": "0.001",
                "nextFundingRateDateTime": int((now + timedelta(hours=1)).timestamp() * 1000),
                "currentFundingRateGranularity": 3600000,
            }
        }

        rows = scan_pair(
            DummyKucoinClient(),
            config,
            pair,
            contracts,
            now,
            {"MIRA": {"SHORT_SPOT_LONG_PERP": {100.0, 500.0}}},
        )

        watch_rows = [row for row in rows if row.direction == "SHORT_SPOT_LONG_PERP"]
        assert {row.notional_usd for row in watch_rows} >= {100.0, 500.0}
        assert all(row.reason == "open_position_watchlist" for row in watch_rows)


def test_adverse_basis_with_weak_funding_tries_profitable_unwind():
    config = KucoinBasisConfig(min_hold_funding_rate_pct=0.30)
    position = make_position(
        entry_basis_pct=-5.0,
        current_basis_pct=-11.1,
        funding_events_captured=1,
    )
    row = replace_row(make_row(500.0, 0.01, 0.01), funding_rate_pct=-0.1)

    should_exit, reason = _should_exit(position, row, config)

    assert should_exit is True
    assert reason == "funding_weak_basis_adverse_try_unwind"


def test_adverse_basis_existing_position_blocks_add():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        now = datetime.now(timezone.utc)
        position = make_position(
            entry_basis_pct=-5.0,
            current_basis_pct=-11.1,
            funding_events_captured=0,
            next_funding_time=now + timedelta(hours=1),
        )
        store.write_positions({position.position_id: position})
        row = replace_row(
            make_row(100.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            basis_pct=-11.1,
            decision="ENTER_CANDIDATE",
            reason="entry_rules_passed",
        )
        opportunity_path = config.opportunities_dir / "kucoin_basis_opportunities_test.csv"
        write_opportunities(opportunity_path, [row])

        run_paper_strategy_once(config, opportunity_path)

        fills = []
        if store.fills_path.exists():
            with store.fills_path.open("r", newline="", encoding="utf-8") as f:
                fills = list(csv.DictReader(f))
        assert not fills
        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            decisions = list(csv.DictReader(f))
        entry_decisions = [row for row in decisions if row["decision_type"] == "ENTRY"]
        assert entry_decisions
        assert entry_decisions[-1]["allowed"] == "False"
        assert entry_decisions[-1]["reason"] == "hold_basis_moved_adversely"


def test_volatility_cooldown_blocks_reentry_for_60_minutes():
    with TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        store = PaperStore(config)
        now = datetime.now(timezone.utc)
        volatile_row = replace_row(
            make_row(100.0, 0.01, 0.01),
            timestamp_utc=now - timedelta(seconds=30),
            basis_std_pct=6.0,
            decision="ENTER_CANDIDATE",
            reason="entry_rules_passed",
        )
        first_path = config.opportunities_dir / "kucoin_basis_opportunities_1.csv"
        write_opportunities(first_path, [volatile_row])

        run_paper_strategy_once(config, first_path)

        assert store.load_active_cooldowns(datetime.now(timezone.utc))
        calm_row = replace_row(
            make_row(100.0, 0.01, 0.01),
            timestamp_utc=datetime.now(timezone.utc) - timedelta(seconds=30),
            basis_std_pct=0.1,
            basis_trend_pct=0.1,
            decision="ENTER_CANDIDATE",
            reason="entry_rules_passed",
        )
        second_path = config.opportunities_dir / "kucoin_basis_opportunities_2.csv"
        write_opportunities(second_path, [calm_row])

        run_paper_strategy_once(config, second_path)

        fills = []
        if store.fills_path.exists():
            with store.fills_path.open("r", newline="", encoding="utf-8") as f:
                fills = list(csv.DictReader(f))
        assert not fills
        with store.decisions_path.open("r", newline="", encoding="utf-8") as f:
            decisions = list(csv.DictReader(f))
        entry_decisions = [row for row in decisions if row["decision_type"] == "ENTRY"]
        assert entry_decisions[-1]["allowed"] == "False"
        assert entry_decisions[-1]["reason"] == "basis_too_volatile_no_entry"


if __name__ == "__main__":
    test_gentle_unwind_chooses_best_net_pnl_pct_after_exit_slippage()
    test_funding_accrues_without_current_opportunity_row()
    test_add_position_fill_logs_added_chunk_not_running_total()
    test_stale_close_row_is_not_used_for_exit_decision()
    test_adverse_basis_holds_instead_of_hard_exit()
    test_very_juicy_next_funding_overrides_basis_take_profit()
    test_profitable_next_funding_holds_until_an_exit_trigger_after_funding()
    test_exceptional_pre_funding_profit_takes_best_partial_chunk()
    test_pre_funding_profit_still_holds_below_basis_improvement_threshold()
    test_pre_funding_profit_must_beat_foregone_funding_hurdle()
    test_moderate_funding_holds_when_no_post_funding_exit_trigger_is_met()
    test_unusually_attractive_all_in_chunk_can_unwind_with_moderate_funding()
    test_large_position_can_recycle_profitable_chunk_when_funding_is_only_moderate()
    test_capital_recycle_rejects_excessive_exit_cost()
    test_juicy_funding_blocks_even_an_unusually_attractive_exit()
    test_dashboard_unwind_status_matches_moderate_funding_triggers()
    test_profitable_post_funding_unwind_closes_best_chunk_only()
    test_post_close_cooldown_blocks_same_loop_reentry()
    test_profitable_carry_unwind_requires_profit_excluding_funding()
    test_weak_funding_exit_harvests_profitable_100_usd_chunk_all_in()
    test_basis_target_can_harvest_funding_when_trade_pnl_is_still_negative()
    test_weak_funding_exit_still_holds_when_harvest_chunk_not_profitable_all_in()
    test_summary_totals_do_not_double_count_open_funding()
    test_decision_schema_upgrade_preserves_legacy_rows()
    test_open_position_watchlist_rows_are_scanned_even_without_entry_shortlist()
    test_adverse_basis_with_weak_funding_tries_profitable_unwind()
    test_adverse_basis_existing_position_blocks_add()
    test_volatility_cooldown_blocks_reentry_for_60_minutes()
    print("kucoin basis strategy tests passed")
