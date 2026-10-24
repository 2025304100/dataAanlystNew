from datetime import date, datetime, timezone
import json

import pytest
from app.models.async_task import AsyncTaskRecord
from app.models.scheduled_task import ScheduledTask, ScheduledTaskRun
from app.services.portfolio_execution_history import read_execution_dispatch_history
from app.services.portfolio_reconciliation import reconcile_trade_date
from tests.test_portfolio_inspection_reconciliation_services import account

pytestmark = pytest.mark.whitebox
DAY = date(2026,9,29)


def old_dispatch(db, portfolio_id, *, result_portfolio=None, dispatch_at=datetime(2026,9,29,6,55)):
    task=AsyncTaskRecord(id='retained-old-auto-task',task_type='portfolio_auto_trade',status='done',stage='done',
        created_at=datetime(2026,8,18,5,59),finished_at=datetime(2026,8,18,6),is_terminal_locked=1,
        payload_json='{"dry_run":false}',result_json=json.dumps({'portfolio_results':[
            {'portfolio_id':result_portfolio or portfolio_id,'error':'原任务行情过期'}]}))
    schedule=ScheduledTask(name='Actual daily dispatcher',task_type='portfolio_auto_trade',frequency='daily',
        time_of_day='14:55',timezone='Asia/Shanghai',enabled=1,payload_json='{}')
    db.add_all([task,schedule]);db.flush()
    run=ScheduledTaskRun(schedule_id=schedule.id,task_source='async',task_id=task.id,
        status='done',created_at=dispatch_at)
    db.add(run);db.commit()
    return task,run


def test_actual_stale_dispatch_is_context_not_execution_proof(account):
    db,p=account;task,dispatch=old_dispatch(db,p.id)
    fact=read_execution_dispatch_history(db,p.id,DAY)
    assert fact['query_status']=='CONTEXT_ONLY' and fact['count']==1
    assert fact['records'][0]['stale_terminal_task_reused']
    assert fact['records'][0]['cross_day_task_reused']
    assert not fact['proves_execution']
    assert fact['records'][0]['task_created_at'].startswith('2026-08-18')
    report=reconcile_trade_date(db,p.id,DAY,persist=False)
    assert report.verdict=='EVIDENCE_INCOMPLETE'
    assert not report.can_confirm
    assert 'DECISION_RUN_NOT_FOUND' in report.reason_codes
    assert any(r['source']=='execution_dispatch_history' and r['row_ids']==[str(dispatch.id)] for r in report.source_refs)
    assert '旧任务完成不代表本日执行完成' in fact['reason']


def test_later_overwritten_terminal_time_does_not_hide_cross_day_task_reuse(account):
    db,p=account;task,dispatch=old_dispatch(db,p.id)
    task.finished_at=datetime(2026,10,22,6,55);db.commit()
    fact=read_execution_dispatch_history(db,p.id,DAY)
    assert fact['records'][0]['cross_day_task_reused']
    assert not fact['records'][0]['stale_terminal_task_reused']
    assert fact['records'][0]['observation_code']=='CROSS_DAY_TASK_REUSED'
    assert not fact['proves_execution']
    assert '不能还原每次历史执行' in fact['reason']


def test_global_schedule_configuration_cannot_expose_another_portfolios_task(account):
    db,p=account;old_dispatch(db,p.id,result_portfolio=p.id+100)
    fact=read_execution_dispatch_history(db,p.id,DAY)
    assert fact['count']==0 and fact['records']==[]


def test_dispatch_date_uses_actual_shanghai_window_and_does_not_invent_signal_day(account):
    db,p=account;old_dispatch(db,p.id,dispatch_at=datetime(2026,9,28,16,1))
    fact=read_execution_dispatch_history(db,p.id,DAY)
    assert fact['count']==1
    assert fact['date_basis']=='observed_dispatch_date_only'
    assert read_execution_dispatch_history(db,p.id,date(2026,9,28))['count']==0


def test_recheck_audit_is_immediately_visible_with_mysql_whole_second_precision(account):
    from app.services.data_governance_audit import DataGovernanceAuditEvent
    from app.schemas.portfolio_inspection_history import HistoryFilters
    from app.services.portfolio_inspection_history import query_inspection_history
    db,p=account;old_dispatch(db,p.id)
    report=reconcile_trade_date(db,p.id,DAY,operator_id='automated-evidence-review')
    db.commit()
    event=db.get(DataGovernanceAuditEvent,report.audit.event_id)
    assert event.occurred_at.microsecond==0
    assert event.occurred_at<=datetime.now(timezone.utc).replace(tzinfo=None)
    page=query_inspection_history(db,p.id,HistoryFilters(trade_date_from=DAY,trade_date_to=DAY,
        report_ref=report.report_id,report_version=report.report_version))
    assert page.total==1 and page.relation_status=='matched'
    assert not report.can_confirm
