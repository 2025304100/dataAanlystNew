"""Public HTTP admission regression against a real-model MySQL fixture copy."""
import json
import os
from pathlib import Path
import httpx
import pytest
from sqlalchemy import create_engine
from tests.admission_display_support import guarded_url,critical_snapshot
from tests.admission_display_flow import review_fixture,waiting_fixture,healthy_fixture,stale_fixture

pytestmark=pytest.mark.whitebox

def test_stale_score_overrides_ready_without_rewriting_audit_state_or_history():
    raw=os.environ.get('ADMISSION_DISPLAY_DB_URL');base=os.environ.get('ADMISSION_DISPLAY_API_URL')
    if not raw or not base:pytest.skip('Explicit isolated MySQL URL and loopback API required')
    assert base.startswith('http://127.0.0.1:')
    url=guarded_url(raw);engine=create_engine(url,hide_parameters=True)
    evidence=Path(os.environ['ADMISSION_DISPLAY_EVIDENCE'])
    before=critical_snapshot(engine)
    with httpx.Client(base_url=base,trust_env=False,timeout=60) as client:
        clock=client.post('/__isolated_e2e__/clock',json={'utc':'2026-10-26T12:35:00'});clock.raise_for_status()
        assert clock.json()['database']==url.database
        status=client.get('/api/v1/portfolios/1/status');status.raise_for_status()
        rows=client.get('/api/v1/portfolios?auto_tag_tests=false');rows.raise_for_status()
        runtime=client.get('/api/v1/portfolios/1/inspection/runtime');runtime.raise_for_status()
        payload={'status':status.json(),'list':rows.json(),'runtime':runtime.json()}
        (evidence/'stale-http.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8')
        assert status.json()['current_state']=='READY'
        assert next(row for row in rows.json() if row['id']==1)['key_members_json']==[100]
        assert runtime.json()['permissions']['allow_new_buys'] is False
        assert status.json()['is_auto_simulation_eligible'] is False
        assert status.json()['permissions']==runtime.json()['permissions']
    assert critical_snapshot(engine,before)==before
    (evidence/'http-read-integrity.json').write_text(json.dumps({'critical_tables':len(before),'all_original_rows_and_columns_equal':True}),encoding='utf8')
    engine.dispose()


def test_full_real_model_admission_display_http_flow_preserves_original_history():
    if os.environ.get('ADMISSION_DISPLAY_RUN_FLOW')!='1':
        pytest.skip('Run through scripts/verify_combination_admission_display.py with a fresh fixture copy')
    url=guarded_url(os.environ['ADMISSION_DISPLAY_DB_URL'])
    base=os.environ['ADMISSION_DISPLAY_API_URL']
    assert base.startswith('http://127.0.0.1:')
    evidence=Path(os.environ['ADMISSION_DISPLAY_EVIDENCE'])
    assert not (evidence/'display-chain.json').exists(),'The full flow requires a fresh fixture copy'
    engine=create_engine(url,hide_parameters=True)
    before=json.loads((evidence/'critical-before.json').read_text(encoding='utf8'))
    assert critical_snapshot(engine,before)==before
    try:
        with httpx.Client(base_url=base,trust_env=False,timeout=120) as client:
            chain=review_fixture(client,engine,evidence,url.database)
            for name,operation in (
                ('review',lambda:None),
                ('waiting',lambda:waiting_fixture(client,chain,evidence)),
                ('healthy',lambda:healthy_fixture(client,engine,chain,evidence)),
                ('stale',lambda:stale_fixture(client,chain,evidence)),
            ):
                operation()
                assert critical_snapshot(engine,before)==before,name+' changed an original critical row'
                (evidence/(name+'-integrity.json')).write_text(json.dumps({
                    'all_original_critical_rows_and_columns_equal':True,'tables':len(before)}),encoding='utf8')
    finally:
        engine.dispose()
