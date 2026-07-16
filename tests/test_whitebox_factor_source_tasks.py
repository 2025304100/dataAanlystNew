from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services.async_tasks import create_async_task, get_async_task
from app.services.financial_data import resolve_financial_report_symbols
from app.services.financial_report_task import (
    _run_financial_report_task,
)
from app.services.lhb_data import LhbInstitutionSyncResult
from app.services.lhb_institution_task import (
    _run_lhb_institution_task,
)
from app.services.scheduled_tasks import validate_task_payload


pytestmark = pytest.mark.whitebox


def _seed_watchlist(db_session):
    cn = Symbol(
        symbol="600519.SH",
        name="贵州茅台",
        asset_type="stock",
        market="sh",
    )
    us = Symbol(
        symbol="AAPL",
        name="Apple",
        asset_type="stock",
        market="us",
    )
    watchlist = Watchlist(name="default", list_type="manual")
    db_session.add_all([cn, us, watchlist])
    db_session.flush()
    db_session.add_all(
        [
            WatchlistItem(
                watchlist_id=watchlist.id, symbol_id=cn.id
            ),
            WatchlistItem(
                watchlist_id=watchlist.id, symbol_id=us.id
            ),
        ]
    )
    db_session.commit()
    return cn


def test_financial_scope_is_bounded_and_cn_only(db_session):
    cn = _seed_watchlist(db_session)
    rows = resolve_financial_report_symbols(
        db_session, source="watchlist", limit=20
    )
    assert rows == [cn]
    with pytest.raises(ValueError, match="limit"):
        resolve_financial_report_symbols(
            db_session, source="watchlist", limit=501
        )


def test_financial_report_worker_records_completed_task(db_session):
    _seed_watchlist(db_session)
    task = create_async_task(
        "financial_report_sync",
        {"source": "watchlist", "limit": 20},
    )
    with patch(
        "app.services.financial_report_task.sync_symbol_financial_reports",
        return_value=8,
    ):
        _run_financial_report_task(task.id)

    result = get_async_task(task.id)
    assert result is not None
    assert result.status == "done"
    assert result.ok_count == 1
    assert result.failed_count == 0
    assert result.result["records"] == 8


def test_lhb_worker_records_completed_task(db_session):
    task = create_async_task(
        "lhb_institution_sync", {"lookback_days": 3}
    )
    summary = LhbInstitutionSyncResult(
        start_date=date(2026, 7, 13),
        end_date=date(2026, 7, 15),
        received=12,
        written=12,
        unmatched=0,
    )
    with patch(
        "app.services.lhb_institution_task.sync_lhb_institution_trades",
        return_value=summary,
    ):
        _run_lhb_institution_task(task.id)

    result = get_async_task(task.id)
    assert result is not None
    assert result.status == "done"
    assert result.ok_count == 12
    assert result.result["written"] == 12


def test_scheduled_payloads_are_bounded():
    assert validate_task_payload(
        "financial_report_sync",
        {"source": "watchlist", "limit": 20},
    ) == {"source": "watchlist", "limit": 20}
    assert validate_task_payload(
        "lhb_institution_sync", {"lookback_days": 3}
    ) == {"lookback_days": 3}
    with pytest.raises(ValueError, match="lookback_days"):
        validate_task_payload(
            "lhb_institution_sync", {"lookback_days": 32}
        )
