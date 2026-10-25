"""Explicit input evidence for G4 HTTP contracts; no result or gate substitutes."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.app_setting import AppSetting
from app.models.decision_engine import DecisionEvidence, DecisionRun, StrategyExecutionSnapshot
from app.models.market_data import MarketCalendar
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol

DAY=date(2026,9,29)
EXECUTION=datetime(2026,9,30,1,30)
NOTE='本人已核对当前报告版本、成交、持仓和完整现金账本'
ROOT=Path(__file__).resolve().parents[1]


@contextmanager
def migrated_account_db(tmp_path,monkeypatch,nodeid):
    raw=os.environ.get('G4_MYSQL57_URL')
    evidence=os.environ.get('COMBINATION_EVIDENCE_DIR')
    if raw:
        base=make_url(raw)
        if base.get_backend_name()!='mysql' or not re.fullmatch(r'codex_combination_[a-z0-9_]{1,34}',base.database or ''):
            raise ValueError('G4 requires an explicit codex_combination_* MySQL URL')
        database=base.database+'_c'+hashlib.sha256(nodeid.encode()).hexdigest()[:8]
        server=create_engine(base._replace(database=None),hide_parameters=True)
        try:
            with server.begin() as conn:
                assert str(conn.scalar(text('SELECT VERSION()'))).startswith('5.7.')
                assert not conn.scalar(text('SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=:db'),{'db':database}), 'Never reuse an existing G4 fixture database'
                conn.exec_driver_sql(f'CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        finally:
            server.dispose()
        raw=base.set(database=database).render_as_string(hide_password=False)
    else:
        raw='sqlite:///'+(tmp_path/'g4.db').as_posix()
    monkeypatch.setenv('ALEMBIC_DATABASE_URL',raw)
    cfg=Config(str(ROOT/'alembic.ini'))
    cfg.set_main_option('script_location',str(ROOT/'alembic'))
    command.upgrade(cfg,'head')
    engine=create_engine(raw,hide_parameters=True,**({'connect_args':{'check_same_thread':False}} if raw.startswith('sqlite:') else {}))
    if raw.startswith('sqlite:'):
        @event.listens_for(engine,'connect')
        def enforce_fk(connection,_record):
            connection.execute('PRAGMA foreign_keys=ON')
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT version_num FROM alembic_version'))=='wps_0024_087_binding_precision'
        if engine.dialect.name=='mysql':
            assert conn.scalar(text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_type='BASE TABLE' AND engine<>'InnoDB'"))==0
    try:
        with Session(engine,autoflush=False,expire_on_commit=False) as db:
            try:
                yield db
            finally:
                db.rollback()
                if evidence:
                    folder=Path(evidence)/'g4-fixtures';folder.mkdir(parents=True,exist_ok=True)
                    identity={'nodeid':nodeid,'dialect':engine.dialect.name,'database':engine.url.database,
                        'revision':'wps_0024_087_binding_precision','table_count':len(inspect(engine).get_table_names()),
                        'financial_rows':financial_rows(db)}
                    (folder/(hashlib.sha256(nodeid.encode()).hexdigest()[:8]+'.json')).write_text(json.dumps(identity,default=str,indent=2),encoding='utf8')
    finally:
        engine.dispose()


def seed_account(db):
    p=Portfolio(name='g4-contract-'+uuid.uuid4().hex,account_type='simulated',asset_scope='mixed',
        total_capital=1_000_000,investable_ratio=.95,cash_reserve_ratio=.05,
        created_at=datetime(2026,9,1),effective_start_date=DAY,is_test=1)
    db.add(p);db.flush()
    db.add(CashLedger(portfolio_id=p.id,entry_type='deposit',amount=1_000_000,balance_after=1_000_000,
        ref_type='portfolio',ref_id=p.id,note='Initial simulated funding',created_at=datetime(2026,9,1)))
    db.commit()
    return p


def seed_buy(db,p,*,fill=True,cash=True,snapshot_type='task_locked'):
    symbol=Symbol(symbol='G4'+uuid.uuid4().hex[:10],name='G4 isolated input',asset_type='stock',market='SH')
    db.add(symbol);db.flush()
    snap=StrategyExecutionSnapshot(id='ss-'+uuid.uuid4().hex,snapshot_no=1,portfolio_id=p.id,snapshot_type=snapshot_type,
        decision_clock_json='{"decision_at":"20:30 Asia/Shanghai"}',member_snapshot_json='[]',
        snapshot_hash=hashlib.sha256(b'explicit-g4-input').hexdigest(),effective_from=DAY,created_by='g4-fixture')
    db.add(snap);db.flush()
    run=DecisionRun(id='dr-'+uuid.uuid4().hex,strategy_snapshot_id=snap.id,portfolio_id=p.id,
        trade_date=DAY,run_type='auto_simulation',status='SUCCEEDED',universe_count=1,member_count=1,
        decision_at=datetime(2026,9,29,12,30),data_cutoff_at=datetime(2026,9,29,12),execution_at=EXECUTION)
    db.add(run);db.flush()
    evidence=DecisionEvidence(id='ev-'+uuid.uuid4().hex,decision_run_id=run.id,strategy_snapshot_id=snap.id,
        portfolio_id=p.id,symbol_id=symbol.id,trade_date=DAY,decision_at=run.decision_at,
        data_cutoff_at=run.data_cutoff_at,execution_at=EXECUTION,action='BUY',min_lot_size=100,
        target_quantity=1000,target_qty_delta=1000,content_hash=hashlib.sha256(b'g4-buy-1000').hexdigest())
    db.add(evidence);db.flush()
    order=SimOrder(portfolio_id=p.id,symbol_id=symbol.id,side='buy',quantity=1000,submitted_price=10,
        status='filled' if fill else 'pending',filled_quantity=1000 if fill else 0,
        filled_price=10 if fill else 0,filled_amount=10000 if fill else 0,fee=5 if fill else 0,
        created_at=EXECUTION,filled_at=EXECUTION if fill else None,decision_evidence_id=evidence.id,source_type='auto_simulation')
    db.add(order);db.flush()
    if fill:
        db.add(SimTrade(portfolio_id=p.id,symbol_id=symbol.id,order_id=order.id,side='buy',quantity=1000,
            price=10,amount=10000,fee=5,created_at=EXECUTION))
        db.add(Position(portfolio_id=p.id,symbol_id=symbol.id,quantity=1000,avg_cost=10,
            asset_type='stock',opened_at=EXECUTION.date()))
        if cash:
            db.add(CashLedger(portfolio_id=p.id,entry_type='buy',amount=-10005,balance_after=989995,
                ref_type='sim_order',ref_id=order.id,note='G4 explicit execution fixture',created_at=EXECUTION))
    p.last_decision_trade_date=DAY
    db.commit()
    return order


def seed_calendar(db,*,proof=True,closed=(),missing=(),provenance=None,actor='exchange-calendar-sync'):
    """Complete synthetic 2026 calendar, explicitly supplied as test input."""
    start=date(2026,1,1)
    days=[start+timedelta(days=i) for i in range(365)]
    shutdown=set([d for d in days if d.weekday()<5][:18])|set(closed)
    opened={d for d in days if d.weekday()<5 and d not in shutdown}
    db.add_all(MarketCalendar(trade_date=d,is_trading_day=int(d in opened)) for d in days if d not in missing)
    if proof:
        db.add(AppSetting(key='market.cn_trade_calendar',updated_by=actor,value_json=json.dumps({
            'year':2026,'source':'https://www.sse.com.cn/disclosure/dealinstruc/closed/',
            'trading_days':len(opened),'refreshed_at':'2026-09-01T00:00:00+00:00','source_version':'g4-synthetic-2026',**(provenance or {})})))
    db.commit()


def financial_rows(db):
    return {table:[dict(row) for row in db.execute(text(f'SELECT * FROM {table} ORDER BY id')).mappings()]
            for table in ('cash_ledger','positions','sim_orders','sim_trades')}
