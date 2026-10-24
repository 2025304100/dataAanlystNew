"""Explicit local additive migration. Does not initialize app lifespan or tasks."""
import json,sys,hashlib,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from sqlalchemy import create_engine,inspect,text
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config
from app.core.config import load_db_config,build_mysql_url
DEST=Path(__file__).parent
def safety():
    def get(path):
        with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=30) as r:return json.load(r)
    runtime=get('/api/v1/portfolios/1/inspection/runtime');queue=get('/api/v1/portfolios/1/inspection/reconciliation-queue')
    return {'mode':get('/api/v1/inspection-acceptance/status'),'permissions':runtime['permissions'],
        'can_confirm':runtime['recovery_summary']['can_confirm'],'audit':get('/api/v1/portfolios/1/inspection/history?page_size=1')['snapshot_event_id'],
        'verified_through':queue['verified_through'],'coverage_start':queue['coverage_start']}
def main():
    import os
    assert not os.getenv('DATABASE_URL') and not os.getenv('ALEMBIC_DATABASE_URL')
    state=safety();assert not state['mode']['background_jobs_enabled'] and not state['mode']['automatic_trading_enabled'] and not state['mode']['final_reviews_allowed']
    (DEST/'safety-before.json').write_text(json.dumps(state,indent=2),encoding='utf8')
    cfg=load_db_config();assert cfg.get('use_mysql')
    url=make_url(build_mysql_url(cfg));assert url.host=='127.0.0.1' and url.database=='gpfx'
    engine=create_engine(url)
    def snapshot():
        with engine.connect() as c:
            names=sorted(inspect(c).get_table_names());values={}
            for name in names:
                if name in {'alembic_version','reconciliation_recheck_requests'}:continue
                quoted=c.dialect.identifier_preparer.quote(name)
                values[name]={'rows':c.scalar(text(f'SELECT COUNT(*) FROM {quoted}')),
                    'checksum':c.execute(text(f'CHECKSUM TABLE {quoted} EXTENDED')).first()[1]}
            revision=c.scalar(text('SELECT version_num FROM alembic_version'))
            return {'revision':revision,'tables':values}
    before=snapshot();assert before['revision']=='wps_0023_076_inspection_workflow'
    assert not inspect(engine).has_table('reconciliation_recheck_requests')
    (DEST/'database-before.json').write_text(json.dumps(before,indent=2),encoding='utf8')
    command.upgrade(Config(str(ROOT/'alembic.ini')),'wps_0024_077_recheck_requests')
    after=snapshot();assert after['revision']=='wps_0024_077_recheck_requests' and before['tables']==after['tables']
    with engine.connect() as c:assert c.scalar(text('SELECT COUNT(*) FROM reconciliation_recheck_requests'))==0
    (DEST/'database-after.json').write_text(json.dumps(after,indent=2),encoding='utf8')
    print(json.dumps({'migration':'passed','existing_tables_unchanged':len(before['tables']),'new_receipts':0}))
if __name__=='__main__':main()
