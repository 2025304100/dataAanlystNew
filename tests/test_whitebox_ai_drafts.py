"""白盒测试 - WP-AI.5 第二阶段草稿工具（三步确认流程）。

覆盖 6 种草稿工具的三步流程（draft / preview / execute）：
1. draft_indicator - 指标草稿
2. draft_filter - 筛选草稿
3. draft_alert - 告警草稿
4. draft_note - 备注草稿
5. draft_review - 复盘草稿
6. draft_order - 订单草稿

关键约束验证：
- 第一步 draft_xxx 返回 requires_confirmation=True，不写业务表
- 第二步 preview_xxx 返回 {is_valid, errors, changes}，不写 DB
- 第三步 execute_xxx 未确认时返回 error="not_confirmed"，确认后调用业务 API
- 模拟订单草稿必须重新经过现金/手数/T+1/涨跌停/数据健康/组合风控校验
- 自动交易永远不由对话触发（disclaimer 必须出现）
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect as sa_inspect, text

from app.models.ai_session import (
    ACTION_DRAFT_ALERT,
    ACTION_DRAFT_FILTER,
    ACTION_DRAFT_INDICATOR,
    ACTION_DRAFT_NOTE,
    ACTION_DRAFT_ORDER,
    ACTION_DRAFT_REVIEW,
)
from app.services.ai.context_pack import ContextPack, build_context_pack
from app.services.ai.drafts import DRAFT_REGISTRY
from app.services.ai.drafts.alert import draft_alert, execute_alert, preview_alert
from app.services.ai.drafts.filter import draft_filter, execute_filter, preview_filter
from app.services.ai.drafts.indicator import (
    draft_indicator,
    execute_indicator,
    preview_indicator,
)
from app.services.ai.drafts.note import draft_note, execute_note, preview_note
from app.services.ai.drafts.order import draft_order, execute_order, preview_order
from app.services.ai.drafts.review import draft_review, execute_review, preview_review
from app.services.ai_session_service import (
    add_action_audit,
    add_message,
    confirm_action,
    create_session,
)


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_symbol(db_session, **kwargs):
    from app.models.symbol import Symbol

    defaults = {
        "symbol": "600010",
        "name": "测试股票",
        "asset_type": "stock",
        "market": "cn",
        "board": "main",
        "is_st": 0,
        "is_active": 1,
    }
    defaults.update(kwargs)
    sym = Symbol(**defaults)
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_daily_bar(db_session, symbol_id, *, trade_date=None, close=10.5):
    from app.models.daily_bar import DailyBar

    if trade_date is None:
        trade_date = _utcnow_naive().date()
    bar = DailyBar(
        symbol_id=symbol_id,
        trade_date=trade_date,
        open=10.0,
        high=11.0,
        low=9.8,
        close=close,
        volume=1000000.0,
        amount=10500000.0,
        turnover_rate=0.01,
    )
    db_session.add(bar)
    db_session.commit()
    return bar


def _make_portfolio(db_session, **kwargs):
    from app.models.portfolio import Portfolio

    defaults = {
        "name": "测试组合",
        "account_type": "simulated",
        "total_capital": 100000.0,
        "investable_ratio": 0.8,
        "cash_reserve_ratio": 0.2,
        "currency": "CNY",
        "is_default": 0,
    }
    defaults.update(kwargs)
    p = Portfolio(**defaults)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _make_context(db_session, **refs) -> ContextPack:
    """构造一个简单的 ContextPack 用于草稿测试。"""
    return build_context_pack(
        db_session,
        user_question="测试问题",
        source_page=refs.pop("source_page", "research"),
        references=refs,
    )


def _make_audit(db_session, action_type: str, suggested_payload: dict):
    """创建一条 AIActionAudit 记录（默认未确认）。"""
    session = create_session(
        db_session,
        title="草稿测试会话",
        source_page="research",
    )
    message = add_message(
        db_session,
        session_id=session.id,
        role="assistant",
        content=f"建议执行 {action_type}",
    )
    audit = add_action_audit(
        db_session,
        message_id=message.id,
        action_type=action_type,
        suggested_payload=suggested_payload,
    )
    return audit


def _row_count(db_session, table_name: str) -> int:
    try:
        return int(
            db_session.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0
        )
    except Exception:
        return -1


# ----------------------------------------------------------------------------
# 1. test_draft_indicator - 指标草稿
# ----------------------------------------------------------------------------


def test_draft_indicator(db_session):
    """【WP-AI.5】draft_indicator 三步流程：建议→预览→确认后执行。"""
    ctx = _make_context(db_session)
    suggestion = {
        "name": "AI建议指标_测试",
        "key": "ai_test_indicator",
        "formula": "close > ma(close, 20)",
        "value_type": "boolean",
        "scope_json": '["backtest", "discovery"]',
    }

    # 第一步：草稿
    before = _row_count(db_session, "custom_indicators")
    result = draft_indicator(db_session, ctx, suggestion)
    after = _row_count(db_session, "custom_indicators")

    assert result["draft_type"] == "draft_indicator"
    assert result["requires_confirmation"] is True
    assert result["validation_status"] == "valid"
    assert result["suggested_payload"]["name"] == "AI建议指标_测试"
    assert result["preview"]["is_valid"] is True
    assert len(result["preview"]["changes"]) >= 1
    # 草稿阶段不写业务表
    assert after == before, "draft_indicator 不应写入 custom_indicators"


# ----------------------------------------------------------------------------
# 2. test_draft_filter - 筛选草稿
# ----------------------------------------------------------------------------


def test_draft_filter(db_session):
    """【WP-AI.5】draft_filter 三步流程。"""
    ctx = _make_context(db_session)
    suggestion = {
        "name": "AI建议筛选_测试",
        "scope_type": "universe",
        "markets": ["cn"],
        "boards": ["main"],
        "filters_json": "[]",
        "sort_mode": "priority_desc",
    }

    before = _row_count(db_session, "scan_presets")
    result = draft_filter(db_session, ctx, suggestion)
    after = _row_count(db_session, "scan_presets")

    assert result["draft_type"] == "draft_filter"
    assert result["requires_confirmation"] is True
    assert result["validation_status"] == "valid"
    assert result["suggested_payload"]["name"] == "AI建议筛选_测试"
    assert after == before, "draft_filter 不应写入 scan_presets"


# ----------------------------------------------------------------------------
# 3. test_draft_alert - 告警草稿
# ----------------------------------------------------------------------------


def test_draft_alert(db_session):
    """【WP-AI.5】draft_alert 三步流程。"""
    ctx = _make_context(db_session)
    suggestion = {
        "name": "AI建议告警_测试",
        "alert_type": "price_alert",
        "severity": "warn",
        "config_json": '{"threshold": 15.0}',
        "cooldown_minutes": 60,
    }

    before = _row_count(db_session, "alert_rules")
    result = draft_alert(db_session, ctx, suggestion)
    after = _row_count(db_session, "alert_rules")

    assert result["draft_type"] == "draft_alert"
    assert result["requires_confirmation"] is True
    assert result["validation_status"] == "valid"
    assert result["suggested_payload"]["alert_type"] == "price_alert"
    assert after == before, "draft_alert 不应写入 alert_rules"


# ----------------------------------------------------------------------------
# 4. test_draft_note - 备注草稿
# ----------------------------------------------------------------------------


def test_draft_note(db_session):
    """【WP-AI.5】draft_note 三步流程。"""
    from app.models.watchlist import Watchlist, WatchlistItem

    sym = _make_symbol(db_session, symbol="600020")
    wl = Watchlist(name="测试观察池", list_type="default")
    db_session.add(wl)
    db_session.commit()

    item = WatchlistItem(
        watchlist_id=wl.id,
        symbol_id=sym.id,
        note="原始备注",
        priority=0,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    ctx = _make_context(db_session, watchlist_item_id=item.id)
    suggestion = {
        "watchlist_item_id": item.id,
        "note": "AI 更新后的备注",
        "tags": ["科技", "龙头"],
        "priority": 5,
    }

    result = draft_note(db_session, ctx, suggestion)

    assert result["draft_type"] == "draft_note"
    assert result["requires_confirmation"] is True
    assert result["validation_status"] == "valid"
    assert result["suggested_payload"]["note"] == "AI 更新后的备注"

    # 验证 preview 显示变更
    changes = result["preview"]["changes"]
    fields_changed = [c["field"] for c in changes]
    assert "watchlist_items.note" in fields_changed
    assert "watchlist_items.tags_json" in fields_changed


# ----------------------------------------------------------------------------
# 5. test_draft_review - 复盘草稿
# ----------------------------------------------------------------------------


def test_draft_review(db_session):
    """【WP-AI.5】draft_review 三步流程。"""
    portfolio = _make_portfolio(db_session)
    ctx = _make_context(db_session, portfolio_id=portfolio.id)
    suggestion = {
        "portfolio_id": portfolio.id,
        "start_date": "2024-01-01",
        "end_date": "2024-06-30",
        "title": "AI 复盘 2024H1",
        "note": "AI 生成的复盘备注",
        "include_attribution_snapshot": True,
    }

    before = _row_count(db_session, "portfolio_reviews")
    result = draft_review(db_session, ctx, suggestion)
    after = _row_count(db_session, "portfolio_reviews")

    assert result["draft_type"] == "draft_review"
    assert result["requires_confirmation"] is True
    assert result["validation_status"] == "valid"
    assert result["suggested_payload"]["title"] == "AI 复盘 2024H1"
    assert after == before, "draft_review 不应写入 portfolio_reviews"


# ----------------------------------------------------------------------------
# 6. test_draft_order - 订单草稿
# ----------------------------------------------------------------------------


def test_draft_order(db_session):
    """【WP-AI.5】draft_order 三步流程 + 自动交易免责声明。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600030")
    _make_daily_bar(db_session, sym.id, close=10.0)

    ctx = _make_context(db_session, portfolio_id=portfolio.id, symbol_id=sym.id)
    suggestion = {
        "portfolio_id": portfolio.id,
        "symbol_id": sym.id,
        "side": "buy",
        "quantity": 200,
        "order_type": "market",
    }

    before_orders = _row_count(db_session, "sim_orders")
    result = draft_order(db_session, ctx, suggestion)
    after_orders = _row_count(db_session, "sim_orders")

    assert result["draft_type"] == "draft_order"
    assert result["requires_confirmation"] is True
    assert result["suggested_payload"]["side"] == "buy"

    # 关键：订单草稿必须包含自动交易免责声明
    preview = result["preview"]
    assert "auto_trade_disclaimer" in preview["extra"]
    assert "自动交易" in preview["extra"]["auto_trade_disclaimer"]

    # 关键：preview 必须包含风控检查详情
    risk_checks = preview["extra"]["risk_checks"]
    check_names = [rc["check"] for rc in risk_checks]
    assert "data_health" in check_names
    assert "lot_size" in check_names
    assert "cash" in check_names  # 买入必有现金校验

    # 草稿阶段不写业务表
    assert after_orders == before_orders, "draft_order 不应写入 sim_orders"


