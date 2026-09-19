"""白盒测试 - WP-P.5 两阶段过滤。

覆盖：
1. _coarse_filter：SQL 粗筛（stats 字段、min_score、asset_types、stages、actions、
   min_data_credibility、limit=0、无 items）
2. _advanced_filter：向量化指标计算（indicator_plan None、3 个有效指标、
   超过 5 个指标、bar_count < 5、ast.Pow 超限、非法节点）
3. _portfolio_filter：组合约束（portfolio_id None、已持仓去重、行业集中度、
   仓位上限、除以零保护）
4. IndicatorFormulaEvaluator：简单算术、函数调用、ast.Pow 超限、非法节点
5. run_fast_scan 端到端：filter_stats 字段、不发起 HTTP 请求

硬约束验证（参照 project_memory）：
- ast.Pow 计算结果 > 1e100 返回 None（防 OOM）
- bar_count < 5 边界安全（不抛 NameError）
- 除以零保护（不返回 Infinity）
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import *  # noqa: F401,F403 - 确保所有模型被注册到 Base.metadata
from app.models.daily_bar import DailyBar
from app.models.discovery_score_snapshot import (
    DiscoveryScoreSnapshot,
    DiscoveryScoreSnapshotItem,
)
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.services import discovery_fast_scan
from app.services.indicator_ast_sandbox import (
    IndicatorFormulaEvaluator,
    POW_RESULT_LIMIT,
)

pytestmark = pytest.mark.whitebox


# ============================================================================
# 辅助函数
# ============================================================================

def _make_universe_symbol(
    db_session,
    *,
    symbol: str,
    asset_type: str = "stock",
    region: str = "cn",
    market: str = "sz",
    bar_count: int = 10,
    last_synced_at: datetime | None = None,
) -> UniverseSymbol:
    """创建一个 universe_symbol 记录。"""
    us = UniverseSymbol(
        symbol=symbol,
        name=f"测试标的 {symbol}",
        asset_type=asset_type,
        market=market,
        region=region,
        bar_count=bar_count,
        last_synced_at=last_synced_at,
        is_synced=1,
        created_at=datetime(2026, 7, 1, 0, 0, 0),
    )
    db_session.add(us)
    db_session.flush()
    return us


def _make_symbol(
    db_session,
    *,
    symbol: str,
    asset_type: str = "stock",
    market: str = "sz",
    industry: str | None = None,
) -> Symbol:
    """创建一个 Symbol 业务表记录。"""
    sym = Symbol(
        symbol=symbol,
        name=f"测试标的 {symbol}",
        asset_type=asset_type,
        market=market,
        industry=industry,
        is_active=1,
    )
    db_session.add(sym)
    db_session.flush()
    return sym


def _make_snapshot(
    db_session,
    *,
    scope: str = "cn_stock",
    status: str = "ready",
    trade_date: datetime | None = None,
    generated_at: datetime | None = None,
    symbol_count: int | None = None,
) -> DiscoveryScoreSnapshot:
    """创建一个快照记录。"""
    if trade_date is None:
        trade_date = datetime(2026, 7, 19, 0, 0, 0)
    snap = DiscoveryScoreSnapshot(
        scope=scope,
        trade_date=trade_date,
        status=status,
        generated_at=generated_at,
        symbol_count=symbol_count,
    )
    db_session.add(snap)
    db_session.flush()
    return snap


def _make_snapshot_item(
    db_session,
    *,
    snapshot_id: int,
    universe_symbol_id: int,
    symbol_id: int,
    quality_score: float = 70.0,
    timing_score: float = 65.0,
    priority_score: float = 75.0,
    stage: str = "accumulate",
    action: str = "buy",
    data_credibility: float | None = 0.8,
) -> DiscoveryScoreSnapshotItem:
    """创建一个快照 item 记录。"""
    item = DiscoveryScoreSnapshotItem(
        snapshot_id=snapshot_id,
        universe_symbol_id=universe_symbol_id,
        symbol_id=symbol_id,
        quality_score=quality_score,
        timing_score=timing_score,
        priority_score=priority_score,
        stage=stage,
        action=action,
        data_credibility=data_credibility,
    )
    db_session.add(item)
    db_session.flush()
    return item


def _make_daily_bar(
    db_session,
    *,
    symbol_id: int,
    trade_date: date,
    close: float = 10.0,
    open_price: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1000.0,
) -> DailyBar:
    """创建一根 K 线。"""
    bar = DailyBar(
        symbol_id=symbol_id,
        trade_date=trade_date,
        open=open_price if open_price is not None else close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=volume,
        amount=close * volume,
        turnover_rate=1.0,
    )
    db_session.add(bar)
    db_session.flush()
    return bar


def _make_portfolio(
    db_session,
    *,
    name: str = "测试组合",
    total_capital: float = 100_000.0,
    investable_ratio: float = 0.9,
    cash_reserve_ratio: float = 0.1,
) -> Portfolio:
    """创建一个组合。"""
    pf = Portfolio(
        name=name,
        account_type="sim",
        total_capital=total_capital,
        investable_ratio=investable_ratio,
        cash_reserve_ratio=cash_reserve_ratio,
        auto_trade_enabled=0,
    )
    db_session.add(pf)
    db_session.flush()
    return pf


def _make_portfolio_rule(
    db_session,
    *,
    portfolio_id: int,
    max_single_position_pct: float = 10.0,
    max_sector_position_pct: float = 30.0,
    max_stock_position_pct: float = 10.0,
    max_etf_position_pct: float = 15.0,
    max_loss_per_trade_pct: float = 5.0,
    max_open_positions: int = 10,
) -> PortfolioRule:
    """创建一个组合规则。"""
    rule = PortfolioRule(
        portfolio_id=portfolio_id,
        rule_name="测试规则",
        max_single_position_pct=max_single_position_pct,
        max_sector_position_pct=max_sector_position_pct,
        max_stock_position_pct=max_stock_position_pct,
        max_etf_position_pct=max_etf_position_pct,
        max_loss_per_trade_pct=max_loss_per_trade_pct,
        max_open_positions=max_open_positions,
        stage_limits_json="{}",
        is_active=1,
    )
    db_session.add(rule)
    db_session.flush()
    return rule


def _make_position(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    quantity: float = 100.0,
    avg_cost: float = 10.0,
    latest_price: float = 10.0,
    market_value: float | None = None,
    asset_type: str = "stock",
) -> Position:
    """创建一条持仓。"""
    pos = Position(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        quantity=quantity,
        avg_cost=avg_cost,
        latest_price=latest_price,
        market_value=market_value if market_value is not None else quantity * latest_price,
        position_pct=1.0,
        asset_type=asset_type,
    )
    db_session.add(pos)
    db_session.flush()
    return pos


def _setup_10_items_with_scores(db_session, *, snapshot_id: int):
    """创建 10 个 symbol + universe_symbol + snapshot_item，priority_score 50~95。

    返回 (symbols, universe_symbols, items) 三元组。
    """
    symbols = []
    universe_symbols = []
    items = []
    for i in range(10):
        score = 50 + i * 5  # 50, 55, 60, 65, 70, 75, 80, 85, 90, 95
        sym = _make_symbol(
            db_session,
            symbol=f"SYM{i:03d}",
            asset_type="stock" if i % 2 == 0 else "etf",
            industry="金融" if i % 3 == 0 else "消费",
        )
        us = _make_universe_symbol(db_session, symbol=f"SYM{i:03d}")
        item = _make_snapshot_item(
            db_session,
            snapshot_id=snapshot_id,
            universe_symbol_id=us.id,
            symbol_id=sym.id,
            priority_score=float(score),
            quality_score=float(score - 5),
            timing_score=float(score - 10),
            stage="accumulate" if i % 2 == 0 else "breakout",
            action="buy" if i % 2 == 0 else "watch",
            data_credibility=0.5 + i * 0.05,  # 0.50 ~ 0.95
        )
        symbols.append(sym)
        universe_symbols.append(us)
        items.append(item)
    db_session.flush()
    return symbols, universe_symbols, items


# ============================================================================
# 1. _coarse_filter
# ============================================================================

def test_coarse_filter_returns_top_5_with_min_score_70(db_session):
    """min_score=70 + limit=5 → 返回 priority_score >= 70 的 Top 5。"""
    snap = _make_snapshot(db_session)
    _setup_10_items_with_scores(db_session, snapshot_id=snap.id)
    db_session.commit()

    items, stats = discovery_fast_scan._coarse_filter(
        db_session,
        snap,
        min_score=70,
        limit=5,
    )

    # 70, 75, 80, 85, 90, 95 → 6 个 >= 70，但 limit=5 → 返回 5 个
    assert len(items) == 5
    assert all(item.priority_score >= 70 for item in items)
    # 按 priority_score desc 排序
    scores = [item.priority_score for item in items]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 95  # 最高分


def test_coarse_filter_stats_fields_correct(db_session):
    """stats 各字段正确：total / after_min_score / after_asset_types / ..."""
    snap = _make_snapshot(db_session)
    _setup_10_items_with_scores(db_session, snapshot_id=snap.id)
    db_session.commit()

    items, stats = discovery_fast_scan._coarse_filter(
        db_session,
        snap,
        min_score=55,
        asset_types=["stock"],
        stages=["accumulate"],
        actions=["buy"],
        min_data_credibility=0.6,
        limit=300,
    )

    # 检查 stats 必需字段
    expected_keys = {
        "total_in_snapshot",
        "after_min_score",
        "after_asset_types",
        "after_stages",
        "after_actions",
        "after_credibility",
        "coarse_match_count",
    }
    assert set(stats.keys()) == expected_keys
    assert stats["total_in_snapshot"] == 10
    # min_score=55 过滤掉 50 → 9 个
    assert stats["after_min_score"] == 9
    # 各阶段统计应该单调不增
    assert stats["after_asset_types"] <= stats["after_min_score"]
    assert stats["after_stages"] <= stats["after_asset_types"]
    assert stats["after_actions"] <= stats["after_stages"]
    assert stats["after_credibility"] <= stats["after_actions"]
    assert stats["coarse_match_count"] == len(items)


def test_coarse_filter_asset_types_filter(db_session):
    """asset_types 过滤只保留指定资产类型。"""
    snap = _make_snapshot(db_session)
    _setup_10_items_with_scores(db_session, snapshot_id=snap.id)
    db_session.commit()

    items, stats = discovery_fast_scan._coarse_filter(
        db_session,
        snap,
        min_score=50,
        asset_types=["etf"],
        limit=300,
    )
    # _setup_10_items_with_scores 中 i 为奇数的是 etf，i=1,3,5,7,9 → 5 个 ETF
    assert stats["after_asset_types"] == 5
    for item in items:
        sym = db_session.get(Symbol, item.symbol_id)
        assert sym.asset_type == "etf"


def test_coarse_filter_stages_filter(db_session):
    """stages 过滤只保留指定阶段。"""
    snap = _make_snapshot(db_session)
    _setup_10_items_with_scores(db_session, snapshot_id=snap.id)
    db_session.commit()

    items, stats = discovery_fast_scan._coarse_filter(
        db_session,
        snap,
        min_score=50,
        stages=["breakout"],
        limit=300,
    )
    # i 为奇数的是 breakout → 5 个
    assert stats["after_stages"] == 5
    for item in items:
        assert item.stage == "breakout"


def test_coarse_filter_actions_filter(db_session):
    """actions 过滤只保留指定动作。"""
    snap = _make_snapshot(db_session)
    _setup_10_items_with_scores(db_session, snapshot_id=snap.id)
    db_session.commit()

    items, stats = discovery_fast_scan._coarse_filter(
        db_session,
        snap,
        min_score=50,
        actions=["watch"],
        limit=300,
    )
    # i 为奇数的是 watch → 5 个
    assert stats["after_actions"] == 5
    for item in items:
        assert item.action == "watch"


def test_coarse_filter_min_data_credibility(db_session):
    """min_data_credibility 过滤掉可信度不足的标的。"""
    snap = _make_snapshot(db_session)
    _setup_10_items_with_scores(db_session, snapshot_id=snap.id)
    db_session.commit()

    # data_credibility 范围 0.50 ~ 0.95，设阈值 0.75 → 应保留 0.75~0.95 = 5 个
    items, stats = discovery_fast_scan._coarse_filter(
        db_session,
        snap,
        min_score=50,
        min_data_credibility=0.75,
        limit=300,
    )
    assert stats["after_credibility"] == 5
    for item in items:
        assert item.data_credibility >= 0.75


def test_coarse_filter_limit_zero_returns_empty(db_session):
    """limit=0 → 直接返回空列表，但 stats["total_in_snapshot"] 仍正确。"""
    snap = _make_snapshot(db_session)
    _setup_10_items_with_scores(db_session, snapshot_id=snap.id)
    db_session.commit()

    items, stats = discovery_fast_scan._coarse_filter(
        db_session,
        snap,
        min_score=50,
        limit=0,
    )
    assert items == []
    assert stats["total_in_snapshot"] == 10
    assert stats["after_min_score"] == 0
    assert stats["coarse_match_count"] == 0


def test_coarse_filter_no_items_returns_empty(db_session):
    """无 items → 空列表，stats 字段全为 0。"""
    snap = _make_snapshot(db_session)
    db_session.commit()

    items, stats = discovery_fast_scan._coarse_filter(
        db_session,
        snap,
        min_score=50,
        limit=300,
    )
    assert items == []
    assert stats["total_in_snapshot"] == 0
    assert stats["after_min_score"] == 0
    assert stats["coarse_match_count"] == 0


def test_coarse_filter_min_data_credibility_zero_no_filter(db_session):
    """min_data_credibility=0 → 不过滤（保留全部）。"""
    snap = _make_snapshot(db_session)
    _setup_10_items_with_scores(db_session, snapshot_id=snap.id)
    db_session.commit()

    items, stats = discovery_fast_scan._coarse_filter(
        db_session,
        snap,
        min_score=50,
        min_data_credibility=0.0,
        limit=300,
    )
    # min_data_credibility=0 不应过滤任何记录
    assert stats["after_credibility"] == stats["after_actions"]
    assert len(items) == stats["after_credibility"]


# ============================================================================
# 2. _advanced_filter
# ============================================================================

def test_advanced_filter_no_plan_returns_original_items(db_session):
    """indicator_plan=None → 返回原 items，degraded_reason=None。"""
    snap = _make_snapshot(db_session)
    sym = _make_symbol(db_session, symbol="A001")
    us = _make_universe_symbol(db_session, symbol="A001")
    item = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us.id,
        symbol_id=sym.id,
        priority_score=80.0,
    )
    db_session.commit()

    items, stats = discovery_fast_scan._advanced_filter(
        [item],
        indicator_plan=None,
        db=db_session,
        snapshot=snap,
    )
    assert len(items) == 1
    assert items[0].id == item.id
    assert stats["degraded_reason"] is None
    assert stats["indicator_count"] == 0


def test_advanced_filter_three_valid_indicators(db_session):
    """indicator_plan 含 3 个有效指标 → 正确计算并过滤。"""
    snap = _make_snapshot(db_session)
    sym = _make_symbol(db_session, symbol="A002")
    us = _make_universe_symbol(db_session, symbol="A002")
    item = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us.id,
        symbol_id=sym.id,
        priority_score=80.0,
    )
    # 给 symbol 加 10 根 K 线（满足 bar_count >= 5）
    base_date = snap.trade_date.date() if snap.trade_date else date(2026, 7, 19)
    for i in range(10):
        _make_daily_bar(
            db_session,
            symbol_id=sym.id,
            trade_date=base_date - timedelta(days=9 - i),
            close=10.0 + i,
        )
    db_session.commit()

    # 3 个有效指标公式：close >= 5 / close <= 100 / (close + 1) > 0
    indicator_plan = {
        "ind1": {"formula": "close", "min_value": 5.0, "max_value": 100.0},
        "ind2": {"formula": "close + 1", "min_value": 10.0},
        "ind3": {"formula": "abs(close - 19)"},
    }
    items, stats = discovery_fast_scan._advanced_filter(
        [item],
        indicator_plan=indicator_plan,
        db=db_session,
        snapshot=snap,
    )
    # 最后 K 线 close=19，满足所有指标 → 保留
    assert len(items) == 1
    assert stats["indicator_count"] == 3
    assert stats["after_indicators"] == 1
    assert stats["degraded_reason"] is None


def test_advanced_filter_exceeds_five_indicators(db_session):
    """indicator_plan 超过 5 个指标 → degraded_reason="INDICATOR_PLAN_TOO_MANY"。"""
    snap = _make_snapshot(db_session)
    sym = _make_symbol(db_session, symbol="A003")
    us = _make_universe_symbol(db_session, symbol="A003")
    item = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us.id,
        symbol_id=sym.id,
        priority_score=80.0,
    )
    db_session.commit()

    # 6 个指标
    indicator_plan = {
        f"ind_{i}": {"formula": "close"} for i in range(6)
    }
    items, stats = discovery_fast_scan._advanced_filter(
        [item],
        indicator_plan=indicator_plan,
        db=db_session,
        snapshot=snap,
    )
    # 超限降级 → 返回原 items，degraded_reason 标记
    assert len(items) == 1
    assert stats["degraded_reason"] == "INDICATOR_PLAN_TOO_MANY"
    assert stats["indicator_errors"]
    assert stats["indicator_errors"][0]["error_code"] == "INDICATOR_PLAN_TOO_MANY"


def test_advanced_filter_bar_count_below_threshold(db_session):
    """bar_count < 5 → 该标的该指标视为 None，整体通过（不抛 NameError）。

    参照 project_memory 硬约束 #2：bar_count < 5 边界安全。
    """
    snap = _make_snapshot(db_session)
    sym = _make_symbol(db_session, symbol="A004")
    us = _make_universe_symbol(db_session, symbol="A004")
    item = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us.id,
        symbol_id=sym.id,
        priority_score=80.0,
    )
    # 只给 3 根 K 线（< 5）
    base_date = snap.trade_date.date() if snap.trade_date else date(2026, 7, 19)
    for i in range(3):
        _make_daily_bar(
            db_session,
            symbol_id=sym.id,
            trade_date=base_date - timedelta(days=2 - i),
            close=10.0,
        )
    db_session.commit()

    indicator_plan = {
        "ind1": {"formula": "close", "min_value": 5.0, "max_value": 100.0},
    }
    items, stats = discovery_fast_scan._advanced_filter(
        [item],
        indicator_plan=indicator_plan,
        db=db_session,
        snapshot=snap,
    )
    # bar_count < 5 → 该标的安全降级（视为通过，不抛异常）
    assert len(items) == 1
    assert stats["degraded_reason"] is None


def test_advanced_filter_ast_pow_overflow_returns_none(db_session):
    """AST 公式 ast.Pow 超限 → 该指标返回 None（不抛异常）。

    参照 project_memory 硬约束 #1：ast.Pow > 1e100 返回 None（防 OOM）。
    """
    snap = _make_snapshot(db_session)
    sym = _make_symbol(db_session, symbol="A005")
    us = _make_universe_symbol(db_session, symbol="A005")
    item = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us.id,
        symbol_id=sym.id,
        priority_score=80.0,
    )
    # 给一个 base 较大的 close 值，配 ** 200 → 超限
    base_date = snap.trade_date.date() if snap.trade_date else date(2026, 7, 19)
    for i in range(10):
        _make_daily_bar(
            db_session,
            symbol_id=sym.id,
            trade_date=base_date - timedelta(days=9 - i),
            close=10.0,  # 10 ** 200 = 1e200 远大于 1e100 → 应返回 None
        )
    db_session.commit()

    # 公式：close ** 200 → ast.Pow，结果超 1e100 → None
    # min_value=9999 让 None 视为不通过；但因 None 不参与筛选（视为通过），
    # 该标的最终应被保留
    indicator_plan = {
        "ind_overflow": {"formula": "close ** 200", "min_value": 9999.0},
    }
    items, stats = discovery_fast_scan._advanced_filter(
        [item],
        indicator_plan=indicator_plan,
        db=db_session,
        snapshot=snap,
    )
    # ast.Pow 超限不抛异常，单指标 None 不阻塞 → 标的保留
    assert len(items) == 1
    assert stats["degraded_reason"] is None


def test_advanced_filter_invalid_formula_node(db_session):
    """公式含非法操作（import / lambda）→ 该指标构造失败，整体降级但不抛异常。"""
    snap = _make_snapshot(db_session)
    sym = _make_symbol(db_session, symbol="A006")
    us = _make_universe_symbol(db_session, symbol="A006")
    item = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us.id,
        symbol_id=sym.id,
        priority_score=80.0,
    )
    base_date = snap.trade_date.date() if snap.trade_date else date(2026, 7, 19)
    for i in range(10):
        _make_daily_bar(
            db_session,
            symbol_id=sym.id,
            trade_date=base_date - timedelta(days=9 - i),
            close=10.0,
        )
    db_session.commit()

    # import 在 eval mode 下其实不能 parse，但 lambda 可以
    # 这里用 lambda 测试非法节点
    indicator_plan = {
        "bad": {"formula": "lambda x: x"},
    }
    items, stats = discovery_fast_scan._advanced_filter(
        [item],
        indicator_plan=indicator_plan,
        db=db_session,
        snapshot=snap,
    )
    # 全部指标公式非法 → 降级，但 items 保留
    assert stats["degraded_reason"] == "ALL_INDICATORS_INVALID"
    assert stats["indicator_errors"]
    assert stats["indicator_errors"][0]["error_code"] == "INVALID_FORMULA"


def test_advanced_filter_one_invalid_one_valid_indicator(db_session):
    """单指标错误不阻塞整体筛选（一个非法 + 一个合法）。"""
    snap = _make_snapshot(db_session)
    sym = _make_symbol(db_session, symbol="A007")
    us = _make_universe_symbol(db_session, symbol="A007")
    item = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us.id,
        symbol_id=sym.id,
        priority_score=80.0,
    )
    base_date = snap.trade_date.date() if snap.trade_date else date(2026, 7, 19)
    for i in range(10):
        _make_daily_bar(
            db_session,
            symbol_id=sym.id,
            trade_date=base_date - timedelta(days=9 - i),
            close=15.0,
        )
    db_session.commit()

    indicator_plan = {
        "bad": {"formula": "lambda x: x"},  # 非法
        "good": {"formula": "close", "min_value": 10.0, "max_value": 20.0},  # 合法
    }
    items, stats = discovery_fast_scan._advanced_filter(
        [item],
        indicator_plan=indicator_plan,
        db=db_session,
        snapshot=snap,
    )
    # 非法指标被记录到 indicator_errors，合法指标照常评估
    assert stats["indicator_count"] == 2
    assert any(e["indicator_key"] == "bad" for e in stats["indicator_errors"])
    # close=15 满足 10<=x<=20 → 保留
    assert len(items) == 1


# ============================================================================
# 3. _portfolio_filter
# ============================================================================

def test_portfolio_filter_no_portfolio_returns_original(db_session):
    """portfolio_id=None → 直接返回原 items。"""
    snap = _make_snapshot(db_session)
    sym = _make_symbol(db_session, symbol="B001")
    us = _make_universe_symbol(db_session, symbol="B001")
    item = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us.id,
        symbol_id=sym.id,
        priority_score=80.0,
    )
    db_session.commit()

    items, stats = discovery_fast_scan._portfolio_filter(
        [item],
        portfolio_id=None,
        portfolio_rule_id=None,
        db=db_session,
    )
    assert len(items) == 1
    assert stats["applied_rules"] == []
    assert stats["after_existing_position_filter"] == 1


def test_portfolio_filter_existing_position_dedup(db_session):
    """已持仓标的去重：item.symbol_id 在 Position 中存在 → 过滤掉。"""
    snap = _make_snapshot(db_session)
    sym1 = _make_symbol(db_session, symbol="B002")
    sym2 = _make_symbol(db_session, symbol="B003")
    us1 = _make_universe_symbol(db_session, symbol="B002")
    us2 = _make_universe_symbol(db_session, symbol="B003")
    item1 = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us1.id,
        symbol_id=sym1.id,
        priority_score=80.0,
    )
    item2 = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us2.id,
        symbol_id=sym2.id,
        priority_score=75.0,
    )
    # 创建组合 + 持仓 sym1
    pf = _make_portfolio(db_session, name="PF_DEDUP")
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym1.id)
    db_session.commit()

    items, stats = discovery_fast_scan._portfolio_filter(
        [item1, item2],
        portfolio_id=pf.id,
        portfolio_rule_id=None,
        db=db_session,
    )
    # sym1 已持仓 → 被去重；只保留 sym2
    assert len(items) == 1
    assert items[0].symbol_id == sym2.id
    assert stats["after_existing_position_filter"] == 1
    assert "existing_position_dedup" in stats["applied_rules"]


def test_portfolio_filter_industry_concentration(db_session):
    """行业集中度限制：同行业不超过 max_per_industry 个。"""
    snap = _make_snapshot(db_session)
    # 创建 4 个 symbol 都属于 "金融" 行业
    symbols = []
    items = []
    for i in range(4):
        sym = _make_symbol(db_session, symbol=f"B{i:03d}", industry="金融")
        us = _make_universe_symbol(db_session, symbol=f"B{i:03d}")
        item = _make_snapshot_item(
            db_session,
            snapshot_id=snap.id,
            universe_symbol_id=us.id,
            symbol_id=sym.id,
            priority_score=80.0 - i,  # 80, 79, 78, 77
        )
        symbols.append(sym)
        items.append(item)
    pf = _make_portfolio(db_session, name="PF_INDUSTRY")
    # max_sector_position_pct=30 → max_per_industry = round(4 * 30 / 100) = 1（金融最多 1 个）
    rule = _make_portfolio_rule(
        db_session,
        portfolio_id=pf.id,
        max_sector_position_pct=30.0,
        max_stock_position_pct=100.0,
        max_etf_position_pct=100.0,
        max_open_positions=10,
    )
    db_session.commit()

    filtered, stats = discovery_fast_scan._portfolio_filter(
        items,
        portfolio_id=pf.id,
        portfolio_rule_id=rule.id,
        db=db_session,
    )
    # 4 个标的同属"金融"，最多 1 个 → 保留 1 个
    assert len(filtered) == 1
    assert stats["after_industry_concentration"] == 1
    assert "industry_concentration" in stats["applied_rules"]


def test_portfolio_filter_position_limit_max_open_positions(db_session):
    """max_open_positions 限制：达到上限后不再新增。"""
    snap = _make_snapshot(db_session)
    symbols = []
    items = []
    # 创建 5 个未持仓的新标的（绕过已持仓去重）
    for i in range(5):
        sym = _make_symbol(db_session, symbol=f"C{i:03d}", industry=f"行业{i}")
        us = _make_universe_symbol(db_session, symbol=f"C{i:03d}")
        item = _make_snapshot_item(
            db_session,
            snapshot_id=snap.id,
            universe_symbol_id=us.id,
            symbol_id=sym.id,
            priority_score=80.0 - i,
        )
        symbols.append(sym)
        items.append(item)
    pf = _make_portfolio(db_session, name="PF_MAXPOS")
    # 当前已有 2 个持仓，max_open_positions=3 → 只能再加 1 个
    # total_capital=100_000, investable_ratio=0.9, cash_reserve_ratio=0.1
    # → max_total = 100_000 * 0.9 * 0.9 = 81_000
    # max_stock_position_pct=5.0 → estimated_position = 81_000 * 5 / 100 = 4_050
    # 5 个新标的 * 4_050 = 20_250 < 79_000（剩余容量）→ 仓位上限不阻塞
    # max_open_positions=3, 当前 2 持仓 → 只能加 1 个
    pos_sym1 = _make_symbol(db_session, symbol="POS001", industry="持仓行业1")
    pos_sym2 = _make_symbol(db_session, symbol="POS002", industry="持仓行业2")
    _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=pos_sym1.id,
        market_value=1_000.0,
    )
    _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=pos_sym2.id,
        market_value=1_000.0,
    )
    rule = _make_portfolio_rule(
        db_session,
        portfolio_id=pf.id,
        max_sector_position_pct=100.0,  # 不限制行业
        max_stock_position_pct=5.0,  # 单标的 5% → estimated 4_050
        max_etf_position_pct=5.0,
        max_open_positions=3,  # 当前 2 个，只能加 1 个
    )
    db_session.commit()

    filtered, stats = discovery_fast_scan._portfolio_filter(
        items,
        portfolio_id=pf.id,
        portfolio_rule_id=rule.id,
        db=db_session,
    )
    # max_open_positions=3，当前 2 持仓 → 只能加 1 个新标的
    assert len(filtered) == 1
    assert stats["after_position_limit"] == 1
    assert "position_limit" in stats["applied_rules"]


def test_portfolio_filter_division_by_zero_protection(db_session):
    """除以零保护：total_capital=0 时不抛异常，不阻塞过滤。

    参照 project_memory 硬约束 #3：除以零保护，不返回 Infinity。
    """
    snap = _make_snapshot(db_session)
    sym = _make_symbol(db_session, symbol="D001", industry="行业X")
    us = _make_universe_symbol(db_session, symbol="D001")
    item = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us.id,
        symbol_id=sym.id,
        priority_score=80.0,
    )
    # total_capital=0 → max_total=0 → 跳过总仓位限制（不抛异常）
    pf = _make_portfolio(db_session, name="PF_ZERO", total_capital=0.0)
    rule = _make_portfolio_rule(
        db_session,
        portfolio_id=pf.id,
        max_stock_position_pct=10.0,
        max_etf_position_pct=15.0,
        max_open_positions=10,
    )
    db_session.commit()

    # 不应抛出 ZeroDivisionError
    items, stats = discovery_fast_scan._portfolio_filter(
        [item],
        portfolio_id=pf.id,
        portfolio_rule_id=rule.id,
        db=db_session,
    )
    # total_capital=0 → 跳过总仓位限制，但 max_open_positions 仍生效
    assert "position_limit" in stats["applied_rules"]
    assert stats["after_position_limit"] == 1


def test_portfolio_filter_portfolio_not_found_degrades(db_session):
    """portfolio_id 指定但未找到 → 降级（不阻塞，返回已持仓过滤后的结果）。"""
    snap = _make_snapshot(db_session)
    sym = _make_symbol(db_session, symbol="E001")
    us = _make_universe_symbol(db_session, symbol="E001")
    item = _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us.id,
        symbol_id=sym.id,
        priority_score=80.0,
    )
    db_session.commit()

    # 使用不存在的 portfolio_id（很大的 ID）
    items, stats = discovery_fast_scan._portfolio_filter(
        [item],
        portfolio_id=999999,
        portfolio_rule_id=None,
        db=db_session,
    )
    # portfolio 未找到 → 已持仓去重仍执行（空），后续降级
    assert len(items) == 1
    assert "existing_position_dedup" in stats["applied_rules"]


# ============================================================================
# 4. IndicatorFormulaEvaluator
# ============================================================================

def test_evaluator_simple_arithmetic():
    """简单算术：1 + 2 → 3。"""
    ev = IndicatorFormulaEvaluator("1 + 2")
    assert ev.evaluate({}) == 3


def test_evaluator_function_call_abs():
    """函数调用：abs(-5) → 5。"""
    ev = IndicatorFormulaEvaluator("abs(-5)")
    assert ev.evaluate({}) == 5


def test_evaluator_subtraction():
    """减法：10 - 3 → 7。"""
    ev = IndicatorFormulaEvaluator("10 - 3")
    assert ev.evaluate({}) == 7


def test_evaluator_multiplication():
    """乘法：4 * 5 → 20。"""
    ev = IndicatorFormulaEvaluator("4 * 5")
    assert ev.evaluate({}) == 20


def test_evaluator_division():
    """除法：10 / 4 → 2.5。"""
    ev = IndicatorFormulaEvaluator("10 / 4")
    assert ev.evaluate({}) == 2.5


def test_evaluator_min_max():
    """min / max 函数。"""
    ev_min = IndicatorFormulaEvaluator("min(3, 5)")
    assert ev_min.evaluate({}) == 3
    ev_max = IndicatorFormulaEvaluator("max(3, 5)")
    assert ev_max.evaluate({}) == 5


def test_evaluator_round():
    """round 函数。"""
    ev = IndicatorFormulaEvaluator("round(3.14159)")
    # Python round(3.14159) 返回 int 3
    assert ev.evaluate({}) == 3


def test_evaluator_variable_from_context():
    """变量从 context 解析。"""
    ev = IndicatorFormulaEvaluator("close + 1")
    assert ev.evaluate({"close": 10.0}) == 11.0


def test_evaluator_variable_not_in_context_returns_none():
    """变量未在 context 中 → 该变量 None，导致表达式返回 None。"""
    ev = IndicatorFormulaEvaluator("undefined_var + 1")
    assert ev.evaluate({}) is None


def test_evaluator_pow_within_limit():
    """ast.Pow 在限内：2 ** 10 = 1024。"""
    ev = IndicatorFormulaEvaluator("2 ** 10")
    assert ev.evaluate({}) == 1024


def test_evaluator_pow_overflow_returns_none():
    """ast.Pow 超限：10 ** 200 → None。

    参照 project_memory 硬约束 #1：ast.Pow > 1e100 返回 None（防 OOM）。
    """
    ev = IndicatorFormulaEvaluator("10 ** 200")
    assert ev.evaluate({}) is None


def test_evaluator_pow_large_exponent_returns_none():
    """ast.Pow 指数过大 → None。"""
    ev = IndicatorFormulaEvaluator("2 ** 10000")
    assert ev.evaluate({}) is None


def test_evaluator_pow_result_limit_constant():
    """POW_RESULT_LIMIT = 1e100。"""
    assert POW_RESULT_LIMIT == 1e100


def test_evaluator_import_raises_value_error():
    """非法节点：import os → 抛 ValueError。

    注：ast.parse("import os", mode="eval") 会抛 SyntaxError，
    IndicatorFormulaEvaluator 将其转换为 ValueError。
    """
    with pytest.raises(ValueError):
        IndicatorFormulaEvaluator("import os")


def test_evaluator_lambda_raises_value_error():
    """非法节点：lambda x: x → 抛 ValueError。

    注：lambda 在 eval mode 下可 parse 但不属于 _ALLOWED_NODES。
    """
    with pytest.raises(ValueError):
        # lambda 在 eval mode 下是合法的 AST，但 ast.Lambda 不在白名单
        IndicatorFormulaEvaluator("lambda x: x")


def test_evaluator_attribute_access_raises_value_error():
    """非法节点：属性访问 → 抛 ValueError。"""
    with pytest.raises(ValueError):
        IndicatorFormulaEvaluator("(1).__class__")


def test_evaluator_subscript_raises_value_error():
    """非法节点：下标访问 → 抛 ValueError。"""
    with pytest.raises(ValueError):
        IndicatorFormulaEvaluator("a[0]")


def test_evaluator_list_comp_raises_value_error():
    """非法节点：列表推导式 → 抛 ValueError。"""
    with pytest.raises(ValueError):
        IndicatorFormulaEvaluator("[x for x in range(10)]")


def test_evaluator_dunder_import_raises_value_error():
    """非法节点：__import__('os') → 抛 ValueError（ast.Call + ast.Attribute）。"""
    with pytest.raises(ValueError):
        IndicatorFormulaEvaluator("__import__('os')")


def test_evaluator_empty_formula_raises_value_error():
    """空公式 → 抛 ValueError。"""
    with pytest.raises(ValueError):
        IndicatorFormulaEvaluator("")


def test_evaluator_too_long_formula_raises_value_error():
    """公式超 500 字符 → 抛 ValueError。"""
    with pytest.raises(ValueError):
        IndicatorFormulaEvaluator("1 + " + "0" * 500)


def test_evaluator_syntax_error_raises_value_error():
    """语法错误 → 抛 ValueError。"""
    with pytest.raises(ValueError):
        IndicatorFormulaEvaluator("1 + + ")


def test_evaluator_nested_function_call():
    """嵌套函数调用：abs(min(-3, -5)) → 5。"""
    ev = IndicatorFormulaEvaluator("abs(min(-3, -5))")
    assert ev.evaluate({}) == 5


def test_evaluator_complex_expression():
    """复杂表达式：100 - (100 / (1 + rs)) 当 rs=1.5 → 60.0。"""
    ev = IndicatorFormulaEvaluator("100 - (100 / (1 + rs))")
    result = ev.evaluate({"rs": 1.5})
    assert result == 60.0


def test_evaluator_division_by_zero_returns_none():
    """除以零 → 返回 None（不抛 ZeroDivisionError）。"""
    ev = IndicatorFormulaEvaluator("1 / 0")
    assert ev.evaluate({}) is None


# ============================================================================
# 5. run_fast_scan 端到端
# ============================================================================

def test_run_fast_scan_filter_stats_structure(db_session, monkeypatch):
    """run_fast_scan 返回的 filter_stats 包含 coarse / advanced / portfolio 三段。"""
    # 拦截 HTTP 守门（不发起真实网络请求）
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None
    )

    snap = _make_snapshot(db_session)
    _setup_10_items_with_scores(db_session, snapshot_id=snap.id)
    db_session.commit()

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
    )

    assert result["snapshot_id"] == snap.id
    assert result["degraded_reason"] is None
    # filter_stats 必需的三段
    assert set(result["filter_stats"].keys()) == {"coarse", "advanced", "portfolio"}
    # coarse stats 字段完整
    coarse = result["filter_stats"]["coarse"]
    for key in ("total_in_snapshot", "after_min_score", "after_credibility",
                "coarse_match_count"):
        assert key in coarse
    # advanced stats 字段完整
    advanced = result["filter_stats"]["advanced"]
    for key in ("input_count", "indicator_count", "after_indicators",
                "advanced_match_count", "degraded_reason"):
        assert key in advanced
    # portfolio stats 字段完整
    portfolio = result["filter_stats"]["portfolio"]
    for key in ("input_count", "after_existing_position_filter",
                "after_industry_concentration", "after_position_limit",
                "advanced_match_count", "applied_rules"):
        assert key in portfolio


def test_run_fast_scan_with_portfolio_and_position(db_session, monkeypatch):
    """有 snapshot + items + portfolio + position 的端到端场景。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None
    )

    snap = _make_snapshot(db_session)
    # 5 个标的，其中 1 个已持仓
    held_sym = _make_symbol(db_session, symbol="HELD001", industry="金融")
    held_us = _make_universe_symbol(db_session, symbol="HELD001")
    _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=held_us.id,
        symbol_id=held_sym.id,
        priority_score=95.0,
    )
    new_symbols = []
    for i in range(4):
        sym = _make_symbol(
            db_session,
            symbol=f"NEW{i:03d}",
            industry=f"行业{i}",
        )
        us = _make_universe_symbol(db_session, symbol=f"NEW{i:03d}")
        _make_snapshot_item(
            db_session,
            snapshot_id=snap.id,
            universe_symbol_id=us.id,
            symbol_id=sym.id,
            priority_score=80.0 - i,
        )
        new_symbols.append(sym)
    pf = _make_portfolio(db_session, name="PF_E2E")
    _make_position(db_session, portfolio_id=pf.id, symbol_id=held_sym.id)
    _make_portfolio_rule(
        db_session,
        portfolio_id=pf.id,
        max_open_positions=10,
        max_stock_position_pct=10.0,
        max_etf_position_pct=15.0,
        max_sector_position_pct=100.0,
    )
    db_session.commit()

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        portfolio_id=pf.id,
        portfolio_rule_id=None,
        db=db_session,
    )

    assert result["snapshot_id"] == snap.id
    assert result["degraded_reason"] is None
    # 已持仓标的应被去重
    symbol_ids_in_result = {r["symbol_id"] for r in result["results"]}
    assert held_sym.id not in symbol_ids_in_result
    # 4 个新标的应被保留
    assert len(result["results"]) == 4
    # filter_stats 中 portfolio 段应有 existing_position_dedup
    portfolio_stats = result["filter_stats"]["portfolio"]
    assert "existing_position_dedup" in portfolio_stats["applied_rules"]
    assert portfolio_stats["after_existing_position_filter"] == 4


