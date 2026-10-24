"""Read-only main scoring inputs, model eligibility and strategy inventory."""
from pathlib import Path
from datetime import date
import hashlib
import json
import math
import sys
from collections import Counter

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
from sqlalchemy import create_engine,event,select,text
from sqlalchemy.orm import Session
from app.core.config import build_mysql_url,load_db_config
from app.models.daily_bar import DailyBar
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.scoring_config import ScoringConfig
from app.models.factor_model import FactorModelRun
from app.services.factor_model_contract import assess_factor_model_readiness,model_factor_set_id,model_training_mode
from app.services.factors.__facade__ import _model_display_name
from app.services.scoring_config_engine import parse_config_json,_enabled_external_factor_keys
from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar

root=Path(__file__).parent
engine=create_engine(build_mysql_url(load_db_config()))
@event.listens_for(engine,'before_cursor_execute')
def read_only(conn,cursor,statement,parameters,context,executemany):
    if statement.lstrip().split(None,1)[0].upper() not in {'SELECT','SHOW','DESCRIBE','SET'}:
        raise RuntimeError('Preflight is read-only')

def item(row):return {column.name:getattr(row,column.name) for column in row.__table__.columns}
def digest(value):return hashlib.sha256(json.dumps(value,default=str,sort_keys=True).encode()).hexdigest()

facts={'score_inputs':[],'models':[]}
with Session(engine,autoflush=False) as db:
    calendar=resolve_inspection_calendar(db)
    assert calendar['status']=='PASSED'
    target=calendar['expected_trade_date']
    assert target==date(2026,10,23)
    facts['trade_date']=target
    for sid,cid in [(1,3),(2,5),(3215,3)]:
        symbol=db.get(Symbol,sid)
        config=db.get(ScoringConfig,cid)
        assert config.is_active and config.version==1 and config.asset_type==symbol.asset_type
        parsed=parse_config_json(config)
        external=_enabled_external_factor_keys(parsed)
        bars=list(db.scalars(select(DailyBar).where(DailyBar.symbol_id==sid,DailyBar.trade_date<=target)
            .order_by(DailyBar.trade_date.desc()).limit(80)))[::-1]
        dates={bar.trade_date for bar in bars}
        required={day for day in calendar['trading_dates'] if bars[0].trade_date<=day<=target}
        old=list(db.scalars(select(Score).where(Score.symbol_id==sid,Score.trade_date==target).order_by(Score.id)))
        entry={'symbol':item(symbol),'config':item(config),'bars':[item(bar) for bar in bars],
            'prior_target_scores':[item(row) for row in old],
            'config_sha256':digest(config.config_json),'bars_sha256':digest([item(bar) for bar in bars]),
            'external_factor_keys':sorted(external),'missing_trade_dates':sorted(required-dates),
            'non_trading_dates':sorted(dates-required),
            'invalid_input_count':sum(any(getattr(bar,field) is None or not math.isfinite(getattr(bar,field))
                for field in ['open','high','low','close','volume','amount']) for bar in bars),
            'source_counts':dict(Counter(bar.source for bar in bars))}
        facts['score_inputs'].append(entry)
    for model in db.scalars(select(FactorModelRun).order_by(FactorModelRun.created_at.desc())):
        readiness=assess_factor_model_readiness(db,model_run_id=model.id).to_dict()
        facts['models'].append({'id':model.id,'name':_model_display_name(model,model_factor_set_id(model)),
            'asset_type':model.asset_type,'status':model.status,'model_type':model.model_type,
            'training_mode':model_training_mode(model),'factor_set_id':model_factor_set_id(model),
            'sample_count':model.sample_count,'symbol_count':model.symbol_count,'trade_date_count':model.trade_date_count,
            'train_start_date':model.train_start_date,'train_end_date':model.train_end_date,
            'validation_start_date':model.validation_start_date,'validation_end_date':model.validation_end_date,
            'created_at':model.created_at,'readiness':readiness,'metrics_json':model.metrics_json,
            'rejection_reason':model.rejection_reason})
    for name in ['portfolio_rules','portfolio_members','portfolio_factor_usages','strategy_execution_snapshots','signal_rules']:
        facts[name]=[dict(row) for row in db.execute(text('SELECT * FROM `'+name+'` WHERE portfolio_id=1 ORDER BY id')).mappings()]
    facts['source_counts']={name:[dict(row) for row in db.execute(text(
        'SELECT trade_date,COUNT(*) n FROM `'+name+'` WHERE portfolio_id=1 AND trade_date BETWEEN :start AND :end GROUP BY trade_date ORDER BY trade_date'),
        {'start':'2026-09-29','end':'2026-10-23'}).mappings()]
        for name in ['decision_runs','decision_evidence']}
    facts['decision_order_plan_count']=db.scalar(text('SELECT COUNT(*) FROM decision_order_plans WHERE portfolio_id=1'))
    facts['simulation_source_totals']={name:db.scalar(text('SELECT COUNT(*) FROM `'+name+'` WHERE portfolio_id=1')) for name in ['sim_orders','sim_trades']}
    facts['protected_baseline_count']=db.scalar(text('SELECT COUNT(*) FROM portfolio_account_baselines WHERE portfolio_id=1'))
    facts['active_tasks']=[dict(row) for row in db.execute(text("SELECT id,task_type,status FROM async_tasks WHERE status IN ('queued','running')")).mappings()]
    facts['audit_max']=db.scalar(text('SELECT MAX(id) FROM data_governance_audit_events'))
(root/'preflight.json').write_text(json.dumps(facts,default=str,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'score_inputs':[{key:value for key,value in entry.items() if key not in ['bars','symbol','config','prior_target_scores']}
    |{'symbol':entry['symbol']['symbol'],'config_name':entry['config']['name'],
      'bar_count':len(entry['bars']),'prior_scores':len(entry['prior_target_scores'])} for entry in facts['score_inputs']],
    'model_count':len(facts['models']),'models':[{key:model[key] for key in ['id','name','status','asset_type','readiness']} for model in facts['models']],
    'source_counts':facts['source_counts'],'active_tasks':facts['active_tasks']},ensure_ascii=False,default=str))
engine.dispose()
