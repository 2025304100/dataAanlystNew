from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from app.models.notification import (
    NotificationChannel,
    NotificationOutbox,
    NotificationPolicy,
    NotificationPolicyChannel,
)
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.portfolio_equity_snapshot import PortfolioEquitySnapshot
from app.models.symbol import Symbol
from app.services.notifications.dispatcher import Dispatcher
from app.services.portfolio_risk_notifications import (
    evaluate_snapshot_risk_notifications,
)
from app.services.sim_accounts import place_sim_order


pytestmark = pytest.mark.whitebox


def _portfolio(db_session, name: str) -> Portfolio:
    portfolio = Portfolio(
        name=name,
        account_type="simulated",
        asset_scope="stock",
        total_capital=100000.0,
        investable_ratio=0.8,
        cash_reserve_ratio=0.2,
        currency="CNY",
    )
    db_session.add(portfolio)
    db_session.flush()
    return portfolio


def _rule(
    db_session,
    portfolio_id: int,
    *,
    max_loss: float = 0.05,
    drawdown: float = 0.05,
) -> PortfolioRule:
    rule = PortfolioRule(
        portfolio_id=portfolio_id,
        rule_name="risk-test",
        max_single_position_pct=0.3,
        max_sector_position_pct=0.5,
        max_stock_position_pct=0.8,
        max_etf_position_pct=0.3,
        max_loss_per_trade_pct=max_loss,
        max_open_positions=10,
        stage_limits_json=json.dumps({"drawdown_circuit_pct": drawdown}),
        is_active=1,
    )
    db_session.add(rule)
    db_session.flush()
    return rule


def _in_app_policy(db_session, event_types: list[str]) -> NotificationChannel:
    channel = NotificationChannel(
        name="risk-in-app",
        channel_type="in_app",
        enabled=True,
        status="enabled",
    )
    policy = NotificationPolicy(
        name="risk-policy",
        enabled=True,
        source_types_json=json.dumps(event_types),
        min_severity="info",
        scope_type="all",
        delivery_mode="instant",
    )
    db_session.add_all([channel, policy])
    db_session.flush()
    db_session.add(
        NotificationPolicyChannel(policy_id=policy.id, channel_id=channel.id)
    )
    db_session.flush()
    return channel


def test_snapshot_loss_and_drawdown_reach_in_app_and_deduplicate(db_session):
    portfolio = _portfolio(db_session, "risk-snapshot")
    _rule(db_session, portfolio.id, max_loss=0.05, drawdown=0.05)
    symbol = Symbol(
        symbol="600901",
        name="risk symbol",
        asset_type="stock",
        market="cn_stock",
    )
    db_session.add(symbol)
    db_session.flush()
    db_session.add(
        Position(
            portfolio_id=portfolio.id,
            symbol_id=symbol.id,
            quantity=100,
            avg_cost=100.0,
            latest_price=90.0,
            market_value=9000.0,
            position_pct=0.09,
            asset_type="stock",
        )
    )
    snapshot = PortfolioEquitySnapshot(
        portfolio_id=portfolio.id,
        snapshot_date=date(2026, 8, 10),
        cash_balance=81000.0,
        market_value=9000.0,
        total_equity=90000.0,
        realized_pnl=0.0,
        unrealized_pnl=-1000.0,
        daily_return=-0.1,
        position_count=1,
    )
    db_session.add(snapshot)
    _in_app_policy(
        db_session,
        ["max_loss_warning", "drawdown_warning"],
    )
    db_session.flush()

    created = evaluate_snapshot_risk_notifications(
        db_session,
        portfolio=portfolio,
        snapshot=snapshot,
    )
    assert created == {"max_loss": 1, "drawdown": 1}
    db_session.commit()

    assert Dispatcher().run_once() == 2
    outboxes = list(
        db_session.execute(
            select(NotificationOutbox).order_by(NotificationOutbox.id)
        ).scalars()
    )
    assert {item.event_type for item in outboxes} == {
        "max_loss_warning",
        "drawdown_warning",
    }
    assert all(item.status == "sent" for item in outboxes)

    duplicate = evaluate_snapshot_risk_notifications(
        db_session,
        portfolio=portfolio,
        snapshot=snapshot,
    )
    assert duplicate == {"max_loss": 0, "drawdown": 0}
    assert db_session.query(NotificationOutbox).count() == 2


def test_realized_sell_loss_reaches_in_app(db_session):
    portfolio = _portfolio(db_session, "risk-realized-sell")
    _rule(db_session, portfolio.id, max_loss=0.05, drawdown=0)
    symbol = Symbol(
        symbol="600902",
        name="sell loss symbol",
        asset_type="stock",
        market="cn_stock",
    )
    db_session.add(symbol)
    _in_app_policy(db_session, ["trade_executed", "max_loss_warning"])
    db_session.commit()

    place_sim_order(
        db_session,
        portfolio=portfolio,
        symbol=symbol,
        side="buy",
        quantity=100,
        price=10.0,
        enforce_rules=False,
        apply_fees=False,
    )
    _, sell_trade = place_sim_order(
        db_session,
        portfolio=portfolio,
        symbol=symbol,
        side="sell",
        quantity=100,
        price=9.0,
        enforce_rules=False,
        apply_fees=False,
    )
    assert sell_trade.realized_pnl == pytest.approx(-100.0)
    db_session.commit()

    outboxes = list(
        db_session.execute(
            select(NotificationOutbox).order_by(NotificationOutbox.id)
        ).scalars()
    )
    assert [item.event_type for item in outboxes].count("trade_executed") == 2
    max_loss_outbox = next(
        item for item in outboxes if item.event_type == "max_loss_warning"
    )
    assert max_loss_outbox.status == "pending"
    payload = json.loads(max_loss_outbox.payload_json)
    assert "10.00%" in payload["body"]
    assert "5.00%" in payload["body"]

    assert Dispatcher().run_once() == 3
    for item in outboxes:
        db_session.refresh(item)
    assert all(item.status == "sent" for item in outboxes)