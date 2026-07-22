"""白盒测试 - WP6.5 安全门禁。

守护 fail-closed 数据健康检查、卖出风控告警、任务取消传播、订单归因验证：
1. check_data_health 数据新鲜/过期/缺失各分支
2. check_sell_risk 数据缺失允许卖出 + 生成高优先级告警
3. 任务取消状态机（check_task_cancelled / cancel_task / reset_task_cancel）
4. record_portfolio_processed/skipped
5. get_task_error_summary 三种 status
6. verify_order_attribution 完整/缺失字段
7. 集成 _check_data_health 调用 WP6.5（候选 rejection_code="BLOCKED"）
8. 任务取消传播到 execute_member_source

测试用 SQLite 内存库（通过 conftest.db_session fixture）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    PortfolioMember,
    STATUS_ACTIVE,
)
from app.models.score import Score
from app.models.sim_account import SimOrder
from app.models.symbol import Symbol
from app.services.auto_trade_member_source import (
    _check_data_health,
    execute_member_source,
    get_buy_candidates,
)
from app.services.auto_trade_safety import (
    DataHealthResult,
    KLINE_FRESHNESS_HOURS,
    RULE_FRESHNESS_HOURS,
    SCORE_FRESHNESS_HOURS,
    cancel_task,
    check_data_health,
    check_sell_risk,
    check_task_cancelled,
    get_task_cancel_state,
    get_task_error_summary,
    record_portfolio_processed,
    record_portfolio_skipped,
    reset_task_cancel,
    verify_order_attribution,
)
from app.services.sim_accounts import ensure_sim_account_seed

pytestmark = pytest.mark.whitebox


# ============================================================================
# 测试辅助
# ============================================================================


def _make_portfolio(
    db_session,
    name: str = "QA-Safety",
    *,
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
        auto_trade_enabled=1,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    ensure_sim_account_seed(db_session, p)
    db_session.commit()
    return p


def _make_symbol(
    db_session,
    symbol: str = "800001",
) -> Symbol:
    sym = Symbol(symbol=symbol, name=f"测试-{symbol}", asset_type="stock", market="sh")
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_score(
    db_session,
    symbol_id: int,
    *,
    action: str = "open",
    trade_date: date = date(2026, 7, 17),
) -> Score:
    score = Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=70.0,
        quality_grade="B",
        timing_score=65.0,
        stage="start",
        action=action,
        priority_score=75.0,
    )
    db_session.add(score)
    db_session.commit()
    db_session.refresh(score)
    return score


def _make_daily_bar(
    db_session,
    symbol_id: int,
    *,
    trade_date: date = date(2026, 7, 17),
    close: float = 10.0,
):
    """写入一条日线行情（created_at 默认为 now，确保数据新鲜）。"""
    from app.models.daily_bar import DailyBar

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


def _make_member(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    execution_mode: str = EXECUTION_AUTO,
    entry_rule_version_id: int | None = None,
) -> PortfolioMember:
    member = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        status=STATUS_ACTIVE,
        execution_mode=execution_mode,
        entry_rule_version_id=entry_rule_version_id,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ============================================================================
# 1. check_data_health 数据新鲜
# ============================================================================


class TestCheckDataHealthFresh:
    """守护数据新鲜时 check_data_health 返回 healthy=True。"""

    def test_data_fresh_returns_healthy(self, db_session):
        """【WP6.5】K 线/评分均为最新时间 → healthy=True。"""
        sym = _make_symbol(db_session, symbol="800001")

        with patch(
            "app.services.auto_trade_safety._get_latest_kline_time",
            return_value=_now_naive(),
        ), patch(
            "app.services.auto_trade_safety._get_latest_score_time",
            return_value=_now_naive(),
        ):
            result = check_data_health(db_session, symbol_id=sym.id)

        assert result.healthy is True
        assert result.reason == ""
        assert result.kline_latest_at is not None
        assert result.score_latest_at is not None

    def test_data_fresh_with_rule_version(self, db_session):
        """【WP6.5】指定 rule_version_id 且版本新鲜 → healthy=True。"""
        sym = _make_symbol(db_session, symbol="800002")

        with patch(
            "app.services.auto_trade_safety._get_latest_kline_time",
            return_value=_now_naive(),
        ), patch(
            "app.services.auto_trade_safety._get_latest_score_time",
            return_value=_now_naive(),
        ), patch(
            "app.services.auto_trade_safety._get_rule_version_time",
            return_value=_now_naive(),
        ):
            result = check_data_health(
                db_session, symbol_id=sym.id, rule_version_id=1001
            )

        assert result.healthy is True
        assert result.rule_version_id == 1001
        assert result.rule_version_at is not None


# ============================================================================
# 2. check_data_health K线过期
# ============================================================================


class TestCheckDataHealthKlineExpired:
    """守护 K 线过期时 fail-closed。"""

    def test_kline_expired_returns_unhealthy(self, db_session):
        """【WP6.5】K 线时间 = 25 小时前 → healthy=False, reason 含"K线数据过期"。"""
        sym = _make_symbol(db_session, symbol="800010")
        expired = _now_naive() - timedelta(hours=KLINE_FRESHNESS_HOURS + 1)

        with patch(
            "app.services.auto_trade_safety._get_latest_kline_time",
            return_value=expired,
        ):
            result = check_data_health(db_session, symbol_id=sym.id)

        assert result.healthy is False
        assert "K线数据过期" in result.reason
        assert result.kline_latest_at == expired


# ============================================================================
# 3. check_data_health K线缺失
# ============================================================================


class TestCheckDataHealthKlineMissing:
    """守护 K 线缺失时 fail-closed。"""

    def test_kline_missing_returns_unhealthy(self, db_session):
        """【WP6.5】K 线时间 = None → healthy=False, reason 含"K线数据缺失"。"""
        sym = _make_symbol(db_session, symbol="800020")

        with patch(
            "app.services.auto_trade_safety._get_latest_kline_time",
            return_value=None,
        ):
            result = check_data_health(db_session, symbol_id=sym.id)

        assert result.healthy is False
        assert "K线数据缺失" in result.reason
        assert result.kline_latest_at is None


# ============================================================================
# 4. check_data_health 评分过期
# ============================================================================


class TestCheckDataHealthScoreExpired:
    """守护评分过期时 fail-closed。"""

    def test_score_expired_returns_unhealthy(self, db_session):
        """【WP6.5】评分时间 = 49 小时前 → healthy=False, reason 含"评分数据过期"。"""
        sym = _make_symbol(db_session, symbol="800030")
        score_expired = _now_naive() - timedelta(hours=SCORE_FRESHNESS_HOURS + 1)

        with patch(
            "app.services.auto_trade_safety._get_latest_kline_time",
            return_value=_now_naive(),
        ), patch(
            "app.services.auto_trade_safety._get_latest_score_time",
            return_value=score_expired,
        ):
            result = check_data_health(db_session, symbol_id=sym.id)

        assert result.healthy is False
        assert "评分数据过期" in result.reason
        assert result.score_latest_at == score_expired


# ============================================================================
# 5. check_data_health 规则版本过期
# ============================================================================


class TestCheckDataHealthRuleExpired:
    """守护规则版本过期时 fail-closed。"""

    def test_rule_version_expired_returns_unhealthy(self, db_session):
        """【WP6.5】规则版本时间 = 8 天前 → healthy=False, reason 含"规则版本过期"。"""
        sym = _make_symbol(db_session, symbol="800040")
        rule_expired = _now_naive() - timedelta(
            hours=RULE_FRESHNESS_HOURS + 24
        )  # 8 天前 + 1 天

        with patch(
            "app.services.auto_trade_safety._get_latest_kline_time",
            return_value=_now_naive(),
        ), patch(
            "app.services.auto_trade_safety._get_latest_score_time",
            return_value=_now_naive(),
        ), patch(
            "app.services.auto_trade_safety._get_rule_version_time",
            return_value=rule_expired,
        ):
            result = check_data_health(
                db_session, symbol_id=sym.id, rule_version_id=1
            )

        assert result.healthy is False
        assert "规则版本过期" in result.reason
        assert result.rule_version_id == 1
        assert result.rule_version_at == rule_expired


# ============================================================================
# 6. check_sell_risk 数据缺失允许卖出
# ============================================================================


class TestCheckSellRiskAllowsSell:
    """守护卖出风控不静默跳过：数据缺失时允许卖出（止损保护）。"""

    def test_data_missing_allows_sell(self, db_session):
        """【WP6.5】数据缺失时 check_sell_risk 返回 (True, "数据缺失但卖出允许")。"""
        sym = _make_symbol(db_session, symbol="800050")

        with patch(
            "app.services.auto_trade_safety._get_latest_kline_time",
            return_value=None,  # K 线缺失
        ):
            allow, reason = check_sell_risk(
                db_session,
                symbol_id=sym.id,
                portfolio_id=999,
            )

        assert allow is True
        assert "数据缺失但卖出允许" in reason


# ============================================================================
# 7. check_sell_risk 数据缺失生成告警
# ============================================================================


class TestCheckSellRiskEmitsAlert:
    """守护卖出风控数据缺失时调用 _emit_sell_data_missing_alert。"""

    def test_data_missing_emits_alert(self, db_session):
        """【WP6.5】数据缺失时 check_sell_risk 调用 _emit_sell_data_missing_alert。"""
        sym = _make_symbol(db_session, symbol="800060")

        with patch(
            "app.services.auto_trade_safety._get_latest_kline_time",
            return_value=None,  # 触发数据缺失
        ), patch(
            "app.services.auto_trade_safety._emit_sell_data_missing_alert"
        ) as mock_alert:
            check_sell_risk(
                db_session,
                symbol_id=sym.id,
                portfolio_id=888,
            )

        # 告警被调用
        assert mock_alert.called
        # 验证告警参数
        call_kwargs = mock_alert.call_args.kwargs
        assert call_kwargs["portfolio_id"] == 888
        assert call_kwargs["symbol_id"] == sym.id
        assert "K线数据缺失" in call_kwargs["reason"]


# ============================================================================
# 8. check_task_cancelled 默认 False
# ============================================================================


class TestCheckTaskCancelledDefault:
    """守护任务取消状态默认 False。"""

    def test_default_not_cancelled(self):
        """【WP6.5】reset_task_cancel 后 check_task_cancelled 返回 False。"""
        reset_task_cancel()
        assert check_task_cancelled() is False


# ============================================================================
# 9. cancel_task
# ============================================================================


class TestCancelTask:
    """守护 cancel_task 设置取消标志。"""

    def test_cancel_task_sets_flag(self):
        """【WP6.5】cancel_task("手动取消") → check_task_cancelled 返回 True。"""
        reset_task_cancel()
        cancel_task("手动取消")
        assert check_task_cancelled() is True

        state = get_task_cancel_state()
        assert state is not None
        assert state.cancelled is True
        assert state.cancel_reason == "手动取消"
        assert state.cancelled_at is not None


# ============================================================================
# 10. reset_task_cancel
# ============================================================================


class TestResetTaskCancel:
    """守护 reset_task_cancel 清除取消状态。"""

    def test_reset_clears_cancelled(self):
        """【WP6.5】cancel_task → reset_task_cancel → check_task_cancelled False。"""
        reset_task_cancel()
        cancel_task("测试取消")
        assert check_task_cancelled() is True

        reset_task_cancel()
        assert check_task_cancelled() is False
        assert get_task_cancel_state() is None


# ============================================================================
# 11. record_portfolio_processed/skipped
# ============================================================================


class TestRecordPortfolioState:
    """守护 record_portfolio_processed/skipped 记录到取消状态。"""

    def test_record_processed_and_skipped(self):
        """【WP6.5】cancel_task 后记录 processed=[1], skipped=[2]。"""
        reset_task_cancel()
        cancel_task("测试")

        record_portfolio_processed(1)
        record_portfolio_skipped(2)

        state = get_task_cancel_state()
        assert state is not None
        assert state.processed_portfolios == [1]
        assert state.skipped_portfolios == [2]

    def test_record_without_cancel_state_no_op(self):
        """【WP6.5】未取消状态下 record_portfolio_* 不报错（no-op）。"""
        reset_task_cancel()
        # 不应抛异常
        record_portfolio_processed(1)
        record_portfolio_skipped(2)
        assert get_task_cancel_state() is None


# ============================================================================
# 12. get_task_error_summary partial_success
# ============================================================================


class TestGetTaskErrorSummaryPartial:
    """守护 get_task_error_summary 部分成功场景。"""

    def test_partial_success(self):
        """【WP6.5】errors + executed_orders 都非空 → status="partial_success"。"""
        summary = get_task_error_summary(
            errors=[{"error": "fail1"}],
            executed_orders=[{"order_id": 1}],
            skipped_orders=[],
        )
        assert summary["status"] == "partial_success"
        assert summary["total_errors"] == 1
        assert summary["total_executed"] == 1
        assert summary["total_skipped"] == 0
        assert summary["errors"] == [{"error": "fail1"}]


# ============================================================================
# 13. get_task_error_summary 全部成功
# ============================================================================


class TestGetTaskErrorSummarySuccess:
    """守护 get_task_error_summary 全部成功场景。"""

    def test_all_success(self):
        """【WP6.5】errors=[] + executed_orders 非空 → status="success"。"""
        summary = get_task_error_summary(
            errors=[],
            executed_orders=[{"order_id": 1}],
            skipped_orders=[],
        )
        assert summary["status"] == "success"
        assert summary["total_errors"] == 0


# ============================================================================
# 14. get_task_error_summary 全部失败
# ============================================================================


class TestGetTaskErrorSummaryFailed:
    """守护 get_task_error_summary 全部失败场景。"""

    def test_all_failed(self):
        """【WP6.5】errors 非空 + executed_orders=[] → status="failed"。"""
        summary = get_task_error_summary(
            errors=[{"error": "fail1"}],
            executed_orders=[],
            skipped_orders=[],
        )
        assert summary["status"] == "failed"
        assert summary["total_errors"] == 1
        assert summary["total_executed"] == 0


# ============================================================================
# 15. verify_order_attribution 完整
# ============================================================================


class TestVerifyOrderAttributionComplete:
    """守护订单归因完整时 verify_order_attribution 返回 (True, "")。"""

    def test_complete_attribution(self, db_session):
        """【WP6.5】所有归因字段非空 → (True, "")。"""
        p = _make_portfolio(db_session, name="QA-Attr-Complete")
        sym = _make_symbol(db_session, symbol="800100")

        order = SimOrder(
            portfolio_id=p.id,
            symbol_id=sym.id,
            side="buy",
            order_type="market",
            quantity=100,
            submitted_price=10.0,
            status="filled",
            member_id=999,
            source_type="member",
            source_id=999,
            signal_id=1,
            signal_snapshot_json='{"action":"open"}',
            rule_version_id=1,
            execution_mode="auto",
            client_order_key="p:s:sym:1:buy:1",
            decision_snapshot_json='{"action":"open"}',
        )
        db_session.add(order)
        db_session.commit()
        db_session.refresh(order)

        ok, reason = verify_order_attribution(order)
        assert ok is True
        assert reason == ""


# ============================================================================
# 16. verify_order_attribution 缺失字段
# ============================================================================


class TestVerifyOrderAttributionMissing:
    """守护订单归因缺失字段时 verify_order_attribution 返回 (False, reason)。"""

    def test_missing_member_id(self, db_session):
        """【WP6.5】member_id=None（非 scan 来源）→ (False, reason 含"member_id")。"""
        p = _make_portfolio(db_session, name="QA-Attr-Missing")
        sym = _make_symbol(db_session, symbol="800110")

        order = SimOrder(
            portfolio_id=p.id,
            symbol_id=sym.id,
            side="buy",
            order_type="market",
            quantity=100,
            submitted_price=10.0,
            status="filled",
            # 故意不设置 member_id（默认 None）
            source_type="member",  # 非 scan，member_id 必填
            signal_id=1,
            signal_snapshot_json='{"action":"open"}',
            rule_version_id=1,
            execution_mode="auto",
            client_order_key="key-missing-member",
            decision_snapshot_json='{"action":"open"}',
        )
        db_session.add(order)
        db_session.commit()
        db_session.refresh(order)

        ok, reason = verify_order_attribution(order)
        assert ok is False
        assert "member_id" in reason

    def test_scan_source_allows_null_member(self, db_session):
        """【WP6.5】source_type="scan" 时 member_id 可为空。"""
        p = _make_portfolio(db_session, name="QA-Attr-Scan")
        sym = _make_symbol(db_session, symbol="800111")

        order = SimOrder(
            portfolio_id=p.id,
            symbol_id=sym.id,
            side="buy",
            order_type="market",
            quantity=100,
            submitted_price=10.0,
            status="filled",
            member_id=None,  # scan 来源允许 None
            source_type="scan",
            signal_id=1,
            signal_snapshot_json='{"action":"open"}',
            rule_version_id=1,
            execution_mode="auto",
            client_order_key="key-scan-source",
            decision_snapshot_json='{"action":"open"}',
        )
        db_session.add(order)
        db_session.commit()
        db_session.refresh(order)

        ok, reason = verify_order_attribution(order)
        assert ok is True
        assert reason == ""

    def test_missing_multiple_fields(self, db_session):
        """【WP6.5】多个字段缺失 → reason 列出所有缺失字段。"""
        p = _make_portfolio(db_session, name="QA-Attr-MissingMulti")
        sym = _make_symbol(db_session, symbol="800112")

        order = SimOrder(
            portfolio_id=p.id,
            symbol_id=sym.id,
            side="buy",
            order_type="market",
            quantity=100,
            submitted_price=10.0,
            status="filled",
            # 全部归因字段都未设置（默认 None）
        )
        db_session.add(order)
        db_session.commit()
        db_session.refresh(order)

        ok, reason = verify_order_attribution(order)
        assert ok is False
        # 至少包含这些字段
        for field in ["member_id", "source_type", "signal_id",
                      "rule_version_id", "execution_mode",
                      "client_order_key", "decision_snapshot_json"]:
            assert field in reason


# ============================================================================
# 17. 集成 _check_data_health 调用 WP6.5
# ============================================================================


class TestCheckDataHealthIntegration:
    """守护 _check_data_health 接入 WP6.5 安全门禁。"""

    def test_check_data_health_calls_safety_module(self, db_session):
        """【WP6.5】_check_data_health 调用 auto_trade_safety.check_data_health。"""
        sym = _make_symbol(db_session, symbol="800200")

        # mock safety 模块的 check_data_health 验证调用
        with patch(
            "app.services.auto_trade_safety.check_data_health",
            return_value=DataHealthResult(
                healthy=False,
                reason="集成测试阻断",
                kline_latest_at=_now_naive(),
            ),
        ) as mock_check:
            ok, reason = _check_data_health(
                db_session,
                sym.id,
                rule_version_id=42,
            )

        assert ok is False
        assert reason == "集成测试阻断"
        # 验证调用参数
        mock_check.assert_called_once()
        call_kwargs = mock_check.call_args.kwargs
        assert call_kwargs["symbol_id"] == sym.id
        assert call_kwargs["rule_version_id"] == 42

    def test_buy_candidate_blocked_when_data_expired(self, db_session):
        """【WP6.5】K 线过期 → get_buy_candidates 返回的候选 rejection_code="BLOCKED"。"""
        p = _make_portfolio(db_session, name="QA-Integration-Blocked")
        sym = _make_symbol(db_session, symbol="800210")
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )

        # mock K 线缺失，触发 fail-closed
        with patch(
            "app.services.auto_trade_safety._get_latest_kline_time",
            return_value=None,
        ):
            candidates = get_buy_candidates(db_session, portfolio_id=p.id)

        # 应返回 1 个候选，且 rejection_code="BLOCKED"
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.rejection_code == "BLOCKED"
        assert cand.data_health_ok is False
        assert "K线数据缺失" in cand.rejection_detail


# ============================================================================
# 18. 任务取消传播
# ============================================================================


class TestTaskCancelPropagation:
    """守护任务取消传播到 execute_member_source。"""

    def test_cancel_skips_portfolio(self, db_session):
        """【WP6.5】cancel_task 后 execute_member_source 不创建 SimOrder 并跳过组合。"""
        p = _make_portfolio(db_session, name="QA-Cancel-Propagate")
        sym = _make_symbol(db_session, symbol="800300")
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )

        # 重置取消状态，然后取消任务
        reset_task_cancel()
        cancel_task("手动取消测试")

        before = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()
        result = execute_member_source(
            db_session, portfolio_id=p.id, dry_run=False
        )
        after = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()

        # 不创建 SimOrder
        assert after == before
        assert after == 0
        # 返回结果标记为取消跳过
        assert result["skipped_due_to_cancel"] is True
        assert len(result["executed_orders"]) == 0

        # 验证组合被记录为跳过
        state = get_task_cancel_state()
        assert state is not None
        assert p.id in state.skipped_portfolios
        assert p.id not in state.processed_portfolios

    def test_no_cancel_executes_normally(self, db_session):
        """【WP6.5】未取消时 execute_member_source 正常执行（创建 SimOrder）。"""
        # 清理状态
        reset_task_cancel()

        p = _make_portfolio(db_session, name="QA-NoCancel-Normal")
        sym = _make_symbol(db_session, symbol="800310")
        _make_daily_bar(db_session, sym.id, close=10.0)  # 确保数据新鲜
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )

        before = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()
        result = execute_member_source(
            db_session, portfolio_id=p.id, dry_run=False
        )
        after = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()

        # 正常执行
        assert after == before + 1
        assert result["skipped_due_to_cancel"] is False
        assert len(result["executed_orders"]) == 1

        # 组合记录为已处理
        state = get_task_cancel_state()
        # 注意：未取消时 _task_cancel_state 可能为 None（无任务上下文）
        # 此处只验证不会误标记为 skipped
        if state is not None:
            assert p.id not in state.skipped_portfolios