# ----------------------------------------------------------------------------
# 7. test_draft_validation_errors - 校验错误
# ----------------------------------------------------------------------------


def test_draft_validation_errors(db_session):
    """【WP-AI.5】校验失败时 validation_status=invalid 并返回 errors 列表。"""
    ctx = _make_context(db_session)

    # 指标草稿：缺 formula、value_type 非法
    bad_suggestion = {
        "name": "非法指标",
        "key": "bad_key",
        "formula": "",  # 缺 formula
        "value_type": "invalid_type",  # 非法 value_type
    }
    result = draft_indicator(db_session, ctx, bad_suggestion)
    assert result["validation_status"] == "invalid"
    assert len(result["validation_errors"]) > 0
    error_text = "; ".join(result["validation_errors"])
    assert "formula" in error_text or "value_type" in error_text

    # 告警草稿：alert_type 非法
    bad_alert = {
        "name": "非法告警",
        "alert_type": "nonexistent_type",
        "severity": "panic",  # 非法 severity
    }
    result = draft_alert(db_session, ctx, bad_alert)
    assert result["validation_status"] == "invalid"
    assert any("alert_type" in e or "severity" in e for e in result["validation_errors"])

    # 筛选草稿：scope_type 非法
    bad_filter = {
        "name": "非法筛选",
        "scope_type": "nonexistent_scope",
        "sort_mode": "invalid_mode",
    }
    result = draft_filter(db_session, ctx, bad_filter)
    assert result["validation_status"] == "invalid"
    assert any("scope_type" in e or "sort_mode" in e for e in result["validation_errors"])


