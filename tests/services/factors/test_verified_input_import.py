from datetime import date,datetime,timedelta
import json
from types import SimpleNamespace
import pandas as pd
import pytest
from sqlalchemy import select,func
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol,UniverseDailyBar
from app.models.daily_bar import DailyBar
from app.models.financial_report import StockFinancialReport
from app.models.capital_flow import CapitalFlow
from app.models.score import Score
from app.services.factors import verified_input_import as imp
from app.services.factors.store import FactorWarehouse
from app.services.factors.health import get_factor_health
from app.services.factors.factor_engine import calculate_stock_factors
from tests.services.factors.test_input_replay import source_bundle

pytestmark=pytest.mark.whitebox

@pytest.fixture
def import_data(db_session):
    db=db_session
    s=Symbol(symbol='000001',name='input-test',asset_type='stock',market='sz')
    u=UniverseSymbol(symbol='000001',name='input-test',asset_type='stock',market='sz',region='cn')
    db.add_all([s,u]);db.flush()
    days=tuple(date(2025,1,1)+timedelta(days=i)for i in range(28))
    bars=pd.DataFrame([dict(symbol='000001',trade_date=d,open=10.123456789,high=11.123456789,low=9.123456789,
        close=10.987654321,volume=123456789.0,amount=1000000001.25,turnover_rate=i+.123456789)for i,d in enumerate(days)])
    ds=SimpleNamespace(symbols=('000001',),calendar=days,bars=bars,dataset_id='test',manifest_sha256='a'*64)
    rows=[dict(symbol='000001',report_period=p,announcement_date=a,report_type='financial_analysis',
        roe_ttm=r,net_profit=123456789.0123,revenue=5432109876.123,net_profit_yoy=None,revenue_yoy=None,evidence={})
        for p,a,r in [('2023-06-30','2023-08-01',2.),('2024-06-30','2024-08-01',5.)]]
    flows=[dict(symbol='000001',trade_date=str(d),evidence={},**{k:.123456789 for k in imp.FLOW_FIELDS})for d in days]
    old=DailyBar(symbol_id=s.id,trade_date=days[0],open=1,high=1,low=1,close=1,volume=10,amount=10,turnover_rate=1,source='old')
    db.add(old);db.commit()
    return ({},dict(financial=rows,flow=flows),ds),s,u,old

def plan(db,data):return imp.build_import_plan(db,bundle_id='test-inputs',manifest_sha256='b'*64,loaded=data)

def test_import_is_atomic_audited_idempotent_and_full_precision(db_session,import_data,tmp_path):
    db=db_session;data,s,u,old=import_data
    before_scores=db.scalar(select(func.count(Score.id)))
    p=plan(db,data)
    result=imp.apply_import_plan(db,p,expected_plan_sha256=imp.digest(p),correlation_id='verified-test')
    db.commit();db.expire_all()
    assert result['correction_rows']==1
    bar=db.scalar(select(DailyBar).where(DailyBar.symbol_id==s.id,DailyBar.trade_date==data[2].calendar[-1]))
    assert bar.volume==123456789 and bar.amount==1000000001.25 and bar.close==10.987654321
    f=db.scalar(select(StockFinancialReport).where(StockFinancialReport.source==imp.SOURCE))
    assert f.revenue==5432109876.123 and json.loads(f.raw_json)['historical_asof_certified']is False
    assert not plan(db,data)['operations']
    assert db.scalar(select(func.count(Score.id)))==before_scores
    warehouse=FactorWarehouse(tmp_path/'default-test.duckdb')
    refreshed=imp.refresh_default_warehouse(db,warehouse,p,batch_id='verified-test')
    assert refreshed['production_scores_published']==0 and not refreshed['model_activated']
    assert next(r for r in refreshed['fixed_pool_coverage']if r['code']=='roe_yoy_growth')['eligible']==1
    with warehouse.connection(read_only=True)as conn:
        assert conn.execute('SELECT close FROM raw_daily_bars ORDER BY trade_date DESC LIMIT 1').fetchone()[0]==10.987654321
        assert conn.execute('SELECT COUNT(*) FROM raw_fund_flows').fetchone()[0]==28

def test_changed_preimage_rejects_every_write(db_session,import_data):
    db=db_session;data,s,u,old=import_data;p=plan(db,data)
    old.close=2;db.commit()
    with pytest.raises(ValueError,match='preimage_changed'):
        imp.apply_import_plan(db,p,expected_plan_sha256=imp.digest(p),correlation_id='conflict')
    db.rollback()
    assert db.scalar(select(func.count(DailyBar.id)).where(DailyBar.symbol_id==s.id))==1
    assert db.scalar(select(func.count(StockFinancialReport.id)).where(StockFinancialReport.symbol_id==s.id))==0

def test_import_rollback_keeps_original_bars(db_session,import_data):
    db=db_session;data,s,u,old=import_data;p=plan(db,data)
    imp.apply_import_plan(db,p,expected_plan_sha256=imp.digest(p),correlation_id='rollback')
    db.rollback();db.expire_all()
    assert db.get(DailyBar,old.id).close==1
    assert not db.scalar(select(func.count(UniverseDailyBar.id)).where(UniverseDailyBar.universe_symbol_id==u.id))

