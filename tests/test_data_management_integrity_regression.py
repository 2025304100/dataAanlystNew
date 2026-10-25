from datetime import date
import json
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import func, select

from app.models.async_task import AsyncTaskRecord
from app.models.daily_bar import DailyBar
from app.models.financial_report import StockFinancialReport
from app.models.index_price import IndexPrice
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services import async_tasks, fundamental_data, index_data, market_data, universe_sync, valuation_provider
from app.services.source_field_integrity import get_source_field_integrity
from app.services.task_outcomes import external_sync_result


def test_valuation_partial_observations_preserve_existing_key_fields_in_both_paths(db_session, monkeypatch):
    symbol = Symbol(symbol="600000", name="test", market="sh", asset_type="stock")
    db_session.add(symbol)
    db_session.flush()
    day = date.today()
    existing = StockValuation(symbol_id=symbol.id, trade_date=day, pe_ttm=10, pb=2,
        total_market_cap=1000, circulating_market_cap=800, industry="银行",
        industry_pe_percentile=20, pe_history_percentile=30, pe_score=50, source="original")
    db_session.add(existing)
    db_session.commit()
    identity = (existing.id, existing.symbol_id, existing.trade_date, existing.created_at)
    payload = {"symbol": "600000", "trade_date": day, "pe_ttm": 15,
               "pb": None, "total_market_cap": float("nan"), "circulating_market_cap": None}
    monkeypatch.setattr("app.services.market_session.is_cn_trading_day", lambda *a: True)
    monkeypatch.setattr(valuation_provider, "fetch_valuation_rows", lambda *a, **k: [payload])
    for sync in (
        lambda: fundamental_data._upsert_valuation_history_row(db_session, symbol, payload, industry=None),
        lambda: fundamental_data.sync_market_valuation_snapshot(db_session, [symbol], day),
    ):
        sync()
        db_session.flush()
        assert db_session.scalar(select(func.count()).select_from(StockValuation)) == 1
        assert (existing.id, existing.symbol_id, existing.trade_date, existing.created_at) == identity
        assert existing.pe_ttm == 15
        assert (existing.pb, existing.total_market_cap, existing.circulating_market_cap) == (2, 1000, 800)
        assert (existing.industry, existing.industry_pe_percentile, existing.pe_history_percentile) == ("银行", 20, 30)


def test_index_bad_rows_never_zero_or_clear_existing_fields(db_session, monkeypatch):
    day = date(2026, 10, 23)
    existing = IndexPrice(symbol="000001", trade_date=day, open=100, high=110, low=90,
                          close=105, volume=1000, amount=2000, source="original")
    db_session.add(existing)
    db_session.commit()
    before = {column.name: getattr(existing, column.name) for column in IndexPrice.__table__.columns}
    valid = dict(trade_date=day, open=100, high=110, low=90, close=105)
    for changes in ({"open": None}, {"close": float("nan")}, {"high": 95},
                    {"low": float("inf")}, {"close": 0}, {"trade_date": date(2026, 10, 22)}):
        frame = pd.DataFrame([{**valid, **changes}])
        monkeypatch.setattr(index_data, "_fetch_index_daily", lambda *a, **k: (frame, "sina"))
        with pytest.raises(ValueError):
            index_data.sync_index_daily(db_session, "000001", start_date=day, end_date=day)
        db_session.refresh(existing)
        assert {column.name: getattr(existing, column.name) for column in IndexPrice.__table__.columns} == before
        assert db_session.scalar(select(func.count()).select_from(IndexPrice)) == 1
    # Optional columns missing from a valid fallback never erase old amount/volume.
    monkeypatch.setattr(index_data, "_fetch_index_daily", lambda *a, **k: (pd.DataFrame([valid]), "sina"))
    assert index_data.sync_index_daily(db_session, "000001", start_date=day, end_date=day).written == 1
    assert (existing.volume, existing.amount) == (1000, 2000)
    for symbol, prefixed in (("000001", "sh000001"), ("000010", "sh000010"), ("399001", "sz399001")):
        assert index_data._to_prefixed_symbol(symbol) == prefixed
    for symbol in ("sh000001", "AAPL", "000001;DELETE", "１２３４５６"):
        with pytest.raises(ValueError):
            index_data.sync_index_daily(db_session, symbol)


