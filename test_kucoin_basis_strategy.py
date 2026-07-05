from datetime import datetime, timedelta, timezone

from kucoin_basis.config import KucoinBasisConfig
from kucoin_basis.models import OpportunityRow
from kucoin_basis.paper_models import PaperPosition
from kucoin_basis.paper_strategy import _choose_partial_close


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


if __name__ == "__main__":
    test_gentle_unwind_chooses_best_net_pnl_pct_after_exit_slippage()
    print("kucoin basis strategy tests passed")