def test_missing_metadata_and_plan_tamper_fail_closed(db_session,import_data):
    db=db_session;data,s,u,old=import_data;p=plan(db,data);sha=imp.digest(p)
    p['operations'][0]['values']['close']=123
    with pytest.raises(ValueError,match='plan_changed'):imp.apply_import_plan(db,p,expected_plan_sha256=sha,correlation_id='bad')
    u.asset_type='etf';db.flush()
    with pytest.raises(ValueError,match='universe_asset_conflict'):plan(db,data)

def test_partial_latest_day_does_not_shrink_health_denominator(tmp_path):
    warehouse=FactorWarehouse(tmp_path/'coverage.duckdb');now=datetime(2026,1,1)
    warehouse.upsert_records('raw_asset_universe',[dict(symbol=f'{i:06d}',asset_type='stock',region='cn',is_active=True,source='test',source_row_id=i,updated_at=now)for i in range(1,11)])
    warehouse.upsert_daily_bars([dict(symbol='000001',trade_date=date(2025,12,1),adjust='qfq',open=10,high=11,low=9,close=10,
        volume=100,amount=1000,turnover_rate=1,source='test',source_origin='test',source_row_id=1,ingested_at=now,batch_id='test')],source_key='test',watermark=1)
    calculate_stock_factors(warehouse,calc_batch_id='partial')
    health=get_factor_health(warehouse)
    assert all(f.universe_symbols==10 and f.observed_bar_symbols==1 for f in health.factors)
    assert all(f.coverage<=.1 for f in health.factors)

def test_bounded_native_refresh_matches_full_history_with_warmup(db_session,import_data,tmp_path):
    from app.services.factors.factor_engine import _load_raw_factor_panel
    data,*_=import_data;p=plan(db_session,data)
    imp.apply_import_plan(db_session,p,expected_plan_sha256=imp.digest(p),correlation_id='warmup');db_session.commit()
    warehouse=FactorWarehouse(tmp_path/'warmup.duckdb')
    imp.refresh_default_warehouse(db_session,warehouse,p,batch_id='initial')
    full=_load_raw_factor_panel(warehouse,adjust='qfq',valuation_max_age_days=7)
    start,end=data[2].calendar[-8],data[2].calendar[-2]
    bounded=_load_raw_factor_panel(warehouse,adjust='qfq',valuation_max_age_days=7,start_date=start,end_date=end)
    columns=list(full.columns)
    full=full[(pd.to_datetime(full.trade_date).dt.date>=start)&(pd.to_datetime(full.trade_date).dt.date<=end)]
    bounded=bounded[(pd.to_datetime(bounded.trade_date).dt.date>=start)&(pd.to_datetime(bounded.trade_date).dt.date<=end)]
    pd.testing.assert_frame_equal(full[columns].reset_index(drop=True),bounded[columns].reset_index(drop=True))

def test_business_status_http_reads_real_default_store_without_sql_mutations(db_session,source_bundle,tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import event
    from app.api.routes.scoring_facade import router
    from app.db.session import get_db
    from app.services.factors import verified_input_status as status
    from app.services.factors.input_replay import _load_inputs
    m,sha,days=source_bundle
    loaded=imp.load_verified_bundle('test-inputs',sha)
    for code in loaded[2].symbols:
        db_session.add(Symbol(symbol=code,name=code,asset_type='stock',market='sz'))
        db_session.add(UniverseSymbol(symbol=code,name=code,asset_type='stock',market='sz',region='cn'))
    db_session.commit()
    p=imp.build_import_plan(db_session,bundle_id='test-inputs',manifest_sha256=sha,loaded=loaded)
    imp.apply_import_plan(db_session,p,expected_plan_sha256=imp.digest(p),correlation_id='status-test');db_session.commit()
    warehouse=FactorWarehouse(tmp_path/'status-default.duckdb')
    imp.refresh_default_warehouse(db_session,warehouse,p,batch_id='status-test')
    monkeypatch.setattr(status,'get_factor_system_config',lambda db:SimpleNamespace(warehouse_path=str(warehouse.path)))
    sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement)
    event.listen(db_session.get_bind(),'before_cursor_execute',capture)
    app=FastAPI();app.include_router(router,prefix='/api/v1');app.dependency_overrides[get_db]=lambda:db_session
    try:
        with TestClient(app)as client:
            for _ in range(2):
                response=client.get('/api/v1/scoring/input-bundles/test-inputs/business-status')
                assert response.status_code==200,response.text
                result=response.json();assert result['read_only']and not result['model_activation_performed']
                assert result['business_counts']['bars']==672 and result['warehouse_counts']['raw_daily_bars']==672
                assert result['calculation_date']==str(days[-1])
                assert next(f for f in result['fixed_pool_coverage']if f['code']=='turnover_z20')['eligible']==24
    finally:event.remove(db_session.get_bind(),'before_cursor_execute',capture)
    assert not any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','ALTER'))for s in sql)
