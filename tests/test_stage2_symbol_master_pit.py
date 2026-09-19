"""Stage-2 PIT checks for security master data."""
from datetime import date, datetime
from types import SimpleNamespace

from app.models.symbol import Symbol
from app.services.decision_engine import (
    RiskAllocationResult,
    ScoredUniverse,
    SignalResult,
    UniverseAndEligibility,
    _annotate_symbol_master_pit,
    _annotate_member_effective_pit,
    _default_build_evidence,
)


def test_symbol_not_listed_on_decision_date_is_annotated(db_session):
    symbol = Symbol(
        symbol="600901",
        name="Future Listing",
        asset_type="stock",
        market="SH",
        listed_at=date(2026, 2, 1),
    )
    db_session.add(symbol)
    db_session.commit()
    member = {"symbol_id": symbol.id}

    _annotate_symbol_master_pit(db_session, [member], date(2026, 1, 31))

    assert member["_master_data_issue"] == "NOT_LISTED_ON_TRADE_DATE"
    assert "listed_at" in member["_master_data_issue_detail"]


def test_missing_symbol_master_is_kept_for_audit(db_session):
    member = {"symbol_id": 987654321}

    _annotate_symbol_master_pit(db_session, [member], date(2026, 1, 31))

    assert member["_master_data_issue"] == "SYMBOL_MASTER_MISSING"


def test_master_data_issue_emits_zero_adjustment_evidence():
    member = {
        "symbol_id": 77,
        "current_quantity": 100.0,
        "current_position_pct": 0.01,
        "_master_data_issue": "NOT_LISTED_ON_TRADE_DATE",
        "_master_data_issue_detail": "listed_at=2026-02-01 > trade_date=2026-01-31",
    }
    snap = SimpleNamespace(
        cost_config={"min_lot_size": 100, "slippage_buy_bps": 5, "slippage_sell_bps": 5},
        versions={},
    )
    clock = SimpleNamespace(
        data_cutoff_at=datetime(2026, 1, 31, 7, 0),
        execution_at=datetime(2026, 2, 1, 1, 30),
        decision_at=datetime(2026, 1, 31, 7, 5),
    )
    evidence = _default_build_evidence(
        snap=snap,
        clock=clock,
        universe=UniverseAndEligibility(universe=[member], universe_count=1, member_count=1),
        scored=ScoredUniverse(items=[], expected=1, actual=0, coverage_pct=0.0),
        signal=SignalResult(items=[]),
        alloc=RiskAllocationResult(items=[{
            "symbol_id": 77,
            "direction": "BUY",
            "target_quantity": 500.0,
            "target_position_pct": 0.05,
            "executed_price": 10.0,
        }]),
        blocking_status="READY",
        blocking_reasons=[],
        price_data_by_symbol={77: {
            "open_price": 10.0,
            "close_price": 10.0,
            "high_price": 10.0,
            "low_price": 10.0,
            "volume": 1000,
            "is_suspended_today": False,
        }},
        trade_date=date(2026, 1, 31),
    )

    row = evidence[0]
    assert row.action == "DATA_BLOCKED"
    assert row.action_subtype == "NOT_LISTED_ON_TRADE_DATE"
    assert row.rejection_reason == "NOT_LISTED_ON_TRADE_DATE"
    assert row.target_quantity == 100.0
    assert row.target_qty_delta == 0.0
    assert row.executed_price is None


def test_historical_member_interval_blocks_new_buy_but_keeps_held_exit():
    members = [
        {
            "member_id": 1,
            "symbol_id": 11,
            "effective_from": "2026-02-01T00:00:00",
            "effective_to": None,
        },
        {
            "member_id": 2,
            "symbol_id": 12,
            "effective_from": "2026-01-01T00:00:00",
            "effective_to": "2026-01-15T00:00:00",
        },
    ]

    _annotate_member_effective_pit(
        members,
        date(2026, 1, 20),
        held_symbol_ids={12},
    )

    assert members[0]["_membership_data_issue"] == "MEMBER_NOT_EFFECTIVE_ON_TRADE_DATE"
    assert members[1]["_membership_exit_only"] is True
    assert "_membership_data_issue" not in members[1]
