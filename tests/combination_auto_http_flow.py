"""Executable public HTTP business flow; only market inputs and time are fixtures."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import time
from sqlalchemy import text
from tests.combination_auto_http_helpers import (
    api_json, append_market_day, financial_rows, publish_scores, wait_task,
)


def save(evidence, name, value):
    (evidence/(name+".json")).write_text(json.dumps(value, default=str, ensure_ascii=False, indent=2), encoding="utf-8")


def equity_snapshot(client, schedules):
    schedule = next(row for row in schedules if row['task_type']=='portfolio_equity_snapshot')
    task = api_json(client, 'POST', f"/scheduled-tasks/{schedule['id']}/run")
    task_id = task['task_id']
    # All opaque task types share the public task reader.
    result = wait_task(client, task_id)
    assert result['status']=='done', result
    return result


def configure_and_accept(client, engine, model_id, set_id, evidence):
    pid = api_json(client, 'POST', '/portfolios', {
        'name':'Real automatic model HTTP acceptance', 'account_type':'simulated', 'asset_scope':'stock',
        'total_capital':100000, 'investable_ratio':.9, 'cash_reserve_ratio':.1,
        'key_members_json':[100], 'auto_trade_enabled':1})['id']
    prefix = f'/portfolios/{pid}'
    api_json(client, 'POST', prefix+'/members', {'symbol_id':100,'source_type':'manual','execution_mode':'auto','priority':50})
    rule = api_json(client, 'POST', prefix+'/rules', {
        'rule_name':'Real Ridge with risk controls', 'max_single_position_pct':.2,
        'max_sector_position_pct':1, 'max_stock_position_pct':.9, 'max_etf_position_pct':.9,
        'max_loss_per_trade_pct':.08, 'max_open_positions':10, 'is_active':True,
        'factor_set_id':set_id, 'factor_model_run_id':model_id,
        'stage_limits_json':{'stages':{'stock':{'S':.05,'M':.1,'L':.2},'etf':{'S':.1,'M':.2,'L':.4}},
            'price_stop_loss_pct':.05,'drawdown_circuit_pct':.1}})
    binding = {'factor_model_run_id':model_id, 'factor_set_id':set_id, 'run_mode':'production_sim',
        'pit_mode':'strict_pit_safe','rule_id':rule['id'],'rule_version':1,'key_members_json':[100]}
    untouched = financial_rows(engine)
    # An actual shadow runtime and shadow Scores cannot authorize production simulation.
    api_json(client,'POST',f'/scoring/models/{model_id}/activate',{'mode':'shadow','actor':'isolated-e2e'})
    publish_scores(client,model_id,set_id,'2026-10-20',evidence,mode='shadow')
    blocked = api_json(client,'POST',prefix+'/strategy-preflight',binding)
    save(evidence,'shadow-preflight',blocked)
    assert not blocked['ok'], blocked
    assert financial_rows(engine)==untouched
    api_json(client,'POST',f'/scoring/models/{model_id}/activate',{'mode':'ridge','actor':'isolated-e2e'})
    publish_scores(client,model_id,set_id,'2026-10-20',evidence)
    preflight = api_json(client,'POST',prefix+'/strategy-preflight',binding)
    save(evidence,'ridge-preflight',preflight)
    assert preflight['ok'], preflight
    applied = api_json(client,'POST',prefix+'/factor-usage',binding)
    snapshot_id = applied['strategy_snapshot_id']
    options = api_json(client,'GET',prefix+'/factor-usage-options')
    save(evidence,'model-options',options)
    model = next(m for m in options['factor_models'] if m['factor_model_run_id']==model_id)
    assert model['sample_count']==25000 and model['data_cutoff_at']
    review = api_json(client,'GET',prefix+'/model-binding-review')
    assert review['can_confirm'], review
    api_json(client,'POST',prefix+'/confirm-model-binding',{'snapshot_id':snapshot_id,
        'model_run_id':model_id,'ack':True,'review_note':'Verified actual validated model and pinned Ridge Scores'})
    schedules = api_json(client,'GET','/scheduled-tasks')
    equity_snapshot(client,schedules)
    initial=api_json(client,'GET',prefix+'/inspection/runtime')
    assert initial['guide']['can_confirm'] and initial['recovery_summary']['can_confirm'],initial
    reviewed=api_json(client,'POST',prefix+'/inspection/guide/review',{
        'expected_snapshot_id':initial['guide']['snapshot_id'],
        'expected_action':initial['recovery_summary']['confirmation_action'],
        'idempotency_key':'real-auto-initial-review','acknowledge_current_proofs':True,
        'review_note':'Reviewed actual automatic members, current model Scores and all applicable initial prerequisites'})
    assert reviewed['current_state']=='READY',reviewed
    save(evidence,'initial-personal-review',reviewed)
    gap = {'trade_date_from':'2026-10-19','trade_date_to':'2026-10-20',
        'reason':'Fresh isolated synthetic fixture contains no prior automatic execution evidence; preserve history as unknown',
        'attempted_sources':['Isolated immutable source rows','Current account ledger'],
        'evidence_refs':[str(evidence/'fixture.json'),str(evidence/'source-baseline.json')]}
    request = {'coverage_start_trade_date':'2026-10-21','history_gaps':[gap]}
    preview = api_json(client,'POST',prefix+'/inspection/coverage/preview',request)
    assert preview['can_accept'], preview
    accepted = api_json(client,'POST',prefix+'/inspection/coverage/accept',{
        **request,'expected_evidence_fingerprint':preview['evidence_fingerprint'],
        'expected_cursor_row_version':preview['cursor_row_version'],'acceptance_key':'real-auto-baseline-accept',
        'review_note':'Verified current source cash, empty holdings and absence of unsettled obligations',
        'acknowledge_history_unknown':True})
    activation = api_json(client,'GET',prefix+'/inspection/coverage/activation-review')
    save(evidence,'automatic-scope-review',activation)
    assert activation['can_confirm'], activation
    data = next(c for c in activation['checks'] if c['key']=='data')
    assert data['has_admission_scope'] is True and data['coverage']['entry_symbols']==1
    assert next(c for c in activation['checks'] if c['key']=='binding')['requires_confirmation'] is False
    payload = {'baseline_id':accepted['baseline_id'],'generation':accepted['generation'],
        'expected_cursor_row_version':activation['cursor_row_version'],
        'expected_evidence_fingerprint':activation['evidence_fingerprint'],
        'activation_key':'real-auto-baseline-activate','acknowledge_new_scope':True,
        'review_note':'Verified actual automatic member, current data and accepted model binding; preserve unknown history'}
    first = api_json(client,'POST',prefix+'/inspection/coverage/activate',payload)
    replay = api_json(client,'POST',prefix+'/inspection/coverage/activate',payload)
    save(evidence,'automatic-scope-activation',{'first':first,'replay':replay})
    chain = {'portfolio_id':pid,'model_id':model_id,'factor_set_id':set_id,'snapshot_id':snapshot_id}
    save(evidence,'chain',chain)
    return chain


def run_automatic_days(client, engine, chain, evidence):
    pid=chain['portfolio_id'];prefix=f'/portfolios/{pid}'
    schedules=api_json(client,'GET','/scheduled-tasks')
    original=financial_rows(engine)
    client.post('/__isolated_e2e__/clock',json={'utc':'2026-10-20T12:35:00'}).raise_for_status()
    # Coverage starts tomorrow; today's real request must still be refused.
    response=client.post('/api/v1'+prefix+'/auto-trade/execute',json={'dry_run':False})
    save(evidence,'before-coverage-execution',{'status':response.status_code,'body':response.json()})
    assert response.status_code==400,response.text
    assert financial_rows(engine)==original
    # Advance inputs by appending observed data. Existing raw history is immutable.
    append_market_day(engine,'2026-10-21')
    client.post('/__isolated_e2e__/clock',json={'utc':'2026-10-21T08:00:00'}).raise_for_status()
    publish_scores(client,chain['model_id'],chain['factor_set_id'],'2026-10-21',evidence)
    equity_snapshot(client,schedules)
    client.post('/__isolated_e2e__/clock',json={'utc':'2026-10-21T12:35:00'}).raise_for_status()
    dry=api_json(client,'POST',prefix+'/auto-trade/execute',{'dry_run':True})
    save(evidence,'real-model-dryrun',dry)
    assert dry['readiness']['eligible_buy_count']==1 and dry['readiness']['ready'],dry
    buy=dry['buys'][0]
    for field in ('symbol','name','order_plan_id','evidence_id','quantity','buy_quantity','ref_price','signal_date','execution_date'):
        assert buy.get(field), (field,buy)
    assert buy['signal_date']=='2026-10-21' and buy['execution_date']=='2026-10-22'
    assert buy['buy_quantity']==buy['quantity'] and buy['ref_price']>0
    assert financial_rows(engine)==original
    schedule=next(row for row in schedules if row['task_type']=='portfolio_auto_trade')
    api_json(client,'PATCH',f"/scheduled-tasks/{schedule['id']}",{
        'enabled':True,'time_of_day':'23:59','payload':{'dry_run':True,'buy_candidate_limit':10}})
    try:
        ready=api_json(client,'GET',prefix+'/auto-trade/readiness?for_schedule=true')
        assert ready['ready'] and ready['schedule_ready'],ready
        dispatched=api_json(client,'POST',f"/scheduled-tasks/{schedule['id']}/run")
        done=wait_task(client,dispatched['task_id'])
        save(evidence,'actual-auto-schedule-dryrun',done)
        assert done['status']=='done',done
        assert done['result']['portfolio_results'][0]['readiness_ready'] is True,done
        assert done['result']['portfolio_results'][0]['buys_count']==1,done
        assert done['result']['portfolio_results'][0]['errors']==[],done
    finally:
        api_json(client,'PATCH',f"/scheduled-tasks/{schedule['id']}",{'enabled':False})
    assert financial_rows(engine)==original
    # Same real HTTP entry from two independent request threads. Orders and
    # immutable plan IDs, rather than response timing, establish idempotence.
    def request(_):
        r=client.post('/api/v1'+prefix+'/auto-trade/execute',json={'dry_run':False})
        return {'status':r.status_code,'body':r.json()}
    with ThreadPoolExecutor(max_workers=2) as pool: responses=list(pool.map(request,range(2)))
    save(evidence,'concurrent-auto-requests',responses)
    assert all(r['status'] in (200,409) for r in responses),responses
    with engine.connect() as conn:
        plans=conn.execute(text('SELECT * FROM decision_order_plans WHERE portfolio_id=:pid'),{'pid':pid}).mappings().all()
        save(evidence,'persisted-plans',[dict(row) for row in plans])
        assert len([row for row in plans if row['action']=='BUY'])==1,plans
    assert not financial_rows(engine)['sim_trades'], 'No fill before the next execution date'
    append_market_day(engine,'2026-10-22')
    client.post('/__isolated_e2e__/clock',json={'utc':'2026-10-22T08:00:00'}).raise_for_status()
    publish_scores(client,chain['model_id'],chain['factor_set_id'],'2026-10-22',evidence)
    equity_snapshot(client,schedules)
    client.post('/__isolated_e2e__/clock',json={'utc':'2026-10-22T12:35:00'}).raise_for_status()
    filled=client.post('/api/v1'+prefix+'/auto-trade/execute',json={'dry_run':False})
    save(evidence,'next-day-execution',{'status':filled.status_code,'body':filled.json()})
    assert filled.status_code==200,filled.text
    assert len(filled.json()['execution_result']['executed_orders'])==1,filled.text
    finance=financial_rows(engine)
    assert len(finance['sim_trades'])==1,finance
    order=next(row for row in finance['sim_orders'] if row['filled_quantity']>0)
    assert order['client_order_key']==buy['order_plan_id']
    assert order['quantity']==buy['quantity'] and order['filled_price']>0 and order['fee']>0
    assert finance['positions'][0]['quantity']==order['filled_quantity']
    save(evidence,'financial-after-fill',finance)
    # Deliberately do not republish: a 2-day-old Score must fail closed and
    # preserve the complete financial state, including any original intent.
    client.post('/__isolated_e2e__/clock',json={'utc':'2026-10-26T12:35:00'}).raise_for_status()
    before=financial_rows(engine)
    stale=client.post('/api/v1'+prefix+'/auto-trade/execute',json={'dry_run':False})
    save(evidence,'stale-score-execution',{'status':stale.status_code,'body':stale.json()})
    assert stale.status_code==409,stale.text
    assert any(b['code']=='MARKET_DATA_STALE' for b in stale.json()['extras']['blockers']),stale.text
    assert financial_rows(engine)==before
    return {'dryrun':dry,'concurrent':responses,'stale_status':stale.status_code}