# ----------------------------------------------------------------------------
# 8. test_preview_shows_changes - 预览变更
# ----------------------------------------------------------------------------


def test_preview_shows_changes(db_session):
    """【WP-AI.5】preview_xxx 返回 changes 列表，每项含 field/old_value/new_value/description。"""
    ctx = _make_context(db_session)

    # 指标 preview
    payload = {
        "name": "预览测试指标",
        "key": "preview_test_indicator",
        "formula": "close > 10",
        "value_type": "boolean",
    }
    preview = preview_indicator(db_session, payload)
    assert preview["is_valid"] is True
    assert len(preview["changes"]) >= 1
    change = preview["changes"][0]
    assert "field" in change
    assert "new_value" in change
    assert "description" in change

    # 告警 preview
    payload = {
        "name": "预览测试告警",
        "alert_type": "price_alert",
        "severity": "warn",
        "config_json": "{}",
    }
    preview = preview_alert(db_session, payload)
    assert preview["is_valid"] is True
    assert len(preview["changes"]) >= 1
    assert preview["changes"][0]["field"] == "alert_rules"

    # 复盘 preview（应包含 extra.portfolio_exists）
    portfolio = _make_portfolio(db_session, name="预览复盘组合")
    payload = {
        "portfolio_id": portfolio.id,
        "start_date": "2024-01-01",
        "end_date": "2024-06-30",
        "title": "预览复盘",
    }
    preview = preview_review(db_session, payload)
    assert preview["is_valid"] is True
    assert preview["extra"]["portfolio_exists"] is True


