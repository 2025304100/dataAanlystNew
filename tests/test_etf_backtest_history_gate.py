"""ETF price support never substitutes for historical trading restrictions."""
from datetime import date

import pytest
from sqlalchemy import select

from app.models.backtest import BacktestRun
from app.schemas.bfg_precheck import BacktestPrecheckRequest
from app.schemas.security_status import SecurityStatusFileRequest
from app.services.backtest import run_backtest
from app.services.bfg_precheck_service import run_backtest_precheck
from app.services.security_status.backtest_guard import BacktestHistoryUnavailable
from app.services.security_status.local_import import preview_import, import_file
from tests.test_backtest_historical_execution_guard import scenario

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize('status_present', [False, True])
def test_etf_prices_and_current_metadata_do_not_unlock_execution_without_daily_history(
    db_session, status_present,
):
    kwargs, symbol, days = scenario(db_session)
    symbol.symbol, symbol.asset_type, symbol.market = '510300', 'etf', 'SH'
    symbol.is_st, symbol.is_active = 0, 1
    db_session.commit()
    if status_present:
        payload = SecurityStatusFileRequest(
            csv_text='symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date,price_limit_mode,upper_limit_price,lower_limit_price,tick_size\n'
                + ''.join(f'510300,{day},0,0,0,1,2012-05-28,,,,,\n' for day in days),
            source_name='provider_archive', source_reference='archive://synthetic-etf-history',
            source_version='synthetic-1', source_updated_at='2025-09-03T18:00:00+08:00',
        )
        preview = preview_import(db_session, payload)
        assert preview['can_import'], preview
        import_file(db_session, payload, preview_sha256=preview['content_sha256'])
        db_session.commit()
    code = 'PRICE_LIMIT_DATA_UNAVAILABLE' if status_present else 'STATUS_DATA_UNAVAILABLE'
    with pytest.raises(BacktestHistoryUnavailable) as rejected:
        run_backtest(**kwargs)
    assert rejected.value.code == code
    precheck = run_backtest_precheck(db_session, BacktestPrecheckRequest(
        portfolio_id=kwargs['portfolio_id'], symbol_ids=[symbol.id],
        start_date=days[0], end_date=days[-1]))
    assert code in {item.code for item in precheck.blocking_reasons}
    assert db_session.scalars(select(BacktestRun)).all() == []
