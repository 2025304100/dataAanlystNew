"""G4 HTTP contracts on fully migrated, isolated evidence.

Historical IDs trace the seven legacy failures. No internal result/gate stubs.
"""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from freezegun import freeze_time

from app.api.routes import portfolio_governance as governance
from app.db.session import get_db
from app.models.sim_account import CashLedger
from app.models.universe import UniverseDailyBar, UniverseSymbol
from tests.g4_contract_support import DAY, NOTE, financial_rows, migrated_account_db, seed_account, seed_buy, seed_calendar

pytestmark=pytest.mark.whitebox


@pytest.fixture(scope='module',autouse=True)
def real_schema_before_clock_boundary():
    # Deferred imports while datetime is frozen can permanently embed
    # FakeDatetime in Pydantic annotations for later, unrelated HTTP tests.
    # Build the real full-app schema before crossing the external clock seam.
    from app.main import app
    app.openapi()


@pytest.fixture
def mounted(tmp_path,monkeypatch,request):
    with migrated_account_db(tmp_path,monkeypatch,request.node.nodeid) as db:
        # FastAPI resolves deferred date annotations again at mount time.
        # Mount before freezegun replaces date; only request time crosses the clock seam.
        from app.api.routes import portfolio_factor_usage, portfolio_inspection, portfolio_inspection_workflow
        app=FastAPI();app.include_router(governance.router,prefix='/api/v1')
        app.include_router(portfolio_factor_usage.router,prefix='/api/v1')
        app.include_router(portfolio_inspection.router,prefix='/api/v1')
        app.include_router(portfolio_inspection_workflow.router,prefix='/api/v1')
        app.openapi()
        def isolated_db():
            try:yield db
            except Exception:
                db.rollback();raise
        app.dependency_overrides[get_db]=isolated_db
        with freeze_time('2026-10-01T00:00:00Z',real_asyncio=True) as clock:
            p=seed_account(db)
            with TestClient(app) as client:
                yield client,db,p,clock


def endpoint(p,suffix):
    return f'/api/v1/portfolios/{p.id}/{suffix}'


def get(client,p,suffix,**kwargs):
    response=client.get(endpoint(p,suffix),**kwargs)
    assert response.status_code==200,response.text
    return response.json()


def post(client,p,suffix,body,expected=200):
    response=client.post(endpoint(p,suffix),json=body,params={'X-User':'g4-reviewer'})
    assert response.status_code==expected,response.text
    return response.json()


def transition(client,p,state,**kwargs):
    return post(client,p,'transition-state',{'to_state':state,**kwargs})


def block(client,p):
    transition(client,p,'READY',review_note=NOTE)
    transition(client,p,'RUNNING_AUTO_SIMULATION')
    transition(client,p,'RECONCILIATION_BLOCKED')


def preview(client,p):
    return get(client,p,'reconciliation-preview',params={'trade_date':DAY.isoformat()})


def confirmation(report,**overrides):
    return {'trade_date':DAY.isoformat(),'acknowledge_all_diffs_cleared':True,'review_note':NOTE,
        'report_id':report['report_id'],'report_version':report['report_version'],**overrides}


def test_g4_api_01_status(mounted):
    client,db,p,_=mounted
    result=get(client,p,'status')
    assert result['current_state']=='PENDING_INITIAL_REVIEW'
    assert result['portfolio_id']==p.id
    assert result['allowed_transitions']==['READY','ADMIN_PAUSED']
    assert result['is_auto_simulation_eligible'] is False
    assert result['permissions']['allow_new_buys'] is False
    assert result['admission_checked_at']


def test_g4_api_02_reconcile(mounted):
    client,db,p,_=mounted
    missing=post(client,p,'reconcile',{'trade_date':DAY.isoformat()})
    assert missing['status']=='BLOCKED'
    assert 'DECISION_RUN_NOT_FOUND' in missing['reason_codes']
    assert missing['last_reconciled_trade_date'] is None
    seed_buy(db,p)
    before=financial_rows(db)
    passed=post(client,p,'reconcile',{'trade_date':DAY.isoformat()})
    assert passed['status']=='PASSED',passed
    assert passed['last_reconciled_trade_date']==DAY.isoformat()
    assert passed['expected_cash']==passed['actual_cash']==989995
    assert passed['actual_position_count']==passed['expected_position_count']==1
    assert financial_rows(db)==before


