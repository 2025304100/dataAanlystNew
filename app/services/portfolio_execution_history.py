"""Retained dispatch context cannot stand in for a day's execution evidence."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from app.models.async_task import AsyncTaskRecord
from app.models.scheduled_task import ScheduledTask, ScheduledTaskRun


def _object(raw):
    try:
        value = json.loads(raw or '{}')
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def read_execution_dispatch_history(db, portfolio_id: int, trade_date: date) -> dict:
    """Show exactly linked original task facts, without inferring execution.

    The date scopes observed dispatches, not a reconstructed signal date.
    Portfolio scope comes from the retained original task, never today's
    schedule configuration or portfolio-name text.
    """
    fact = {'evidence_id': f'execution-dispatch:{portfolio_id}:{trade_date}',
        'source_code': 'execution_dispatch_history', 'source_label': '自动调度与原任务历史',
        'query_status': 'CONTEXT_UNAVAILABLE', 'count': None, 'trade_date': trade_date.isoformat(),
        'date_basis': 'observed_dispatch_date_only', 'source_record_refs': [], 'records': [],
        'reason': '调度历史来源尚不可读取；不能从任务状态推断本日已执行', 'proves_execution': False}
    required = {
        'scheduled_tasks': {'id','task_type'},
        'scheduled_task_runs': {'id','schedule_id','task_source','task_id','status','created_at'},
        'async_tasks': {'id','task_type','created_at','finished_at','status','payload_json','result_json'},
    }
    inspector = inspect(db.connection())
    tables = set(inspector.get_table_names())
    if any(name not in tables or not cols <= {col['name'] for col in inspector.get_columns(name)}
            for name, cols in required.items()):
        return fact
    local = datetime.combine(trade_date, datetime.min.time(), ZoneInfo('Asia/Shanghai'))
    start = local.astimezone(timezone.utc).replace(tzinfo=None)
    end = (local + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    statement = select(ScheduledTaskRun.id.label('dispatch_id'), ScheduledTaskRun.schedule_id,
        ScheduledTaskRun.created_at.label('dispatch_created_at'), ScheduledTaskRun.status.label('dispatch_status'),
        AsyncTaskRecord.id.label('task_id'), AsyncTaskRecord.task_type,
        AsyncTaskRecord.created_at.label('task_created_at'), AsyncTaskRecord.finished_at.label('task_finished_at'),
        AsyncTaskRecord.status.label('task_status'), AsyncTaskRecord.payload_json, AsyncTaskRecord.result_json
    ).join(ScheduledTask, ScheduledTask.id == ScheduledTaskRun.schedule_id
    ).join(AsyncTaskRecord, AsyncTaskRecord.id == ScheduledTaskRun.task_id
    ).where(ScheduledTask.task_type == 'portfolio_auto_trade', ScheduledTaskRun.task_source == 'async',
        AsyncTaskRecord.task_type == 'portfolio_auto_trade',
        ScheduledTaskRun.created_at >= start, ScheduledTaskRun.created_at < end
    ).order_by(ScheduledTaskRun.id)
    from app.services.portfolio_reconciliation import _execute_read
    records = []
    for row in _execute_read(db, statement).mappings():
        payload, result = _object(row['payload_json']), _object(row['result_json'])
        items = result.get('portfolio_results', [])
        matching = [item for item in items if isinstance(item, dict) and item.get('portfolio_id') == portfolio_id] if isinstance(items,list) else []
        if payload.get('portfolio_id') != portfolio_id and not matching:
            continue
        cross_day = row['task_created_at'] < start
        stale = bool(cross_day and row['task_finished_at']
            and row['task_finished_at'] < row['dispatch_created_at']
            and row['task_status'] in {'done','partial','failed','cancelled'})
        scoped = {'portfolio_id': portfolio_id, 'portfolio_results': matching,
                  'dry_run': payload.get('dry_run', result.get('dry_run'))}
        record = {key: row[key] for key in ('schedule_id','dispatch_status','task_id','task_status')}
        record.update(id=row['dispatch_id'], dispatch_created_at=row['dispatch_created_at'].isoformat(),
            task_created_at=row['task_created_at'].isoformat(),
            task_finished_at=row['task_finished_at'].isoformat() if row['task_finished_at'] else None,
            original_scoped_result=scoped, stale_terminal_task_reused=stale,
            cross_day_task_reused=cross_day,
            observation_code='STALE_TERMINAL_TASK_REUSED' if stale else 'CROSS_DAY_TASK_REUSED' if cross_day else 'DISPATCH_CONTEXT_ONLY',
            proves_execution=False)
        record['source_hash'] = hashlib.sha256(json.dumps(record,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        records.append(record)
    stale_count = sum(row['stale_terminal_task_reused'] for row in records)
    cross_day_count = sum(row['cross_day_task_reused'] for row in records)
    fact.update(query_status='CONTEXT_ONLY', count=len(records), records=records,
        source_record_refs=[ref for row in records for ref in (f"scheduled_task_run:{row['id']}", f"async_task:{row['task_id']}")],
        reason=(f'本日 {len(records)} 条调度有本组合的原任务关联，其中 {cross_day_count} 条复用早期创建的任务'
                f'（{stale_count} 条可核实原终态早于本次调度）；旧任务完成不代表本日执行完成，'
                '原任务的当前起止时间和结果也不能还原每次历史执行，不能据此补造运行或消除对账缺口') if cross_day_count else
               '仅展示明确关联本组合的调度与原任务，不作为本日决策、订单或无活动结案证明')
    return fact