# ----------------------------------------------------------------------------
# 9. test_execute_requires_confirmation - 未确认不执行
# ----------------------------------------------------------------------------


def test_execute_requires_confirmation(db_session):
    """【WP-AI.5】未确认时 execute_xxx 返回 error="not_confirmed"，不产生任何 DB 变化。"""
    # 为每种草稿创建未确认的 audit
    cases = [
        (ACTION_DRAFT_INDICATOR, {"name": "X1", "key": "x1", "formula": "close>1"}, "custom_indicators", execute_indicator),
        (ACTION_DRAFT_FILTER, {"name": "X筛选", "scope_type": "universe"}, "scan_presets", execute_filter),
        (ACTION_DRAFT_ALERT, {"name": "X告警", "alert_type": "price_alert"}, "alert_rules", execute_alert),
        (ACTION_DRAFT_REVIEW, {"portfolio_id": 99999, "start_date": "2024-01-01", "end_date": "2024-06-30"}, "portfolio_reviews", execute_review),
    ]

    for action_type, payload, table_name, execute_fn in cases:
        audit = _make_audit(db_session, action_type, payload)
        before = _row_count(db_session, table_name)
        result = execute_fn(db_session, audit.id)
        after = _row_count(db_session, table_name)

        assert result["success"] is False, f"{action_type} 应返回 success=False"
        assert result["error"] == "not_confirmed", (
            f"{action_type} 未确认时应返回 error=not_confirmed，实际: {result.get('error')}"
        )
        assert after == before, (
            f"{action_type} 未确认时不应写入 {table_name}: before={before} after={after}"
        )


# ----------------------------------------------------------------------------
# 10. test_execute_after_confirmation - 确认后执行
# ----------------------------------------------------------------------------


