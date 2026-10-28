"""I/O reductions must keep freshness, cancellation and exact coverage semantics."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import request_coalescing as shared
from app.services import capital_flow_provider, financial_provider, etf_provider, valuation_provider
from app.services.factors import formula_catalog as catalog
from app.services.factors.store import FactorWarehouse
from tests.test_external_dataset_sync_contract import environment, install_http, quote, report, TODAY
from tests.test_etf_sync_contract import Today

pytestmark = pytest.mark.whitebox


@pytest.fixture(autouse=True)
def release_isolated_warehouse_connections(tmp_path):
    # The store intentionally pools live application connections. These
    # UUID-scoped test warehouses have no owners after each test finishes.
    yield
    from app.services.factors import store
    for path in list(store._WH_POOLS):
        if Path(path).resolve().is_relative_to(tmp_path.resolve()):
            store.close_owned_warehouse_path(path)


def observed_future(monkeypatch):
    waiting = Event()
    class ObservedFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout=timeout)
    monkeypatch.setattr(shared, "Future", ObservedFuture)
    return waiting


def test_overlapping_reads_share_io_but_later_refresh_is_fresh(monkeypatch):
    entered, release = Event(), Event()
    waiting = observed_future(monkeypatch)
    calls=[]
    def read():
        calls.append(1); entered.set()
        assert release.wait(5)
        return {"values": [len(calls)]}
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(shared.coalesced_read, "same-request", read)
        assert entered.wait(5)
        second = pool.submit(shared.coalesced_read, "same-request", read)
        try:
            assert waiting.wait(5)
        finally:
            release.set()
        a,b=first.result(),second.result()
    assert len(calls)==1 and a==b
    a["values"].append(99)
    assert b=={"values":[1]}
    assert shared.coalesced_read("same-request", read)=={"values":[2]}


def test_cancelled_waiter_does_not_cancel_other_readers(monkeypatch):
    entered, release, cancel = Event(), Event(), Event()
    waiting = observed_future(monkeypatch)
    def read():
        entered.set(); assert release.wait(5)
        return 42
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(shared.coalesced_read,"cancel-waiter",read)
        assert entered.wait(5)
        second=pool.submit(shared.coalesced_read,"cancel-waiter",read,should_cancel=cancel.is_set)
        try:
            assert waiting.wait(5)
            cancel.set()
            with pytest.raises(InterruptedError): second.result(timeout=2)
        finally:
            release.set()
        assert first.result()==42


def test_provider_timeout_is_not_mistaken_for_wait_timeout(monkeypatch):
    entered, release = Event(), Event()
    waiting=observed_future(monkeypatch)
    def read():
        entered.set(); assert release.wait(5)
        raise TimeoutError("provider timed out")
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(shared.coalesced_read,"failed-request",read)
        assert entered.wait(5)
        second=pool.submit(shared.coalesced_read,"failed-request",read)
        try:
            assert waiting.wait(5)
        finally:
            release.set()
        for result in (first,second):
            with pytest.raises(TimeoutError, match="provider timed out"):
                result.result(timeout=2)
    assert shared.coalesced_read("failed-request", lambda: 7)==7


def test_key_coalesces_code_order_but_keeps_page_date_and_field_scope():
    key=shared.request_key
    assert key("api","url",{"secids":"1.600000,0.000001"})==key("api","url",{"secids":"0.000001,1.600000"})
    assert key("api","url",{"filter":'(SECUCODE in ("600000.SH","000001.SZ"))'})==key("api","url",{"filter":'(SECUCODE in ("000001.SZ","600000.SH"))'})
    base={"page":1,"date":"2026-10-19","fields":"f62"}
    for change in ({"page":2},{"date":"2026-10-16"},{"fields":"f184"}):
        assert key("api","url",base)!=key("api","url",{**base,**change})


@pytest.mark.parametrize("provider,invoke,payload", [
    (capital_flow_provider, lambda p:p.fetch_capital_flow_quotes(None,["1.600000"]), {"rc":0,"data":None}),
    (etf_provider, lambda p:p.fetch_etf_quotes(None,["510300"]), {"rc":0,"data":None}),
    (financial_provider, lambda p:p.fetch_financial_rows(None,["600000.SH"],date(2026,10,19),date(2026,10,19)), {"success":True,"result":{"count":0,"pages":0,"data":[]}}),
    (valuation_provider, lambda p:p.fetch_valuation_rows(None,["600000"],date(2026,10,19),date(2026,10,19)), {"success":True,"result":{"count":0,"pages":0,"data":[]}}),
])
def test_actual_providers_coalesce_duplicate_calls(provider,invoke,payload,monkeypatch):
    entered, release = Event(), Event()
    waiting=observed_future(monkeypatch)
    calls=[]
    def get(*args,**kwargs):
        calls.append(1); entered.set(); assert release.wait(5)
        return Mock(json=lambda:payload)
    monkeypatch.setattr(provider.requests,"get",get)
    monkeypatch.setattr("app.services.akshare_registry.apply_delay",lambda *a:None)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(invoke,provider)
        assert entered.wait(5)
        second=pool.submit(invoke,provider)
        try:
            assert waiting.wait(5)
        finally:
            release.set()
        assert first.result()==second.result()
    assert len(calls)==1
    invoke(provider)
    assert len(calls)==2


def test_catalog_coalesces_overlapping_full_reads(monkeypatch):
    waiting=observed_future(monkeypatch)
    entered,release=Event(),Event()
    calls=[]
    def query(warehouse):
        calls.append(1); entered.set(); assert release.wait(5)
        return {"close":{"table_rows":1,"nonnull_rows":1}}
    monkeypatch.setattr(catalog,"_warehouse_cache_key",lambda warehouse:("parallel-catalog",1))
    monkeypatch.setattr(catalog,"_query_field_stats",query)
    catalog.invalidate_formula_catalog_cache()
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(catalog.build_formula_catalog,object())
        assert entered.wait(5)
        second=pool.submit(catalog.build_formula_catalog,object())
        try:
            assert waiting.wait(5)
        finally:
            release.set()
        assert first.result()==second.result()
    assert len(calls)==1
    catalog.invalidate_formula_catalog_cache()


def test_wal_commit_invalidates_cached_catalog_without_waiting_for_ttl(tmp_path,monkeypatch):
    path=tmp_path/"file.duckdb"
    path.write_bytes(b"fixed main file")
    calls=[]
    def query(warehouse):
        calls.append(1)
        return {"close":{"table_rows":len(calls),"nonnull_rows":len(calls)}}
    monkeypatch.setattr(catalog,"_query_field_stats",query)
    catalog.invalidate_formula_catalog_cache()
    warehouse=SimpleNamespace(path=path)
    catalog.build_formula_catalog(warehouse)
    catalog.build_formula_catalog(warehouse)
    assert len(calls)==1
    Path(str(path)+".wal").write_bytes(b"committed changes")
    catalog.build_formula_catalog(warehouse)
    assert len(calls)==2
    catalog.invalidate_formula_catalog_cache()


def test_fresh_full_catalog_satisfies_subset_read(monkeypatch):
    calls=[]
    def query(warehouse):
        calls.append(1)
        return {"close":{"table_rows":1,"nonnull_rows":1},"main_net_inflow":{"table_rows":1,"nonnull_rows":1}}
    monkeypatch.setattr(catalog,"_warehouse_cache_key",lambda w:("full-to-subset",1))
    monkeypatch.setattr(catalog,"_query_field_stats",query)
    catalog.invalidate_formula_catalog_cache()
    warehouse=object()
    catalog.build_formula_catalog(warehouse)
    subset=catalog.build_formula_catalog(warehouse,field_keys={"main_net_inflow"})
    assert len(calls)==1 and [f["key"] for f in subset["fields"]]==["main_net_inflow"]
    catalog.invalidate_formula_catalog_cache()


def test_batched_coverage_matches_original_on_duplicates_gaps_and_report_revisions(tmp_path,monkeypatch):
    warehouse=FactorWarehouse(tmp_path/"equivalence.duckdb")
    warehouse.initialize()
    closed=date(2026,10,1)
    monkeypatch.setattr("app.services.market_session.read_cn_calendar",lambda:[(closed,0)])
    with warehouse.connection() as conn:
        conn.execute("""INSERT INTO raw_asset_universe VALUES
            ('000001','a','stock','sz','cn',TRUE,'test',1,CURRENT_TIMESTAMP),
            ('600000','b','stock','sh','cn',TRUE,'test',2,CURRENT_TIMESTAMP),
            ('600001','inactive','stock','sh','cn',FALSE,'test',3,CURRENT_TIMESTAMP),
            ('SPY','foreign','etf','us','us',TRUE,'test',4,CURRENT_TIMESTAMP)""")
        conn.execute("""INSERT INTO raw_daily_bars(symbol,trade_date,adjust,close,source,source_origin,source_row_id,ingested_at,batch_id)
            SELECT symbol,session_date,adjust,10,'test','test',1,CURRENT_TIMESTAMP,'b'
            FROM (VALUES ('000001'),('600000'),('600001'),('SPY')) symbols(symbol)
            CROSS JOIN (SELECT CAST(range AS DATE) AS session_date FROM range(DATE '2025-07-01',DATE '2026-10-20',INTERVAL 1 DAY)) dates
            CROSS JOIN (VALUES ('qfq'),('hfq')) adjustments(adjust)""")
        conn.execute("""INSERT INTO raw_valuation_snapshots(symbol,trade_date,pe_ttm,pb,source,ingested_at,batch_id)
            SELECT symbol,DATE '2026-10-01'+CAST(day_number AS INTEGER)-1,
            CASE WHEN day_number%3=0 THEN NULL ELSE 10 END,CASE WHEN day_number%5=0 THEN NULL ELSE 1 END,
            source,CURRENT_TIMESTAMP,'v' FROM (VALUES ('000001'),('600000')) symbols(symbol)
            CROSS JOIN (SELECT range AS day_number FROM range(1,20)) dates
            CROSS JOIN (VALUES ('one'),('two')) sources(source)""")
        conn.execute("""INSERT INTO raw_financial_reports(symbol,report_period,announcement_date,report_type,roe_ttm,source,ingested_at,batch_id)
            VALUES ('000001','2026-03-31','2026-06-30','quarter',5,'test',CURRENT_TIMESTAMP,'f'),
                   ('000001','2026-06-30','2026-10-01','quarter',NULL,'test',CURRENT_TIMESTAMP,'f'),
                   ('000001','2026-03-31','2026-10-01','old',7,'test',CURRENT_TIMESTAMP,'f'),
                   ('600000','2026-06-30','2026-10-09','quarter',9,'test',CURRENT_TIMESTAMP,'f'),
                   ('600000','2026-09-30','2026-10-19','quarter',10,'test',CURRENT_TIMESTAMP,'f')""")
    stats=catalog._query_field_stats(warehouse,field_keys={"pe_ttm","pb","roe_ttm","close","prev_close"})
    with warehouse.connection() as conn:
        catalog._prepare_coverage_calendar(conn)
        for key in stats:
            spec=catalog.FIELD_CATALOG[key]
            physical="close" if key=="prev_close" else catalog.physical_field_column(spec.source_table,key)
            expected=catalog._query_cross_section_coverage(conn,table=spec.source_table,field=physical,minimum_coverage=0,_calendar_ready=True)
            for metric,value in expected.items():
                assert stats[key][metric]==value,(key,metric,stats[key],expected)


@pytest.mark.parametrize("dataset",["etf","financial"])
def test_101_symbol_progress_has_bounded_commits_and_complete_partitions(environment,db_session,monkeypatch,dataset):
    import json
    from app.models.symbol import Symbol
    from app.models.data_sync_plan import DataSyncPartition
    from app.services import external_data_sync_task as tasks
    client,_,_=environment
    if dataset=="etf":
        monkeypatch.setattr("app.services.etf_basic_data.date", Today)
        symbols=[Symbol(symbol=str(510000+i),name="budget",asset_type="etf",market="sh") for i in range(101)]
        db_session.add_all(symbols);db_session.commit()
        def quotes(db,codes,**kwargs):
            return {code:{"trade_date":TODAY,"close":1.1,"nav":1,"total_shares":100} for code in codes}
        monkeypatch.setattr("app.services.etf_basic_data.fetch_etf_quotes",quotes)
    else:
        db_session.add_all([Symbol(symbol=str(600001+i),name="budget",asset_type="stock",market="sh") for i in range(99)])
        db_session.commit()
        monkeypatch.setattr(financial_provider,"fetch_financial_rows",lambda *a,**k:[])
    response=client.post('/api/v1/external-data/sync-tasks',json={'dataset':dataset,'source':'all','include_northbound':False})
    assert response.status_code==200,response.text
    task=response.json();payload=json.loads(task['payload_json'])
    commits=[]
    commit=db_session.commit
    def counted_commit():
        commits.append(1);return commit()
    monkeypatch.setattr(db_session,'commit',counted_commit)
    result=tasks._run_symbol_sync(db_session,task['id'],dataset,payload)
    db_session.expire_all()
    partitions=db_session.query(DataSyncPartition).filter_by(plan_id=payload['sync_plan_id']).all()
    assert len(partitions)==101
    assert all(p.status==('done' if dataset=='etf' else 'skipped') and p.attempts==1 for p in partitions)
    assert result['success' if dataset=='etf' else 'skipped']==101
    assert len(commits)<=12,len(commits)


def test_capital_flow_history_and_current_rows_are_prefetched_in_two_queries(environment,db_session,monkeypatch):
    from datetime import timedelta
    from sqlalchemy import event
    from app.models.symbol import Symbol
    from app.models.capital_flow import CapitalFlow
    from app.services.capital_flow_data import sync_market_capital_flow_snapshot,calc_main_net_inflow_score
    _,symbols,_=environment
    extra=[Symbol(symbol=str(600001+i),name="budget",asset_type="stock",market="sh") for i in range(98)]
    db_session.add_all(extra);db_session.flush()
    symbols+=extra
    for symbol in symbols[:2]:
        db_session.add_all([CapitalFlow(symbol_id=symbol.id,trade_date=TODAY-timedelta(days=i),
            main_net_inflow=10+i,source="test") for i in range(1,10)])
    db_session.commit()
    install_http(monkeypatch,'capital_flow',[quote(s.symbol) for s in symbols])
    reads=[]
    def on_sql(conn,cursor,statement,parameters,context,executemany):
        if statement.lstrip().lower().startswith('select') and 'capital_flows' in statement.lower():reads.append(statement)
    engine=db_session.get_bind()
    event.listen(engine,'before_cursor_execute',on_sql)
    try:
        assert len(sync_market_capital_flow_snapshot(db_session,symbols))==100
    finally:
        event.remove(engine,'before_cursor_execute',on_sql)
    assert len(reads)==2
    row=db_session.query(CapitalFlow).filter_by(symbol_id=symbols[0].id,trade_date=TODAY).one()
    assert row.main_net_inflow_score==calc_main_net_inflow_score(100,[100]+list(range(11,20)))


def test_etf_batch_prefetch_preserves_partial_values_and_refreshes_again(environment, db_session, monkeypatch):
    from sqlalchemy import event
    from app.models.symbol import Symbol
    from app.models.etf_indicator import EtfIndicator
    from app.services import etf_basic_data as etf
    from tests.test_etf_sync_contract import Today

    monkeypatch.setattr(etf, "date", Today)
    symbols = [Symbol(symbol=str(510000 + i), name="budget", market="sh", asset_type="etf") for i in range(100)]
    db_session.add_all(symbols)
    db_session.flush()
    for symbol in symbols[:50]:
        db_session.add(EtfIndicator(symbol_id=symbol.id, trade_date=TODAY, nav=1, close=1.1,
            total_shares=100, fund_size=100, premium_discount=10, tracking_error=2, source="original"))
    db_session.commit()
    calls = []
    def quotes(db, codes, **kwargs):
        calls.append(list(codes))
        return {code: {"trade_date": TODAY, "close": 1.2, "nav": None, "total_shares": 200} for code in codes}
    monkeypatch.setattr(etf, "fetch_etf_quotes", quotes)
    reads = []
    def on_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select") and "etf_indicators" in statement.lower():
            reads.append(statement)
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", on_sql)
    try:
        first = etf.sync_etf_snapshots(db_session, symbols, TODAY)
        db_session.commit()
        second = etf.sync_etf_snapshots(db_session, symbols, TODAY)
    finally:
        event.remove(engine, "before_cursor_execute", on_sql)
    assert len(reads) == 2 and len(calls) == 2
    assert first["records"] == second["records"] == 100
    records = db_session.query(EtfIndicator).order_by(EtfIndicator.symbol_id).all()
    assert len(records) == 100
    for index, row in enumerate(records):
        assert row.total_shares == 200 and row.close == 1.2
        assert row.fund_size == (100 if index < 50 else None)
        assert row.premium_discount == (10 if index < 50 else None)
        assert row.tracking_error == (2 if index < 50 else None)


def test_financial_batch_prefetch_keeps_versions_and_detects_changes(environment, db_session, monkeypatch):
    from datetime import timedelta
    from sqlalchemy import event
    from app.models.symbol import Symbol
    from app.models.financial_report import StockFinancialReport
    from app.services import financial_data as finance

    _, symbols, _ = environment
    extra = [Symbol(symbol=str(600001 + i), name="budget", market="sh", asset_type="stock") for i in range(99)]
    db_session.add_all(extra)
    db_session.flush()
    symbols += extra
    rows = [report(finance._akshare_financial_symbol(symbol)) for symbol in symbols]
    calls = []
    def fetch(db, codes, start, end, **kwargs):
        calls.append(list(codes))
        return [row.copy() for row in rows if row["SECUCODE"] in codes]
    monkeypatch.setattr(financial_provider, "fetch_financial_rows", fetch)
    reads = []
    def on_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select") and "stock_financial_reports" in statement.lower():
            reads.append(statement)
    engine = db_session.get_bind()
    def run():
        reads.clear()
        event.listen(engine, "before_cursor_execute", on_sql)
        try:
            result = finance.sync_financial_announcements(db_session, symbols, TODAY, TODAY)
            db_session.commit()
        finally:
            event.remove(engine, "before_cursor_execute", on_sql)
        assert len(reads) == 2
        return result
    assert run()["records"] == 101
    assert run()["records"] == 0
    original = db_session.query(StockFinancialReport).filter_by(symbol_id=symbols[0].id).one()
    db_session.add(StockFinancialReport(symbol_id=symbols[0].id, report_period=original.report_period,
        announcement_date=TODAY-timedelta(days=1), report_type=original.report_type,
        source=original.source, roe_ttm=3, revenue=150))
    db_session.add(StockFinancialReport(symbol_id=symbols[0].id, report_period=original.report_period,
        announcement_date=TODAY, report_type=original.report_type,
        source="other_provider", roe_ttm=9))
    db_session.commit()
    rows[0].update(ROEJQ=5, TOTALOPERATEREVE=None)
    assert run()["records"] == 1
    versions = db_session.query(StockFinancialReport).filter_by(symbol_id=symbols[0].id).all()
    assert len(versions) == 3
    actual = next(item for item in versions if item.source == original.source and item.announcement_date == TODAY)
    assert actual.roe_ttm == 5 and actual.revenue == 200
    assert next(item for item in versions if item.announcement_date < TODAY).roe_ttm == 3
    assert next(item for item in versions if item.source == "other_provider").roe_ttm == 9
    assert len(calls) == 6


def test_cancellation_probe_is_bounded_but_network_boundary_checks_are_fresh(monkeypatch):
    from app.services import sync_progress
    clock,calls=[0.0],[]
    monkeypatch.setattr(sync_progress,'monotonic',lambda:clock[0])
    cancelled=[False]
    def read():calls.append(1);return cancelled[0]
    probe=sync_progress.CancellationProbe(read)
    for _ in range(1000): assert not probe()
    assert len(calls)==1
    cancelled[0]=True
    with pytest.raises(InterruptedError):
        shared.coalesced_read('fresh-cancellation',lambda:pytest.fail('must not perform I/O'),should_cancel=probe)
    assert len(calls)==2


def test_cancellation_flushes_processed_rows_and_remains_cancelled(environment,db_session,monkeypatch):
    import json
    from sqlalchemy import update
    from app.models.async_task import AsyncTaskRecord
    from app.models.symbol import Symbol
    from app.models.etf_indicator import EtfIndicator
    from app.models.data_sync_plan import DataSyncPartition
    from app.services import etf_basic_data, external_data_sync_task as tasks
    client,_,_=environment
    symbols=[Symbol(symbol=str(510000+i),name='cancel',asset_type='etf',market='sh') for i in range(3)]
    db_session.add_all(symbols);db_session.commit()
    response=client.post('/api/v1/external-data/sync-tasks',json={'dataset':'etf','source':'all'})
    task=response.json();payload=json.loads(task['payload_json'])
    def sync(db,selected,day,*,should_cancel,on_result):
        row=EtfIndicator(symbol_id=selected[0].id,trade_date=TODAY,total_shares=100)
        db.add(row);db.flush()
        on_result(selected[0],row,{'total':3,'success':1,'skipped':0,'failed':0,'records':1})
        # SQLite serializes writers. Inject the public cancellation state in
        # this transaction, then verify the checkpoint preserves its intent
        # and publishes already processed facts before acknowledging cancel.
        db.execute(update(AsyncTaskRecord).where(AsyncTaskRecord.id==task['id']).values(cancel_requested=1))
        db.execute(update(DataSyncPartition).where(DataSyncPartition.plan_id==payload['sync_plan_id']).values(status='cancelled'))
        raise InterruptedError('cancelled between processed symbols')
    monkeypatch.setattr(etf_basic_data,'sync_etf_snapshots',sync)
    tasks._run_external_sync_task(task['id'],'etf',payload)
    final=client.get(f"/api/v1/external-data/sync-tasks/{task['id']}").json()
    assert final['status']=='cancelled',final
    assert db_session.query(EtfIndicator).count()==1
    db_session.expire_all()
    partitions=db_session.query(DataSyncPartition).filter_by(plan_id=payload['sync_plan_id']).all()
    assert sorted(p.status for p in partitions)==['cancelled','cancelled','done']
    assert sum(p.rows_written for p in partitions)==1
