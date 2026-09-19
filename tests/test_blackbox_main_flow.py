"""黑盒主链路验证（Final.1）。

验证完整链路可走通：
扫描 → 候选来源与数据日期 → 观察池 → 修改标签/原因 → 组合成员（非持仓）
→ 单股回测 → 待确认订单 → 模拟成交 → 现金/持仓/净值/归因 → 复盘

使用 SQLite 内存库（conftest.db_session fixture），AAA 模式，测试独立可运行。
不修改被测代码，仅通过服务层接口验证链路完整性。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import (
    EXECUTION_MANUAL,
    SOURCE_OBSERVATION,
    STATUS_ACTIVE,
    PortfolioMember,
)
from app.models.review import Review
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.sim_account import SimOrder, SimTrade
from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem
# 注意：attribution 与 portfolio_performance 存在循环导入，
# 必须先导入 portfolio_performance（其 __all__ 已 re-export get_attribution_report），
# 否则直接导入 attribution 会触发 ImportError: cannot import name 'attribute_by_member'
from app.services.portfolio_performance import (  # noqa: E402
    compute_portfolio_performance,
    get_attribution_report,
)
from app.services.backtest import run_backtest
from app.services.observations import (
    ORIGIN_SCAN_RESULT,
    STATUS_WATCHING,
    idempotent_add_observation,
    update_observation,
)
from app.services.portfolio_members import create_member, has_position
from app.services.scans import run_scan
from app.services.sim_accounts import (
    build_sim_account_summary,
    cash_balance,
    ensure_sim_account_seed,
    place_sim_order,
)

pytestmark = pytest.mark.blackbox


# ============================================================================
# 测试辅助
# ============================================================================


def _make_portfolio(
    db_session,
    name="QA-Blackbox-Main",
    account_type="simulated",
    total_capital=100000.0,
    auto_trade_enabled=True,
) -> Portfolio:
    """造一个组合并初始化 CashLedger。"""
    p = Portfolio(
        name=name,
        account_type=account_type,
        total_capital=total_capital,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        is_default=0,
        auto_trade_enabled=int(auto_trade_enabled),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    if account_type == "simulated":
        ensure_sim_account_seed(db_session, p)
        db_session.commit()
    return p


def _make_symbol(
    db_session,
    symbol="600010",
    name="测试标的",
    market="sh",
    asset_type="stock",
) -> Symbol:
    sym = Symbol(symbol=symbol, name=name, asset_type=asset_type, market=market)
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_daily_bar(
    db_session,
    symbol_id: int,
    trade_date: date,
    close: float = 10.0,
) -> DailyBar:
    bar = DailyBar(
        symbol_id=symbol_id,
        trade_date=trade_date,
        open=close,
        high=close * 1.02,
        low=close * 0.98,
        close=close,
        volume=100000.0,
    )
    db_session.add(bar)
    db_session.commit()
    return bar


def _make_score(
    db_session,
    symbol_id: int,
    *,
    action: str = "open",
    stage: str = "start",
    trade_date: date = date(2026, 7, 17),
    quality_score: float = 70.0,
    timing_score: float = 65.0,
    priority_score: float = 75.0,
) -> Score:
    score = Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=quality_score,
        quality_grade="B",
        timing_score=timing_score,
        stage=stage,
        action=action,
        priority_score=priority_score,
    )
    db_session.add(score)
    db_session.commit()
    db_session.refresh(score)
    return score


def _make_watchlist(db_session, name="QA-Watchlist") -> Watchlist:
    wl = Watchlist(name=name, list_type="observation")
    db_session.add(wl)
    db_session.commit()
    db_session.refresh(wl)
    return wl


# ============================================================================
# 黑盒主链路测试
# ============================================================================


class TestBlackboxMainFlow:
    """Final.1：验证完整黑盒主链路可走通。"""

    def test_scan_produces_candidates_with_source_and_data_date(self, db_session):
        """步骤 1-2：运行扫描 → 查看候选来源与数据日期。

        验证：ScanRun 完成后产生 ScanResult，包含来源（scan_run_id）和数据日期（created_at）。
        """
        # Arrange
        sym = _make_symbol(db_session, symbol="600100", name="扫描测试标的")
        _make_score(
            db_session,
            sym.id,
            action="open",
            stage="start",
            trade_date=date(2026, 7, 17),
        )
        scope_snapshot = {"asset_types": ["stock"], "markets": ["sh"]}

        # Act
        scan_run = run_scan(
            db_session,
            scope_snapshot=scope_snapshot,
            filters_snapshot=None,
            portfolio_id=None,
            portfolio_rule_id=None,
            run_name="QA-Blackbox-Scan",
            preset_id=None,
        )
        db_session.commit()

        # Assert
        assert scan_run.status == "done"
        assert scan_run.run_name == "QA-Blackbox-Scan"
        assert scan_run.started_at is not None
        assert scan_run.finished_at is not None

        # 查看候选来源与数据日期
        results = (
            db_session.query(ScanResult)
            .filter_by(scan_run_id=scan_run.id)
            .all()
        )
        assert len(results) > 0

        executable_results = [r for r in results if r.result_type == "executable"]
        assert len(executable_results) >= 1

        candidate = executable_results[0]
        # 候选来源：scan_run_id 指向 ScanRun
        assert candidate.scan_run_id == scan_run.id
        # 数据日期：created_at 存在
        assert candidate.created_at is not None
        assert candidate.symbol_id == sym.id
        assert candidate.action == "open"
        assert candidate.rank_no >= 1

    def test_observation_add_and_update_tags_reason(self, db_session):
        """步骤 3-4：加入观察池 → 修改观察标签/原因。

        验证：幂等加入观察池后，可更新标签和原因，变更持久化。
        """
        # Arrange
        p = _make_portfolio(db_session, name="QA-Obs-Port")
        sym = _make_symbol(db_session, symbol="600200", name="观察测试")
        _make_score(db_session, sym.id, action="open", stage="start")
        wl = _make_watchlist(db_session, name="QA-Obs-WL")

        # Act 1: 加入观察池
        item = idempotent_add_observation(
            db_session,
            watchlist_id=wl.id,
            symbol_id=sym.id,
            origin_type=ORIGIN_SCAN_RESULT,
            origin_id=12345,
            reason={"source": "scan", "priority": "high"},
            score_snapshot={"quality": 70.0, "timing": 65.0},
            priority=5,
            tags=["科技", "龙头"],
            target_portfolio_id=p.id,
            note="QA test observation",
        )

        # Assert 1: 观察项已创建
        assert item.id is not None
        assert item.symbol_id == sym.id
        assert item.status == STATUS_WATCHING
        assert item.origin_type == ORIGIN_SCAN_RESULT
        assert item.target_portfolio_id == p.id

        # Act 2: 修改观察标签和原因
        updated = update_observation(
            db_session,
            watchlist_item_id=item.id,
            tags=["科技", "龙头", "突破"],
            reason={"source": "scan", "priority": "high", "updated": True},
            priority=8,
            note="Updated note",
        )
        db_session.commit()

        # Assert 2: 变更已持久化
        assert updated is not None
        assert updated.priority == 8
        tags = json.loads(updated.tags_json)
        assert "突破" in tags
        reason = json.loads(updated.reason_json)
        assert reason.get("updated") is True
        assert updated.note == "Updated note"

    def test_member_created_without_position(self, db_session):
        """步骤 5-6：加入指定组合 → 确认只是成员而非持仓。

        验证：创建组合成员后，has_position 返回 (False, 0.0)，成员不等于持仓。
        """
        # Arrange
        p = _make_portfolio(db_session, name="QA-Member-Port")
        sym = _make_symbol(db_session, symbol="600300", name="成员测试")

        # Act
        member = create_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            status=STATUS_ACTIVE,
            execution_mode=EXECUTION_MANUAL,
            source_type=SOURCE_OBSERVATION,
            source_id=999,
            note="From observation pool",
        )

        # Assert: 成员已创建
        assert member.id is not None
        assert member.portfolio_id == p.id
        assert member.symbol_id == sym.id
        assert member.status == STATUS_ACTIVE
        assert member.effective_to is None  # 当前有效

        # 关键验证：只是成员而非持仓
        has_pos, qty = has_position(
            db_session, portfolio_id=p.id, symbol_id=sym.id
        )
        assert has_pos is False
        assert qty == 0.0

        # 确认无 Position 记录
        positions = (
            db_session.query(Position)
            .filter_by(portfolio_id=p.id, symbol_id=sym.id)
            .all()
        )
        assert len(positions) == 0

    def test_single_symbol_backtest_completes(self, db_session):
        """步骤 7：运行单股回测。

        验证：对单标的运行回测，BacktestRun 正常完成。
        """
        # Arrange
        p = _make_portfolio(
            db_session, name="QA-Bt-Port", total_capital=100000.0
        )
        sym = _make_symbol(db_session, symbol="600400", name="回测标的")

        # 造 5 天行情
        for i, d in enumerate(
            [
                date(2026, 1, 5),
                date(2026, 1, 6),
                date(2026, 1, 7),
                date(2026, 1, 8),
                date(2026, 1, 9),
            ]
        ):
            _make_daily_bar(db_session, sym.id, d, close=10.0 + i * 0.5)

        # 买入信号 + 卖出信号
        _make_score(
            db_session,
            sym.id,
            action="open",
            stage="start",
            trade_date=date(2026, 1, 5),
        )
        _make_score(
            db_session,
            sym.id,
            action="exit",
            stage="overheat",
            trade_date=date(2026, 1, 7),
        )

        rule_config = {
            "buy_conditions": {"actions": ["open", "buy_dip"]},
            "sell_conditions": {"score_actions": ["exit", "reduce"]},
            "position_config": {
                "type": "fixed_pct",
                "value": 0.2,
                "max_positions": 10,
            },
            "execution_config": {
                "entry_timing": "signal_close",
                "exit_timing": "signal_close",
            },
        }

        # Act
        run = run_backtest(
            db_session,
            portfolio_id=p.id,
            symbol_ids=[sym.id],
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 9),
            rule_config=rule_config,
            cost_config=None,
            run_name="QA-Blackbox-Backtest",
            score_weight_mode="manual",
        )

        # Assert
        assert run is not None
        assert run.portfolio_id == p.id
        assert run.status == "completed"
        assert run.run_name == "QA-Blackbox-Backtest"

    def test_sim_order_to_trade_cash_position_nav(self, db_session):
        """步骤 8-10：生成订单 → 模拟成交 → 查看现金/持仓/净值。

        验证：下单后产生 SimOrder + SimTrade，现金减少，持仓创建，净值可查。
        """
        # Arrange
        p = _make_portfolio(
            db_session, name="QA-Order-Port", total_capital=100000.0
        )
        sym = _make_symbol(db_session, symbol="600500", name="订单标的")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)

        initial_cash = cash_balance(db_session, p.id)
        assert initial_cash == 100000.0

        # Act: 买入 1000 股 @ 10.0
        order, trade = place_sim_order(
            db_session,
            portfolio=p,
            symbol=sym,
            side="buy",
            quantity=1000,
            price=10.0,
            order_type="limit",
            note="QA blackbox buy",
            enforce_rules=False,  # 跳过 T+1/涨跌停（测试环境无前收盘价）
            apply_fees=False,  # 跳过手续费简化断言
        )
        db_session.commit()

        # Assert: 订单与成交
        assert order.side == "buy"
        assert order.quantity == 1000
        assert order.filled_quantity == 1000
        assert order.filled_price == 10.0
        assert order.filled_amount == 10000.0
        assert order.status == "filled"

        assert trade.side == "buy"
        assert trade.quantity == 1000
        assert trade.price == 10.0

        # 现金减少
        new_cash = cash_balance(db_session, p.id)
        assert new_cash == 90000.0  # 100000 - 10000

        # 持仓创建
        has_pos, qty = has_position(
            db_session, portfolio_id=p.id, symbol_id=sym.id
        )
        assert has_pos is True
        assert qty == 1000

        # 净值可查
        summary = build_sim_account_summary(db_session, p)
        assert summary["cash_balance"] == 90000.0
        assert summary["market_value"] == 10000.0  # 1000 * 10.0
        assert summary["total_equity"] == 100000.0  # 90000 + 10000
        assert summary["position_count"] == 1

    def test_attribution_report_and_review_snapshot(self, db_session):
        """步骤 11：查看归因 → 创建复盘。

        验证：归因报告返回完整结构，复盘记录可创建并保存归因快照。
        """
        # Arrange: 造一笔已平仓交易
        p = _make_portfolio(
            db_session, name="QA-Attr-Port", total_capital=100000.0
        )
        sym = _make_symbol(db_session, symbol="600600", name="归因标的")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)

        # 买入
        place_sim_order(
            db_session,
            portfolio=p,
            symbol=sym,
            side="buy",
            quantity=1000,
            price=10.0,
            order_type="limit",
            enforce_rules=False,
            apply_fees=False,
            note="Buy for attribution",
        )
        db_session.commit()

        # 卖出
        place_sim_order(
            db_session,
            portfolio=p,
            symbol=sym,
            side="sell",
            quantity=1000,
            price=12.0,
            order_type="limit",
            enforce_rules=False,
            apply_fees=False,
            note="Sell for attribution",
        )
        db_session.commit()

        # Act 1: 查看归因
        report = get_attribution_report(
            db_session,
            portfolio_id=p.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )

        # Assert 1: 归因报告结构完整
        assert report["portfolio_id"] == p.id
        assert "by_member" in report
        assert "by_execution_mode" in report
        assert "by_source" in report
        assert "by_rule_signal" in report
        assert "backtest_vs_sim" in report
        assert "cost_impact" in report
        assert "summary" in report

        # Act 2: 创建复盘
        review = Review(
            portfolio_id=p.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            report_snapshot_json=json.dumps(
                report, ensure_ascii=False, default=str
            ),
            note="QA blackbox review",
            title="Final.1 归因复盘",
        )
        db_session.add(review)
        db_session.commit()
        db_session.refresh(review)

        # Assert 2: 复盘记录已保存
        assert review.id is not None
        assert review.portfolio_id == p.id
        assert review.title == "Final.1 归因复盘"
        snapshot = json.loads(review.report_snapshot_json)
        assert snapshot["portfolio_id"] == p.id

    def test_full_chain_scan_to_review(self, db_session):
        """完整链路端到端：扫描 → 观察 → 成员 → 回测 → 订单 → 成交 → 归因 → 复盘。

        验证：全链路可走通，无断裂。
        """
        # 1. 运行扫描
        sym = _make_symbol(db_session, symbol="600700", name="全链路标的")
        _make_score(
            db_session,
            sym.id,
            action="open",
            stage="start",
            trade_date=date(2026, 7, 17),
        )
        p = _make_portfolio(db_session, name="QA-FullChain-Port")

        scan_run = run_scan(
            db_session,
            scope_snapshot={"asset_types": ["stock"], "markets": ["sh"]},
            filters_snapshot=None,
            portfolio_id=p.id,
            portfolio_rule_id=None,
            run_name="QA-FullChain-Scan",
            preset_id=None,
        )
        db_session.commit()
        assert scan_run.status == "done"

        # 2. 加入观察池
        wl = _make_watchlist(db_session, name="QA-FullChain-WL")
        item = idempotent_add_observation(
            db_session,
            watchlist_id=wl.id,
            symbol_id=sym.id,
            origin_type=ORIGIN_SCAN_RESULT,
            origin_id=scan_run.id,
            reason={"source": "scan", "scan_run_id": scan_run.id},
            score_snapshot={"quality": 70.0},
            priority=5,
            tags=["全链路"],
        )
        assert item.id is not None

        # 3. 加入指定组合（成员）
        member = create_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            source_type=SOURCE_OBSERVATION,
            source_id=item.id,
        )
        assert member.id is not None

        # 4. 确认只是成员而非持仓
        has_pos, _ = has_position(
            db_session, portfolio_id=p.id, symbol_id=sym.id
        )
        assert has_pos is False

        # 5. 运行单股回测
        for i, d in enumerate(
            [
                date(2026, 7, 14),
                date(2026, 7, 15),
                date(2026, 7, 16),
                date(2026, 7, 17),
                date(2026, 7, 18),
            ]
        ):
            _make_daily_bar(db_session, sym.id, d, close=10.0 + i * 0.3)

        bt_run = run_backtest(
            db_session,
            portfolio_id=p.id,
            symbol_ids=[sym.id],
            start_date=date(2026, 7, 14),
            end_date=date(2026, 7, 18),
            rule_config={
                "buy_conditions": {"actions": ["open", "buy_dip"]},
                "sell_conditions": {"score_actions": ["exit", "reduce"]},
                "position_config": {
                    "type": "fixed_pct",
                    "value": 0.2,
                    "max_positions": 10,
                },
                "execution_config": {
                    "entry_timing": "signal_close",
                    "exit_timing": "signal_close",
                },
            },
            run_name="QA-FullChain-BT",
            score_weight_mode="manual",
        )
        assert bt_run.status == "completed"

        # 6. 生成模拟订单 → 手动确认模拟成交
        _make_daily_bar(db_session, sym.id, date(2026, 7, 19), close=11.0)
        order, trade = place_sim_order(
            db_session,
            portfolio=p,
            symbol=sym,
            side="buy",
            quantity=1000,
            price=11.0,
            order_type="limit",
            enforce_rules=False,
            apply_fees=False,
            note="Full chain buy",
        )
        db_session.commit()
        assert order.status == "filled"

        # 7. 查看现金/持仓/净值
        summary = build_sim_account_summary(db_session, p)
        assert summary["position_count"] == 1
        assert summary["cash_balance"] < 100000.0  # 现金减少
        assert summary["total_equity"] > 0

        # 8. 查看归因
        report = get_attribution_report(
            db_session,
            portfolio_id=p.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        assert "summary" in report

        # 9. 创建复盘
        review = Review(
            portfolio_id=p.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            report_snapshot_json=json.dumps(
                report, ensure_ascii=False, default=str
            ),
            title="Final.1 全链路复盘",
            note="End-to-end chain verified",
        )
        db_session.add(review)
        db_session.commit()
        db_session.refresh(review)
        assert review.id is not None
