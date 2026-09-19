from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.hot_rank_snapshot import StockHotRankSnapshot
from app.services.hot_rank_data import (
    HotRankSyncResult,
    sync_hot_rank_snapshot,
)


pytestmark = pytest.mark.whitebox


def _provider_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"当前排名": 1, "代码": "SZ000001", "股票名称": "平安银行"},
            {"当前排名": 2, "代码": "SH600519", "股票名称": "贵州茅台"},
        ]
    )


def test_hot_rank_sync_captures_current_snapshot_idempotently(db_session):
    with patch(
        "app.services.hot_rank_data.call_akshare_with_retry",
        return_value=_provider_frame(),
    ) as fetch:
        first = sync_hot_rank_snapshot(
            db_session, as_of=date(2026, 7, 15)
        )
        db_session.commit()
        second = sync_hot_rank_snapshot(
            db_session, as_of=date(2026, 7, 15)
        )
        db_session.commit()

    assert first == HotRankSyncResult(
        trade_date=date(2026, 7, 15),
        received=2,
        written=2,
        unmatched=0,
    )
    assert second == first
    rows = db_session.execute(
        select(StockHotRankSnapshot).order_by(
            StockHotRankSnapshot.hot_rank
        )
    ).scalars().all()
    assert len(rows) == 2
    assert rows[0].symbol == "000001"
    assert rows[0].hot_rank == 1
    assert rows[0].hot_rank_total == 2
    assert rows[0].hot_rank_pct == pytest.approx(50)
    assert "股票名称" in (rows[0].raw_json or "")
    assert fetch.call_args.kwargs["api_key"] == "stock_hot_rank_em"
    assert "symbol" not in fetch.call_args.kwargs


def test_hot_rank_sync_route_returns_snapshot_counts(db_session):
    from app.api.routes.external_data import sync_hot_rank

    summary = HotRankSyncResult(
        trade_date=date(2026, 7, 15),
        received=100,
        written=98,
        unmatched=2,
    )
    with patch(
        "app.services.hot_rank_data.sync_hot_rank_snapshot",
        return_value=summary,
    ) as sync:
        result = sync_hot_rank(db=db_session)

    assert result.total == 100
    assert result.success == 98
    assert result.skipped == 2
    assert result.records == 98
    sync.assert_called_once_with(db_session)
