import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tmp/inspection-server-readonly-2026-10-24'))
from migrate_and_annotate import engine,snapshot,safety,get
from sqlalchemy import text
OUT=Path(__file__).parent
previous=json.loads((ROOT/'docs/audits/portfolio-inspection-2026-10-24/server-enforced-readonly/final-safety.json').read_text(encoding='utf8'))
e=engine();after=snapshot(e);assert previous['tables']==after['tables'] and after['revision']==previous['revision']
with e.connect() as c:
    assert c.scalar(text('SELECT COUNT(*) FROM reconciliation_recheck_requests'))==1
    assert c.scalar(text('SELECT COUNT(*) FROM reconciliation_request_annotations'))==1
    annotation=dict(c.execute(text('SELECT * FROM reconciliation_request_annotations')).mappings().one())
    assert json.loads(json.dumps(annotation,default=str))==previous['annotation']
    original=dict(c.execute(text('SELECT * FROM reconciliation_recheck_requests')).mappings().one())
    expected=json.loads((ROOT/'docs/audits/portfolio-inspection-2026-10-24/server-enforced-readonly/retained-request-before.json').read_text(encoding='utf8'))
    assert json.loads(json.dumps(original,default=str))==expected
current=safety();assert current==previous['state']
mode=get('/api/v1/inspection-acceptance/status');assert mode['read_only'] and not mode['task_dispatch_allowed'] and not mode['task_registration_allowed'] and not mode['background_jobs_enabled']
result={'state':current,'mode':mode,'revision':after['revision'],'existing_tables_unchanged':157,
    'request_count':1,'annotation_count':1,'original_request_unchanged':True,'annotation_unchanged':True,'tables':after['tables']}
(OUT/'final-safety.json').write_text(json.dumps(result,ensure_ascii=False,default=str,indent=2),encoding='utf8')
print(json.dumps({k:v for k,v in result.items() if k!='tables'},ensure_ascii=False))
