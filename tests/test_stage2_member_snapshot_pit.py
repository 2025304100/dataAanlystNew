"""Stage 2 PIT coverage for strategy member snapshots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.portfolio import Portfolio
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    PortfolioMember,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
)
from app.services.factor_usage_service import _build_member_snapshot


def _member(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    effective_from: datetime,
    effective_to: datetime | None = None,
    status: str = STATUS_ACTIVE,
) -> PortfolioMember:
    row = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        status=status,
        execution_mode=EXECUTION_AUTO,
        source_type="manual",
        entry_rule_version_id=1,
        exit_rule_version_id=2,
        effective_from=effective_from,
        effective_to=effective_to,
        created_at=effective_from,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_member_snapshot_uses_as_of_interval_and_not_current_member_list(db_session):
    """Future and expired rows must not leak into a newly built snapshot."""
    portfolio = Portfolio(
        name="stage2-member-pit",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.0,
        currency="CNY",
        default_single_position_pct=0.2,
        auto_trade_enabled=1,
    )
    db_session.add(portfolio)
    db_session.flush()

    as_of = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
    in_window = _member(
        db_session,
        portfolio_id=portfolio.id,
        symbol_id=101,
        effective_from=datetime(2026, 1, 1),
        effective_to=datetime(2026, 1, 20),
    )
    archived_after_as_of = _member(
        db_session,
        portfolio_id=portfolio.id,
        symbol_id=104,
        effective_from=datetime(2026, 1, 1),
        effective_to=datetime(2026, 1, 20),
        status=STATUS_ARCHIVED,
    )
    _member(
        db_session,
        portfolio_id=portfolio.id,
        symbol_id=102,
        effective_from=datetime(2026, 1, 11),
    )
    _member(
        db_session,
        portfolio_id=portfolio.id,
        symbol_id=103,
        effective_from=datetime(2025, 12, 1),
        effective_to=datetime(2026, 1, 10),
        status=STATUS_ARCHIVED,
    )
    db_session.commit()

    snapshot = _build_member_snapshot(db_session, portfolio.id, as_of)

    assert [row["id"] for row in snapshot] == [in_window.id, archived_after_as_of.id]
    assert [row["symbol_id"] for row in snapshot] == [101, 104]


def test_member_snapshot_normalizes_timezone_and_keeps_paused_effective_row(db_session):
    """Timezone-aware ``as_of`` must compare correctly; pause is not expiry."""
    portfolio = Portfolio(
        name="stage2-member-pit-paused",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.0,
        currency="CNY",
        default_single_position_pct=0.2,
        auto_trade_enabled=1,
    )
    db_session.add(portfolio)
    db_session.flush()

    effective = _member(
        db_session,
        portfolio_id=portfolio.id,
        symbol_id=201,
        effective_from=datetime(2026, 1, 10, 11, 0),
        effective_to=None,
        status="paused",
    )
    db_session.commit()

    snapshot = _build_member_snapshot(
        db_session,
        portfolio.id,
        datetime(2026, 1, 10, 20, 0, tzinfo=timezone(timedelta(hours=8))),
    )

    assert [row["id"] for row in snapshot] == [effective.id]
    assert snapshot[0]["execution_mode"] == EXECUTION_AUTO
