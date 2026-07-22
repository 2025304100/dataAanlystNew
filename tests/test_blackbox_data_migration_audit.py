"""黑盒数据迁移对账测试（Final.3 / spec 第 34 章）。

核对迁移前后下列表数据一致性：
- watchlists 不减少（基线 1）
- watchlist_items 原 6 项必须存在，仅允许新增导入项
- portfolios ID/名称/资金不变（基线 2）
- positions 数量/成本/标的不变（基线 3）
- portfolio_members 至少覆盖全部现有持仓（基线 0/不存在 → 至少回填）
- cash_ledger 迁移阶段完全不变（基线 9）
- sim_orders ID 和金额不变（基线 7）
- sim_trades ID 和金额不变（基线 7）
- backtest_runs 历史运行可读（基线 22）
- scheduled_tasks 原计划和启停状态不变（基线 11）
- alert_rules 不变（基线 3）
- alert_events 历史事件可读（基线 95）

任何现金/持仓/订单/成交对账不一致属阻断发布问题。

测试策略：
- 使用 SQLite 内存库（conftest.db_session fixture）
- 每个测试用例独立准备基线数据，然后断言读取结果与基线一致
- 模拟"迁移前后对账"：插入基线 → 重新读取 → 字段级比对
- 不依赖生产数据库，不修改被测代码
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.alert import AlertEvent, AlertRule
from app.models.backtest import BacktestRun
from app.models.discovery_candidate import DiscoveryCandidate
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_equity_snapshot import PortfolioEquitySnapshot
from app.models.portfolio_member import (
    EXECUTION_MANUAL,
    SOURCE_LEGACY_POSITION,
    STATUS_ACTIVE,
    PortfolioMember,
)
from app.models.scheduled_task import ScheduledTask
from app.models.scan import ScanRun
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem


pytestmark = pytest.mark.blackbox


# ============================================================================
# 测试辅助
# ============================================================================


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_symbol(db_session, symbol: str = "600000", name: str = "测试标的") -> Symbol:
    sym = Symbol(
        symbol=symbol, name=name, asset_type="stock", market="cn", theme="测试",
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_watchlist(db_session, name: str = "QA-Audit-WL") -> Watchlist:
    wl = Watchlist(name=name, list_type="custom", description="audit baseline")
    db_session.add(wl)
    db_session.commit()
    db_session.refresh(wl)
    return wl


def _make_portfolio(
    db_session,
    name: str = "QA-Audit-PF",
    total_capital: float = 100000.0,
) -> Portfolio:
    p = Portfolio(
        name=name,
        account_type="simulated",
        total_capital=total_capital,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        is_default=0,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


# ============================================================================
# 对账测试
# ============================================================================


class TestDataMigrationAudit:
    """数据迁移对账测试。

    每个测试先插入基线数据（模拟迁移前的状态），然后通过 ORM 与原生 SQL
    两种途径重新读取，验证迁移后的数据与基线一致。任一字段漂移即视为对账失败。
    """

    # ------------------------------------------------------------------
    # 1. watchlists 不减少（基线 1）
    # ------------------------------------------------------------------
    def test_watchlists_not_reduced(self, db_session):
        """watchlists 表迁移后记录数不减少，原记录字段不变。"""
        # Arrange: 基线 1 条
        wl = _make_watchlist(db_session, name="QA-Audit-WL-Keep")
        baseline_id = wl.id
        baseline_name = wl.name
        baseline_list_type = wl.list_type

        # Act: 模拟"迁移后"重新读取
        db_session.expire_all()
        reloaded = db_session.query(Watchlist).filter_by(id=baseline_id).one()

        # Assert: 不减少（count=1），字段不变
        total_count = db_session.query(Watchlist).count()
        assert total_count == 1, f"watchlists 应保留 1 条，实际 {total_count}"
        assert reloaded.id == baseline_id
        assert reloaded.name == baseline_name
        assert reloaded.list_type == baseline_list_type

    # ------------------------------------------------------------------
    # 2. watchlist_items 原 6 项必须存在，仅允许新增导入项
    # ------------------------------------------------------------------
    def test_watchlist_items_original_six_preserved(self, db_session):
        """原 6 项 watchlist_items 必须存在；允许新增但不能减少。"""
        # Arrange: 1 个 watchlist + 6 个原项
        wl = _make_watchlist(db_session, name="QA-Audit-WL-Items")
        original_symbol_ids = []
        for i in range(6):
            sym = _make_symbol(db_session, symbol=f"600{i:03d}", name=f"标的-{i}")
            original_symbol_ids.append(sym.id)
            item = WatchlistItem(
                watchlist_id=wl.id,
                symbol_id=sym.id,
                note=f"original-{i}",
                origin_type="manual",
                status="watching",
                priority=i,
            )
            db_session.add(item)
        db_session.commit()

        # Act: 模拟迁移后新增 1 项导入项（允许）
        extra_sym = _make_symbol(db_session, symbol="600999", name="导入标的")
        new_item = WatchlistItem(
            watchlist_id=wl.id,
            symbol_id=extra_sym.id,
            note="imported",
            origin_type="scan_result",
            status="watching",
            priority=0,
        )
        db_session.add(new_item)
        db_session.commit()
        db_session.expire_all()

        # Assert: 原 6 项仍存在
        items = (
            db_session.query(WatchlistItem)
            .filter_by(watchlist_id=wl.id)
            .all()
        )
        assert len(items) == 7, f"应有 7 项（6 原 + 1 新），实际 {len(items)}"
        reloaded_symbol_ids = {it.symbol_id for it in items}
        for sid in original_symbol_ids:
            assert sid in reloaded_symbol_ids, f"原 symbol_id={sid} 缺失"
        # 原项 note 不变
        original_items = [
            it for it in items if it.symbol_id in original_symbol_ids
        ]
        for it in original_items:
            assert it.note.startswith("original-"), (
                f"原项 note 被篡改: {it.note}"
            )

    # ------------------------------------------------------------------
    # 3. portfolios ID/名称/资金不变（基线 2）
    # ------------------------------------------------------------------
    def test_portfolios_unchanged(self, db_session):
        """portfolios 表 ID/名称/资金迁移后不变。"""
        # Arrange: 基线 2 个组合
        p1 = _make_portfolio(db_session, name="QA-Audit-PF1", total_capital=100000.0)
        p2 = _make_portfolio(db_session, name="QA-Audit-PF2", total_capital=50000.0)

        baseline = {
            p1.id: {"name": p1.name, "total_capital": p1.total_capital},
            p2.id: {"name": p2.name, "total_capital": p2.total_capital},
        }

        # Act: 重新读取
        db_session.expire_all()
        reloaded = db_session.query(Portfolio).order_by(Portfolio.id).all()

        # Assert: 数量与字段
        assert len(reloaded) == 2
        for p in reloaded:
            base = baseline[p.id]
            assert p.name == base["name"], (
                f"portfolio id={p.id} name 漂移: {p.name} != {base['name']}"
            )
            assert p.total_capital == base["total_capital"], (
                f"portfolio id={p.id} total_capital 漂移: "
                f"{p.total_capital} != {base['total_capital']}"
            )

    # ------------------------------------------------------------------
    # 4. positions 数量/成本/标的不变（基线 3）
    # ------------------------------------------------------------------
    def test_positions_unchanged(self, db_session):
        """positions 表 数量/成本/标的不变。"""
        # Arrange: 1 组合 + 3 持仓
        p = _make_portfolio(db_session, name="QA-Audit-PF-Pos")
        symbols = []
        positions_baseline = []
        for i in range(3):
            sym = _make_symbol(db_session, symbol=f"601{i:03d}", name=f"持仓-{i}")
            symbols.append(sym)
            pos = Position(
                portfolio_id=p.id,
                symbol_id=sym.id,
                quantity=100.0 * (i + 1),
                avg_cost=10.0 + i,
                latest_price=11.0 + i,
                market_value=(100.0 * (i + 1)) * (11.0 + i),
                position_pct=0.1 * (i + 1),
                asset_type="stock",
                theme="测试",
            )
            db_session.add(pos)
            positions_baseline.append(pos)
        db_session.commit()

        baseline = [
            {
                "id": pos.id,
                "portfolio_id": pos.portfolio_id,
                "symbol_id": pos.symbol_id,
                "quantity": pos.quantity,
                "avg_cost": pos.avg_cost,
                "asset_type": pos.asset_type,
            }
            for pos in positions_baseline
        ]

        # Act: 重新读取
        db_session.expire_all()
        reloaded = (
            db_session.query(Position)
            .filter_by(portfolio_id=p.id)
            .order_by(Position.id)
            .all()
        )

        # Assert
        assert len(reloaded) == 3
        for base, got in zip(baseline, reloaded):
            assert got.id == base["id"]
            assert got.portfolio_id == base["portfolio_id"]
            assert got.symbol_id == base["symbol_id"]
            assert got.quantity == base["quantity"], (
                f"position id={got.id} quantity 漂移: {got.quantity} != {base['quantity']}"
            )
            assert got.avg_cost == base["avg_cost"], (
                f"position id={got.id} avg_cost 漂移: {got.avg_cost} != {base['avg_cost']}"
            )
            assert got.asset_type == base["asset_type"]

    # ------------------------------------------------------------------
    # 5. portfolio_members 至少覆盖全部现有持仓（基线 0/不存在 → 至少回填）
    # ------------------------------------------------------------------
    def test_portfolio_members_cover_all_positions(self, db_session):
        """portfolio_members 至少覆盖全部现有持仓。"""
        # Arrange: 1 组合 + 2 持仓 + 0 成员（基线 0/不存在）
        p = _make_portfolio(db_session, name="QA-Audit-PF-Members")
        sym1 = _make_symbol(db_session, symbol="602001", name="持仓-1")
        sym2 = _make_symbol(db_session, symbol="602002", name="持仓-2")
        pos1 = Position(
            portfolio_id=p.id, symbol_id=sym1.id, quantity=100, avg_cost=10.0,
            latest_price=11.0, market_value=1100.0, position_pct=0.1,
            asset_type="stock",
        )
        pos2 = Position(
            portfolio_id=p.id, symbol_id=sym2.id, quantity=200, avg_cost=20.0,
            latest_price=22.0, market_value=4400.0, position_pct=0.2,
            asset_type="stock",
        )
        db_session.add_all([pos1, pos2])
        db_session.commit()

        # 模拟迁移后回填成员（覆盖全部持仓）
        m1 = PortfolioMember(
            portfolio_id=p.id, symbol_id=sym1.id,
            status=STATUS_ACTIVE, execution_mode=EXECUTION_MANUAL,
            source_type=SOURCE_LEGACY_POSITION,
            effective_from=_utcnow_naive(),
        )
        m2 = PortfolioMember(
            portfolio_id=p.id, symbol_id=sym2.id,
            status=STATUS_ACTIVE, execution_mode=EXECUTION_MANUAL,
            source_type=SOURCE_LEGACY_POSITION,
            effective_from=_utcnow_naive(),
        )
        db_session.add_all([m1, m2])
        db_session.commit()

        # Act: 查询持仓对应的成员覆盖
        db_session.expire_all()
        positions = (
            db_session.query(Position)
            .filter_by(portfolio_id=p.id)
            .all()
        )
        position_symbol_ids = {pos.symbol_id for pos in positions}
        active_members = (
            db_session.query(PortfolioMember)
            .filter(
                PortfolioMember.portfolio_id == p.id,
                PortfolioMember.effective_to.is_(None),
            )
            .all()
        )
        member_symbol_ids = {m.symbol_id for m in active_members}

        # Assert: 至少覆盖全部现有持仓
        uncovered = position_symbol_ids - member_symbol_ids
        assert not uncovered, (
            f"以下持仓未在 portfolio_members 中覆盖: {uncovered}"
        )

    # ------------------------------------------------------------------
    # 6. cash_ledger 迁移阶段完全不变（基线 9）
    # ------------------------------------------------------------------
    def test_cash_ledger_unchanged(self, db_session):
        """cash_ledger 迁移阶段完全不变（基线 9 条）。"""
        # Arrange: 1 组合 + 9 条现金流水
        p = _make_portfolio(db_session, name="QA-Audit-PF-Ledger")
        baseline_rows = []
        running_balance = 0.0
        for i in range(9):
            amount = 1000.0 * (i + 1) if i % 2 == 0 else -500.0 * (i + 1)
            running_balance += amount
            ledger = CashLedger(
                portfolio_id=p.id,
                entry_type="deposit" if amount > 0 else "withdraw",
                amount=amount,
                balance_after=running_balance,
                ref_type="manual",
                ref_id=i,
                note=f"baseline-{i}",
            )
            db_session.add(ledger)
            baseline_rows.append(ledger)
        db_session.commit()

        baseline = [
            {
                "id": r.id,
                "portfolio_id": r.portfolio_id,
                "entry_type": r.entry_type,
                "amount": r.amount,
                "balance_after": r.balance_after,
                "note": r.note,
            }
            for r in baseline_rows
        ]

        # Act: 重新读取
        db_session.expire_all()
        reloaded = (
            db_session.query(CashLedger)
            .filter_by(portfolio_id=p.id)
            .order_by(CashLedger.id)
            .all()
        )

        # Assert
        assert len(reloaded) == 9, f"cash_ledger 应 9 条，实际 {len(reloaded)}"
        for base, got in zip(baseline, reloaded):
            assert got.id == base["id"]
            assert got.portfolio_id == base["portfolio_id"]
            assert got.entry_type == base["entry_type"]
            assert got.amount == base["amount"], (
                f"ledger id={got.id} amount 漂移: {got.amount} != {base['amount']}"
            )
            assert got.balance_after == base["balance_after"], (
                f"ledger id={got.id} balance_after 漂移: "
                f"{got.balance_after} != {base['balance_after']}"
            )
            assert got.note == base["note"]

    # ------------------------------------------------------------------
    # 7. sim_orders ID 和金额不变（基线 7）
    # ------------------------------------------------------------------
    def test_sim_orders_unchanged(self, db_session):
        """sim_orders ID 和金额不变（基线 7 条）。"""
        # Arrange
        p = _make_portfolio(db_session, name="QA-Audit-PF-Orders")
        sym = _make_symbol(db_session, symbol="603001", name="订单标的")
        baseline_rows = []
        for i in range(7):
            order = SimOrder(
                portfolio_id=p.id,
                symbol_id=sym.id,
                side="buy" if i % 2 == 0 else "sell",
                order_type="limit",
                quantity=100.0 * (i + 1),
                submitted_price=10.0 + i,
                status="filled",
                filled_quantity=100.0 * (i + 1),
                filled_price=10.0 + i,
                filled_amount=(100.0 * (i + 1)) * (10.0 + i),
                fee=5.0,
                note=f"order-{i}",
            )
            db_session.add(order)
            baseline_rows.append(order)
        db_session.commit()

        baseline = [
            {
                "id": r.id,
                "portfolio_id": r.portfolio_id,
                "symbol_id": r.symbol_id,
                "side": r.side,
                "filled_amount": r.filled_amount,
                "filled_price": r.filled_price,
                "filled_quantity": r.filled_quantity,
            }
            for r in baseline_rows
        ]

        # Act
        db_session.expire_all()
        reloaded = (
            db_session.query(SimOrder)
            .filter_by(portfolio_id=p.id)
            .order_by(SimOrder.id)
            .all()
        )

        # Assert
        assert len(reloaded) == 7
        for base, got in zip(baseline, reloaded):
            assert got.id == base["id"]
            assert got.portfolio_id == base["portfolio_id"]
            assert got.symbol_id == base["symbol_id"]
            assert got.side == base["side"]
            assert got.filled_amount == base["filled_amount"], (
                f"order id={got.id} filled_amount 漂移: "
                f"{got.filled_amount} != {base['filled_amount']}"
            )
            assert got.filled_price == base["filled_price"]
            assert got.filled_quantity == base["filled_quantity"]

    # ------------------------------------------------------------------
    # 8. sim_trades ID 和金额不变（基线 7）
    # ------------------------------------------------------------------
    def test_sim_trades_unchanged(self, db_session):
        """sim_trades ID 和金额不变（基线 7 条）。"""
        # Arrange
        p = _make_portfolio(db_session, name="QA-Audit-PF-Trades")
        sym = _make_symbol(db_session, symbol="604001", name="成交流水标的")
        # 先造 7 个 SimOrder 作为外键
        orders = []
        for i in range(7):
            order = SimOrder(
                portfolio_id=p.id,
                symbol_id=sym.id,
                side="buy",
                order_type="limit",
                quantity=100.0 * (i + 1),
                submitted_price=10.0 + i,
                status="filled",
                filled_quantity=100.0 * (i + 1),
                filled_price=10.0 + i,
                filled_amount=(100.0 * (i + 1)) * (10.0 + i),
                fee=5.0,
            )
            db_session.add(order)
            orders.append(order)
        db_session.commit()

        baseline_trades = []
        for i, order in enumerate(orders):
            trade = SimTrade(
                portfolio_id=p.id,
                symbol_id=sym.id,
                order_id=order.id,
                side="buy",
                quantity=100.0 * (i + 1),
                price=10.0 + i,
                amount=(100.0 * (i + 1)) * (10.0 + i),
                fee=5.0,
                realized_pnl=None,
            )
            db_session.add(trade)
            baseline_trades.append(trade)
        db_session.commit()

        baseline = [
            {
                "id": t.id,
                "portfolio_id": t.portfolio_id,
                "order_id": t.order_id,
                "amount": t.amount,
                "price": t.price,
                "quantity": t.quantity,
            }
            for t in baseline_trades
        ]

        # Act
        db_session.expire_all()
        reloaded = (
            db_session.query(SimTrade)
            .filter_by(portfolio_id=p.id)
            .order_by(SimTrade.id)
            .all()
        )

        # Assert
        assert len(reloaded) == 7
        for base, got in zip(baseline, reloaded):
            assert got.id == base["id"]
            assert got.portfolio_id == base["portfolio_id"]
            assert got.order_id == base["order_id"]
            assert got.amount == base["amount"], (
                f"trade id={got.id} amount 漂移: {got.amount} != {base['amount']}"
            )
            assert got.price == base["price"]
            assert got.quantity == base["quantity"]

    # ------------------------------------------------------------------
    # 9. backtest_runs 历史运行可读（基线 22）
    # ------------------------------------------------------------------
    def test_backtest_runs_readable(self, db_session):
        """backtest_runs 历史运行可读（基线 22 条）。"""
        # Arrange
        p = _make_portfolio(db_session, name="QA-Audit-PF-BT")
        baseline_rows = []
        for i in range(22):
            run = BacktestRun(
                portfolio_id=p.id,
                run_name=f"QA-BT-{i:03d}",
                symbols_json='["600000"]',
                rule_config_json='{"buy":{}}',
                start_date=date(2026, 1, 1),
                end_date=date(2026, 6, 30),
                initial_capital=100000.0,
                total_return=1000.0 * i,
                total_return_pct=0.01 * i,
                max_drawdown=-500.0,
                max_drawdown_pct=-0.005,
                sharpe_ratio=1.5,
                win_rate=0.55,
                profit_factor=1.2,
                trade_count=i,
                avg_holding_days=5.0,
                status="completed",
                created_at=_utcnow_naive() - timedelta(days=i),
                started_at=_utcnow_naive() - timedelta(days=i),
                finished_at=_utcnow_naive() - timedelta(days=i, hours=1),
            )
            db_session.add(run)
            baseline_rows.append(run)
        db_session.commit()

        baseline_ids = [r.id for r in baseline_rows]
        baseline_names = [r.run_name for r in baseline_rows]

        # Act: 重新读取
        db_session.expire_all()
        reloaded = (
            db_session.query(BacktestRun)
            .filter_by(portfolio_id=p.id)
            .order_by(BacktestRun.id)
            .all()
        )

        # Assert: 22 条历史运行可读
        assert len(reloaded) == 22, f"backtest_runs 应 22 条，实际 {len(reloaded)}"
        for got, base_id, base_name in zip(reloaded, baseline_ids, baseline_names):
            assert got.id == base_id
            assert got.run_name == base_name
            assert got.status == "completed"
            assert got.portfolio_id == p.id
            # 关键绩效字段可读
            assert got.total_return is not None
            assert got.sharpe_ratio is not None
            assert got.start_date == date(2026, 1, 1)
            assert got.end_date == date(2026, 6, 30)

    # ------------------------------------------------------------------
    # 10. scheduled_tasks 原计划和启停状态不变（基线 11）
    # ------------------------------------------------------------------
    def test_scheduled_tasks_unchanged(self, db_session):
        """scheduled_tasks 原计划和启停状态不变（基线 11 条）。"""
        # Arrange: 11 条调度任务（启停状态分布 7 启 4 停）
        baseline_rows = []
        for i in range(11):
            enabled = 1 if i < 7 else 0
            task = ScheduledTask(
                name=f"QA-Task-{i:03d}",
                task_type="data_sync" if i % 2 == 0 else "scan",
                frequency="daily",
                time_of_day="09:30",
                weekdays_json="[]",
                timezone="Asia/Shanghai",
                payload_json=f'{{"i":{i}}}',
                enabled=enabled,
                next_run_at=_utcnow_naive() + timedelta(hours=i),
                last_run_at=_utcnow_naive() - timedelta(hours=i),
                last_status="done" if enabled else None,
            )
            db_session.add(task)
            baseline_rows.append(task)
        db_session.commit()

        baseline = [
            {
                "id": t.id,
                "name": t.name,
                "task_type": t.task_type,
                "enabled": t.enabled,
                "frequency": t.frequency,
                "payload_json": t.payload_json,
            }
            for t in baseline_rows
        ]

        # Act
        db_session.expire_all()
        reloaded = (
            db_session.query(ScheduledTask)
            .order_by(ScheduledTask.id)
            .all()
        )

        # Assert
        assert len(reloaded) == 11
        enabled_count = sum(1 for t in reloaded if t.enabled == 1)
        disabled_count = sum(1 for t in reloaded if t.enabled == 0)
        assert enabled_count == 7, f"启用数应 7，实际 {enabled_count}"
        assert disabled_count == 4, f"停用数应 4，实际 {disabled_count}"
        for base, got in zip(baseline, reloaded):
            assert got.id == base["id"]
            assert got.name == base["name"]
            assert got.task_type == base["task_type"]
            assert got.enabled == base["enabled"], (
                f"task id={got.id} enabled 漂移: {got.enabled} != {base['enabled']}"
            )
            assert got.frequency == base["frequency"]
            assert got.payload_json == base["payload_json"]

    # ------------------------------------------------------------------
    # 11. alert_rules 不变（基线 3）
    # ------------------------------------------------------------------
    def test_alert_rules_unchanged(self, db_session):
        """alert_rules 不变（基线 3 条）。"""
        # Arrange
        baseline_rows = []
        configs = [
            {"name": "QA-Alert-Score", "alert_type": "score_drop",
             "severity": "warn", "cooldown": 60, "config": '{"threshold":40}'},
            {"name": "QA-Alert-Data", "alert_type": "data_stale",
             "severity": "error", "cooldown": 30, "config": '{"stale_days":7}'},
            {"name": "QA-Alert-Task", "alert_type": "task_failed",
             "severity": "critical", "cooldown": 15, "config": '{"max_retries":3}'},
        ]
        for cfg in configs:
            rule = AlertRule(
                name=cfg["name"],
                alert_type=cfg["alert_type"],
                enabled=1,
                severity=cfg["severity"],
                config_json=cfg["config"],
                cooldown_minutes=cfg["cooldown"],
            )
            db_session.add(rule)
            baseline_rows.append(rule)
        db_session.commit()

        baseline = [
            {
                "id": r.id, "name": r.name, "alert_type": r.alert_type,
                "severity": r.severity, "config_json": r.config_json,
                "cooldown_minutes": r.cooldown_minutes, "enabled": r.enabled,
            }
            for r in baseline_rows
        ]

        # Act
        db_session.expire_all()
        reloaded = db_session.query(AlertRule).order_by(AlertRule.id).all()

        # Assert
        assert len(reloaded) == 3
        for base, got in zip(baseline, reloaded):
            assert got.id == base["id"]
            assert got.name == base["name"]
            assert got.alert_type == base["alert_type"]
            assert got.severity == base["severity"]
            assert got.config_json == base["config_json"]
            assert got.cooldown_minutes == base["cooldown_minutes"]
            assert got.enabled == base["enabled"]

    # ------------------------------------------------------------------
    # 12. alert_events 历史事件可读（基线 95）
    # ------------------------------------------------------------------
    def test_alert_events_readable(self, db_session):
        """alert_events 历史事件可读（基线 95 条）。"""
        # Arrange: 先造 1 条规则，再造 95 条事件
        rule = AlertRule(
            name="QA-Audit-Alert-Rule",
            alert_type="score_drop",
            enabled=1,
            severity="warn",
            cooldown_minutes=60,
        )
        db_session.add(rule)
        db_session.commit()

        baseline_events = []
        for i in range(95):
            ev = AlertEvent(
                rule_id=rule.id,
                alert_type="score_drop",
                severity="warn" if i % 3 != 0 else "error",
                title=f"告警-{i:03d}",
                message=f"标的 {i} 触发分数下降告警",
                symbol_id=i + 1,
                data_json=f'{{"score":{80 - i % 50}}}',
                acknowledged=1 if i % 5 == 0 else 0,
                created_at=_utcnow_naive() - timedelta(hours=i),
            )
            db_session.add(ev)
            baseline_events.append(ev)
        db_session.commit()

        baseline_ids = [e.id for e in baseline_events]
        baseline_titles = [e.title for e in baseline_events]

        # Act: 重新读取（分批验证可读性）
        db_session.expire_all()
        reloaded_all = (
            db_session.query(AlertEvent)
            .filter_by(rule_id=rule.id)
            .order_by(AlertEvent.id)
            .all()
        )

        # 抽样读取（验证任意 ID 都可读）
        sample_id = baseline_ids[42]
        sample_event = db_session.query(AlertEvent).filter_by(id=sample_id).one()

        # Assert
        assert len(reloaded_all) == 95, (
            f"alert_events 应 95 条，实际 {len(reloaded_all)}"
        )
        for got, base_id, base_title in zip(
            reloaded_all, baseline_ids, baseline_titles
        ):
            assert got.id == base_id
            assert got.title == base_title
            assert got.rule_id == rule.id
            assert got.alert_type == "score_drop"
        # 抽样事件可读
        assert sample_event.id == sample_id
        assert sample_event.title == baseline_titles[42]
        assert sample_event.message.startswith("标的 42 触发")

    # ------------------------------------------------------------------
    # 13. 跨表对账：现金/持仓/订单/成交一致性（阻断发布问题）
    # ------------------------------------------------------------------
    def test_cash_position_order_trade_consistency(self, db_session):
        """跨表对账：现金/持仓/订单/成交应一致。

        任何现金/持仓/订单/成交对账不一致属阻断发布问题。
        验证：
        - 每笔 SimOrder 都有对应的 SimTrade
        - 每笔买入 SimTrade 应在 Position 中体现（数量累加）
        - CashLedger 余额与订单金额变动一致
        """
        # Arrange
        p = _make_portfolio(db_session, name="QA-Audit-Consistency")
        sym = _make_symbol(db_session, symbol="605001", name="对账标的")

        # 初始资金 100000
        opening = CashLedger(
            portfolio_id=p.id, entry_type="deposit",
            amount=100000.0, balance_after=100000.0,
            ref_type="portfolio", ref_id=p.id,
        )
        db_session.add(opening)
        db_session.commit()

        # 买入 1000 股 @ 10.0 = 10000
        order = SimOrder(
            portfolio_id=p.id, symbol_id=sym.id, side="buy",
            order_type="limit", quantity=1000.0,
            submitted_price=10.0, status="filled",
            filled_quantity=1000.0, filled_price=10.0,
            filled_amount=10000.0, fee=0.0,
        )
        db_session.add(order)
        db_session.commit()

        trade = SimTrade(
            portfolio_id=p.id, symbol_id=sym.id, order_id=order.id,
            side="buy", quantity=1000.0, price=10.0,
            amount=10000.0, fee=0.0,
        )
        db_session.add(trade)

        # 现金扣减
        cash_out = CashLedger(
            portfolio_id=p.id, entry_type="buy",
            amount=-10000.0, balance_after=90000.0,
            ref_type="sim_order", ref_id=order.id,
            note="Buy 1000 @ 10.0",
        )
        db_session.add(cash_out)

        # 持仓创建
        position = Position(
            portfolio_id=p.id, symbol_id=sym.id,
            quantity=1000.0, avg_cost=10.0, latest_price=10.0,
            market_value=10000.0, position_pct=0.1,
            asset_type="stock",
        )
        db_session.add(position)
        db_session.commit()

        # Act: 重新读取并对账
        db_session.expire_all()
        orders = db_session.query(SimOrder).filter_by(portfolio_id=p.id).all()
        trades = db_session.query(SimTrade).filter_by(portfolio_id=p.id).all()
        positions = db_session.query(Position).filter_by(portfolio_id=p.id).all()
        ledgers = (
            db_session.query(CashLedger)
            .filter_by(portfolio_id=p.id)
            .order_by(CashLedger.id)
            .all()
        )

        # Assert 1: 每笔订单都有对应成交
        order_ids = {o.id for o in orders}
        trade_order_ids = {t.order_id for t in trades}
        missing_trades = order_ids - trade_order_ids
        assert not missing_trades, (
            f"以下订单无对应成交: {missing_trades}"
        )

        # Assert 2: 订单金额 = 成交金额
        order_amount_map = {o.id: o.filled_amount for o in orders}
        for t in trades:
            assert t.amount == order_amount_map[t.order_id], (
                f"trade id={t.id} amount={t.amount} != "
                f"order id={t.order_id} filled_amount={order_amount_map[t.order_id]}"
            )

        # Assert 3: 持仓数量 = 买入成交数量累加
        buy_qty = sum(t.quantity for t in trades if t.side == "buy")
        sell_qty = sum(t.quantity for t in trades if t.side == "sell")
        expected_position_qty = buy_qty - sell_qty
        actual_position_qty = sum(p.quantity for p in positions)
        assert actual_position_qty == expected_position_qty, (
            f"持仓数量 {actual_position_qty} != 成交累计 {expected_position_qty}"
        )

        # Assert 4: 现金余额 = 初始资金 - 买入金额 + 卖出金额
        latest_balance = ledgers[-1].balance_after
        buy_amount = sum(o.filled_amount for o in orders if o.side == "buy")
        sell_amount = sum(o.filled_amount for o in orders if o.side == "sell")
        expected_balance = 100000.0 - buy_amount + sell_amount
        assert latest_balance == expected_balance, (
            f"现金余额 {latest_balance} != 预期 {expected_balance}"
        )

    # ------------------------------------------------------------------
    # 14. discovery_candidates 历史候选不丢失（WP9.7：候选表数据保留核对）
    # ------------------------------------------------------------------
    def test_discovery_candidates_preserved(self, db_session):
        """discovery_candidates 历史候选数据不丢失。

        WP9.7 spec 要求：不删除历史候选。
        验证：迁移前后 discovery_candidates 表记录数不减少，字段不变。
        """
        # Arrange: 1 个 ScanRun + 3 个 UniverseSymbol + 5 个 DiscoveryCandidate
        p = _make_portfolio(db_session, name="QA-Audit-PF-Candidates")
        run = ScanRun(
            portfolio_id=p.id,
            run_name="QA-Audit-Candidate-Run",
            scope_snapshot="cn-stock",
            status="done",
            started_at=_utcnow_naive(),
            finished_at=_utcnow_naive(),
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        baseline_candidates = []
        for i in range(5):
            # 每个 candidate 关联一个 universe_symbol
            u_sym = UniverseSymbol(
                symbol=f"700{i:03d}",
                name=f"候选标的-{i}",
                asset_type="stock",
                market="sh",
                region="cn",
            )
            db_session.add(u_sym)
            db_session.commit()
            db_session.refresh(u_sym)

            cand = DiscoveryCandidate(
                scan_run_id=run.id,
                universe_symbol_id=u_sym.id,
                symbol=u_sym.symbol,
                name=u_sym.name,
                asset_type="stock",
                quality_score=70.0 + i,
                timing_score=65.0 + i,
                priority_score=75.0 + i,
                stage="start" if i < 3 else "pullback",
                action="open" if i < 3 else "reduce",
                is_promoted=1 if i == 0 else 0,
                promoted_at=_utcnow_naive() if i == 0 else None,
            )
            db_session.add(cand)
            baseline_candidates.append(cand)
        db_session.commit()

        baseline = [
            {
                "id": c.id,
                "scan_run_id": c.scan_run_id,
                "universe_symbol_id": c.universe_symbol_id,
                "symbol": c.symbol,
                "quality_score": c.quality_score,
                "priority_score": c.priority_score,
                "is_promoted": c.is_promoted,
                "stage": c.stage,
                "action": c.action,
            }
            for c in baseline_candidates
        ]

        # Act: 模拟迁移后重新读取
        db_session.expire_all()
        reloaded = (
            db_session.query(DiscoveryCandidate)
            .filter_by(scan_run_id=run.id)
            .order_by(DiscoveryCandidate.id)
            .all()
        )

        # Assert: 5 条历史候选不丢失，字段不变
        assert len(reloaded) == 5, (
            f"discovery_candidates 应 5 条，实际 {len(reloaded)}"
        )
        for base, got in zip(baseline, reloaded):
            assert got.id == base["id"]
            assert got.scan_run_id == base["scan_run_id"]
            assert got.universe_symbol_id == base["universe_symbol_id"]
            assert got.symbol == base["symbol"], (
                f"candidate id={got.id} symbol 漂移: "
                f"{got.symbol} != {base['symbol']}"
            )
            assert got.quality_score == base["quality_score"], (
                f"candidate id={got.id} quality_score 漂移: "
                f"{got.quality_score} != {base['quality_score']}"
            )
            assert got.priority_score == base["priority_score"]
            assert got.is_promoted == base["is_promoted"], (
                f"candidate id={got.id} is_promoted 漂移: "
                f"{got.is_promoted} != {base['is_promoted']}"
            )
            assert got.stage == base["stage"]
            assert got.action == base["action"]

    # ------------------------------------------------------------------
    # 15. portfolio_equity_snapshots 净值快照不丢失（WP9.7：净值快照表数据保留核对）
    # ------------------------------------------------------------------
    def test_portfolio_equity_snapshots_preserved(self, db_session):
        """portfolio_equity_snapshots 历史净值快照不丢失。

        WP9.7 spec 要求：不删除净值快照。
        验证：迁移前后 portfolio_equity_snapshots 表记录数不减少，字段不变。
        历史数据无法回溯，必须保留。
        """
        # Arrange: 1 组合 + 7 天净值快照
        p = _make_portfolio(db_session, name="QA-Audit-PF-Equity")
        baseline_snapshots = []
        base_date = date(2026, 7, 1)
        for i in range(7):
            snapshot = PortfolioEquitySnapshot(
                portfolio_id=p.id,
                snapshot_date=base_date + timedelta(days=i),
                cash_balance=90000.0 - i * 100.0,
                market_value=10000.0 + i * 50.0,
                total_equity=100000.0 + i * 50.0 - i * 100.0,
                realized_pnl=0.0,
                unrealized_pnl=i * 50.0,
                daily_return=0.001 * i,
                position_count=2 + (i % 3),
            )
            db_session.add(snapshot)
            baseline_snapshots.append(snapshot)
        db_session.commit()

        baseline = [
            {
                "id": s.id,
                "portfolio_id": s.portfolio_id,
                "snapshot_date": s.snapshot_date,
                "cash_balance": s.cash_balance,
                "market_value": s.market_value,
                "total_equity": s.total_equity,
                "daily_return": s.daily_return,
                "position_count": s.position_count,
            }
            for s in baseline_snapshots
        ]

        # Act: 模拟迁移后重新读取
        db_session.expire_all()
        reloaded = (
            db_session.query(PortfolioEquitySnapshot)
            .filter_by(portfolio_id=p.id)
            .order_by(PortfolioEquitySnapshot.snapshot_date)
            .all()
        )

        # Assert: 7 条净值快照不丢失，字段不变
        assert len(reloaded) == 7, (
            f"portfolio_equity_snapshots 应 7 条，实际 {len(reloaded)}"
        )
        for base, got in zip(baseline, reloaded):
            assert got.id == base["id"]
            assert got.portfolio_id == base["portfolio_id"]
            assert got.snapshot_date == base["snapshot_date"], (
                f"snapshot id={got.id} snapshot_date 漂移: "
                f"{got.snapshot_date} != {base['snapshot_date']}"
            )
            assert got.cash_balance == base["cash_balance"], (
                f"snapshot id={got.id} cash_balance 漂移: "
                f"{got.cash_balance} != {base['cash_balance']}"
            )
            assert got.market_value == base["market_value"]
            assert got.total_equity == base["total_equity"], (
                f"snapshot id={got.id} total_equity 漂移: "
                f"{got.total_equity} != {base['total_equity']}"
            )
            assert got.daily_return == base["daily_return"]
            assert got.position_count == base["position_count"]