def test_g4_api_03_confirm(mounted):
    client,db,p,_=mounted
    transition(client,p,'READY',review_note=NOTE)
    post(client,p,'confirm-reconciliation',confirmation({'report_id':'not-a-report','report_version':'not-a-version'}),428)
    block(client,p)
    blocked=preview(client,p)
    post(client,p,'confirm-reconciliation',confirmation(blocked,acknowledge_all_diffs_cleared=False),400)
    post(client,p,'confirm-reconciliation',confirmation(blocked),409)
    seed_buy(db,p)
    passed=preview(client,p)
    assert passed['status']=='PASSED',passed
    before=financial_rows(db)
    ok=post(client,p,'confirm-reconciliation',confirmation(passed))
    assert ok['portfolio_now_ready'] is True
    assert ok['from_state']=='RECONCILIATION_BLOCKED' and ok['to_state']=='READY'
    assert ok['last_reconciled_trade_date']==DAY.isoformat()
    assert financial_rows(db)==before
    audit=get(client,p,'audit-events',params={'action':'DATA_BLOCK_RESOLUTION'})
    attrs=next(row['attributes'] for row in audit['items'] if row['attributes'].get('confirmation_type')=='RECONCILIATION_CONFIRMED')
    assert attrs['report_id']==passed['report_id'] and attrs['report_version']==passed['report_version']
    assert attrs['review_note']==NOTE


def test_g4_api_04_transition(mounted):
    client,db,p,_=mounted
    transition(client,p,'READY',review_note=NOTE)
    post(client,p,'transition-state',{'to_state':'DOES_NOT_EXIST'},400)
    assert transition(client,p,'ADMIN_PAUSED',reason='Review isolated account')['to_state']=='ADMIN_PAUSED'
    post(client,p,'transition-state',{'to_state':'RUNNING_AUTO_SIMULATION'},409)
    before=financial_rows(db)
    post(client,p,'transition-state',{'to_state':'READY'},422)
    assert get(client,p,'status')['current_state']=='ADMIN_PAUSED'
    ok=transition(client,p,'READY',review_note=NOTE)
    assert ok['status']=='OK' and ok['to_state']=='READY'
    assert financial_rows(db)==before


def seed_bars(db,*,region='cn',partial=False):
    for index in range(20 if partial else 1):
        symbol=UniverseSymbol(symbol=f'G4-market-{region}-{index}',name='G4 input',asset_type='stock',market='sh',region=region,is_synced=1)
        db.add(symbol);db.flush()
        # Match the original 10% partial-day scenario: 20 previous-day bars,
        # but only 2 on the independently due day. It must remain the due day.
        dates=[datetime(2026,9,29).date()]
        if not partial or index<2:dates.append(datetime(2026,9,30).date())
        if not partial:dates.append(datetime(2026,10,1).date())
        db.add_all(UniverseDailyBar(universe_symbol_id=symbol.id,trade_date=day,open=10,high=10,low=10,close=10,volume=1) for day in dates)
    db.commit()


def test_g4_api_08a_excludes_today(mounted):
    """A stored current-day bar cannot make a pre-close session due."""
    client,db,p,_=mounted
    seed_calendar(db);seed_bars(db)
    assert post(client,p,'reconcile',{})['trade_date']=='2026-09-30'


def test_g4_api_08b_skips_incomplete_day(mounted):
    """Legacy ID: incomplete bars must NOT rewind the independently due day."""
    client,db,p,_=mounted
    seed_calendar(db);seed_bars(db,partial=True)
    result=post(client,p,'reconcile',{})
    assert result['trade_date']=='2026-09-30'
    assert result['status']=='BLOCKED' and result['last_reconciled_trade_date'] is None


def test_g4_api_08c_us_region_does_not_count(mounted):
    client,db,p,_=mounted
    seed_calendar(db);seed_bars(db,region='us',partial=True)
    assert post(client,p,'reconcile',{})['trade_date']=='2026-09-30'


def test_g4_api_08d_falls_back_when_universe_empty(mounted):
    """Legacy ID: empty bars use verified calendar, never natural yesterday."""
    client,db,p,_=mounted
    seed_calendar(db,closed=[datetime(2026,9,30).date()])
    assert post(client,p,'reconcile',{})['trade_date']=='2026-09-29'


