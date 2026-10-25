"""Replay a retained real-model acceptance fixture into a fresh isolated schema."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from scripts.db_backup import backup_mysql, write_manifest
from scripts.db_restore import restore_mysql
from tests.combination_auto_support import start_api


def guarded_url(raw):
    url=make_url(raw)
    if url.get_backend_name()!='mysql' or not re.fullmatch(r'codex_combination_[A-Za-z0-9_]+',url.database or ''):
        raise ValueError('Explicit codex_combination_* MySQL database required')
    return url


def snapshot(engine,bounds=None,tables=None):
    result={}
    with engine.connect() as conn:
        for name in tables or sorted(inspect(conn).get_table_names()):
            predicate=' WHERE id<=:maximum' if bounds and bounds[name].get('max_id') is not None else ''
            parameters={'maximum':bounds[name]['max_id']} if predicate else {}
            hashes=[]
            for row in conn.execution_options(stream_results=True).execute(text(f'SELECT * FROM `{name}`'+predicate),parameters).mappings():
                encoded=json.dumps(dict(row),sort_keys=True,default=str,ensure_ascii=False,separators=(',',':')).encode()
                hashes.append(hashlib.sha256(encoded).digest())
            hashes.sort()
            result[name]={'rows':len(hashes),'sha256':hashlib.sha256(b''.join(hashes)).hexdigest()}
            if bounds and bounds[name].get('max_id') is not None:result[name]['max_id']=bounds[name]['max_id']
    return result


CRITICAL=('symbols','universe_symbols','daily_bars','universe_daily_bars','stock_valuations',
          'scores','cash_ledger','positions','sim_orders','sim_trades','portfolios')

def critical_snapshot(engine,bounds=None):
    if bounds is None:
        with engine.connect() as conn:
            bounds={name:{'max_id':conn.scalar(text(f'SELECT COALESCE(MAX(id),0) FROM `{name}`'))} for name in CRITICAL}
    return snapshot(engine,bounds,CRITICAL)


def prepare(source_raw,target_raw,evidence):
    source_url,target_url=guarded_url(source_raw),guarded_url(target_raw)
    assert source_url.database!=target_url.database
    evidence=Path(evidence).resolve();evidence.mkdir(parents=True,exist_ok=True)
    source=create_engine(source_url,hide_parameters=True);target=create_engine(target_url,hide_parameters=True)
    for engine in (source,target):
        with engine.connect() as conn:assert conn.scalar(text('SELECT VERSION()'))=='5.7.26'
    with target.connect() as conn:assert not inspect(conn).get_table_names(),'Never overwrite an existing schema'
    before=snapshot(source)
    with source.connect() as conn:
        basedir=Path(conn.scalar(text('SELECT @@basedir')))
        warehouse=Path(conn.scalar(text('SELECT warehouse_path FROM factor_system_config WHERE id=1'))).resolve()
        assert conn.scalar(text("SELECT COUNT(*) FROM async_tasks WHERE status IN ('queued','running')"))==0,'Fixture writers must be stopped'
    binary=lambda name:os.environ.get(name.upper()+'_BIN') or shutil.which(name) or str(basedir/'bin'/(name+('.exe' if os.name=='nt' else '')))
    artifact=backup_mysql(source_raw,evidence,binary('mysqldump'));write_manifest(artifact)
    manifest=json.loads(Path(artifact.manifest_file).read_text(encoding='utf8'))
    assert restore_mysql(manifest,evidence/artifact.backup_file,target_raw,binary('mysql'),False,False,False)==0
    restored=snapshot(target);assert restored==before,[name for name in before if restored.get(name)!=before[name]]
    assert snapshot(source)==before,'Source fixture changed while cloning'
    assert warehouse.is_file()
    copied=evidence/'warehouse.duckdb';assert not copied.exists();shutil.copy2(warehouse,copied)
    assert hashlib.sha256(warehouse.read_bytes()).digest()==hashlib.sha256(copied.read_bytes()).digest()
    with target.begin() as conn:
        conn.execute(text('UPDATE factor_system_config SET warehouse_path=:path WHERE id=1'),{'path':str(copied)})
    report={'source_database':source_url.database,'database':target_url.database,'mysql_version':'5.7.26',
        'all_tables':before,'table_count':len(before),'all_restored_rows_and_columns_equal':True,
        'source_fixture_unchanged':True,'warehouse_copy_sha256':hashlib.sha256(copied.read_bytes()).hexdigest()}
    (evidence/'clone-integrity.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    (evidence/'critical-before.json').write_text(json.dumps(critical_snapshot(target),indent=2),encoding='utf8')
    process,url,log=start_api(target_raw,evidence)
    server={'database':target_url.database,'pid':process.pid,'url':url}
    (evidence/'server.json').write_text(json.dumps(server),encoding='utf8')
    source.dispose();target.dispose()
    return server
