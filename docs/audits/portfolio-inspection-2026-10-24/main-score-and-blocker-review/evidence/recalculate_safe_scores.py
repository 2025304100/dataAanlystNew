"""Append only verified current-rule scores; model and trading gates stay intact."""
from pathlib import Path
from datetime import date,datetime,timezone
import hashlib
import json
import math
import sys
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
from sqlalchemy import create_engine,event,select
from sqlalchemy.orm import Session
import requests
from app.core.config import build_mysql_url,load_db_config
from app.models.daily_bar import DailyBar
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.scoring_config import ScoringConfig
from app.services.scoring_config_engine import calculate_symbol_score_with_config,parse_config_json,_enabled_external_factor_keys
from app.services.data_governance_audit import write_audit_event
from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar

root=Path(__file__).parent
with urlopen('http://127.0.0.1:8000/api/v1/inspection-acceptance/status',timeout=5) as response:
    mode=json.load(response)
assert mode['mode']=='inspection_acceptance' and not any(mode[key] for key in [
    'background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed'])
before=json.loads((root/'preflight.json').read_text(encoding='utf-8'))
verified={row['symbol']:row for row in json.loads((root/'rule-input-verification.json').read_text(encoding='utf-8')) if row['status']=='VERIFIED'}
target=date.fromisoformat(before['trade_date'])
assert target==date(2026,10,23) and verified
requests.sessions.Session.request=lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('Score calculation must not contact HTTP services'))
engine=create_engine(build_mysql_url(load_db_config()))
audit_writes=[]
@event.listens_for(engine,'before_cursor_execute')
def score_writes_only(conn,cursor,statement,parameters,context,executemany):
    verb=statement.lstrip().split(None,1)[0].upper()
    if verb in {'SELECT','SHOW','DESCRIBE','SET'}:return
    table=getattr(getattr(getattr(context,'compiled',None),'statement',None),'table',None)
    if verb!='INSERT' or getattr(table,'name',None) not in {'scores','data_governance_audit_events'}:
        raise RuntimeError('Only append-only scores and linked audit are authorized')
    audit_writes.append(table.name)

def row_values(row):return {column.name:getattr(row,column.name) for column in row.__table__.columns}
def digest(value):return hashlib.sha256(json.dumps(value,default=str,sort_keys=True).encode()).hexdigest()

results=[]
with Session(engine) as db:
    assert resolve_inspection_calendar(db,current_read=True)['expected_trade_date']==target
    for entry in before['score_inputs']:
        code=entry['symbol']['symbol']
        if code not in verified:continue
        symbol=db.get(Symbol,entry['symbol']['id'],with_for_update=True)
        config=db.get(ScoringConfig,entry['config']['id'],with_for_update=True)
        assert config.is_active and config.version==entry['config']['version']
        assert config.asset_type==symbol.asset_type and digest(config.config_json)==entry['config_sha256']
        assert not _enabled_external_factor_keys(parse_config_json(config))
        bars=list(db.scalars(select(DailyBar).where(DailyBar.symbol_id==symbol.id,DailyBar.trade_date<=target)
            .order_by(DailyBar.trade_date.desc()).limit(80).with_for_update()))[::-1]
        assert digest([row_values(row) for row in bars])==entry['bars_sha256']
        assert len(bars)>=50 and bars[-1].trade_date==target
        batch=f'sc-{config.id}-v{config.version}-{target.isoformat()}'
        existing=db.scalar(select(Score).where(Score.symbol_id==symbol.id,Score.trade_date==target,Score.calc_batch_id==batch).with_for_update())
        if existing is not None:raise RuntimeError('This score batch already exists; no existing score was overwritten')
        score=calculate_symbol_score_with_config(db,symbol,target,config=config)
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        assert score.weight_mode=='manual' and score.factor_model_run_id is None and score.factor_set_id is None
        assert all(math.isfinite(getattr(score,key)) for key in ['quality_score','timing_score','priority_score'])
        freshness=json.loads(score.factor_scores_json)['_data_freshness']
        assert freshness['latest_bar_trade_date']==target.isoformat() and freshness['expected_trade_date']==target.isoformat()
        assert score.pit_safety!='PIT_VERIFIED'
        output={'symbol':code,'name':symbol.name,'score_id':score.id,'trade_date':target.isoformat(),
            'config_id':config.id,'config_name':config.name,'config_version':config.version,
            'quality_score':score.quality_score,'timing_score':score.timing_score,'priority_score':score.priority_score,
            'data_credibility':score.data_credibility,'published_at':score.published_at.isoformat(),
            'factor_scores':json.loads(score.factor_scores_json),'dimension_scores':json.loads(score.dimension_scores_json),
            'weight_mode':'manual','production_model_score':False,'pit_safety':score.pit_safety,
            'input_bars_sha256':entry['bars_sha256'],'configuration_sha256':entry['config_sha256'],
            'runtime_state_changed':False,'formal_binding_remains_blocked':True}
        audit=write_audit_event(db,'UNKNOWN_AUDIT_ACTION',portfolio_id=1,symbol_id=symbol.id,
            business_key='main-safe-rule-score-20261024:'+str(symbol.id),occurred_at=now,
            operator_id='codex-local-maintenance',after=output,
            attributes={'original_action':'INSPECTION_RULE_SCORE_REFRESH','executed_by':'user_authorized_local_maintenance',
                'objects':[{'symbol_id':symbol.id,'symbol':code,'trade_date':target.isoformat()}],
                'scope':'current_active_rule_only','does_not_resolve_frozen_model_score':True,
                'input_verification':verified[code]})
        output['audit_event_id']=audit.event_id
        results.append(output)
    assert len(results)==len(verified)
    db.commit()
engine.dispose()
result={'completed':results,'skipped':[row['symbol']['symbol'] for row in before['score_inputs'] if row['symbol']['symbol'] not in verified],
    'allowed_write_tables':sorted(set(audit_writes)),'runtime_state_changed':False,'market_bars_changed':False,
    'original_score_rows_overwritten':False,'model_or_strategy_changed':False}
(root/'score-refresh-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
