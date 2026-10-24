"""A later dispatch must not inherit an old terminal task or start its worker."""
from datetime import datetime
from unittest.mock import Mock

import pytest
from app.models.async_task import AsyncTaskRecord
from app.services import auto_trade_task

pytestmark = pytest.mark.whitebox


def test_same_dispatch_day_deduplicates_but_shanghai_next_day_creates_new_task(db_session, monkeypatch):
    clock = [datetime(2026, 10, 22, 15, 59)]
    monkeypatch.setattr(auto_trade_task, '_now', lambda: clock[0])
    monkeypatch.setattr(auto_trade_task, 'list_async_tasks', lambda **kwargs: [])
    start = Mock()
    monkeypatch.setattr(auto_trade_task, '_start_worker', start)
    first = auto_trade_task.create_portfolio_auto_trade_task(dry_run=False)
    row = db_session.get(AsyncTaskRecord, first.id)
    row.status = 'done';row.is_terminal_locked = 1;db_session.commit()
    same = auto_trade_task.create_portfolio_auto_trade_task(dry_run=False)
    assert same.id == first.id and same.status == 'done'
    assert start.call_count == 1
    clock[0] = datetime(2026, 10, 22, 16, 1)
    following = auto_trade_task.create_portfolio_auto_trade_task(dry_run=False)
    assert following.id != first.id and following.status == 'queued'
    assert start.call_count == 2
    assert '2026-10-22' in row.payload_json
    assert '2026-10-23' in db_session.get(AsyncTaskRecord, following.id).payload_json


def test_terminal_worker_cannot_reenter_portfolio_execution(db_session, monkeypatch):
    row = AsyncTaskRecord(id='terminal-auto-acceptance',task_type='portfolio_auto_trade',status='done',stage='done',
        payload_json='{}',is_terminal_locked=1)
    db_session.add(row);db_session.commit()
    execute = Mock(side_effect=AssertionError('Terminal task must not execute a portfolio'))
    monkeypatch.setattr(auto_trade_task, 'run_auto_trade', execute)
    auto_trade_task._run_auto_trade_task(row.id)
    execute.assert_not_called()
    db_session.expire_all()
    assert db_session.get(AsyncTaskRecord,row.id).status == 'done'
