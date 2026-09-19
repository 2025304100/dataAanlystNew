"""白盒测试 - 标的统一关联状态接口（WP1.5）。

覆盖 app/services/symbol_relationships.py 与 app/api/routes/symbols.py 中的
GET /api/v1/symbols/{symbol_id}/relationships 端点。

测试维度：
1. 空标的（无任何关联）— 所有 has_* = False
2. 只有候选
3. 只有观察项
4. 只有持仓
5. 只有告警
6. 五类关联都有
7. 最新 added_at 的观察项优先返回（WP2 archived 过滤生效前的等价测试）
8. 多持仓取第一条
9. 已平仓持仓不显示
10. 已确认告警不计入 active_alert_events
11. 不存在的 symbol_id 返回 404
12. 子查询失败降级（degraded=True）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.routes.symbols import get_symbol_relationships_endpoint
from app.models.alert import AlertEvent, AlertRule
from app.models.discovery_candidate import DiscoveryCandidate
from app.models.portfolio import Portfolio, Position
from app.models.scan import ScanRun
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services.symbol_relationships import get_symbol_relationships

pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_symbol(db_session, symbol="600000", name="测试-标的") -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=name,
        asset_type="stock",
        market="cn",
        theme="测试",
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_universe_symbol(db_session, symbol="600000") -> UniverseSymbol:
    """基础数据层 UniverseSymbol（DiscoveryCandidate 依赖）。"""
    us = UniverseSymbol(
        symbol=symbol,
        name=f"US-{symbol}",
        asset_type="stock",
        market="sh",
        region="cn",
    )
    db_session.add(us)
    db_session.commit()
    db_session.refresh(us)
    return us


def _make_scan_run(db_session, run_name="QA-ScanRun") -> ScanRun:
    sr = ScanRun(
        run_name=run_name,
        scope_snapshot="{}",
        status="completed",
        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.add(sr)
    db_session.commit()
    db_session.refresh(sr)
    return sr


def _make_candidate(
    db_session,
    *,
    symbol_str: str,
    scan_run_id: int,
    universe_symbol_id: int,
    quality_score: float = 75.0,
    timing_score: float = 70.0,
    priority_score: float = 72.0,
    stage: str = "hold",
    action: str = "watch",
    created_at: datetime | None = None,
) -> DiscoveryCandidate:
    dc = DiscoveryCandidate(
        scan_run_id=scan_run_id,
        universe_symbol_id=universe_symbol_id,
        symbol=symbol_str,
        name=f"候选-{symbol_str}",
        asset_type="stock",
        quality_score=quality_score,
        timing_score=timing_score,
        priority_score=priority_score,
        stage=stage,
        action=action,
        is_promoted=0,
        created_at=created_at or datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.add(dc)
    db_session.commit()
    db_session.refresh(dc)
    return dc


def _make_watchlist(db_session, name="QA-Watchlist") -> Watchlist:
    wl = Watchlist(name=name, list_type="custom", description="qa")
    db_session.add(wl)
    db_session.commit()
    db_session.refresh(wl)
    return wl


def _make_watchlist_item(
    db_session,
    *,
    watchlist_id: int,
    symbol_id: int,
    added_at: datetime | None = None,
) -> WatchlistItem:
    item = WatchlistItem(
        watchlist_id=watchlist_id,
        symbol_id=symbol_id,
        note="qa",
        added_at=added_at or datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def _make_portfolio(db_session, name="QA-Portfolio") -> Portfolio:
    pf = Portfolio(
        name=name,
        account_type="cash",
        total_capital=100000.0,
        investable_ratio=0.8,
        cash_reserve_ratio=0.2,
    )
    db_session.add(pf)
    db_session.commit()
    db_session.refresh(pf)
    return pf


def _make_position(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    quantity: float = 100.0,
    avg_cost: float = 10.0,
    latest_price: float = 11.0,
    market_value: float = 1100.0,
    opened_at: datetime | None = None,
) -> Position:
    pos = Position(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        quantity=quantity,
        avg_cost=avg_cost,
        latest_price=latest_price,
        market_value=market_value,
        position_pct=0.0,
        asset_type="stock",
        opened_at=opened_at or datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


def _make_alert_rule(db_session, name="QA-AlertRule") -> AlertRule:
    rule = AlertRule(
        name=name,
        alert_type="score_drop",
        enabled=1,
        severity="warn",
        config_json='{"threshold": 40}',
        cooldown_minutes=60,
    )
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)
    return rule


def _make_alert_event(
    db_session,
    *,
    rule_id: int,
    symbol_id: int,
    acknowledged: int = 0,
    severity: str = "warn",
    created_at: datetime | None = None,
) -> AlertEvent:
    ev = AlertEvent(
        rule_id=rule_id,
        alert_type="score_drop",
        severity=severity,
        title="QA alert",
        message="qa",
        symbol_id=symbol_id,
        acknowledged=acknowledged,
        created_at=created_at or datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.add(ev)
    db_session.commit()
    db_session.refresh(ev)
    return ev


# ----------------------------------------------------------------------------
# 1. 空标的（无任何关联）
# ----------------------------------------------------------------------------


def test_empty_symbol_returns_all_false(db_session):
    """【WP1.5 白盒】空标的：所有 has_* 标志均为 False。"""
    sym = _make_symbol(db_session, symbol="000001")

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.symbol_id == sym.id
    assert result.symbol == "000001"
    assert result.candidate.has_candidate is False
    assert result.observation.has_observation is False
    assert result.portfolio_member.has_portfolio_membership is False
    assert result.position.has_position is False
    assert result.alert.has_active_alert is False
    assert result.degraded is False
    assert result.fetched_at  # 非空


# ----------------------------------------------------------------------------
# 2. 只有候选
# ----------------------------------------------------------------------------


def test_only_candidate(db_session):
    """【WP1.5 白盒】只有候选时：has_candidate=True，其余为 False。"""
    sym = _make_symbol(db_session, symbol="600000")
    us = _make_universe_symbol(db_session, symbol="600000")
    sr = _make_scan_run(db_session)
    _make_candidate(
        db_session,
        symbol_str="600000",
        scan_run_id=sr.id,
        universe_symbol_id=us.id,
        quality_score=82.0,
        timing_score=78.0,
        priority_score=80.0,
        stage="hold",
        action="watch",
    )

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.candidate.has_candidate is True
    assert result.candidate.candidate_id is not None
    assert result.candidate.quality_score == 82.0
    assert result.candidate.timing_score == 78.0
    assert result.candidate.priority_score == 80.0
    assert result.candidate.stage == "hold"
    assert result.candidate.action == "watch"
    assert result.candidate.scan_run_id == sr.id
    assert result.candidate.generated_at  # ISO 8601 字符串
    # 其余状态为空
    assert result.observation.has_observation is False
    assert result.position.has_position is False
    assert result.alert.has_active_alert is False


# ----------------------------------------------------------------------------
# 3. 只有观察项
# ----------------------------------------------------------------------------


def test_only_observation(db_session):
    """【WP1.5 白盒】只有观察项时：has_observation=True。"""
    sym = _make_symbol(db_session, symbol="600001")
    wl = _make_watchlist(db_session, name="QA-WL-Obs")
    _make_watchlist_item(
        db_session,
        watchlist_id=wl.id,
        symbol_id=sym.id,
    )

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.observation.has_observation is True
    assert result.observation.watchlist_id == wl.id
    assert result.observation.watchlist_name == "QA-WL-Obs"
    assert result.observation.watchlist_item_id is not None
    # WP2.1 后 origin_type 字段存在，新建观察项默认 manual
    assert result.observation.origin_type == "manual"
    # 默认 watching
    assert result.observation.status == "watching"
    assert result.observation.added_at  # ISO 8601 字符串
    # 其余为空
    assert result.candidate.has_candidate is False
    assert result.position.has_position is False
    assert result.alert.has_active_alert is False


# ----------------------------------------------------------------------------
# 4. 只有持仓
# ----------------------------------------------------------------------------


def test_only_position(db_session):
    """【WP1.5 白盒】只有持仓时：has_position=True。"""
    sym = _make_symbol(db_session, symbol="600002")
    pf = _make_portfolio(db_session, name="QA-PF-Pos")
    _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        quantity=200.0,
        avg_cost=15.5,
        latest_price=16.0,
        market_value=3200.0,
    )

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.position.has_position is True
    assert result.position.portfolio_id == pf.id
    assert result.position.portfolio_name == "QA-PF-Pos"
    assert result.position.position_id is not None
    assert result.position.quantity == 200.0
    # Position.avg_cost 对外字段名为 cost_price
    assert result.position.cost_price == 15.5
    assert result.position.latest_price == 16.0
    assert result.position.market_value == 3200.0
    assert result.position.opened_at  # ISO 8601 字符串
    # 其余为空
    assert result.candidate.has_candidate is False
    assert result.observation.has_observation is False
    assert result.alert.has_active_alert is False


# ----------------------------------------------------------------------------
# 5. 只有告警
# ----------------------------------------------------------------------------


def test_only_alert(db_session):
    """【WP1.5 白盒】只有未确认告警时：has_active_alert=True, active_alert_events=1。"""
    sym = _make_symbol(db_session, symbol="600003")
    rule = _make_alert_rule(db_session, name="QA-Rule-Alert")
    _make_alert_event(
        db_session,
        rule_id=rule.id,
        symbol_id=sym.id,
        acknowledged=0,
        severity="warn",
    )

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.alert.has_active_alert is True
    assert rule.id in result.alert.alert_rule_ids
    assert result.alert.active_alert_events == 1
    assert result.alert.latest_alert_severity == "warn"
    assert result.alert.latest_alert_at  # ISO 8601 字符串
    # 其余为空
    assert result.candidate.has_candidate is False
    assert result.observation.has_observation is False
    assert result.position.has_position is False


# ----------------------------------------------------------------------------
# 6. 五类关联都有
# ----------------------------------------------------------------------------


def test_all_relationships_present(db_session):
    """【WP1.5 白盒】五类关联都有时：所有 has_* 均为 True。"""
    sym = _make_symbol(db_session, symbol="600004")
    us = _make_universe_symbol(db_session, symbol="600004")
    sr = _make_scan_run(db_session)
    _make_candidate(
        db_session,
        symbol_str="600004",
        scan_run_id=sr.id,
        universe_symbol_id=us.id,
    )
    wl = _make_watchlist(db_session, name="QA-WL-All")
    _make_watchlist_item(db_session, watchlist_id=wl.id, symbol_id=sym.id)
    pf = _make_portfolio(db_session, name="QA-PF-All")
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym.id)
    rule = _make_alert_rule(db_session, name="QA-Rule-All")
    _make_alert_event(db_session, rule_id=rule.id, symbol_id=sym.id, acknowledged=0)

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.candidate.has_candidate is True
    assert result.observation.has_observation is True
    assert result.portfolio_member.has_portfolio_membership is False  # 第一阶段未实现
    assert result.position.has_position is True
    assert result.alert.has_active_alert is True
    assert result.degraded is False


# ----------------------------------------------------------------------------
# 7. 最新 added_at 的观察项优先返回
# ----------------------------------------------------------------------------


def test_latest_observation_returned(db_session):
    """【WP1.5 白盒】多个观察项时返回 added_at 最新的一条。

    注：WP2 后此场景演化为"未归档优先于归档"，此时仅验证最新 added_at 优先。
    WatchlistItem 有 (watchlist_id, symbol_id) 唯一约束，故使用两个 watchlist。
    """
    sym = _make_symbol(db_session, symbol="600005")
    wl_old = _make_watchlist(db_session, name="QA-WL-Old")
    wl_new = _make_watchlist(db_session, name="QA-WL-New")
    base_time = datetime.now(timezone.utc).replace(tzinfo=None)
    old_item = _make_watchlist_item(
        db_session,
        watchlist_id=wl_old.id,
        symbol_id=sym.id,
        added_at=base_time - timedelta(days=2),
    )
    new_item = _make_watchlist_item(
        db_session,
        watchlist_id=wl_new.id,
        symbol_id=sym.id,
        added_at=base_time,
    )
    assert old_item.id != new_item.id

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.observation.has_observation is True
    # 返回 added_at 最新的那条（new_item）
    assert result.observation.watchlist_item_id == new_item.id
    assert result.observation.watchlist_id == wl_new.id


# ----------------------------------------------------------------------------
# 8. 多持仓取第一条
# ----------------------------------------------------------------------------


def test_multiple_positions_returns_first(db_session):
    """【WP1.5 白盒】多组合持仓时返回 id 升序的第一条。"""
    sym = _make_symbol(db_session, symbol="600006")
    pf1 = _make_portfolio(db_session, name="QA-PF-First")
    pf2 = _make_portfolio(db_session, name="QA-PF-Second")
    pos1 = _make_position(
        db_session,
        portfolio_id=pf1.id,
        symbol_id=sym.id,
        quantity=100.0,
        avg_cost=10.0,
    )
    pos2 = _make_position(
        db_session,
        portfolio_id=pf2.id,
        symbol_id=sym.id,
        quantity=200.0,
        avg_cost=20.0,
    )

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.position.has_position is True
    # 按 Position.id asc 排序，返回 id 较小的那条
    expected_id = min(pos1.id, pos2.id)
    assert result.position.position_id == expected_id


# ----------------------------------------------------------------------------
# 9. 已平仓持仓不显示
# ----------------------------------------------------------------------------


def test_closed_position_not_shown(db_session):
    """【WP1.5 白盒】quantity=0 的已平仓持仓不显示。"""
    sym = _make_symbol(db_session, symbol="600007")
    pf = _make_portfolio(db_session, name="QA-PF-Closed")
    _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        quantity=0.0,  # 已平仓
    )

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.position.has_position is False


# ----------------------------------------------------------------------------
# 10. 已确认告警不计入 active_alert_events
# ----------------------------------------------------------------------------


def test_acknowledged_alert_not_counted(db_session):
    """【WP1.5 白盒】acknowledged=1 的告警不计入 active_alert_events。

    只有已确认事件时：has_active_alert=False、active_alert_events=0、
    alert_rule_ids=[]、latest_alert_severity=None。
    """
    sym = _make_symbol(db_session, symbol="600008")
    rule = _make_alert_rule(db_session, name="QA-Rule-Acked")
    _make_alert_event(
        db_session,
        rule_id=rule.id,
        symbol_id=sym.id,
        acknowledged=1,  # 已确认
        severity="warn",
    )

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.alert.has_active_alert is False
    assert result.alert.active_alert_events == 0
    assert result.alert.alert_rule_ids == []
    assert result.alert.latest_alert_severity is None
    assert result.alert.latest_alert_at is None


# ----------------------------------------------------------------------------
# 11. 不存在的 symbol_id 返回 404
# ----------------------------------------------------------------------------


def test_nonexistent_symbol_returns_404(db_session):
    """【WP1.5 白盒】不存在的 symbol_id 返回 404 HTTPException。"""
    with pytest.raises(HTTPException) as exc:
        get_symbol_relationships_endpoint(symbol_id=99999, db=db_session)
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail.lower()


# ----------------------------------------------------------------------------
# 12. 子查询失败降级
# ----------------------------------------------------------------------------


def test_subquery_failure_degrades(db_session, monkeypatch):
    """【WP1.5 白盒】子查询失败时降级（degraded=True）但其他字段仍正常返回。"""
    sym = _make_symbol(db_session, symbol="600009")
    pf = _make_portfolio(db_session, name="QA-PF-Degrade")
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym.id)

    # monkeypatch _query_alert 抛异常
    from app.services import symbol_relationships as svc

    def _boom(db, *, symbol_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "_query_alert", _boom)

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    # 降级标记
    assert result.degraded is True
    assert result.degraded_reason is not None
    assert "alert" in result.degraded_reason
    assert "boom" in result.degraded_reason
    # 其他字段仍正常返回
    assert result.symbol == "600009"
    assert result.position.has_position is True
    assert result.candidate.has_candidate is False
    # alert 字段降级为默认空
    assert result.alert.has_active_alert is False
    assert result.alert.active_alert_events == 0


# ----------------------------------------------------------------------------
# 13. 路由层通过 endpoint 函数验证（与 service 一致）
# ----------------------------------------------------------------------------


def test_endpoint_returns_relationships(db_session):
    """【WP1.5 白盒】路由层 endpoint 直接调用返回 SymbolRelationships。"""
    sym = _make_symbol(db_session, symbol="600010")

    result = get_symbol_relationships_endpoint(symbol_id=sym.id, db=db_session)

    assert result.symbol_id == sym.id
    assert result.symbol == "600010"
    assert result.candidate.has_candidate is False
    assert result.observation.has_observation is False
    assert result.position.has_position is False
    assert result.alert.has_active_alert is False
    assert result.degraded is False


# ----------------------------------------------------------------------------
# 14. 多告警事件计数
# ----------------------------------------------------------------------------


def test_multiple_alert_events_counted(db_session):
    """【WP1.5 白盒】多个未确认告警事件计数正确。"""
    sym = _make_symbol(db_session, symbol="600011")
    rule = _make_alert_rule(db_session, name="QA-Rule-Multi")
    _make_alert_event(
        db_session,
        rule_id=rule.id,
        symbol_id=sym.id,
        acknowledged=0,
        severity="info",
    )
    _make_alert_event(
        db_session,
        rule_id=rule.id,
        symbol_id=sym.id,
        acknowledged=0,
        severity="warn",
    )

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.alert.has_active_alert is True
    assert result.alert.active_alert_events == 2
    assert result.alert.alert_rule_ids == [rule.id]
    # 最新一条 severity=warn（created_at 倒序）
    assert result.alert.latest_alert_severity == "warn"


# ----------------------------------------------------------------------------
# 15. 禁用规则的事件不计入
# ----------------------------------------------------------------------------


def test_disabled_rule_events_not_counted(db_session):
    """【WP1.5 白盒】规则禁用（enabled=0）时事件不计入。"""
    sym = _make_symbol(db_session, symbol="600012")
    rule = AlertRule(
        name="QA-Rule-Disabled",
        alert_type="score_drop",
        enabled=0,  # 禁用
        severity="warn",
    )
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)
    _make_alert_event(
        db_session,
        rule_id=rule.id,
        symbol_id=sym.id,
        acknowledged=0,
    )

    result = get_symbol_relationships(db_session, symbol_id=sym.id)

    assert result.alert.has_active_alert is False
    assert result.alert.active_alert_events == 0
    assert result.alert.alert_rule_ids == []