def test_execute_after_confirmation(db_session):
    """【WP-AI.5】确认后 execute_xxx 调用业务 API 并产生 DB 变化。"""
    # 指标草稿：确认后写入 custom_indicators
    payload = {
        "name": "确认后执行_指标",
        "key": "confirmed_indicator",
        "formula": "close > ma(close, 5)",
        "value_type": "boolean",
        "scope_json": '["backtest"]',
    }
    audit = _make_audit(db_session, ACTION_DRAFT_INDICATOR, payload)
    confirm_action(db_session, audit.id)

    before = _row_count(db_session, "custom_indicators")
    result = execute_indicator(db_session, audit.id)
    after = _row_count(db_session, "custom_indicators")

    assert result["success"] is True, f"execute_indicator 失败: {result}"
    assert result["indicator_id"] is not None
    assert after == before + 1, f"custom_indicators 行数应 +1: before={before} after={after}"

    # 筛选草稿：确认后写入 scan_presets
    payload = {
        "name": "确认后执行_筛选",
        "scope_type": "universe",
        "markets": ["cn"],
        "filters_json": "[]",
        "sort_mode": "priority_desc",
    }
    audit = _make_audit(db_session, ACTION_DRAFT_FILTER, payload)
    confirm_action(db_session, audit.id)

    before = _row_count(db_session, "scan_presets")
    result = execute_filter(db_session, audit.id)
    after = _row_count(db_session, "scan_presets")

    assert result["success"] is True, f"execute_filter 失败: {result}"
    assert result["preset_id"] is not None
    assert after == before + 1

    # 告警草稿：确认后写入 alert_rules
    payload = {
        "name": "确认后执行_告警",
        "alert_type": "price_alert",
        "severity": "warn",
        "config_json": '{"threshold": 10}',
        "cooldown_minutes": 30,
    }
    audit = _make_audit(db_session, ACTION_DRAFT_ALERT, payload)
    confirm_action(db_session, audit.id)

    before = _row_count(db_session, "alert_rules")
    result = execute_alert(db_session, audit.id)
    after = _row_count(db_session, "alert_rules")

    assert result["success"] is True, f"execute_alert 失败: {result}"
    assert result["rule_id"] is not None
    assert after == before + 1


# ----------------------------------------------------------------------------
# 11. test_order_draft_revalidates - 订单草稿重新校验
# ----------------------------------------------------------------------------


def test_order_draft_revalidates(db_session):
    """【WP-AI.5】订单草稿在 preview 阶段做完整风控校验，execute 阶段业务 API 再次校验。

    覆盖：
    - 数据健康校验（无行情数据时失败）
    - 手数校验（数量不足一手时失败）
    - 现金校验（现金不足时失败）
    - 不存在的组合/标的
    """
    ctx = _make_context(db_session)

    # ── 场景 1：不存在的组合 ──
    suggestion = {
        "portfolio_id": 99999,
        "symbol_id": 99999,
        "side": "buy",
        "quantity": 100,
    }
    result = draft_order(db_session, ctx, suggestion)
    assert result["validation_status"] == "invalid"
    assert any("组合" in e or "标的" in e for e in result["validation_errors"])

    # ── 场景 2：标的无行情数据（数据健康失败） ──
    portfolio = _make_portfolio(db_session, name="订单风控测试组合")
    sym = _make_symbol(db_session, symbol="600999")
    # 不创建任何 daily_bar

    suggestion = {
        "portfolio_id": portfolio.id,
        "symbol_id": sym.id,
        "side": "buy",
        "quantity": 100,
    }
    result = draft_order(db_session, ctx, suggestion)
    assert result["validation_status"] == "invalid"
    risk_checks = result["preview"]["extra"]["risk_checks"]
    data_health_check = next(
        (rc for rc in risk_checks if rc["check"] == "data_health"), None
    )
    assert data_health_check is not None
    assert data_health_check["passed"] is False

    # ── 场景 3：手数不足（A 股 100 股为 1 手，下 50 股） ──
    _make_daily_bar(db_session, sym.id, close=10.0)
    suggestion = {
        "portfolio_id": portfolio.id,
        "symbol_id": sym.id,
        "side": "buy",
        "quantity": 50,  # 不足一手
    }
    result = draft_order(db_session, ctx, suggestion)
    assert result["validation_status"] == "invalid"
    risk_checks = result["preview"]["extra"]["risk_checks"]
    lot_size_check = next(
        (rc for rc in risk_checks if rc["check"] == "lot_size"), None
    )
    assert lot_size_check is not None
    assert lot_size_check["passed"] is False

    # ── 场景 4：现金不足（组合无现金种子，下单金额远超总资金） ──
    sym2 = _make_symbol(db_session, symbol="600998")
    _make_daily_bar(db_session, sym2.id, close=10.0)

    # 给 portfolio 注入少量现金
    from app.models.sim_account import CashLedger
    ledger = CashLedger(
        portfolio_id=portfolio.id,
        entry_type="deposit",
        amount=1000.0,  # 只有 1000 现金
        balance_after=1000.0,
        ref_type="portfolio",
        ref_id=portfolio.id,
        note="测试种子资金",
    )
    db_session.add(ledger)
    db_session.commit()

    suggestion = {
        "portfolio_id": portfolio.id,
        "symbol_id": sym2.id,
        "side": "buy",
        "quantity": 10000,  # 10000 股 × 10 元 = 100000 元，远超 1000 现金
    }
    result = draft_order(db_session, ctx, suggestion)
    assert result["validation_status"] == "invalid"
    risk_checks = result["preview"]["extra"]["risk_checks"]
    cash_check = next(
        (rc for rc in risk_checks if rc["check"] == "cash"), None
    )
    assert cash_check is not None
    assert cash_check["passed"] is False
    assert cash_check["reason"] == "insufficient_cash"


