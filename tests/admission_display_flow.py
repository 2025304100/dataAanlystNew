"""Real HTTP fixture phases for admission UI; no admission/result stubs."""
import json
from tests.combination_auto_http_helpers import api_json,append_market_day,publish_scores

def save(folder,name,value):
    (folder/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,default=str,indent=2),encoding='utf8')

def observe(client,pid,folder,name,eligible):
    state=api_json(client,'GET',f'/portfolios/{pid}/status')
    runtime=api_json(client,'GET',f'/portfolios/{pid}/inspection/runtime')
    assert state['permissions']==runtime['permissions']
    assert state['is_auto_simulation_eligible'] is eligible,(state,runtime)
    save(folder,name,{'status':state,'runtime':runtime})
    return runtime

def review_fixture(client,engine,folder,database):
    moved=client.post('/__isolated_e2e__/clock',json={'utc':'2026-10-22T13:00:00'});moved.raise_for_status()
    assert moved.json()['database']==database
    options=api_json(client,'GET','/portfolios/1/factor-usage-options')
    model=next(row for row in options['factor_models'] if row['factor_model_run_id']==options['global_active_model_run_id'])
    model_id,set_id=model['factor_model_run_id'],model['factor_set_id']
    pid=api_json(client,'POST','/portfolios',{'name':'实时准入组合','account_type':'simulated','asset_scope':'stock',
        'total_capital':100000,'investable_ratio':.9,'cash_reserve_ratio':.1,'key_members_json':[100],'auto_trade_enabled':1})['id']
    path=f'/portfolios/{pid}'
    api_json(client,'POST',path+'/members',{'symbol_id':100,'source_type':'manual','execution_mode':'auto','priority':50})
    rule=api_json(client,'POST',path+'/rules',{'rule_name':'实时准入展示回归','max_single_position_pct':.2,
        'max_sector_position_pct':1,'max_stock_position_pct':.9,'max_etf_position_pct':.9,'max_loss_per_trade_pct':.08,
        'max_open_positions':10,'is_active':True,'factor_set_id':set_id,'factor_model_run_id':model_id,
        'stage_limits_json':{'stages':{'stock':{'S':.05,'M':.1,'L':.2},'etf':{'S':.1,'M':.2,'L':.4}},'price_stop_loss_pct':.05,'drawdown_circuit_pct':.1}})
    binding={'factor_model_run_id':model_id,'factor_set_id':set_id,'run_mode':'production_sim','pit_mode':'strict_pit_safe',
        'rule_id':rule['id'],'rule_version':1,'key_members_json':[100]}
    preflight=api_json(client,'POST',path+'/strategy-preflight',binding);assert preflight['ok'],preflight
    applied=api_json(client,'POST',path+'/factor-usage',binding)
    chain={'portfolio_id':pid,'model_id':model_id,'factor_set_id':set_id,'snapshot_id':applied['strategy_snapshot_id']}
    save(folder,'display-chain',chain)
    return finish_review(client,engine,chain,folder)

def finish_review(client,engine,chain,folder):
    pid=chain['portfolio_id'];path=f'/portfolios/{pid}'
    # The 22nd evening application is effective on the 23rd. Exercise the
    # genuine next-day model/Score boundary instead of changing effective dates.
    append_market_day(engine,'2026-10-23')
    client.post('/__isolated_e2e__/clock',json={'utc':'2026-10-23T08:00:00'}).raise_for_status()
    publish_scores(client,chain['model_id'],chain['factor_set_id'],'2026-10-23',folder)
    api_json(client,'POST',path+'/confirm-model-binding',{'snapshot_id':chain['snapshot_id'],'model_run_id':chain['model_id'],
        'ack':True,'review_note':'隔离回归核实真实模型、当日评分和当前成员绑定，保留历史证据'})
    runtime=observe(client,pid,folder,'review-required',False)
    assert runtime['recovery_summary']['can_confirm'],runtime
    assert runtime['current_state']=='PENDING_INITIAL_REVIEW'
    return chain

def waiting_fixture(client,chain,folder):
    pid=chain['portfolio_id'];path=f'/portfolios/{pid}'
    runtime=api_json(client,'GET',path+'/inspection/runtime')
    api_json(client,'POST',path+'/inspection/guide/review',{'expected_snapshot_id':runtime['guide']['snapshot_id'],
        'expected_action':runtime['recovery_summary']['confirmation_action'],'idempotency_key':'display-initial-review',
        'acknowledge_current_proofs':True,'review_note':'本人核实隔离组合当前数据与绑定，初次审核不改写任何旧期结果'})
    request={'coverage_start_trade_date':'2026-10-26','history_gaps':[{'trade_date_from':'2026-10-21','trade_date_to':'2026-10-23',
        'reason':'新建隔离回归组合无早期自动执行材料，原数据保留，历史继续未知',
        'attempted_sources':['恢复副本与现存账本'],'evidence_refs':[str(folder/'clone-integrity.json')]}]}
    preview=api_json(client,'POST',path+'/inspection/coverage/preview',request);assert preview['can_accept'],preview
    accepted=api_json(client,'POST',path+'/inspection/coverage/accept',{**request,
        'expected_evidence_fingerprint':preview['evidence_fingerprint'],'expected_cursor_row_version':preview['cursor_row_version'],
        'acceptance_key':'display-new-baseline-accept','review_note':'已核验新组合现金与空仓，保留历史未知并承担新范围核对责任','acknowledge_history_unknown':True})
    review=api_json(client,'GET',path+'/inspection/coverage/activation-review');assert review['can_confirm'],review
    api_json(client,'POST',path+'/inspection/coverage/activate',{'baseline_id':accepted['baseline_id'],'generation':accepted['generation'],
        'expected_cursor_row_version':review['cursor_row_version'],'expected_evidence_fingerprint':review['evidence_fingerprint'],
        'activation_key':'display-new-baseline-activate','acknowledge_new_scope':True,'review_note':'核实真实自动成员与模型准入，仅启用下一交易日范围，旧期未知不变'})
    runtime=observe(client,pid,folder,'waiting-coverage',False)
    assert runtime['recovery_summary']['phase']=='WAITING_COVERAGE_START',runtime

def healthy_fixture(client,engine,chain,folder):
    append_market_day(engine,'2026-10-26')
    client.post('/__isolated_e2e__/clock',json={'utc':'2026-10-26T08:00:00'}).raise_for_status()
    publish_scores(client,chain['model_id'],chain['factor_set_id'],'2026-10-26',folder)
    runtime=observe(client,chain['portfolio_id'],folder,'healthy-runtime',True)
    assert runtime['permissions']['allow_new_buys'] is True and runtime['current_state']=='READY'

def stale_fixture(client,chain,folder):
    client.post('/__isolated_e2e__/clock',json={'utc':'2026-10-28T13:00:00'}).raise_for_status()
    runtime=observe(client,chain['portfolio_id'],folder,'stale-runtime',False)
    assert runtime['permissions']['allow_new_buys'] is False and runtime['current_state']=='READY'
