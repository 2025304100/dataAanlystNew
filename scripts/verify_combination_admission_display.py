"""Replay genuine model admission over HTTP in a new MySQL 5.7 fixture copy.

The source must be the stopped, retained auto_http acceptance fixture, after
its Oct 22 simulated fill. No application configuration fallback is allowed.
Neither source nor target is dropped, truncated or reused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET

from sqlalchemy import create_engine,text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.admission_display_support import guarded_url,prepare,snapshot,critical_snapshot


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-database',required=True)
    parser.add_argument('--evidence-dir',required=True,type=Path)
    args=parser.parse_args()
    raw=os.environ.get('ADMISSION_DISPLAY_SOURCE_URL')
    if not raw:parser.error('ADMISSION_DISPLAY_SOURCE_URL must be explicit')
    source_url=guarded_url(raw)
    if not re.fullmatch(r'codex_combination_[a-z0-9_]{1,40}',args.target_database):
        parser.error('Use a new codex_combination_* database name')
    if args.target_database==source_url.database:parser.error('Source and target must differ')
    folder=args.evidence_dir.resolve()
    if folder.exists() and any(folder.iterdir()):parser.error('Use a new evidence directory')
    folder.mkdir(parents=True,exist_ok=True)
    target_url=source_url.set(database=args.target_database)
    server_engine=create_engine(source_url._replace(database=None),hide_parameters=True)
    with server_engine.begin() as conn:
        assert str(conn.scalar(text('SELECT VERSION()'))).startswith('5.7.')
        assert conn.scalar(text('SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=:db'),
                           {'db':args.target_database})==0,'Never overwrite an existing schema'
        conn.exec_driver_sql(f'CREATE DATABASE `{args.target_database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    server_engine.dispose()
    server=prepare(raw,target_url.render_as_string(hide_password=False),folder)
    source=create_engine(source_url,hide_parameters=True)
    target=create_engine(target_url,hide_parameters=True)
    report={'database':args.target_database,'source_database':source_url.database,'passed':False}
    try:
        env=dict(os.environ,ADMISSION_DISPLAY_DB_URL=target_url.render_as_string(hide_password=False),
            ADMISSION_DISPLAY_API_URL=server['url'],ADMISSION_DISPLAY_EVIDENCE=str(folder),
            ADMISSION_DISPLAY_RUN_FLOW='1',DATABASE_URL='sqlite:///:memory:',PYTHONIOENCODING='utf-8')
        with (folder/'http.log').open('w',encoding='utf8') as log:
            run=subprocess.run([sys.executable,'-m','pytest','tests/test_combination_admission_http_mysql57.py',
                '-k','test_full_real_model','-q','--tb=short','--junitxml',str(folder/'http.xml')],
                cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
        suites=list(ET.parse(folder/'http.xml').iter('testsuite'))
        counts={key:sum(int(suite.get(key,'0')) for suite in suites) for key in ('tests','failures','errors','skipped')}
        original=json.loads((folder/'clone-integrity.json').read_text(encoding='utf8'))
        report.update(counts=counts,exit_code=run.returncode,
            original_critical_rows_unchanged=critical_snapshot(target,json.loads((folder/'critical-before.json').read_text(encoding='utf8')))==
                json.loads((folder/'critical-before.json').read_text(encoding='utf8')),
            source_all_tables_unchanged=snapshot(source)==original['all_tables'])
        with source.connect() as conn:
            warehouse=Path(conn.scalar(text('SELECT warehouse_path FROM factor_system_config WHERE id=1')))
        report['source_warehouse_unchanged']=hashlib.sha256(warehouse.read_bytes()).hexdigest()==original['warehouse_copy_sha256']
        report['passed']=run.returncode==0 and counts['tests']==1 and not any(counts[key] for key in ('failures','errors','skipped')) and all(
            report[key] for key in ('original_critical_rows_unchanged','source_all_tables_unchanged','source_warehouse_unchanged'))
    finally:
        with target.connect() as conn:
            active=conn.scalar(text("SELECT COUNT(*) FROM async_tasks WHERE status IN ('queued','running')"))
        report['active_tasks']=active
        if active==0:
            os.kill(server['pid'],signal.SIGTERM)
            report['temporary_api_stopped']=True
        else:
            report['temporary_api_stopped']=False
            report['passed']=False
        source.dispose();target.dispose()
        (folder/'acceptance-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
