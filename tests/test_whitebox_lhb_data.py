from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.lhb_institution_trade import LhbInstitutionTrade
from app.models.symbol import Symbol
from app.services.lhb_data import (
    LhbInstitutionSyncResult,
    sync_lhb_institution_trades,
)


pytestmark = pytest.mark.whitebox


def _provider_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "代码": "600519",
                "上榜日期": "2026-07-14",
                "买方机构数": 3,
                "卖方机构数": 2,
                "机构买入总额": 900,
                "机构卖出总额": 400,
                "机构买入净额": 500,
                "市场总成交额": 10_000,
                "机构净买额占总成交额比": 5,
                "换手率": 2.5,
                "上榜原因": "日涨幅偏离",
            },
            {
                "代码": "999999",
                "上榜日期": "2026-07-14",
                "买方机构数": 1,
                "卖方机构数": 1,
                "机构买入总额": 200,
                "机构卖出总额": 300,
                "机构买入净额": -100,
                "市场总成交额": 5_000,
                "机构净买额占总成交额比": -2,
                "换手率": 3,
                "上榜原因": "测试未匹配代码",
            },
        ]
    )


def test_lhb_sync_uses_institution_api_and_is_idempotent(db_session):
    with patch(
        "app.services.lhb_data.call_akshare_with_retry",
        return_value=_provider_frame(),
    ) as fetch:
        first = sync_lhb_institution_trades(
            db_session,
            start_date=date(2026, 7, 14),
            end_date=date(2026, 7, 14),
        )
        db_session.commit()
        second = sync_lhb_institution_trades(
            db_session,
            start_date=date(2026, 7, 14),
            end_date=date(2026, 7, 14),
        )
        db_session.commit()

    assert first.received == 2
    assert first.written == 2
    assert first.unmatched == 0
    assert second == first
    rows = db_session.execute(
        select(LhbInstitutionTrade)
    ).scalars().all()
    assert len(rows) == 2
    matched = next(row for row in rows if row.symbol == "600519")
    assert matched.institution_buy == 900
    assert matched.institution_sell == 400
    assert matched.institution_net == 500
    assert matched.institution_net_pct == pytest.approx(5)
    assert "机构买入净额" in (matched.raw_json or "")
    assert fetch.call_args.kwargs["start_date"] == "20260714"
    assert fetch.call_args.kwargs["end_date"] == "20260714"
    assert fetch.call_args.kwargs["api_key"] == "stock_lhb_jgmmtj_em"


def test_lhb_sync_rejects_invalid_or_oversized_range(db_session):
    with pytest.raises(ValueError, match="start_date"):
        sync_lhb_institution_trades(
            db_session,
            start_date=date(2026, 7, 15),
            end_date=date(2026, 7, 14),
        )
    with pytest.raises(ValueError, match="31"):
        sync_lhb_institution_trades(
            db_session,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 7, 14),
        )


def test_lhb_sync_does_not_require_business_symbol(db_session):
    with patch(
        "app.services.lhb_data.call_akshare_with_retry",
        return_value=_provider_frame().iloc[[0]],
    ):
        result = sync_lhb_institution_trades(
            db_session,
            start_date=date(2026, 7, 14),
            end_date=date(2026, 7, 14),
        )
    assert result.received == 1
    assert result.written == 1
    assert result.unmatched == 0


def test_lhb_sync_route_uses_bounded_lookback(db_session):
    from app.api.routes.external_data import sync_lhb_institution

    summary = LhbInstitutionSyncResult(
        start_date=date(2026, 7, 8),
        end_date=date(2026, 7, 14),
        received=9,
        written=8,
        unmatched=1,
    )
    with patch(
        "app.services.lhb_data.sync_lhb_institution_trades",
        return_value=summary,
    ) as sync:
        result = sync_lhb_institution(
            lookback_days=7,
            end_date=date(2026, 7, 14),
            db=db_session,
        )

    assert result.total == 9
    assert result.success == 8
    assert result.skipped == 1
    assert result.records == 8
    sync.assert_called_once_with(
        db_session,
        start_date=date(2026, 7, 8),
        end_date=date(2026, 7, 14),
    )
