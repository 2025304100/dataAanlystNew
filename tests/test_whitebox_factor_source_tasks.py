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
    from freezegun import freeze_time
    from app.main import app
    app.openapi()
    _seed_watchlist(db_session)
    task=create_async_task("financial_report_sync",{"source":"watchlist","limit":20})
    rows=[dict(SECUCODE="600519.SH",REPORT_DATE=period,NOTICE_DATE="2026-10-21",
               ROEJQ=18.2,PARENT_NETPROFIT=100,TOTAL_OPERATE_INCOME=200)
          for period in ("2024-03-31","2024-06-30","2024-09-30","2024-12-31",
                         "2025-03-31","2025-06-30","2025-09-30","2025-12-31")]
    with freeze_time('2026-10-21T08:00:00Z'), patch(
            "app.services.financial_provider.fetch_financial_rows",return_value=rows):
        _run_financial_report_task(task.id)
    result=get_async_task(task.id)
    assert result is not None and result.status=="done", result
    assert result.ok_count==1 and result.failed_count==0
    assert result.result["records"]==8
    from app.models.financial_report import StockFinancialReport
    assert db_session.query(StockFinancialReport).count()==8


def test_lhb_worker_records_completed_task(db_session):
    import pandas as pd
    from freezegun import freeze_time
    from app.main import app
    from app.models.market_data import MarketCalendar
    app.openapi()
    symbols=[Symbol(symbol=str(600501+i),name="isolated LHB",asset_type="stock",market="sh") for i in range(12)]
    db_session.add_all(symbols+[MarketCalendar(trade_date=date(2026,10,21),is_trading_day=1)])
    db_session.commit()
    frame=pd.DataFrame([{"代码":symbol.symbol,"上榜日期":"2026-10-21","买方机构数":3,"卖方机构数":2,
        "机构买入总额":900,"机构卖出总额":400,"机构买入净额":500,"市场总成交额":10000,
        "机构净买额占总成交额比":5,"换手率":2.5,"上榜原因":"isolated provider input"} for symbol in symbols])
    task=create_async_task("lhb_institution_sync",{"lookback_days":3})
    with freeze_time('2026-10-21T08:00:00Z'), patch("app.services.lhb_data.call_akshare_with_retry",return_value=frame):
        _run_lhb_institution_task(task.id)
    result=get_async_task(task.id)
    assert result is not None and result.status=="done", result
    assert result.ok_count==12 and result.result["written"]==12
    from app.models.lhb_institution_trade import LhbInstitutionTrade
    assert db_session.query(LhbInstitutionTrade).count()==12


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
