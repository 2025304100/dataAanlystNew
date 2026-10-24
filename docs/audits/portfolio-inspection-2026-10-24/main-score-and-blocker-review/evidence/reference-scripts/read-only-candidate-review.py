"""Verify retained candidate contracts with SQL mutations prohibited."""
import hashlib
import json
from pathlib import Path
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from app.core.config import build_mysql_url, load_db_config
from app.services.factors import production_execution as execution
from app.services.factors import production_prediction_journal as journal
from app.services.factors import production_release as release
from app.services.factors import production_cost_evaluation as cost
from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
from datetime import datetime, timezone, timedelta, date

root=Path('tmp/main-history-investigation')
plan='153f0acfa78e482437a65bcc7b19c9a15ccabac2765c801309ccf01c81ba5f40'
engine=create_engine(build_mysql_url(load_db_config()))
@event.listens_for(engine,'before_cursor_execute')
def read_only(conn,cursor,statement,parameters,context,executemany):
    if statement.lstrip().split(None,1)[0].upper() not in {'SELECT','SHOW','DESCRIBE','SET'}:
        raise RuntimeError('Candidate review is read-only')

facts={'read_only':True,'plan_id':plan,'checks':{},'files':[]}
with Session(engine,autoflush=False) as db:
    calendar=resolve_inspection_calendar(db)
    local=datetime.now(timezone(timedelta(hours=8)))
    facts['postboundary_calendar']={'status':calendar['status'],
        'closed_dates':[str(day) for day in sorted(calendar['trading_dates'])
            if day>date(2026,10,17) and (day<local.date() or day==local.date() and (local.hour,local.minute)>=(15,15))]}
    for name,reader in {
        'candidate_contract':lambda:journal._candidate(db,plan),
        'prediction_journal':lambda:journal.journal_status(db,plan),
        'release_review_original_bundle':lambda:release.review_release(db,plan,bundle_id='fixed-129-inputs-20261017-v1'),
    }.items():
        try:facts['checks'][name]={'status':'READ','result':reader()}
        except Exception as error:facts['checks'][name]={'status':'UNAVAILABLE','error_type':type(error).__name__,'reason':str(error)}
    facts['candidate_tasks']=[dict(row) for row in db.execute(text(
        "SELECT id,task_type,status,stage,created_at,finished_at,payload_json,result_json,errors_json FROM async_tasks "
        "WHERE task_type IN ('factor_production_candidate','factor_independent_evaluation') ORDER BY created_at,id"
    )).mappings()]
    facts['strategy_rules']=[dict(row) for row in db.execute(text('SELECT * FROM portfolio_rules WHERE portfolio_id=1 ORDER BY id')).mappings()]
    facts['signal_rules']=[dict(row) for row in db.execute(text('SELECT * FROM signal_rules WHERE portfolio_id=1 ORDER BY id')).mappings()]
    facts['main_members']=[dict(row) for row in db.execute(text('SELECT pm.*,s.symbol,s.name,s.asset_type FROM portfolio_members pm JOIN symbols s ON s.id=pm.symbol_id WHERE pm.portfolio_id=1 ORDER BY pm.id')).mappings()]
for path in sorted(execution.EXECUTION_ROOT.glob(plan+'*.json')):
    facts['files'].append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size})
(root/'candidate-review-details.json').write_text(json.dumps(facts,default=str,ensure_ascii=False,indent=2),encoding='utf-8')
for name,fact in facts['checks'].items():
    print(name,json.dumps(fact,default=str,ensure_ascii=False)[:6000])
print('candidate_tasks',len(facts['candidate_tasks']))
engine.dispose()