def test_run_fast_scan_does_not_initiate_http_requests(db_session, monkeypatch):
    """run_fast_scan 不发起任何 HTTP 请求（monkeypatch urllib/requests/httpx）。"""
    snap = _make_snapshot(db_session)
    _setup_10_items_with_scores(db_session, snapshot_id=snap.id)
    db_session.commit()

    def _http_violation(*args, **kwargs):
        raise AssertionError(
            "fast_scan 不应发起任何第三方 HTTP 请求，"
            f"但调用了 args={args!r} kwargs={kwargs!r}"
        )

    # urllib.request.urlopen
    try:
        import urllib.request as _urllib_request
        monkeypatch.setattr(_urllib_request, "urlopen", _http_violation)
    except ImportError:
        pass

    # requests
    try:
        import requests as _requests
        monkeypatch.setattr(_requests, "get", _http_violation, raising=False)
        monkeypatch.setattr(_requests, "post", _http_violation, raising=False)
        if hasattr(_requests, "Session"):
            monkeypatch.setattr(_requests.Session, "request", _http_violation, raising=False)
    except ImportError:
        pass

    # httpx
    try:
        import httpx as _httpx
        if hasattr(_httpx, "Client"):
            monkeypatch.setattr(_httpx.Client, "get", _http_violation, raising=False)
            monkeypatch.setattr(_httpx.Client, "post", _http_violation, raising=False)
            monkeypatch.setattr(_httpx.Client, "request", _http_violation, raising=False)
    except ImportError:
        pass

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
    )
    assert result["degraded_reason"] is None
    assert result["coarse_match_count"] > 0


