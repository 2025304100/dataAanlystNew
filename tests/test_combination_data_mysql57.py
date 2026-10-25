"""Real MySQL 5.7 score-query and no-loss field compensation acceptance."""
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
import tracemalloc

import pandas as pd
import pytest
from sqlalchemy import MetaData, create_engine, event, insert, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.async_task import AsyncTaskRecord
from app.models.daily_bar import DailyBar
from app.models.decision_engine import PortfolioFactorUsage, StrategyExecutionSnapshot
from app.models.index_price import IndexPrice
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.score_query_service import fetch_latest_pit_scores
from app.services.universe_field_repair import repair_universe_fields
from app.services import market_data, universe_sync

pytestmark = pytest.mark.whitebox


@pytest.fixture(scope="module")
def engine():
    raw = os.environ.get("MYSQL57_TEST_URL")
    if not raw:
        pytest.skip("MYSQL57_TEST_URL required for real MySQL 5.7")
    url = make_url(raw)
    assert url.get_backend_name() == "mysql" and (url.database or "").startswith("codex_combination_")
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT VERSION()")).startswith("5.7.")
        assert conn.scalar(text("SELECT DATABASE()")) == url.database
        assert not inspect(conn).get_table_names(), "Supply a fresh schema; evidence is never cleared"
    metadata = MetaData()
    for model in (Symbol, Score, UniverseSymbol, UniverseDailyBar, DailyBar, AsyncTaskRecord,
                  Portfolio, PortfolioFactorUsage, StrategyExecutionSnapshot, IndexPrice):
        table = model.__table__.to_metadata(metadata)
        table.dialect_options["mysql"]["engine"] = "InnoDB"
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_real_mysql57_snapshot_coverage_uses_portable_nearest_snapshot_query(engine):
    from app.services.score_query_service import estimate_score_coverage_window, check_key_member_score_coverage
    with Session(engine) as db:
        p = Portfolio(name="snapshot coverage", account_type="manual", total_capital=100000,
                      investable_ratio=.8, cash_reserve_ratio=.2)
        db.add(p); db.flush()
        for number, stamp, member in ((1, datetime(2026, 10, 18, 23), 123), (2, datetime(2026, 10, 20), 456)):
            db.add(StrategyExecutionSnapshot(id=f"mysql57-snapshot-{number}", snapshot_no=number, portfolio_id=p.id,
                decision_clock_json="{}", member_snapshot_json=json.dumps([{"symbol_id": member}]),
                key_members_json=json.dumps([member]), snapshot_hash=str(number)*64, effective_from=stamp, created_at=stamp))
        db.commit()
        args = dict(portfolio_id=p.id, start_date=date(2026, 10, 19), end_date=date(2026, 10, 19),
                    bound_factor_model_run_id="snapshot-model")
        coverage = estimate_score_coverage_window(db, **args)
        assert coverage["member_ids_used"] == [123]
        key_members = check_key_member_score_coverage(db, **args)
        assert key_members["key_members_effective"] == [123]


