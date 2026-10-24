"""Explicit additive provenance write authorized by the user, outside the server."""
import sys,json,hashlib
from pathlib import Path
from datetime import datetime
import urllib.request
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from sqlalchemy import create_engine,text,select
from sqlalchemy.orm import Session
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config
from app.core.config import load_db_config,build_mysql_url
from app.models.portfolio_inspection_workflow import ReconciliationRecheckRequest,ReconciliationRequestAnnotation
OUT=Path(__file__).parent
KEY='45fd110f-7cc0-4111-b0e2-4df89a42e1c6'
SOURCE='ACCEPTANCE_ROUTE_TEARDOWN_LEAK'
NOTE='该关联产生于本地界面验收：关闭发起页时浏览器拦截撤销，一次来源重检登记意外到达后端。关联的是已有对账任务；本记录保留用于追溯，不代表正式业务发起、核对通过或交易授权。'
EVIDENCE='docs/audits/portfolio-inspection-2026-10-24/recheck-request-association/README.md'
def get(path):
    with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=30) as r:return json.load(r)
def safety():
    rt=get('/api/v1/portfolios/1/inspection/runtime');q=get('/api/v1/portfolios/1/inspection/reconciliation-queue')
    return {'permissions':rt['permissions'],'can_confirm':rt['recovery_summary']['can_confirm'],
        'audit':get('/api/v1/portfolios/1/inspection/history?page_size=1')['snapshot_event_id'],
        'verified_through':q['verified_through'],'coverage_start':q['coverage_start']}
def engine():
    import os
    assert not os.getenv('DATABASE_URL') and not os.getenv('ALEMBIC_DATABASE_URL')
    cfg=load_db_config();assert cfg.get('use_mysql')
    url=make_url(build_mysql_url(cfg));assert url.host=='127.0.0.1' and url.database=='gpfx'
    return create_engine(url)
def snapshot(e):
    from sqlalchemy import inspect
    with e.connect() as c:
        tables={}
        for name in sorted(inspect(c).get_table_names()):
            if name in {'alembic_version','reconciliation_request_annotations'}:continue
            q=c.dialect.identifier_preparer.quote(name)
            tables[name]={'rows':c.scalar(text(f'SELECT COUNT(*) FROM {q}')),
                'checksum':c.execute(text(f'CHECKSUM TABLE {q} EXTENDED')).first()[1]}
        return {'revision':c.scalar(text('SELECT version_num FROM alembic_version')),'tables':tables}
def save(name,value): (OUT/name).write_text(json.dumps(value,ensure_ascii=False,default=str,indent=2),encoding='utf8')
def main():
    e=engine();state=safety();save('safety-before.json',state)
    before=snapshot(e);save('database-before.json',before)
    assert before['revision']=='wps_0024_077_recheck_requests'
    with e.connect() as c:
        original=dict(c.execute(text('SELECT * FROM reconciliation_recheck_requests WHERE portfolio_id=1 AND request_key=:key'),{'key':KEY}).mappings().one())
        assert original['task_id']=='9a27ac797035cd19619f3a832c92ee00'
    save('retained-request-before.json',original)
    command.upgrade(Config(str(ROOT/'alembic.ini')),'wps_0024_078_request_origin')
    aid=hashlib.sha256((original['id']+':'+SOURCE).encode()).hexdigest()
    with Session(e) as db:
        note=db.get(ReconciliationRequestAnnotation,aid)
        if note is None:
            db.add(ReconciliationRequestAnnotation(id=aid,request_id=original['id'],source_code=SOURCE,
                note=NOTE,evidence_ref=EVIDENCE,annotated_by='codex-local-acceptance-investigation'))
            db.commit()
        else:assert note.note==NOTE and note.evidence_ref==EVIDENCE
    with e.connect() as c:
        retained=dict(c.execute(text('SELECT * FROM reconciliation_recheck_requests WHERE portfolio_id=1 AND request_key=:key'),{'key':KEY}).mappings().one())
        assert retained==original
        assert c.scalar(text('SELECT COUNT(*) FROM reconciliation_request_annotations'))==1
    after=snapshot(e);assert after['revision']=='wps_0024_078_request_origin' and before['tables']==after['tables']
    save('database-after-migration.json',after)
    save('annotation.json',{'id':aid,'source_code':SOURCE,'note':NOTE,'evidence_ref':EVIDENCE,
        'request_unchanged':True,'existing_tables_unchanged':len(before['tables'])})
    print(json.dumps({'migration':'passed','annotation_count':1,'original_unchanged':True,'existing_tables_unchanged':len(before['tables'])}))
if __name__=='__main__':main()