def test_run_fast_scan_min_data_credibility_param(db_session, monkeypatch):
    """run_fast_scan 接受 min_data_credibility 参数并传递给 _coarse_filter。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None
    )

    snap = _make_snapshot(db_session)
    _setup_10_items_with_scores(db_session, snapshot_id=snap.id)
    db_session.commit()

    # min_data_credibility=0.75 → 应过滤掉 5 个低可信度标的
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=50,
        min_data_credibility=0.75,
        db=db_session,
    )
    coarse_stats = result["filter_stats"]["coarse"]
    assert coarse_stats["after_credibility"] == 5
    # 结果中所有标的的 data_credibility 应 >= 0.75
    # 通过 symbol_id 反查 item（或在结果中检查）
    assert result["coarse_match_count"] == 5


def test_run_fast_scan_with_indicator_plan(db_session, monkeypatch):
    """run_fast_scan 接受 indicator_plan 并传递给 _advanced_filter。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None
    )

    snap = _make_snapshot(db_session)
    sym = _make_symbol(db_session, symbol="IND001")
    us = _make_universe_symbol(db_session, symbol="IND001")
    _make_snapshot_item(
        db_session,
        snapshot_id=snap.id,
        universe_symbol_id=us.id,
        symbol_id=sym.id,
        priority_score=90.0,
    )
    # 给 10 根 K 线
    base_date = snap.trade_date.date() if snap.trade_date else date(2026, 7, 19)
    for i in range(10):
        _make_daily_bar(
            db_session,
            symbol_id=sym.id,
            trade_date=base_date - timedelta(days=9 - i),
            close=15.0,
        )
    db_session.commit()

    indicator_plan = {
        "ind1": {"formula": "close", "min_value": 10.0, "max_value": 20.0},
    }
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        indicator_plan=indicator_plan,
        db=db_session,
    )
    assert result["degraded_reason"] is None
    advanced_stats = result["filter_stats"]["advanced"]
    assert advanced_stats["indicator_count"] == 1
    assert advanced_stats["after_indicators"] == 1