def test_real_mysql57_indexed_pit_lookup_and_streaming_version_path(engine):
    day, cutoff = date(2026, 10, 23), datetime(2026, 10, 23, 8)
    with Session(engine) as db:
        symbols = [Symbol(symbol=f"QA{i:04}", name="PIT query", market="SH", asset_type="stock") for i in range(40)]
        db.add_all(symbols); db.flush()
        ids = [item.id for item in symbols]
        for offset in range(500):
            db.execute(insert(Score), [dict(symbol_id=sid, trade_date=day-timedelta(days=offset),
                factor_model_run_id="mysql57-query", factor_set_id="mysql57-set", weight_mode="ridge",
                factor_data_cutoff_at=cutoff-timedelta(days=offset), published_at=cutoff-timedelta(days=offset),
                model_alpha_score=80, quality_score=80, timing_score=80, priority_score=80,
                quality_grade="A", stage="growth", action="hold", calc_batch_id=f"mysql57-{offset}",
                factor_member_versions_json='{"factor":{"version":1}}',
                factor_scores_json=json.dumps({"evidence": "x"*2048})) for sid in ids])
        db.execute(update(Score).where(Score.trade_date == day).values(factor_member_versions_json='{"factor":{"version":2}}'))
        db.commit()
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("SELECT") and "scores" in statement:
                statements.append((statement, parameters))
        event.listen(engine, "before_cursor_execute", capture)
        try:
            tracemalloc.start(); started = time.perf_counter()
            rows = fetch_latest_pit_scores(db, factor_model_run_id="mysql57-query", factor_set_id="mysql57-set",
                symbol_ids=list(reversed(ids)), decision_date=day, data_cutoff_at=cutoff, require_published_at=True)
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert [(row.symbol_id, row.score_rank) for row in rows] == list(zip(ids, range(1, 41)))
        assert peak < 6_000_000, peak
        assert len(statements) == 1
        plans = [dict(row) for row in db.connection().exec_driver_sql("EXPLAIN " + statements[0][0], statements[0][1]).mappings()]
        # 5.7 may prefer the symbol/date unique index for a single-model sample.
        # Either indexed plan is valid; do not force a worse production plan.
        subplan = next(row for row in plans if row["select_type"] == "DEPENDENT SUBQUERY")
        assert subplan["type"] != "ALL" and subplan["key"], plans
        assert "idx_score_fmr_sym_dt" in subplan["possible_keys"], plans
        assert elapsed < 10, f"20k-row isolated acceptance budget exceeded: {elapsed:.3f}s"
        older = fetch_latest_pit_scores(db, factor_model_run_id="mysql57-query", factor_set_id="mysql57-set",
            symbol_ids=ids + [999999], decision_date=day, data_cutoff_at=cutoff, require_published_at=True,
            factor_member_versions={"factor": 1})
        assert {row.symbol_id for row in older} == set(ids)
        assert all(row.trade_date == day-timedelta(days=1) for row in older)
        report = {"server_version": db.scalar(text("SELECT VERSION()")), "rows_seeded": 20000,
                  "requested_symbols": 40, "query_seconds": elapsed, "python_peak_bytes": peak, "explain": plans}
        if directory := os.environ.get("COMBINATION_EVIDENCE_DIR"):
            path = Path(directory); path.mkdir(parents=True, exist_ok=True)
            (path / "mysql57-score-query.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def test_real_mysql57_field_repair_and_audit_are_atomic(engine, monkeypatch):
    with Session(engine) as db:
        instrument = UniverseSymbol(symbol="600789", name="Field compensation", asset_type="stock", market="sh", region="cn")
        db.add(instrument); db.flush()
        row = UniverseDailyBar(universe_symbol_id=instrument.id, trade_date=date(2026, 10, 19),
            open=10, high=12, low=9, close=11, volume=1000, amount=11000, turnover_rate=None,
            source="akshare:em:cn-stock:v1:qfq", created_at=datetime(2026, 10, 20))
        db.add(row); db.commit()
        original = dict(db.execute(select(UniverseDailyBar.__table__)).mappings().one())
        frame = pd.DataFrame([dict(trade_date=row.trade_date, open=10, high=12, low=9, close=11,
                                  volume=1000, amount=11000, turnover_rate=2)])
        frame.attrs["history_contract"] = dict(provider="eastmoney", endpoint="stock_zh_a_hist",
            version="cn-stock-shares-v1", volume_unit="share", amount_unit="CNY", turnover_unit="percentage_point")
        frame.attrs["production_price_mode"] = {"requested_adjustment": "qfq"}
        monkeypatch.setattr(universe_sync, "_fetch_universe_history", lambda *_: frame)
        args = dict(symbol_ids=[instrument.id], start_date=row.trade_date, end_date=row.trade_date, fields=["turnover_rate"])
        preview = repair_universe_fields(db, **args)
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TRIGGER reject_field_audit BEFORE INSERT ON async_tasks FOR EACH ROW "
                                "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='field audit failure'")
        try:
            with pytest.raises(Exception, match="field audit failure"):
                repair_universe_fields(db, **args, apply=True, expected_preview_sha256=preview["preview_sha256"])
            assert dict(db.execute(select(UniverseDailyBar.__table__)).mappings().one()) == original
            db.rollback()
        finally:
            with engine.begin() as conn:
                conn.exec_driver_sql("DROP TRIGGER reject_field_audit")
        result = repair_universe_fields(db, **args, apply=True, expected_preview_sha256=preview["preview_sha256"])
        after = dict(db.execute(select(UniverseDailyBar.__table__)).mappings().one())
        assert {key for key in original if original[key] != after[key]} == {"turnover_rate", "created_at"}
        assert result["filled_fields"] == 1 and db.get(AsyncTaskRecord, result["audit_task_id"]).status == "done"
        audit = json.loads(db.get(AsyncTaskRecord, result['audit_task_id']).result_json)
        expected_times = [{'row_id': row.id,
            'before': {'type': 'datetime', 'value': original['created_at'].isoformat(timespec='microseconds')},
            'after': {'type': 'datetime', 'value': after['created_at'].isoformat(timespec='microseconds')}}]
        assert result['availability_changes'] == audit['availability_changes'] == expected_times
        assert after['created_at'] >= original['created_at']
        assert after['created_at'] >= datetime.fromisoformat(result['observed_at'])
        assert result['instrument_snapshots'] == [{'id': instrument.id, 'symbol': '600789',
            'market': 'sh', 'asset_type': 'stock', 'region': 'cn'}]


