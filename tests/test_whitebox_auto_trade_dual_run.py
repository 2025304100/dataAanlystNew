"""白盒测试 - WP6.4 双跑切换。

守护 auto_trade_dual_run 的关键行为：
1. is_member_source_enabled 全局/组合级开关（WP9.5默认 true / 全局显式 / 白名单 / 黑名单）
2. TradeSet 数据类 to_dict / from_dict 往返
3. diff_trade_sets 各种差异原因（无差异 / member_missing / member_paused /
   signal_diff / data_expired / risk_blocked）
4. run_dual_trade 主入口（旧/新来源路由 / 差异捕获 / save_diff 开关）
5. rollback_to_old_source 回退（全局 / 组合级）
6. can_switch_to_member_source 切换检查

测试用 SQLite 内存库（通过 conftest.db_session fixture），环境变量用 monkeypatch。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.portfolio import Portfolio
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    EXECUTION_CONFIRM,
    EXECUTION_MANUAL,
    PortfolioMember,
    STATUS_ACTIVE,
    STATUS_PAUSED,
)
from app.models.symbol import Symbol
from app.services.auto_trade_dual_run import (
    ENV_FLAG,
    TradeSet,
    can_switch_to_member_source,
    diff_trade_sets,
    is_member_source_enabled,
    rollback_to_old_source,
    run_dual_trade,
)
from app.services.sim_accounts import ensure_sim_account_seed

pytestmark = pytest.mark.whitebox


# ============================================================================
# 测试辅助
# ============================================================================


def _make_portfolio(
    db_session,
    name: str = "QA-DualRun",
    *,
    auto_trade_enabled: bool = True,
) -> Portfolio:
    """造一个非默认模拟组合并初始化 CashLedger。"""
    p = Portfolio(
        name=name,
        account_type="simulated",
        total_capital=100000.0,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        is_default=0,
        auto_trade_enabled=int(auto_trade_enabled),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    ensure_sim_account_seed(db_session, p)
    db_session.commit()
    return p


def _make_symbol(
    db_session,
    symbol: str = "600010",
    name: str = "测试标的",
) -> Symbol:
    sym = Symbol(symbol=symbol, name=name, asset_type="stock", market="sh")
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_member(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    status: str = STATUS_ACTIVE,
    execution_mode: str = EXECUTION_AUTO,
) -> PortfolioMember:
    member = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        status=status,
        execution_mode=execution_mode,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


# ============================================================================
# 1-4. is_member_source_enabled 开关
# ============================================================================


class TestIsMemberSourceEnabled:
    """守护全局/组合级开关逻辑。"""

    def test_default_true(self, monkeypatch):
        """【WP9.5】不设置环境变量 → 回退到 settings 默认值 True（成员来源为默认）。"""
        monkeypatch.delenv(ENV_FLAG, raising=False)
        assert is_member_source_enabled() is True

    def test_global_true(self, monkeypatch):
        """【WP6.4】AUTO_TRADE_MEMBER_SOURCE_ENABLED=true → 返回 True。"""
        monkeypatch.setenv(ENV_FLAG, "true")
        assert is_member_source_enabled() is True

    def test_portfolio_whitelist_overrides_global_false(self, monkeypatch):
        """【WP6.4】全局 false + 白名单 → 白名单内组合启用，其他组合不启用。"""
        monkeypatch.setenv(ENV_FLAG, "false")
        monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_PORTFOLIOS", "1,3")
        assert is_member_source_enabled(portfolio_id=1) is True
        assert is_member_source_enabled(portfolio_id=3) is True
        assert is_member_source_enabled(portfolio_id=2) is False

    def test_portfolio_blacklist_overrides_global_true(self, monkeypatch):
        """【WP6.4】全局 true + 黑名单 → 黑名单内组合不启用，其他组合启用。"""
        monkeypatch.setenv(ENV_FLAG, "true")
        monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_EXCLUDED_PORTFOLIOS", "2")
        assert is_member_source_enabled(portfolio_id=2) is False
        assert is_member_source_enabled(portfolio_id=1) is True


# ============================================================================
# 5. TradeSet 数据类
# ============================================================================


class TestTradeSet:
    """守护 TradeSet 数据类。"""

    def test_to_dict_and_from_dict_roundtrip(self):
        """【WP6.4】TradeSet to_dict / from_dict 往返保持数据一致。"""
        original = TradeSet(
            buys=[{"symbol_id": 1, "action": "open"}],
            sells=[{"symbol_id": 2, "action": "exit"}],
            rejected=[{"symbol_id": 3, "rejection_code": "BLOCKED"}],
        )
        d = original.to_dict()
        restored = TradeSet.from_dict(d)
        assert restored.buys == original.buys
        assert restored.sells == original.sells
        assert restored.rejected == original.rejected

    def test_default_empty_lists(self):
        """【WP6.4】TradeSet 默认空列表。"""
        ts = TradeSet()
        assert ts.buys == []
        assert ts.sells == []
        assert ts.rejected == []

    def test_from_dict_handles_missing_keys(self):
        """【WP6.4】from_dict 对缺失键容错。"""
        ts = TradeSet.from_dict({})
        assert ts.buys == []
        assert ts.sells == []
        assert ts.rejected == []


# ============================================================================
# 6-11. diff_trade_sets 差异分析
# ============================================================================


class TestDiffTradeSets:
    """守护新旧交易集合差异分析逻辑。"""

    def test_no_diff_when_sets_equal(self, db_session):
        """【WP6.4】old_set == new_set → 无差异。"""
        p = _make_portfolio(db_session, name="QA-Diff-Equal")
        old_set = TradeSet(buys=[{"symbol_id": 1, "action": "open"}])
        new_set = TradeSet(buys=[{"symbol_id": 1, "action": "open"}])
        diffs = diff_trade_sets(old_set, new_set, db_session, portfolio_id=p.id)
        assert diffs == []

    def test_member_missing(self, db_session):
        """【WP6.4】旧有买入，新无（无成员）→ reason=member_missing。"""
        p = _make_portfolio(db_session, name="QA-Diff-Missing")
        sym = _make_symbol(db_session, symbol="600001")
        # 不创建 PortfolioMember，制造成员缺失
        old_set = TradeSet(buys=[{"symbol_id": sym.id, "action": "open"}])
        new_set = TradeSet(buys=[])
        diffs = diff_trade_sets(old_set, new_set, db_session, portfolio_id=p.id)
        assert len(diffs) == 1
        assert diffs[0].symbol_id == sym.id
        assert diffs[0].side == "buy"
        assert diffs[0].reason == "member_missing"
        assert diffs[0].old_action == "open"
        assert diffs[0].new_action is None

    def test_member_paused(self, db_session):
        """【WP6.4】旧有买入，新无（成员 paused）→ reason=member_paused。"""
        p = _make_portfolio(db_session, name="QA-Diff-Paused")
        sym = _make_symbol(db_session, symbol="600002")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            status=STATUS_PAUSED,
            execution_mode=EXECUTION_AUTO,
        )
        old_set = TradeSet(buys=[{"symbol_id": sym.id, "action": "open"}])
        new_set = TradeSet(buys=[])
        diffs = diff_trade_sets(old_set, new_set, db_session, portfolio_id=p.id)
        assert len(diffs) == 1
        assert diffs[0].reason == "member_paused"

    def test_member_paused_due_to_non_auto_mode(self, db_session):
        """【WP6.4】旧有买入，新无（成员 manual 模式）→ reason=member_paused。"""
        p = _make_portfolio(db_session, name="QA-Diff-Manual")
        sym = _make_symbol(db_session, symbol="600003")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            status=STATUS_ACTIVE,
            execution_mode=EXECUTION_MANUAL,
        )
        old_set = TradeSet(buys=[{"symbol_id": sym.id, "action": "open"}])
        new_set = TradeSet(buys=[])
        diffs = diff_trade_sets(old_set, new_set, db_session, portfolio_id=p.id)
        assert len(diffs) == 1
        assert diffs[0].reason == "member_paused"

    def test_signal_diff_when_both_present_different_action(self, db_session):
        """【WP6.4】新旧都存在但 action 不同 → reason=signal_diff。"""
        p = _make_portfolio(db_session, name="QA-Diff-Signal")
        sym = _make_symbol(db_session, symbol="600004")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            status=STATUS_ACTIVE,
            execution_mode=EXECUTION_AUTO,
        )
        old_set = TradeSet(buys=[{"symbol_id": sym.id, "action": "open"}])
        new_set = TradeSet(buys=[{"symbol_id": sym.id, "action": "hold"}])
        diffs = diff_trade_sets(old_set, new_set, db_session, portfolio_id=p.id)
        assert len(diffs) == 1
        assert diffs[0].reason == "signal_diff"
        assert diffs[0].old_action == "open"
        assert diffs[0].new_action == "hold"

    def test_signal_diff_when_new_present_old_absent(self, db_session):
        """【WP6.4】新有旧无 → reason=signal_diff。"""
        p = _make_portfolio(db_session, name="QA-Diff-NewOnly")
        old_set = TradeSet(buys=[])
        new_set = TradeSet(buys=[{"symbol_id": 1, "action": "open"}])
        diffs = diff_trade_sets(old_set, new_set, db_session, portfolio_id=p.id)
        assert len(diffs) == 1
        assert diffs[0].reason == "signal_diff"

    def test_data_expired(self, db_session):
        """【WP6.4】新逻辑数据过期拒绝 → reason=data_expired。"""
        p = _make_portfolio(db_session, name="QA-Diff-DataExp")
        old_set = TradeSet(buys=[{"symbol_id": 1, "action": "open"}])
        new_set = TradeSet(
            buys=[
                {
                    "symbol_id": 1,
                    "action": "rejected",
                    "rejection_code": "DATA_EXPIRED",
                }
            ]
        )
        diffs = diff_trade_sets(old_set, new_set, db_session, portfolio_id=p.id)
        assert len(diffs) == 1
        assert diffs[0].reason == "data_expired"

    def test_risk_blocked(self, db_session):
        """【WP6.4】新逻辑风控阻断 → reason=risk_blocked。"""
        p = _make_portfolio(db_session, name="QA-Diff-RiskBlk")
        old_set = TradeSet(buys=[{"symbol_id": 1, "action": "open"}])
        new_set = TradeSet(
            buys=[
                {
                    "symbol_id": 1,
                    "action": "rejected",
                    "rejection_code": "RISK_BLOCKED",
                }
            ]
        )
        diffs = diff_trade_sets(old_set, new_set, db_session, portfolio_id=p.id)
        assert len(diffs) == 1
        assert diffs[0].reason == "risk_blocked"

    def test_sell_side_diff(self, db_session):
        """【WP6.4】卖出侧差异也能捕获。"""
        p = _make_portfolio(db_session, name="QA-Diff-Sell")
        old_set = TradeSet(sells=[{"symbol_id": 1, "action": "exit"}])
        new_set = TradeSet(sells=[])
        diffs = diff_trade_sets(old_set, new_set, db_session, portfolio_id=p.id)
        assert len(diffs) == 1
        assert diffs[0].side == "sell"
        assert diffs[0].old_action == "exit"


# ============================================================================
# 12-15. run_dual_trade 主入口
# ============================================================================


class TestRunDualTrade:
    """守护 run_dual_trade 主入口。"""

    def test_old_source_executed_when_disabled(self, db_session, monkeypatch):
        """新契约：旧来源只做对照，成员来源关闭时真实执行必须阻断。"""
        monkeypatch.setenv(ENV_FLAG, "false")
        p = _make_portfolio(db_session, name="QA-DualRun-Old")
        result = run_dual_trade(db_session, portfolio_id=p.id)
        assert result["executed_source"] == "blocked"
        assert result["member_source_enabled"] is False
        assert result["portfolio_id"] == p.id
        # 应同时返回新旧交易集合快照
        assert "old_trade_set" in result
        assert "new_trade_set" in result
        assert "execution_result" in result

    def test_new_source_executed_when_enabled(self, db_session, monkeypatch):
        """【WP6.4】AUTO_TRADE_MEMBER_SOURCE_ENABLED=true → executed_source=new。"""
        monkeypatch.setenv(ENV_FLAG, "true")
        p = _make_portfolio(db_session, name="QA-DualRun-New")
        result = run_dual_trade(db_session, portfolio_id=p.id)
        assert result["executed_source"] == "new"
        assert result["member_source_enabled"] is True
        assert "execution_result" in result

    def test_captures_diffs_when_save_diff_true(self, db_session, monkeypatch):
        """【WP6.4】save_diff=True → 捕获差异（mock 新旧捕获制造 member_missing）。"""
        monkeypatch.setenv(ENV_FLAG, "false")
        p = _make_portfolio(db_session, name="QA-DualRun-SaveDiff")
        sym = _make_symbol(db_session, symbol="600010")
        # 不创建成员 → member_missing
        old_set = TradeSet(buys=[{"symbol_id": sym.id, "action": "open"}])
        new_set = TradeSet(buys=[])
        with patch(
            "app.services.auto_trade_dual_run.capture_trade_set_from_old_logic",
            return_value=old_set,
        ), patch(
            "app.services.auto_trade_dual_run.capture_trade_set_from_new_logic",
            return_value=new_set,
        ):
            result = run_dual_trade(
                db_session, portfolio_id=p.id, save_diff=True
            )
        assert len(result["diffs"]) > 0
        assert result["diffs"][0]["reason"] == "member_missing"
        assert result["diffs"][0]["symbol_id"] == sym.id

    def test_no_diffs_when_save_diff_false(self, db_session, monkeypatch):
        """【WP6.4】save_diff=False → 不捕获差异。"""
        monkeypatch.setenv(ENV_FLAG, "false")
        p = _make_portfolio(db_session, name="QA-DualRun-NoSave")
        sym = _make_symbol(db_session, symbol="600011")
        old_set = TradeSet(buys=[{"symbol_id": sym.id, "action": "open"}])
        new_set = TradeSet(buys=[])
        with patch(
            "app.services.auto_trade_dual_run.capture_trade_set_from_old_logic",
            return_value=old_set,
        ), patch(
            "app.services.auto_trade_dual_run.capture_trade_set_from_new_logic",
            return_value=new_set,
        ):
            result = run_dual_trade(
                db_session, portfolio_id=p.id, save_diff=False
            )
        assert result["diffs"] == []


# ============================================================================
# 16-17. rollback_to_old_source 回退
# ============================================================================


class TestRollbackToOldSource:
    """守护回退逻辑（开关关闭可立即回退）。"""

    def test_global_rollback_disables_member_source(self, monkeypatch):
        """【WP6.4】全局回退 → is_member_source_enabled 返回 False。"""
        monkeypatch.setenv(ENV_FLAG, "true")
        assert is_member_source_enabled() is True
        rollback_to_old_source()
        assert is_member_source_enabled() is False

    def test_portfolio_level_rollback_only_affects_target(self, monkeypatch):
        """【WP6.4】组合级回退 → 仅该组合回退，其他组合不受影响。"""
        monkeypatch.setenv(ENV_FLAG, "false")
        monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_PORTFOLIOS", "1,3")
        assert is_member_source_enabled(portfolio_id=1) is True
        assert is_member_source_enabled(portfolio_id=3) is True
        rollback_to_old_source(portfolio_id=1)
        assert is_member_source_enabled(portfolio_id=1) is False
        assert is_member_source_enabled(portfolio_id=3) is True

    def test_portfolio_level_rollback_removes_only_entry(self, monkeypatch):
        """【WP6.4】白名单只含单个组合时回退 → 白名单变量被清除。"""
        monkeypatch.setenv(ENV_FLAG, "false")
        monkeypatch.setenv("AUTO_TRADE_MEMBER_SOURCE_PORTFOLIOS", "1")
        rollback_to_old_source(portfolio_id=1)
        assert is_member_source_enabled(portfolio_id=1) is False


# ============================================================================
# 18. can_switch_to_member_source
# ============================================================================


class TestCanSwitchToMemberSource:
    """守护切换检查逻辑。"""

    def test_blocks_without_a_g5_summary(self, db_session):
        """没有真实 G5 汇总不得用旧的占位语义放行。"""
        p = _make_portfolio(db_session, name="QA-Switch-Check")
        can_switch, reason = can_switch_to_member_source(
            db_session, portfolio_id=p.id
        )
        assert can_switch is False
        assert "缺少" in reason

    def test_rejects_unproven_external_g5_summary(self, db_session):
        p = _make_portfolio(db_session, name="QA-Switch-G5")
        can_switch, reason = can_switch_to_member_source(
            db_session,
            portfolio_id=p.id,
            g5_summary={
                "days_replayed": 10,
                "skipped_days": [],
                "total_p0_unexplained": 0,
                "total_p1_hold_noaction_flip": 0,
                "g5_eligible_for_g6": True,
            },
        )
        assert can_switch is False
        assert "来源证明" in reason

    def test_blocks_a_g5_summary_with_a_failed_day(self, db_session):
        p = _make_portfolio(db_session, name="QA-Switch-G5-Failed")
        can_switch, reason = can_switch_to_member_source(
            db_session,
            portfolio_id=p.id,
            g5_summary={
                "days_replayed": 12,
                "skipped_days": ["2026-08-19"],
                "total_p0_unexplained": 0,
                "total_p1_hold_noaction_flip": 0,
                "g5_eligible_for_g6": False,
            },
        )
        assert can_switch is False
        assert "来源证明" in reason
