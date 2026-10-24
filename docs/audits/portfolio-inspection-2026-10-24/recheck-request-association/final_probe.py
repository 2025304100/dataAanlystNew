import json,sys,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from app.core.config import load_db_config,build_mysql_url
from sqlalchemy import create_engine,text
def get(path):
    with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=30) as r:return json.load(r)
runtime=get('/api/v1/portfolios/1/inspection/runtime');queue=get('/api/v1/portfolios/1/inspection/reconciliation-queue')
state={'mode':get('/api/v1/inspection-acceptance/status'),'permissions':runtime['permissions'],
    'can_confirm':runtime['recovery_summary']['can_confirm'],'audit':get('/api/v1/portfolios/1/inspection/history?page_size=1')['snapshot_event_id'],
    'verified_through':queue['verified_through'],'coverage_start':queue['coverage_start']}
before=json.loads((Path(__file__).parent/'safety-before.json').read_text());assert state==before
try:
    get('/api/v1/portfolios/1/inspection/reconciliation-job/requests/read-only-not-found-probe')
    raise AssertionError('unexpected receipt')
except urllib.error.HTTPError as exc:
    assert exc.code==404
    response=json.load(exc)
engine=create_engine(build_mysql_url(load_db_config()))
with engine.connect() as c:
    count=c.scalar(text('SELECT COUNT(*) FROM reconciliation_recheck_requests'))
    assert count==1  # Retain the single unexpected QA association; never erase evidence.
    keys=c.execute(text('SELECT request_key, task_id FROM reconciliation_recheck_requests')).all()
    assert keys==[('45fd110f-7cc0-4111-b0e2-4df89a42e1c6','9a27ac797035cd19619f3a832c92ee00')]
    revision=c.scalar(text('SELECT version_num FROM alembic_version'))
result={'state':state,'actual_request_receipts':count,'revision':revision,'negative_lookup':{'status':404,'response':response}}
recovered=get('/api/v1/portfolios/1/inspection/reconciliation-job/requests/45fd110f-7cc0-4111-b0e2-4df89a42e1c6')
assert recovered['task_id']=='9a27ac797035cd19619f3a832c92ee00' and recovered['task_status']=='done'
assert recovered['reconciliation_passed'] is False and recovered['trading_resumed'] is False
result['retained_association_readback']={key:recovered[key] for key in ['portfolio_id','request_key','task_id','task_status','reconciliation_passed','trading_resumed']}
before_db=json.loads((Path(__file__).parent/'database-before.json').read_text())
with engine.connect() as c:
    changed={}
    for name,old in before_db['tables'].items():
        quoted=c.dialect.identifier_preparer.quote(name)
        current={'rows':c.scalar(text(f'SELECT COUNT(*) FROM {quoted}')),'checksum':c.execute(text(f'CHECKSUM TABLE {quoted} EXTENDED')).first()[1]}
        if old!=current:changed[name]={'before':old,'after':current}
    result['changed_existing_tables_since_migration']=changed
(Path(__file__).parent/'final-safety.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps({'safety_unchanged':True,'actual_request_receipts':count,'negative_lookup':404,'audit':state['audit']}))