def test_real_mysql57_field_repair_does_not_advance_future_availability(engine, monkeypatch):
    with Session(engine) as db:
        instrument = UniverseSymbol(symbol='600786', name='Future availability', asset_type='stock', market='sh', region='cn')
        db.add(instrument); db.flush()
        row = UniverseDailyBar(universe_symbol_id=instrument.id, trade_date=date(2026, 10, 19),
            open=10, high=12, low=9, close=11, volume=1000, amount=11000, turnover_rate=None,
            source='akshare:em:cn-stock:v1:qfq', created_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=5))
        db.add(row); db.commit()
        row_id = row.id
        before = dict(db.execute(select(UniverseDailyBar.__table__).where(UniverseDailyBar.id == row_id)).mappings().one())
        frame = pd.DataFrame([dict(trade_date=date(2026, 10, 19), open=10, high=12, low=9, close=11,
            volume=1000, amount=11000, turnover_rate=2)])
        frame.attrs['history_contract'] = dict(provider='eastmoney', endpoint='stock_zh_a_hist',
            version='cn-stock-shares-v1', volume_unit='share', amount_unit='CNY', turnover_unit='percentage_point')
        frame.attrs['production_price_mode'] = {'requested_adjustment': 'qfq'}
        monkeypatch.setattr(universe_sync, '_fetch_universe_history', lambda *_: frame)
        args = dict(symbol_ids=[instrument.id], start_date=row.trade_date, end_date=row.trade_date, fields=['turnover_rate'])
        preview = repair_universe_fields(db, **args)
        result = repair_universe_fields(db, **args, apply=True, expected_preview_sha256=preview['preview_sha256'])
        after = dict(db.execute(select(UniverseDailyBar.__table__).where(UniverseDailyBar.id == row_id)).mappings().one())
        assert {key for key in before if before[key] != after[key]} == {'turnover_rate'}
        assert after['created_at'] >= datetime.fromisoformat(result['observed_at'])
        audit = json.loads(db.get(AsyncTaskRecord, result['audit_task_id']).result_json)
        original_time = {'type': 'datetime', 'value': before['created_at'].isoformat(timespec='microseconds')}
        assert audit['availability_changes'] == [{'row_id': row_id, 'before': original_time, 'after': original_time}]


