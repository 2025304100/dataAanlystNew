from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.discovery_candidate import DiscoveryCandidate
from app.models.scan import ScanRun
from app.models.tail_accumulation_snapshot import (
    TailAccumulationSnapshot,
)
from app.models.universe import UniverseSymbol
from app.services.tail_proxy_data import (
    TailProxySyncResult,
    resolve_tail_proxy_symbols,
    sync_tail_proxy_snapshots,
)


pytestmark = pytest.mark.whitebox


def minute_frame(trade_date: date):
    morning = pd.date_range(
        f"{trade_date} 09:30:00",
        f"{trade_date} 11:30:00",
        freq="1min",
    )
    afternoon = pd.date_range(
        f"{trade_date} 13:00:00",
        f"{trade_date} 15:00:00",
        freq="1min",
    )
    timestamps = morning.append(afternoon)
    rows = []
    for index, timestamp in enumerate(timestamps):
        close = 10.0 + index / max(len(timestamps) - 1, 1)
        rows.append(
            {
                "时间": timestamp,
                "收盘": close,
                "最高": close + 0.1,
                "最低": close - 0.1,
                "成交额": (
                    200.0
                    if timestamp.time() >= pd.Timestamp("14:30").time()
                    else 100.0
                ),
            }
        )
    return pd.DataFrame(rows)


def _seed_candidates(db_session):
    run = ScanRun(
        run_name="tail candidates",
        scope_snapshot="{}",
        status="completed",
    )
    db_session.add(run)
    db_session.flush()
    first = UniverseSymbol(
        symbol="000001",
        name="平安银行",
        asset_type="stock",
        market="sz",
        region="cn",
    )
    second = UniverseSymbol(
        symbol="600519",
        name="贵州茅台",
        asset_type="stock",
        market="sh",
        region="cn",
    )
    db_session.add_all([first, second])
    db_session.flush()
    db_session.add_all(
        [
            DiscoveryCandidate(
                scan_run_id=run.id,
                universe_symbol_id=first.id,
                symbol=first.symbol,
                name=first.name,
                asset_type="stock",
                priority_score=70,
            ),
            DiscoveryCandidate(
                scan_run_id=run.id,
                universe_symbol_id=second.id,
                symbol=second.symbol,
                name=second.name,
                asset_type="stock",
                priority_score=90,
            ),
        ]
    )
    db_session.commit()


def test_tail_proxy_sync_is_candidate_scoped_and_idempotent(db_session):
    _seed_candidates(db_session)
    trade_date = date(2026, 7, 14)
    assert resolve_tail_proxy_symbols(
        db_session, source="candidates", limit=20
    ) == ["600519", "000001"]
    with patch(
        "app.services.tail_proxy_data._fetch_minute_frame",
        return_value=minute_frame(trade_date),
    ):
        first = sync_tail_proxy_snapshots(
            db_session,
            source="candidates",
            limit=20,
            trade_date=trade_date,
        )
        db_session.commit()
        second = sync_tail_proxy_snapshots(
            db_session,
            source="candidates",
            limit=20,
            trade_date=trade_date,
        )
        db_session.commit()

    assert first == TailProxySyncResult(
        trade_date=trade_date,
        source_scope="candidates",
        total=2,
        written=2,
        skipped=0,
        failed=0,
        errors=(),
    )
    assert second == first
    rows = db_session.execute(
        select(TailAccumulationSnapshot)
    ).scalars().all()
    assert len(rows) == 2
    assert all(row.minute_count >= 240 for row in rows)
    assert all(row.proxy_score > 0 for row in rows)
    assert "成交额" in (rows[0].raw_json or "")


def test_tail_proxy_sync_skips_incomplete_data(db_session):
    _seed_candidates(db_session)
    trade_date = date(2026, 7, 14)
    with patch(
        "app.services.tail_proxy_data._fetch_minute_frame",
        return_value=minute_frame(trade_date).tail(5),
    ):
        result = sync_tail_proxy_snapshots(
            db_session,
            trade_date=trade_date,
        )
    assert result.total == 2
    assert result.written == 0
    assert result.skipped == 2
    assert result.failed == 0


def test_tail_proxy_route_returns_counts(db_session):
    from app.api.routes.external_data import sync_tail_proxy

    summary = TailProxySyncResult(
        trade_date=date(2026, 7, 14),
        source_scope="candidates",
        total=20,
        written=17,
        skipped=2,
        failed=1,
        errors=("000001: timeout",),
    )
    with patch(
        "app.services.tail_proxy_data.sync_tail_proxy_snapshots",
        return_value=summary,
    ) as sync:
        result = sync_tail_proxy(
            source="candidates",
            limit=20,
            db=db_session,
        )
    assert result.total == 20
    assert result.success == 17
    assert result.skipped == 2
    assert result.failed == 1
    assert result.errors == ["000001: timeout"]
    sync.assert_called_once_with(
        db_session, source="candidates", limit=20
    )


def test_tail_proxy_opens_circuit_after_three_network_failures(
    db_session,
):
    with patch(
        "app.services.tail_proxy_data.resolve_tail_proxy_symbols",
        return_value=["000001", "000002", "000003", "000004", "000005"],
    ), patch(
        "app.services.tail_proxy_data._fetch_minute_frame",
        side_effect=ConnectionError("blocked"),
    ) as fetch:
        result = sync_tail_proxy_snapshots(
            db_session,
            trade_date=date(2026, 7, 14),
        )

    assert fetch.call_count == 3
    assert result.total == 5
    assert result.failed == 5
    assert any("circuit_open" in item for item in result.errors)