# ----------------------------------------------------------------------------
# 12. test_draft_registry_complete - 6 种草稿注册完整
# ----------------------------------------------------------------------------


def test_draft_registry_complete():
    """【WP-AI.5】DRAFT_REGISTRY 包含 6 种草稿，每项为 (draft_fn, preview_fn, execute_fn) 三元组。"""
    expected_drafts = {
        "draft_indicator",
        "draft_filter",
        "draft_alert",
        "draft_note",
        "draft_review",
        "draft_order",
    }
    assert set(DRAFT_REGISTRY.keys()) == expected_drafts
    for name, triple in DRAFT_REGISTRY.items():
        assert isinstance(triple, tuple) and len(triple) == 3, (
            f"{name} 必须是三元组 (draft_fn, preview_fn, execute_fn)"
        )
        for fn in triple:
            assert callable(fn), f"{name} 中存在不可调用项"


# ----------------------------------------------------------------------------
# 13. test_execute_after_confirmation_order - 订单确认后执行成功
# ----------------------------------------------------------------------------


def test_execute_after_confirmation_order(db_session):
    """【WP-AI.5】订单草稿确认后调用 place_sim_order 并产生成交。"""
    portfolio = _make_portfolio(db_session, name="订单确认执行组合")
    sym = _make_symbol(db_session, symbol="600888")
    _make_daily_bar(db_session, sym.id, close=10.0)

    # 注入足够现金
    from app.models.sim_account import CashLedger
    ledger = CashLedger(
        portfolio_id=portfolio.id,
        entry_type="deposit",
        amount=100000.0,
        balance_after=100000.0,
        ref_type="portfolio",
        ref_id=portfolio.id,
        note="测试种子资金",
    )
    db_session.add(ledger)
    db_session.commit()

    payload = {
        "portfolio_id": portfolio.id,
        "symbol_id": sym.id,
        "side": "buy",
        "quantity": 200,
        "order_type": "market",
        "note": "AI 起草订单（用户确认后执行）",
    }
    audit = _make_audit(db_session, ACTION_DRAFT_ORDER, payload)
    confirm_action(db_session, audit.id)

    before_orders = _row_count(db_session, "sim_orders")
    result = execute_order(db_session, audit.id)
    after_orders = _row_count(db_session, "sim_orders")

    assert result["success"] is True, f"execute_order 失败: {result}"
    assert result["order_id"] is not None
    assert result["trade_id"] is not None
    assert result["filled_quantity"] >= 100  # A 股至少一手
    assert after_orders == before_orders + 1
    # 自动交易免责声明必须出现在最终结果中
    assert "auto_trade_disclaimer" in result
    assert "自动交易" in result["auto_trade_disclaimer"] or "模拟订单" in result["auto_trade_disclaimer"]


# ----------------------------------------------------------------------------
# 14. test_execute_order_rejected_by_business_api - 业务 API 拒绝订单
# ----------------------------------------------------------------------------