def test_real_mysql57_field_repair_locks_and_rechecks_instrument_identity(engine, monkeypatch):
    with Session(engine) as db:
        instrument = UniverseSymbol(symbol='600787', name='Identity race', asset_type='stock', market='sh', region='cn')
        db.add(instrument); db.flush()
        symbol_id = instrument.id
        row = UniverseDailyBar(universe_symbol_id=symbol_id, trade_date=date(2026, 10, 19),
            open=10, high=12, low=9, close=11, volume=1000, amount=11000, turnover_rate=None,
            source='akshare:em:cn-stock:v1:qfq', created_at=datetime(2026, 10, 20))
        db.add(row); db.commit()
        row_id = row.id
        original = dict(db.execute(select(UniverseDailyBar.__table__).where(UniverseDailyBar.id == row_id)).mappings().one())
        previous_audits = db.scalars(select(AsyncTaskRecord.id).where(AsyncTaskRecord.task_type == 'universe_field_repair')).all()
        frame = pd.DataFrame([dict(trade_date=date(2026, 10, 19), open=10, high=12, low=9, close=11,
            volume=1000, amount=11000, turnover_rate=2)])
        frame.attrs['history_contract'] = dict(provider='eastmoney', endpoint='stock_zh_a_hist',
            version='cn-stock-shares-v1', volume_unit='share', amount_unit='CNY', turnover_unit='percentage_point')
        frame.attrs['production_price_mode'] = {'requested_adjustment': 'qfq'}
        monkeypatch.setattr(universe_sync, '_fetch_universe_history', lambda *_: frame)
        args = dict(symbol_ids=[symbol_id], start_date=date(2026, 10, 19), end_date=date(2026, 10, 19), fields=['turnover_rate'])
        preview = repair_universe_fields(db, **args)
        def concurrent_market_change(request_instrument, *_):
            assert request_instrument.market == 'sh'
            with Session(engine) as other:
                other.get(UniverseSymbol, symbol_id).market = 'bj'
                other.commit()
            return frame
        monkeypatch.setattr(universe_sync, '_fetch_universe_history', concurrent_market_change)
        with pytest.raises(ValueError, match='preview_changed.*instrument identity'):
            repair_universe_fields(db, **args, apply=True, expected_preview_sha256=preview['preview_sha256'])
        assert dict(db.execute(select(UniverseDailyBar.__table__).where(UniverseDailyBar.id == row_id)).mappings().one()) == original
        assert db.scalars(select(AsyncTaskRecord.id).where(AsyncTaskRecord.task_type == 'universe_field_repair')).all() == previous_audits


@pytest.mark.parametrize("layer", ["daily", "universe"])
def test_real_mysql57_fallback_keeps_all_existing_fields(engine, monkeypatch, layer):
    with Session(engine) as db:
        instrument = (Symbol(symbol="QAguard", name="guard", asset_type="stock", market="SH") if layer == "daily"
                      else UniverseSymbol(symbol="600788", name="guard", asset_type="stock", market="sh", region="cn", sync_failed=0))
        db.add(instrument); db.flush()
        model = DailyBar if layer == "daily" else UniverseDailyBar
        key = "symbol_id" if layer == "daily" else "universe_symbol_id"
        row = model(**{key: instrument.id}, trade_date=date(2026, 10, 19), open=10, high=12, low=9, close=11,
                    volume=1000, amount=11000, turnover_rate=2, source="akshare:em:cn-stock:v1:qfq")
        db.add(row); db.commit()
        before = dict(db.execute(select(model.__table__).where(model.id == row.id)).mappings().one())
        frame = pd.DataFrame([dict(trade_date=row.trade_date, open=10, high=12, low=9, close=10.5,
                                  volume=900, amount=10000, turnover_rate=None)])
        frame.attrs["history_contract"] = dict(provider="tencent", endpoint="stock_zh_a_hist_tx",
            version="cn-stock-shares-v1", volume_unit="share", amount_unit="CNY", turnover_unit="percentage_point")
        frame.attrs["production_price_mode"] = {"requested_adjustment": "qfq"}
        if layer == "daily":
            monkeypatch.setattr(market_data, "_fetch_history", lambda **_: frame)
            call = lambda: market_data.sync_symbol_daily_bars(db, instrument, row.trade_date, row.trade_date)
        else:
            monkeypatch.setattr(universe_sync, "_fetch_universe_history", lambda *_: frame)
            call = lambda: universe_sync.repair_one_universe_symbol(db, instrument, row.trade_date, row.trade_date)
        with pytest.raises(ValueError, match="history_source_conflict"):
            call()
        db.rollback()
        assert dict(db.execute(select(model.__table__).where(model.id == row.id)).mappings().one()) == before