def test_g4_api_08e_route_uses_default_only_when_payload_omits_date(mounted):
    client,db,p,_=mounted
    seed_calendar(db)
    assert post(client,p,'reconcile',{})['trade_date']=='2026-09-30'
    explicit='2026-09-28'
    assert post(client,p,'reconcile',{'trade_date':explicit})['trade_date']==explicit


def test_g4_http_confirmation_contract_rejections(mounted):
    client,db,p,_=mounted
    block(client,p);seed_buy(db,p)
    report=preview(client,p);assert report['status']=='PASSED'
    before=financial_rows(db)
    for change,status in [
        ({'report_id':None},422),({'report_version':None},422),
        ({'acknowledge_all_diffs_cleared':False},400),({'review_note':'short'},422),
        ({'force_skip':True},400),({'force_skip_re_reconcile':True},400),({'force_rerun_before':False},400),
    ]:
        post(client,p,'confirm-reconciliation',confirmation(report,**change),status)
        assert get(client,p,'status')['current_state']=='RECONCILIATION_BLOCKED'
        assert financial_rows(db)==before


def test_g4_http_stale_and_wrong_report_rejected(mounted):
    client,db,p,_=mounted
    block(client,p);seed_buy(db,p)
    report=preview(client,p);assert report['status']=='PASSED'
    before=financial_rows(db)
    for change in ({'report_id':'reconciliation::999999::2026-09-29'},{'report_version':'tampered'}):
        result=post(client,p,'confirm-reconciliation',confirmation(report,**change),409)
        assert 'RECONCILIATION_REPORT_CHANGED' in result['detail']
        assert financial_rows(db)==before
    # A real, arithmetically consistent new movement still invalidates old consent.
    db.add(CashLedger(portfolio_id=p.id,entry_type='deposit',amount=100,balance_after=990095,
        ref_type='manual',note='New isolated evidence after preview',created_at=datetime(2026,9,30,2)))
    db.commit();after=financial_rows(db)
    current=preview(client,p)
    assert current['status']=='PASSED' and current['report_version']!=report['report_version']
    result=post(client,p,'confirm-reconciliation',confirmation(report),409)
    assert 'RECONCILIATION_REPORT_CHANGED' in result['detail']
    assert get(client,p,'status')['last_reconciled_trade_date'] is None
    assert financial_rows(db)==after


def test_g4_http_generic_ready_never_clears_reconciliation(mounted):
    client,db,p,_=mounted
    block(client,p);seed_buy(db,p)
    assert preview(client,p)['status']=='PASSED'
    before=financial_rows(db)
    result=post(client,p,'transition-state',{'to_state':'READY','review_note':NOTE},409)
    assert 'RECONCILIATION_CONFIRMATION_REQUIRED' in result['detail']
    assert get(client,p,'status')['current_state']=='RECONCILIATION_BLOCKED'
    assert financial_rows(db)==before


def test_g4_http_incomplete_execution_stays_blocked(mounted):
    client,db,_p,_=mounted
    for fixture,reason in [({'fill':False},'UNFILLED_PLAN'),({'cash':False},'NAV_BROKEN')]:
        p=seed_account(db);seed_buy(db,p,**fixture)
        before=financial_rows(db)
        result=post(client,p,'reconcile',{'trade_date':DAY.isoformat()})
        assert result['status']=='BLOCKED' and reason in result['reason_codes']
        assert result['last_reconciled_trade_date'] is None
        assert get(client,p,'status')['is_auto_simulation_eligible'] is False
        assert financial_rows(db)==before


@pytest.mark.parametrize('kind',['absent','unproven','incomplete','wrong-source','wrong-actor','future-stamp'])
def test_g4_http_unknown_calendar_no_fallback(mounted,kind):
    client,db,p,_=mounted
    if kind=='unproven':seed_calendar(db,proof=False)
    elif kind=='incomplete':seed_calendar(db,missing=[DAY])
    elif kind=='wrong-source':seed_calendar(db,provenance={'source':'unverified-source'})
    elif kind=='wrong-actor':seed_calendar(db,actor='unverified-import')
    elif kind=='future-stamp':seed_calendar(db,provenance={'refreshed_at':'2026-10-02T00:00:00Z'})
    seed_bars(db)
    before=financial_rows(db)
    result=post(client,p,'reconcile',{},409)
    assert result['detail']['error_code']=='RECONCILIATION_CALENDAR_UNKNOWN'
    assert get(client,p,'status')['current_state']=='PENDING_INITIAL_REVIEW'
    assert financial_rows(db)==before


