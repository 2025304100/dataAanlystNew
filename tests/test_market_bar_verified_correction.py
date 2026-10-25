"""Guarded local corrections use the caller's isolated SQL transaction."""
from datetime import date, datetime
import json

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.market_bar_correction import (
    VerifiedBarCorrection, apply_verified_bar_corrections, bar_snapshot,
)
from app.services.market_data import _upsert_bars
from app.services.market_history_contract import history_source_tag
from app.services.universe_sync import _upsert_universe_bars

pytestmark = pytest.mark.whitebox


def seed(db):
    symbol = Symbol(symbol="601168", name="证据测试", asset_type="stock", market="sh")
    universe = UniverseSymbol(symbol="601168", name="证据测试", asset_type="stock", market="sh", region="cn")
    db.add_all([symbol, universe]); db.flush()
    bars = [DailyBar(symbol_id=symbol.id), UniverseDailyBar(universe_symbol_id=universe.id)]
    for bar in bars:
        bar.trade_date = date(2026, 8, 7)
        bar.open, bar.high, bar.low, bar.close = 27.70, 27.74, 26.12, 26.37
        bar.volume, bar.amount, bar.turnover_rate = 575533, 1541975676, 2.42
        bar.source, bar.created_at = "akshare", datetime(2026, 8, 7, 18)
    db.add_all(bars); db.commit()
    return bars


def replacements(bars):
    value = dict(open=40.13, high=43.29, low=39.44, close=43.07,
        volume=111038800, amount=4656185449, turnover_rate=4.66,
        source="verified:em:cn-stock:v1")
    return [VerifiedBarCorrection(bar.__tablename__, bar.id, bar_snapshot(bar), value.copy()) for bar in bars]


def evidence():
    return {"responses": [{"path": "frozen-provider.response", "sha256": "a" * 64}],
        "volume_unit": "share", "amount_unit": "CNY", "turnover_unit": "percentage_point",
        "adjustment_limit": "Current qfq/raw agree; historical as-of is not authenticated"}


def apply(db, plan, ev=None):
    return apply_verified_bar_corrections(db, plan, operator_id="local_user",
        correlation_id="isolated-correction", evidence=evidence() if ev is None else ev)


def audits(db):
    from app.models.data_governance_audit import DataGovernanceAuditEvent
    return db.execute(select(DataGovernanceAuditEvent).where(
        DataGovernanceAuditEvent.correlation_id == "isolated-correction")).scalars().all()


def test_verified_correction_preserves_keys_and_records_stored_before_after(db_session):
    bars = seed(db_session); old = [bar_snapshot(b) for b in bars]
    ids = apply(db_session, replacements(bars))
    assert len(ids) == 2
    events = audits(db_session)
    assert len(events) == 2 and all(e.action == "MARKET_BAR_CORRECTION" for e in events)
    for row, before, audit in zip(bars, old, events):
        assert row.trade_date == date(2026, 8, 7) and row.id == before["id"]
        assert row.close == 43.07 and row.volume == 111038800 and row.turnover_rate == 4.66
        assert row.created_at > datetime(2026, 8, 7, 18)
        assert json.loads(audit.before_json) == before
        assert json.loads(audit.after_json) == bar_snapshot(row)
        assert json.loads(audit.attributes_json)["volume_unit"] == "share"


def test_bar_and_receipt_rollback_together_without_hidden_commit(db_session):
    bars = seed(db_session); before = [bar_snapshot(b) for b in bars]
    apply(db_session, replacements(bars))
    assert len(audits(db_session)) == 2
    db_session.rollback()
    assert [bar_snapshot(b) for b in bars] == before and audits(db_session) == []


def test_changed_second_row_rejects_whole_plan_before_writes(db_session):
    bars = seed(db_session); plan = replacements(bars)
    bars[1].close = 26.50; db_session.commit()
    with pytest.raises(ValueError, match="precondition changed"):
        apply(db_session, plan)
    assert bars[0].close == 26.37 and bars[1].close == 26.50 and audits(db_session) == []


def test_locked_precondition_refreshes_stale_identity_map(db_session):
    from sqlalchemy import update
    bars = seed(db_session); plan = replacements(bars)
    with db_session.get_bind().begin() as other:
        other.execute(update(UniverseDailyBar).where(UniverseDailyBar.id == bars[1].id).values(close=26.50))
    assert bars[1].close == 26.37  # The caller's identity map is deliberately stale.
    with pytest.raises(ValueError, match="precondition changed"):
        apply(db_session, plan)
    assert bars[0].close == 26.37 and audits(db_session) == []


@pytest.mark.parametrize("bad", ["identity", "nan", "ordering", "duplicate", "unverified_units"])
def test_unsafe_or_unreviewed_plan_is_rejected(db_session, bad):
    bars = seed(db_session); plan = replacements(bars); ev = evidence()
    if bad == "identity":
        plan[0].replacement["trade_date"] = "2026-06-26"
    elif bad == "nan":
        plan[0].replacement["amount"] = float("nan")
    elif bad == "ordering":
        plan[0].replacement["low"] = 45
    elif bad == "duplicate":
        plan.append(plan[0])
    else:
        ev["volume_unit"] = "infer_from_magnitude"
    with pytest.raises(ValueError):
        apply(db_session, plan, ev)
    assert bars[0].close == 26.37 and audits(db_session) == []


@pytest.mark.parametrize("provider,endpoint,tag", [
    ("eastmoney", "stock_zh_a_hist", "akshare:em:cn-stock:v1"),
    ("sina", "stock_zh_a_daily", "akshare:sina:cn-stock:v1"),
    ("tencent", "stock_zh_a_hist_tx", "akshare:tx:cn-stock:v1"),
])
def test_unit_contract_survives_both_sql_writers(db_session, provider, endpoint, tag):
    bars = seed(db_session)
    # This case updates the same verified source; source replacement remains separately forbidden.
    for bar in bars:
        bar.source = tag
    db_session.commit()
    frame = pd.DataFrame([dict(trade_date=date(2026, 8, 7), open=40.13, high=43.29,
        low=39.44, close=43.07, volume=111038800, amount=None, turnover_rate=None)])
    frame.attrs["history_contract"] = dict(provider=provider, endpoint=endpoint,
        version="cn-stock-shares-v1", volume_unit="share", amount_unit="CNY", turnover_unit="percentage_point")
    assert _upsert_bars(db_session, bars[0].symbol_ref, frame) == (0, 1)
    assert _upsert_universe_bars(db_session, bars[1].universe_symbol_id, frame) == (0, 1)
    db_session.flush(); db_session.expire_all()
    assert all(b.source == tag and b.volume == 111038800 and b.amount == 1541975676 and b.turnover_rate == 2.42 for b in bars)


def test_unmarked_history_never_claims_new_contract_and_bad_marker_fails_closed(db_session):
    bars = seed(db_session)
    frame = pd.DataFrame([dict(trade_date=date(2026, 8, 7), open=10, high=11, low=9, close=10)])
    assert history_source_tag(frame) == "akshare"
    frame.attrs["history_contract"] = {"provider": "eastmoney", "volume_unit": "lot"}
    for writer, identity in [(_upsert_bars, bars[0].symbol_ref), (_upsert_universe_bars, bars[1].universe_symbol_id)]:
        with pytest.raises(ValueError, match="contract"):
            writer(db_session, identity, frame)
    assert bars[0].close == 26.37 and bars[1].close == 26.37
