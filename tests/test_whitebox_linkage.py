"""白盒测试 - WP8.2 跨模块联动 API（P1-1）。

守护 app/api/routes/linkage.py 中 6 个联动端点的关键行为：
1. GET /orders/{order_id}/context            订单上下文（member/signal/rule/cost/trade_result）
2. GET /trades/{trade_id}/context            成交上下文（反查 order + member + signal）
3. GET /alerts/{alert_id}/context            告警关联观察项/成员/持仓
4. GET /dashboard/today-decision             今日决策待办聚合
5. GET /portfolios/{id}/backtests/{run_id}/members  回测成员快照
6. GET /portfolios/{id}/attribution/suggest-review  归因异常建议复盘

边界守护：
- 订单/成交/告警/组合/回测不存在时返回 404
- best-effort 关联：member/signal 缺失时返回 null，不阻塞主响应
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.alert import AlertEvent, AlertRule
from app.models.async_task import AsyncTaskRecord
from app.models.backtest import BacktestRun
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import PortfolioMember, STATUS_ARCHIVED
from app.models.sim_account import SimOrder, SimTrade
from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services.portfolio_equity_snapshot import upsert_snapshot
from app.services.sim_accounts import ensure_sim_account_seed

pytestmark = pytest.mark.whitebox


def _make_portfolio(db_session, name="QA-Linkage", account_type="simulated", total_capital=100000.0) -> Portfolio:
    p = Portfolio(
        name=name,
        account_type=account_type,
        total_capital=total_capital,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        is_default=0,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    if account_type == "simulated":
        ensure_sim_account_seed(db_session, p)
        db_session.commit()
    return p


def _make_symbol(db_session, symbol="600010", name="测试标的") -> Symbol:
    sym = Symbol(symbol=symbol, name=name, asset_type="stock", market="sh")
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_member(
    db_session, portfolio_id, symbol_id, execution_mode="manual", source_type="manual",
    entry_rule_version_id=None, exit_rule_version_id=None, status="active",
) -> PortfolioMember:
    m = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        status=status,
        execution_mode=execution_mode,
        source_type=source_type,
        entry_rule_version_id=entry_rule_version_id,
        exit_rule_version_id=exit_rule_version_id,
    )
    db_session.add(m)
    db_session.commit()
    db_session.refresh(m)
    return m


def _make_order_with_attribution(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    side: str = "sell",
    status: str = "filled",
    member_id: int | None = None,
    execution_mode: str | None = None,
    source_type: str | None = None,
    signal_id: int | None = None,
    rule_version_id: int | None = None,
    signal_snapshot_json: str | None = None,
    decision_snapshot_json: str | None = None,
    submitted_price: float = 10.0,
    filled_price: float = 11.0,
    filled_quantity: float = 100.0,
    fee: float = 5.0,
    rejection_code: str | None = None,
    created_at: datetime | None = None,
) -> SimOrder:
    if created_at is None:
        created_at = datetime(2026, 7, 16, 10, 0)
    order = SimOrder(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        side=side,
        order_type="market",
        quantity=filled_quantity,
        submitted_price=submitted_price,
        status=status,
        filled_quantity=filled_quantity if status in ("filled", "partial") else 0,
        filled_price=filled_price if status in ("filled", "partial") else 0,
        filled_amount=filled_price * filled_quantity if status in ("filled", "partial") else 0,
        fee=fee,
        filled_at=created_at if status in ("filled", "partial") else None,
        member_id=member_id,
        execution_mode=execution_mode,
        source_type=source_type,
        signal_id=signal_id,
        rule_version_id=rule_version_id,
        signal_snapshot_json=signal_snapshot_json,
        decision_snapshot_json=decision_snapshot_json,
        rejection_code=rejection_code,
        created_at=created_at,
    )
    db_session.add(order)
    db_session.flush()
    return order


def _make_trade_for_order(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    order_id: int,
    side: str = "sell",
    price: float = 11.0,
    quantity: float = 100.0,
    realized_pnl: float | None = None,
    created_at: datetime | None = None,
) -> SimTrade:
    if created_at is None:
        created_at = datetime(2026, 7, 16, 10, 0)
    trade = SimTrade(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        order_id=order_id,
        side=side,
        quantity=quantity,
        price=price,
        amount=price * quantity,
        fee=0.0,
        realized_pnl=realized_pnl,
        created_at=created_at,
    )
    db_session.add(trade)
    db_session.commit()
    return trade


def _seed_snapshots(db_session, portfolio_id, start_equity=100000.0, days=5, start_date=date(2026, 7, 15)):
    for i in range(days):
        snap_date = start_date + timedelta(days=i)
        equity = start_equity + i * 100
        upsert_snapshot(
            db_session,
            portfolio_id=portfolio_id,
            snapshot_date=snap_date,
            cash_balance=equity - i * 50,
            market_value=float(i * 50),
            total_equity=equity,
            realized_pnl=float(i * 10),
            unrealized_pnl=float(i * 40),
            position_count=1 if i > 0 else 0,
        )
    db_session.commit()


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


class TestOrderContextAPI:
    """【WP8.2 测试】订单上下文 API。"""

    def test_order_context_api(self, client, db_session):
        """【WP8.2 测试】GET /orders/{id}/context 返回 member/signal/rule_version/cost_breakdown/trade_result。"""
        p = _make_portfolio(db_session, name="QA-Link-Order")
        sym = _make_symbol(db_session, symbol="800001")
        m = _make_member(db_session, p.id, sym.id, execution_mode="auto", source_type="candidate")
        _seed_snapshots(db_session, p.id, days=3)

        order = _make_order_with_attribution(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            side="sell",
            status="filled",
            member_id=m.id,
            execution_mode="auto",
            source_type="candidate",
            signal_id=4242,
            rule_version_id=99,
            signal_snapshot_json=json.dumps({"trigger": "take_profit", "price_target": 11.5}),
            decision_snapshot_json=json.dumps({
                "reason": "rule triggered",
                "cost_breakdown": {"stamp_duty": 1.1},
            }),
            submitted_price=10.5,
            filled_price=11.0,
            filled_quantity=100.0,
            fee=5.0,
        )
        _make_trade_for_order(
            db_session,
            portfolio_id=p.id, symbol_id=sym.id, order_id=order.id,
            side="sell", price=11.0, quantity=100.0, realized_pnl=200.0,
        )

        resp = client.get(f"/api/v1/orders/{order.id}/context")
        assert resp.status_code == 200
        data = resp.json()

        assert data["order"]["id"] == order.id
        assert data["order"]["side"] == "sell"
        assert data["symbol"]["id"] == sym.id
        assert data["symbol"]["symbol"] == "800001"
        assert data["member"] is not None
        assert data["member"]["id"] == m.id
        assert data["member"]["execution_mode"] == "auto"
        assert data["signal"] is not None
        assert data["signal"]["id"] == 4242
        assert data["signal"]["snapshot"]["trigger"] == "take_profit"
        assert data["rule_version"] == {"id": 99}
        assert data["execution_mode"] == "auto"
        assert data["source_type"] == "candidate"
        assert data["decision_snapshot"]["reason"] == "rule triggered"
        cost = data["cost_breakdown"]
        assert cost["commission"] == 5.0
        assert cost["stamp_duty"] == 1.1
        assert cost["slippage"] == 50.0
        assert cost["total"] == pytest.approx(56.1, abs=0.01)
        tr = data["trade_result"]
        assert tr["status"] == "filled"
        assert tr["filled_quantity"] == 100.0
        assert tr["filled_price"] == 11.0
        assert tr["realized_pnl"] == 200.0
        assert tr["trade_count"] == 1

    def test_order_context_not_found(self, client, db_session):
        """【WP8.2 测试】订单不存在返回 404。"""
        resp = client.get("/api/v1/orders/99999/context")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error_code"] == "NOT_FOUND"
        assert "not found" in body["technical_details"]["error_message"].lower()

    def test_order_context_without_attribution_fields(self, client, db_session):
        """【WP8.2 测试】订单无 member/signal 时返回 null，不阻塞主响应（best-effort）。"""
        p = _make_portfolio(db_session, name="QA-Link-Order-Minimal")
        sym = _make_symbol(db_session, symbol="800002")

        order = _make_order_with_attribution(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            side="buy",
            status="pending",
            member_id=None,
            execution_mode=None,
            source_type=None,
            signal_id=None,
            rule_version_id=None,
            submitted_price=10.0,
            fee=0.0,
        )

        resp = client.get(f"/api/v1/orders/{order.id}/context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["member"] is None
        assert data["signal"] is None
        assert data["rule_version"] is None
        assert data["order"]["id"] == order.id
        assert data["trade_result"]["status"] == "pending"


class TestTradeContextAPI:
    """【WP8.2 测试】成交上下文 API。"""

    def test_trade_context_api(self, client, db_session):
        """【WP8.2 测试】GET /trades/{id}/context 通过 order_id 反查 member/signal/rule/cost。"""
        p = _make_portfolio(db_session, name="QA-Link-Trade")
        sym = _make_symbol(db_session, symbol="800010")
        m = _make_member(db_session, p.id, sym.id, execution_mode="confirm", source_type="observation")

        order = _make_order_with_attribution(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            side="sell",
            status="filled",
            member_id=m.id,
            execution_mode="confirm",
            source_type="observation",
            signal_id=555,
            rule_version_id=7,
            signal_snapshot_json=json.dumps({"note": "exit signal"}),
            submitted_price=10.0,
            filled_price=10.5,
            filled_quantity=200.0,
            fee=2.0,
        )
        trade = _make_trade_for_order(
            db_session,
            portfolio_id=p.id, symbol_id=sym.id, order_id=order.id,
            side="sell", price=10.5, quantity=200.0, realized_pnl=80.0,
        )

        resp = client.get(f"/api/v1/trades/{trade.id}/context")
        assert resp.status_code == 200
        data = resp.json()

        assert data["trade"]["id"] == trade.id
        assert data["trade"]["side"] == "sell"
        assert data["order"]["id"] == order.id
        assert data["symbol"]["id"] == sym.id
        assert data["member"]["id"] == m.id
        assert data["member"]["execution_mode"] == "confirm"
        assert data["signal"]["id"] == 555
        assert data["signal"]["snapshot"]["note"] == "exit signal"
        assert data["rule_version"] == {"id": 7}
        assert data["execution_mode"] == "confirm"
        assert data["source_type"] == "observation"
        cost = data["cost_breakdown"]
        assert cost["commission"] == 2.0
        assert cost["stamp_duty"] == 0.0
        assert cost["slippage"] == 100.0
        assert data["trade_result"]["realized_pnl"] == 80.0
        assert data["trade_result"]["trade_count"] == 1

    def test_trade_context_not_found(self, client, db_session):
        """【WP8.2 测试】成交不存在返回 404。"""
        resp = client.get("/api/v1/trades/99999/context")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error_code"] == "NOT_FOUND"
        assert "not found" in body["technical_details"]["error_message"].lower()


class TestAlertContextAPI:
    """【WP8.2 测试】告警关联上下文 API。"""

    def test_alert_context_api(self, client, db_session):
        """【WP8.2 测试】GET /alerts/{id}/context 返回关联观察项/成员/持仓 + alert_rule + alert_data。"""
        p = _make_portfolio(db_session, name="QA-Link-Alert")
        sym = _make_symbol(db_session, symbol="800020")

        wl = Watchlist(name="QA-WL-Alert", list_type="observation")
        db_session.add(wl)
        db_session.commit()
        db_session.refresh(wl)
        wl_item = WatchlistItem(
            watchlist_id=wl.id,
            symbol_id=sym.id,
            note="观察项 A",
            origin_type="candidate",
            status="watching",
        )
        db_session.add(wl_item)

        m = _make_member(db_session, p.id, sym.id, execution_mode="auto")

        pos = Position(
            portfolio_id=p.id,
            symbol_id=sym.id,
            quantity=500,
            avg_cost=10.0,
            latest_price=11.0,
            market_value=5500.0,
            position_pct=0.5,
            asset_type="stock",
        )
        db_session.add(pos)
        db_session.commit()

        rule = AlertRule(
            name="跌破止损",
            alert_type="indicator_trigger",
            enabled=1,
            severity="warn",
            config_json=json.dumps({"threshold": 9.5}),
        )
        db_session.add(rule)
        db_session.commit()
        db_session.refresh(rule)

        alert = AlertEvent(
            rule_id=rule.id,
            alert_type="indicator_trigger",
            severity="warn",
            title="价格跌破止损线",
            message="600020 跌破 9.5",
            symbol_id=sym.id,
            data_json=json.dumps({"price": 9.3, "threshold": 9.5}),
            acknowledged=0,
        )
        db_session.add(alert)
        db_session.commit()
        db_session.refresh(alert)

        resp = client.get(f"/api/v1/alerts/{alert.id}/context")
        assert resp.status_code == 200
        data = resp.json()

        assert data["alert"]["id"] == alert.id
        assert data["alert"]["symbol_id"] == sym.id
        assert data["alert_rule"]["id"] == rule.id
        assert data["alert_rule"]["name"] == "跌破止损"
        assert data["symbol"]["id"] == sym.id
        assert data["related_watchlist_item"] is not None
        assert data["related_watchlist_item"]["symbol_id"] == sym.id
        assert data["related_member"] is not None
        assert data["related_member"]["id"] == m.id
        assert data["related_position"] is not None
        assert data["related_position"]["symbol_id"] == sym.id
        assert data["alert_data"]["price"] == 9.3
        assert data["alert_data"]["threshold"] == 9.5

    def test_alert_context_no_related_entities(self, client, db_session):
        """【WP8.2 测试】告警 symbol 无关联实体时返回 null，不阻塞主响应。"""
        sym = _make_symbol(db_session, symbol="800021")

        alert = AlertEvent(
            rule_id=0,
            alert_type="data_stale",
            severity="warn",
            title="数据陈旧",
            message="无关联",
            symbol_id=sym.id,
            data_json=None,
            acknowledged=0,
        )
        db_session.add(alert)
        db_session.commit()
        db_session.refresh(alert)

        resp = client.get(f"/api/v1/alerts/{alert.id}/context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["alert"]["id"] == alert.id
        assert data["alert_rule"] is None
        assert data["related_watchlist_item"] is None
        assert data["related_member"] is None
        assert data["related_position"] is None
        assert data["symbol"]["id"] == sym.id

    def test_alert_context_not_found(self, client, db_session):
        """【WP8.2 测试】告警不存在返回 404。"""
        resp = client.get("/api/v1/alerts/99999/context")
        assert resp.status_code == 404


class TestTodayDecisionAPI:
    """【WP8.2 测试】今日决策待办聚合 API。"""

    def test_today_decision_api(self, client, db_session):
        """【WP8.2 测试】GET /dashboard/today-decision 返回 pending_orders/data_gate_blocks/member_issues/alert_summary。"""
        p = _make_portfolio(db_session, name="QA-Link-Today")
        sym1 = _make_symbol(db_session, symbol="800030", name="决策标的1")
        sym2 = _make_symbol(db_session, symbol="800031", name="决策标的2")
        sym3 = _make_symbol(db_session, symbol="800032", name="决策标的3")

        pending_order = _make_order_with_attribution(
            db_session,
            portfolio_id=p.id, symbol_id=sym1.id,
            side="buy", status="pending",
            member_id=None, execution_mode="manual",
            submitted_price=10.0, fee=0.0,
        )
        _make_order_with_attribution(
            db_session,
            portfolio_id=p.id, symbol_id=sym1.id,
            side="sell", status="filled",
            submitted_price=11.0, filled_price=11.0, fee=1.0,
        )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        task = AsyncTaskRecord(
            id="qa-link-task-failed",
            task_type="market_data_sync",
            status="failed",
            stage="sync",
            message="akshare timeout",
            errors_json=json.dumps({"detail": "connection refused"}),
            created_at=now,
            finished_at=now,
            updated_at=now,
        )
        db_session.add(task)
        old_task = AsyncTaskRecord(
            id="qa-link-task-old",
            task_type="market_data_sync",
            status="failed",
            stage="sync",
            message="old failure",
            created_at=now - timedelta(hours=48),
            finished_at=now - timedelta(hours=48),
        )
        db_session.add(old_task)
        db_session.commit()

        _make_member(
            db_session, p.id, sym2.id, execution_mode="manual",
            status=STATUS_ARCHIVED,
        )
        _make_member(
            db_session, p.id, sym3.id, execution_mode="auto",
            entry_rule_version_id=None, exit_rule_version_id=None,
        )

        alert = AlertEvent(
            rule_id=0,
            alert_type="data_stale",
            severity="error",
            title="数据异常",
            message="block",
            symbol_id=sym1.id,
            acknowledged=0,
        )
        db_session.add(alert)
        db_session.commit()

        resp = client.get("/api/v1/dashboard/today-decision", params={"portfolio_id": p.id})
        assert resp.status_code == 200
        data = resp.json()

        assert data["portfolio_id"] == p.id
        assert isinstance(data["pending_orders"], list)
        assert len(data["pending_orders"]) == 1
        po = data["pending_orders"][0]
        assert po["order_id"] == pending_order.id
        assert po["symbol"] == "800030"
        assert po["status"] == "pending"

        blocks = data["data_gate_blocks"]
        assert isinstance(blocks, list)
        assert len(blocks) == 1
        assert blocks[0]["task_id"] == "qa-link-task-failed"
        assert blocks[0]["task_type"] == "market_data_sync"
        assert blocks[0]["reason"] == "akshare timeout"
        assert blocks[0]["errors"]["detail"] == "connection refused"

        issues = data["member_issues"]
        assert isinstance(issues, list)
        assert len(issues) == 2
        issue_types = {it["issue"] for it in issues}
        assert "member_archived" in issue_types
        assert "rule_expired" in issue_types

        assert data["alert_summary"]["active_count"] == 1
        assert data["alert_summary"]["critical_count"] == 1

        assert data["summary"]["pending_order_count"] == 1
        assert data["summary"]["data_gate_block_count"] == 1
        assert data["summary"]["member_issue_count"] == 2

    def test_today_decision_portfolio_not_found(self, client, db_session):
        """【WP8.2 测试】组合不存在返回 404。"""
        resp = client.get("/api/v1/dashboard/today-decision", params={"portfolio_id": 99999})
        assert resp.status_code == 404

    def test_today_decision_empty_portfolio(self, client, db_session):
        """【WP8.2 测试】空组合返回空列表与零计数。"""
        p = _make_portfolio(db_session, name="QA-Link-Today-Empty")

        resp = client.get("/api/v1/dashboard/today-decision", params={"portfolio_id": p.id})
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending_orders"] == []
        assert data["data_gate_blocks"] == []
        assert data["member_issues"] == []
        assert data["summary"]["pending_order_count"] == 0
        assert data["summary"]["data_gate_block_count"] == 0
        assert data["summary"]["member_issue_count"] == 0


class TestBacktestMemberSnapshotAPI:
    """【WP8.2 测试】回测成员快照 API。"""

    def test_backtest_member_snapshot_api(self, client, db_session):
        """【WP8.2 测试】GET /portfolios/{id}/backtests/{run_id}/members 返回成员快照列表。"""
        p = _make_portfolio(db_session, name="QA-Link-BtSnap")
        sym1 = _make_symbol(db_session, symbol="800040", name="回测标的1")
        sym2 = _make_symbol(db_session, symbol="800041", name="回测标的2")

        member_snapshot = [
            {
                "member_id": 101,
                "symbol_id": sym1.id,
                "effective_from": "2026-07-01T00:00:00",
                "effective_to": None,
                "execution_mode": "auto",
                "entry_rule_version_id": 11,
                "exit_rule_version_id": 12,
            },
            {
                "member_id": 102,
                "symbol_id": sym2.id,
                "effective_from": "2026-07-01T00:00:00",
                "effective_to": None,
                "execution_mode": "auto",
                "entry_rule_version_id": 13,
                "exit_rule_version_id": 14,
            },
        ]
        excluded_members = [
            {"member_id": 999, "symbol_id": sym1.id, "reason": "manual excluded"},
        ]

        bt_run = BacktestRun(
            portfolio_id=p.id,
            run_name="QA-BtRun-Snap",
            symbols_json="[]",
            rule_config_json="{}",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            initial_capital=100000.0,
            status="completed",
            member_snapshot_json=json.dumps(member_snapshot),
            excluded_members_json=json.dumps(excluded_members),
            source_type="member",
            score_mode="combined",
        )
        db_session.add(bt_run)
        db_session.commit()
        db_session.refresh(bt_run)

        resp = client.get(f"/api/v1/portfolios/{p.id}/backtests/{bt_run.id}/members")
        assert resp.status_code == 200
        data = resp.json()

        assert data["portfolio_id"] == p.id
        assert data["run_id"] == bt_run.id
        assert data["has_snapshot"] is True
        assert data["member_count"] == 2
        members = data["members"]
        assert len(members) == 2
        syms_in_snap = {m["symbol"] for m in members}
        assert "800040" in syms_in_snap
        assert "800041" in syms_in_snap
        assert data["excluded_members"] == excluded_members
        assert data["run_meta"]["run_name"] == "QA-BtRun-Snap"
        assert data["run_meta"]["source_type"] == "member"
        assert data["run_meta"]["score_mode"] == "combined"

    def test_backtest_member_snapshot_no_snapshot(self, client, db_session):
        """【WP8.2 测试】历史回测无 member_snapshot_json 时 has_snapshot=False。"""
        p = _make_portfolio(db_session, name="QA-Link-BtSnap-Empty")
        bt_run = BacktestRun(
            portfolio_id=p.id,
            run_name="QA-BtRun-Legacy",
            symbols_json="[]",
            rule_config_json="{}",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            initial_capital=100000.0,
            status="completed",
            member_snapshot_json=None,
        )
        db_session.add(bt_run)
        db_session.commit()
        db_session.refresh(bt_run)

        resp = client.get(f"/api/v1/portfolios/{p.id}/backtests/{bt_run.id}/members")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_snapshot"] is False
        assert data["members"] == []
        assert data["member_count"] == 0
        assert "message" in data

    def test_backtest_member_snapshot_run_not_found(self, client, db_session):
        """【WP8.2 测试】回测 run 不存在返回 404。"""
        p = _make_portfolio(db_session, name="QA-Link-BtSnap-NoRun")
        resp = client.get(f"/api/v1/portfolios/{p.id}/backtests/99999/members")
        assert resp.status_code == 404

    def test_backtest_member_snapshot_cross_portfolio(self, client, db_session):
        """【WP8.2 测试】回测不属于该组合返回 400。"""
        p1 = _make_portfolio(db_session, name="QA-Link-BtSnap-P1")
        p2 = _make_portfolio(db_session, name="QA-Link-BtSnap-P2")
        bt_run = BacktestRun(
            portfolio_id=p2.id,
            run_name="QA-BtRun-P2",
            symbols_json="[]",
            rule_config_json="{}",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            initial_capital=100000.0,
            status="completed",
        )
        db_session.add(bt_run)
        db_session.commit()
        db_session.refresh(bt_run)

        resp = client.get(f"/api/v1/portfolios/{p1.id}/backtests/{bt_run.id}/members")
        assert resp.status_code == 400


class TestAttributionSuggestReviewAPI:
    """【WP8.2 测试】归因异常建议复盘 API。"""

    def test_attribution_suggest_review_with_negative_member(self, client, db_session):
        """【WP8.2 测试】成员贡献为负时 suggested_review=True，anomalies 含 negative_member。"""
        p = _make_portfolio(db_session, name="QA-Link-Suggest-Neg", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="800050")
        m = _make_member(db_session, p.id, sym.id, execution_mode="manual")
        _seed_snapshots(db_session, p.id, days=3)

        order = _make_order_with_attribution(
            db_session,
            portfolio_id=p.id, symbol_id=sym.id,
            side="sell", status="filled",
            member_id=m.id, execution_mode="manual", source_type="manual",
            submitted_price=10.0, filled_price=9.5, filled_quantity=100.0, fee=1.0,
        )
        _make_trade_for_order(
            db_session,
            portfolio_id=p.id, symbol_id=sym.id, order_id=order.id,
            side="sell", price=9.5, quantity=100.0, realized_pnl=-300.0,
        )

        resp = client.get(
            f"/api/v1/portfolios/{p.id}/attribution/suggest-review",
            params={"start_date": "2026-07-01", "end_date": "2026-07-31"},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["portfolio_id"] == p.id
        assert data["start_date"] == "2026-07-01"
        assert data["end_date"] == "2026-07-31"
        assert data["suggested_review"] is True
        assert data["anomaly_count"] >= 1
        anomaly_types = {a["type"] for a in data["anomalies"]}
        assert "negative_member" in anomaly_types
        neg = next(a for a in data["anomalies"] if a["type"] == "negative_member")
        assert neg["member_id"] == m.id
        assert neg["pnl"] == -300.0

    def test_attribution_suggest_review_sample_insufficient(self, client, db_session):
        """【WP8.2 测试】样本不足时 anomalies 含 sample_insufficient。"""
        p = _make_portfolio(db_session, name="QA-Link-Suggest-Sample", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="800051")
        _seed_snapshots(db_session, p.id, days=3)

        order = _make_order_with_attribution(
            db_session,
            portfolio_id=p.id, symbol_id=sym.id,
            side="sell", status="filled",
            execution_mode="manual",
            submitted_price=10.0, filled_price=11.0, filled_quantity=100.0, fee=0.0,
        )
        _make_trade_for_order(
            db_session,
            portfolio_id=p.id, symbol_id=sym.id, order_id=order.id,
            side="sell", price=11.0, quantity=100.0, realized_pnl=100.0,
        )

        resp = client.get(
            f"/api/v1/portfolios/{p.id}/attribution/suggest-review",
            params={"start_date": "2026-07-01", "end_date": "2026-07-31"},
        )
        assert resp.status_code == 200
        data = resp.json()
        anomaly_types = {a["type"] for a in data["anomalies"]}
        assert "sample_insufficient" in anomaly_types
        assert data["suggested_review"] is True

    def test_attribution_suggest_review_portfolio_not_found(self, client, db_session):
        """【WP8.2 测试】组合不存在返回 404。"""
        resp = client.get(
            "/api/v1/portfolios/99999/attribution/suggest-review",
            params={"start_date": "2026-07-01", "end_date": "2026-07-31"},
        )
        assert resp.status_code == 404