def test_g4_http_calendar_close_weekend_and_holiday_boundaries(mounted):
    client,db,p,clock=mounted
    seed_calendar(db,closed=[datetime(2026,10,2).date()])
    before=financial_rows(db)
    # Read-only previews avoid creating an earlier unresolved day in the queue.
    for now,expected in [('2026-10-01T06:59:59Z','2026-09-30'),
                         ('2026-10-01T07:00:00Z','2026-10-01'),
                         ('2026-10-04T08:00:00Z','2026-10-01')]:
        clock.move_to(now)
        result=get(client,p,'reconciliation-preview')
        assert result['trade_date']==expected and result['read_only'] is True
    assert financial_rows(db)==before


def test_g4_http_explicit_date_diagnosis_does_not_invent_calendar_pass(mounted):
    client,db,p,_=mounted
    before=financial_rows(db)
    result=post(client,p,'reconcile',{'trade_date':DAY.isoformat()})
    assert result['trade_date']==DAY.isoformat() and result['status']=='BLOCKED'
    assert result['last_reconciled_trade_date'] is None
    assert financial_rows(db)==before


def test_g4_http_confirmation_retains_other_dimensions(mounted):
    client,db,p,_=mounted
    block(client,p);seed_buy(db,p)
    p.status_data='DATA_INCOMPLETE_PAUSED';p.status_model='MODEL_INACTIVE';db.commit()
    report=preview(client,p);assert report['status']=='PASSED'
    before=financial_rows(db)
    result=post(client,p,'confirm-reconciliation',confirmation(report))
    db.refresh(p)
    assert result['portfolio_now_ready'] is False
    assert p.status_reconciliation=='READY'
    assert p.status_data=='DATA_INCOMPLETE_PAUSED' and p.status_model=='MODEL_INACTIVE'
    assert get(client,p,'status')['is_auto_simulation_eligible'] is False
    assert financial_rows(db)==before


def test_g4_http_confirmation_does_not_accept_unreviewed_applied_binding(mounted):
    client,db,p,_=mounted
    block(client,p);seed_buy(db,p,snapshot_type='save_and_apply')
    report=preview(client,p);assert report['status']=='PASSED'
    before=financial_rows(db)
    result=post(client,p,'confirm-reconciliation',confirmation(report))
    assert result['portfolio_now_ready'] is False
    assert get(client,p,'status')['is_auto_simulation_eligible'] is False
    assert financial_rows(db)==before


def test_g4_http_applied_binding_is_immediately_effective_at_fractional_second(mounted):
    from tests.test_stage3_factor_binding_contract import _ready_model, _activate
    client,db,p,clock=mounted
    model=_ready_model(db,set_id="fs-clock-contract",model_id="model-clock-contract")
    _activate(db,model.id);db.commit()
    before=financial_rows(db)
    clock.move_to('2026-10-01T00:00:00.800000Z')
    result=post(client,p,'factor-usage',{
        'factor_model_run_id':model.id,'factor_set_id':'fs-clock-contract',
        'run_mode':'production_sim','pit_mode':'strict_pit_safe'})
    diagnosis=get(client,p,'inspection/data-diagnostics')
    assert not diagnosis['binding'].get('not_effective'),diagnosis['binding']
    assert datetime.fromisoformat(diagnosis['generated_at'].replace('Z','+00:00')).microsecond==800000
    from tests.test_whitebox_portfolio_confirmation import _legacy_state
    p.status_reconciliation='RECONCILIATION_BLOCKED'
    p.status_score='SCORE_STALE';p.status_data='DATA_INCOMPLETE_PAUSED'
    _legacy_state(db,p,'MODEL_INACTIVE');db.commit()
    review=get(client,p,'model-binding-review')
    assert review['can_confirm'] is True,review
    assert review['snapshot_id']==result['strategy_snapshot_id']
    accepted=post(client,p,'confirm-model-binding',{'snapshot_id':review['snapshot_id'],
        'model_run_id':model.id,'ack':True,'review_note':NOTE})
    assert accepted['current_state']=='RECONCILIATION_BLOCKED'
    db.refresh(p)
    assert p.status_model=='READY' and p.status_reconciliation=='RECONCILIATION_BLOCKED'
    assert p.status_score=='SCORE_STALE' and p.status_data=='DATA_INCOMPLETE_PAUSED'
    assert financial_rows(db)==before
