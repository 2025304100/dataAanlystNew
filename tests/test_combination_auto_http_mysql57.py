"""Real HTTP/worker/Ridge/Score/automatic-scope lane, without readiness stubs."""
import json
import os
from pathlib import Path
import httpx
import pytest
from tests.combination_auto_support import migrate,seed_market,source_hashes,start_api,wait_task

pytestmark=pytest.mark.whitebox

def test_real_training_activation_score_and_automatic_admission():
    raw=os.environ.get("COMBINATION_AUTO_E2E_URL")
    if not raw:pytest.skip("COMBINATION_AUTO_E2E_URL must name a fresh isolated MySQL 5.7 schema")
    evidence=Path(os.environ["COMBINATION_AUTO_E2E_EVIDENCE"]).resolve();evidence.mkdir(parents=True,exist_ok=True)
    engine,migration=migrate(raw)
    (evidence/'migration.json').write_text(json.dumps(migration,indent=2),encoding='utf8')
    fixture=seed_market(engine,evidence)
    from tests.combination_auto_http_helpers import seed_score_history,score_rows
    seed_score_history(engine)
    history=score_rows(engine)
    assert len(history)==32
    (evidence/'score-history-before.json').write_text(json.dumps(history,default=str,indent=2),encoding='utf8')
    (evidence/"fixture.json").write_text(json.dumps(fixture,indent=2),encoding="utf-8")
    before=source_hashes(engine)
    process,url,log=start_api(raw,evidence)
    (evidence/"server.json").write_text(json.dumps({"pid":process.pid,"url":url,"database":migration["after"]["database"]}),encoding="utf-8")
    (evidence/'source-baseline.json').write_text(json.dumps(before,indent=2),encoding='utf8')
    try:
        run_http_chain(engine,url,fixture,evidence,before)
        assert score_rows(engine,max(row['id'] for row in history))==history
        (evidence/'score-history-preserved.json').write_text(json.dumps({'original_rows':32,'all_columns_equal':True}),encoding='utf8')
    finally:
        if os.environ.get('COMBINATION_AUTO_KEEP_SERVER')!='1':
            process.terminate()
            try:process.wait(timeout=15)
            except Exception:process.kill();process.wait(timeout=15)
            log.close()
        engine.dispose()


def run_http_chain(engine,url,fixture,evidence,before):
    from tests.combination_auto_http_flow import configure_and_accept,run_automatic_days
    with httpx.Client(base_url=url,timeout=90,trust_env=False) as client:
        # Disable all unrelated startup schedules through the product API.
        schedules=client.get('/api/v1/scheduled-tasks').json()
        assert len(schedules)>10, schedules
        for row in schedules:
            r=client.patch(f"/api/v1/scheduled-tasks/{row['id']}",json={'enabled':False})
            assert r.status_code==200,r.text
        compiled=client.post('/api/v1/factors/ep_ttm/versions',json={"formula_expr":"1 / pe_ttm",
            "source_mapping":{"table":"raw_valuation_snapshots","field":"pe_ttm"},"created_by":"isolated-e2e"})
        assert compiled.status_code==201,compiled.text
        assert compiled.json()['validation_status']=='valid',compiled.text
        response=client.post('/api/v1/factor-sets',json={"name":"Isolated automatic genuine Ridge"})
        assert response.status_code==200,response.text
        set_id=response.json()['id']
        response=client.post(f'/api/v1/factor-sets/{set_id}/members',json={"factor_code":"ep_ttm","factor_version":compiled.json()['version'],"role":"feature","missing_policy":"exclude"})
        assert response.status_code==200,response.text
        response=client.post(f'/api/v1/scoring/factorsets/{set_id}/freeze',json={"actor":"isolated-e2e","reason":"Frozen actual computed feature"})
        assert response.status_code==200,response.text
        response=client.post('/api/v1/scoring/tasks',json={"train_model":True,"materialize_scores":False,
            "factor_set_id":set_id,"start_date":fixture['days'][0],"end_date":"2026-10-19",
            "window_days":250,"validation_days":50,"actor":"isolated-e2e"})
        assert response.status_code==200,response.text
        task=wait_task(client,response.json()['id'])
        (evidence/"training-task.json").write_text(json.dumps(task,ensure_ascii=False,indent=2),encoding='utf-8')
        assert task['status']=='done',task
        assert task['result']['model']['status']=='validated',task
        model_id=task['result']['model']['model_run_id']
        (evidence/'chain.json').write_text(json.dumps({'url':url,'factor_set_id':set_id,'model_id':model_id}),encoding='utf-8')
        assert source_hashes(engine,before)==before
        chain=configure_and_accept(client,engine,model_id,set_id,evidence)
        run_automatic_days(client,engine,chain,evidence)
        after=source_hashes(engine,before)
        assert after==before
        (evidence/'source-integrity.json').write_text(json.dumps({'before':before,'after':after,'all_original_rows_equal':True},indent=2),encoding='utf8')