def test_execute_order_rejected_by_business_api(db_session):
    """【WP-AI.5】即使 audit 已确认，place_sim_order 仍会再次校验并拒绝违规订单。

    构造场景：现金不足，execute_order 调用 place_sim_order 时业务 API 拒绝。
    """
    portfolio = _make_portfolio(db_session, name="订单拒绝测试组合")
    sym = _make_symbol(db_session, symbol="600777")
    _make_daily_bar(db_session, sym.id, close=10.0)

    # 注入少量现金（不够买 1000 股 × 10 元 = 10000 元）
    from app.models.sim_account import CashLedger
    ledger = CashLedger(
        portfolio_id=portfolio.id,
        entry_type="deposit",
        amount=500.0,
        balance_after=500.0,
        ref_type="portfolio",
        ref_id=portfolio.id,
        note="少量测试资金",
    )
    db_session.add(ledger)
    db_session.commit()

    payload = {
        "portfolio_id": portfolio.id,
        "symbol_id": sym.id,
        "side": "buy",
        "quantity": 1000,
        "order_type": "market",
    }
    audit = _make_audit(db_session, ACTION_DRAFT_ORDER, payload)
    confirm_action(db_session, audit.id)

    before = _row_count(db_session, "sim_orders")
    result = execute_order(db_session, audit.id)
    after = _row_count(db_session, "sim_orders")

    # 业务 API 应拒绝（现金不足）
    assert result["success"] is False
    assert result["error"] == "order_rejected"
    # 关键：拒绝时不写入 sim_orders
    assert after == before, "订单被拒绝时不应写入 sim_orders"


# ----------------------------------------------------------------------------
# 15. test_draft_does_not_write_any_business_table - 草稿阶段不写任何业务表
# ----------------------------------------------------------------------------


def test_draft_does_not_write_any_business_table(db_session):
    """【WP-AI.5】调用所有 6 种草稿的第一步（draft_xxx）后，任何业务表行数都不变。"""
    # 准备必要数据
    portfolio = _make_portfolio(db_session, name="草稿不变测试组合")
    sym = _make_symbol(db_session, symbol="600666")
    _make_daily_bar(db_session, sym.id, close=10.0)

    from app.models.watchlist import Watchlist, WatchlistItem
    wl = Watchlist(name="草稿不变观察池", list_type="default")
    db_session.add(wl)
    db_session.commit()
    item = WatchlistItem(watchlist_id=wl.id, symbol_id=sym.id)
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    ctx = _make_context(
        db_session,
        portfolio_id=portfolio.id,
        symbol_id=sym.id,
        watchlist_item_id=item.id,
    )

    suggestions = {
        "draft_indicator": {
            "name": "不变测试_指标",
            "key": "no_write_indicator",
            "formula": "close > 1",
        },
        "draft_filter": {
            "name": "不变测试_筛选",
            "scope_type": "universe",
        },
        "draft_alert": {
            "name": "不变测试_告警",
            "alert_type": "price_alert",
        },
        "draft_note": {
            "watchlist_item_id": item.id,
            "note": "不变测试备注",
        },
        "draft_review": {
            "portfolio_id": portfolio.id,
            "start_date": "2024-01-01",
            "end_date": "2024-06-30",
        },
        "draft_order": {
            "portfolio_id": portfolio.id,
            "symbol_id": sym.id,
            "side": "buy",
            "quantity": 100,
        },
    }

    # 收集所有业务表行数（排除 ai_* 表）
    engine = db_session.bind
    inspector = sa_inspect(engine)
    business_tables = [
        t for t in inspector.get_table_names()
        if not t.startswith("ai_") and not t.startswith("sqlite_")
    ]
    before_counts = {t: _row_count(db_session, t) for t in business_tables}

    # 调用所有 6 种草稿
    for draft_name, suggestion in suggestions.items():
        draft_fn, _, _ = DRAFT_REGISTRY[draft_name]
        draft_fn(db_session, ctx, suggestion)

    # 验证所有业务表行数未变化
    for tname, before_cnt in before_counts.items():
        after_cnt = _row_count(db_session, tname)
        assert after_cnt == before_cnt, (
            f"调用 draft_xxx 后业务表 {tname} 行数变化: {before_cnt} -> {after_cnt}"
        )