def test_source_integrity_exposes_financial_and_etf_blind_spots_without_writes(db_session):
    day = date(2026, 10, 23)
    symbol = Symbol(symbol="600000", name="test", market="sh", asset_type="stock")
    etf = UniverseSymbol(symbol="510300", name="test", market="sh", region="cn", asset_type="etf", last_bar_date=day)
    db_session.add_all([symbol, etf])
    db_session.flush()
    db_session.add(StockFinancialReport(symbol_id=symbol.id, report_period=date(2026, 6, 30),
        announcement_date=day, report_type="interim", roe_ttm=8, net_profit=100, revenue=200, source="test"))
    db_session.add(UniverseDailyBar(universe_symbol_id=etf.id, trade_date=day, open=4,
        high=5, low=3, close=4.2, amount=100, volume=20, source="test"))
    db_session.commit()
    models = (StockFinancialReport, UniverseDailyBar, StockValuation, IndexPrice)
    before = {model.__name__: db_session.execute(select(model.__table__)).all() for model in models}
    report = get_source_field_integrity(db_session)
    fields = {(row["table"], row["field"]): row for row in report["fields"]}
    assert fields["stock_financial_reports", "net_profit"]["nonnull"] == 1
    assert fields["stock_financial_reports", "net_profit_yoy"]["missing"] == 1
    assert fields["universe_daily_bars", "turnover_rate"]["missing"] == 1
    assert fields["index_prices", "amount"]["coverage_pct"] is None  # empty is unknown, not 100%
    assert {model.__name__: db_session.execute(select(model.__table__)).all() for model in models} == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_external_outcomes_do_not_change_retry_state_or_original_results():
    task = SimpleNamespace(task_type="external_sync_fundamental", status="failed", ok_count=5549,
                           failed_count=6, processed=5555, total=5555)
    original = {"success": 5549, "failed": 6, "skipped": 0, "records": 5549}
    result = external_sync_result(task, original)
    assert result["outcome"] == "partial_success"
    assert task.status == "failed" and "outcome" not in original
    task.status = "done"
    result = external_sync_result(task, {"total": 1642, "success": 11, "failed": 0, "skipped": 1631})
    assert result["skip_ratio"] > .99
    assert external_sync_result(task, {"success": 0, "failed": 0, "skipped": 1642})["outcome"] == "skipped"


def test_selected_capital_gaps_do_not_expand_to_northbound_or_other_dates(monkeypatch):
    from app.services import external_data_sync_task as tasks

    captured = {}
    monkeypatch.setattr(tasks, "start_external_data_sync", lambda dataset, payload: captured.update(dataset=dataset, **payload))
    tasks.start_external_gap_repair("capital_flow", start_date=date(2026, 10, 14), end_date=date(2026, 10, 16),
        gaps=[{"symbol_id": 3276, "symbol": "600519", "trade_date": date(2026, 10, 14)},
              {"symbol_id": 3276, "symbol": "600519", "trade_date": date(2026, 10, 16)}])
    assert captured["include_northbound"] is False
    assert captured["sync_policy"] == "missing"
    assert captured["_selected_gap_dates"] == {"3276": ["2026-10-14", "2026-10-16"]}


def test_market_and_universe_upserts_preserve_optional_fields_and_reject_bad_batch(db_session):
    day = date(2026, 10, 23)
    symbol = Symbol(symbol="600000", name="test", market="sh", asset_type="stock")
    universe = UniverseSymbol(symbol="600000", name="test", market="sh", region="cn", asset_type="stock")
    db_session.add_all([symbol, universe])
    db_session.flush()
    seed = dict(trade_date=day, open=10, high=12, low=9, close=11, volume=1000, amount=11000, turnover_rate=2)
    business = DailyBar(symbol_id=symbol.id, **seed)
    base = UniverseDailyBar(universe_symbol_id=universe.id, **seed)
    db_session.add_all([business, base])
    db_session.commit()
    valid = {"trade_date": day, "open": 10, "high": 12, "low": 9, "close": 11}
    for model, existing, upsert in (
        (DailyBar, business, lambda frame: market_data._upsert_bars(db_session, symbol, frame)),
        (UniverseDailyBar, base, lambda frame: universe_sync._upsert_universe_bars(db_session, universe.id, frame)),
    ):
        assert upsert(pd.DataFrame([valid])) == (0, 1)
        assert (existing.volume, existing.amount, existing.turnover_rate) == (1000, 11000, 2)
        before = {column.name: getattr(existing, column.name) for column in model.__table__.columns}
        bad = pd.DataFrame([{**valid, "close": 10.5}, {**valid, "trade_date": date(2026, 10, 22), "high": 5}])
        with pytest.raises(ValueError):
            upsert(bad)
        assert {column.name: getattr(existing, column.name) for column in model.__table__.columns} == before
        assert db_session.scalar(select(func.count()).select_from(model)) == 1


def test_restart_preserves_committed_data_and_recovery_checkpoint(db_session):
    recovery = json.dumps({"last_symbol_id": 123, "processed": 891, "total": 5555})
    task = AsyncTaskRecord(id="audit-restart", task_type="external_sync_fundamental", status="running",
        stage="sync", total=5555, processed=891, ok_count=891, failed_count=0, batch_recovery_json=recovery)
    db_session.add(task)
    db_session.commit()
    async_tasks.interrupt_orphaned_async_tasks(db_session)
    db_session.refresh(task)
    assert (task.processed, task.total, task.ok_count, task.batch_recovery_json) == (891, 5555, 891, recovery)
    assert "已提交数据保留" in task.message and "继续上次更新" in task.suggested_action
    assert task.status == "failed" and task.is_terminal_locked == 1
