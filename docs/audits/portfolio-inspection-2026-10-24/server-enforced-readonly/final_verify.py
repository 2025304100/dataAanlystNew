import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from migrate_and_annotate import engine,snapshot,safety,OUT,KEY,get
from sqlalchemy import text
e=engine();before=json.loads((OUT/'database-before.json').read_text(encoding='utf8'));after=snapshot(e)
assert before['tables']==after['tables'] and after['revision']=='wps_0024_078_request_origin'
with e.connect() as c:
    original=dict(c.execute(text('SELECT * FROM reconciliation_recheck_requests WHERE portfolio_id=1 AND request_key=:key'),{'key':KEY}).mappings().one())
    expected=json.loads((OUT/'retained-request-before.json').read_text(encoding='utf8'))
    assert json.loads(json.dumps(original,default=str))==expected
    assert c.scalar(text('SELECT COUNT(*) FROM reconciliation_recheck_requests'))==1
    assert c.scalar(text('SELECT COUNT(*) FROM reconciliation_request_annotations'))==1
    annotation=dict(c.execute(text('SELECT * FROM reconciliation_request_annotations')).mappings().one())
current=safety();assert current==json.loads((OUT/'safety-before.json').read_text(encoding='utf8'))
mode=get('/api/v1/inspection-acceptance/status');assert mode['read_only'] and not mode['task_dispatch_allowed'] and not mode['background_jobs_enabled']
result={'state':current,'mode':mode,'revision':after['revision'],'existing_tables_unchanged':len(after['tables']),
    'request_count':1,'annotation_count':1,'original_request_unchanged':True,'annotation':annotation,'tables':after['tables']}
(OUT/'final-safety.json').write_text(json.dumps(result,ensure_ascii=False,default=str,indent=2),encoding='utf8')
print(json.dumps({key:value for key,value in result.items() if key not in {'tables','annotation'}},ensure_ascii=False))
