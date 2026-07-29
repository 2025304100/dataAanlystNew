"""WP-P.4 用户快速扫描链。

将"读取 ready 快照 → SQL 粗筛 → Top-K 高级指标过滤 → 组合约束过滤 → 写入小规模候选"
这一用户敏感路径从后台数据准备链中剥离，作为同步操作执行（目标 < 5 分钟，理想 < 60s）。

核心约束：
- 不发起任何第三方 HTTP 请求（market_data_sync / financial_report_task /
  macro_update_task / hot_rank_task / lhb_institution_task / tail_proxy_task）
- 不调用 factors/pipeline_task（增量计算是后台数据准备的职责）
- 消息分数读取最近缓存，不在用户扫描中同步抓取新闻
- 自定义指标只处理 Top 300，超限明确拒绝或转后台预计算（WP-P.5 完整实现）
- 绝不读 building 状态快照（防半成品）
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.discovery_score_snapshot import (
    DiscoveryScoreSnapshot,
    DiscoveryScoreSnapshotItem,
)
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.scan import ScanResult, ScanRun
from app.models.symbol import Symbol
from app.models.trade_setup import TradeSetup
from app.services.discovery_stage_budget import (
    CancelToken,
    ScanBudgetExceededError,
    ScanCancelledError,
    ScanTimings,
    check_cancel_token,
)
from app.services.indicator_ast_sandbox import IndicatorFormulaEvaluator

logger = logging.getLogger(__name__)

# 自定义指标数量上限（WP-P.5 完整实现向量化，WP-P.4 占位时也强制此上限）
MAX_CUSTOM_INDICATORS = 5
# 粗筛 Top-K 上限
DEFAULT_TOP_K = 300
# 缓存键前缀
_CACHE_KEY_PREFIX = "snapshot"
# WP-P.8 异步任务类型标识（用于 AsyncTaskRecord.task_type）
# 当前 fast_scan 为同步执行，但若由 data_prep 链路自动触发或后续改为异步时使用
TASK_TYPE_FAST_SCAN = "discovery_fast_scan"

# bar_count < 此值视为数据不足（参照 project_memory 硬约束 #2）
_MIN_BAR_COUNT = 5
# K 线回看窗口（覆盖 RSI/MACD/BOLL 等常见指标最大周期）
_KLINE_LOOKBACK_DAYS = 250


def _now() -> datetime:
    """UTC 当前时间（naive，与 DB 中其它时间戳一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_scope(scope: str) -> str:
    """归一化 scope 为下划线格式（snapshot 表约定）。"""
    return scope.replace("-", "_")


def _assert_no_http_request() -> None:
    """快速扫描路径的 HTTP 请求守门（开发/测试用，可被 monkeypatch 触发）。

    此函数在 fast_scan 中调用，若内部触发了第三方 HTTP 请求则视为违规。

    实现思路：
    - 在测试中通过 monkeypatch urllib.request.urlopen / httpx.Client /
      requests.Session 等网络方法检测任何 HTTP 请求并抛 AssertionError
    - 实际运行时此函数本身不阻断请求，仅作为守门点供测试 hook
    """
    # 守门点：测试时可 monkeypatch 此函数注入断言逻辑
    return None


def get_ready_snapshot(db: Session, scope: str) -> DiscoveryScoreSnapshot | None:
    """获取某 scope 当前的 ready 评分快照。

    若无 ready 快照返回 None，调用方应给出"数据未就绪，请前往基础数据"提示。
    绝不返回 building 状态快照（防半成品）。

    Args:
        db: 数据库会话
        scope: 范围（cn_stock / cn_etf / us_stock / us_etf，兼容连字符格式）

    Returns:
        DiscoveryScoreSnapshot 或 None
    """
    normalized = _normalize_scope(scope)
    stmt = (
        select(DiscoveryScoreSnapshot)
        .where(
            DiscoveryScoreSnapshot.scope == normalized,
            DiscoveryScoreSnapshot.status == "ready",
        )
        .order_by(DiscoveryScoreSnapshot.generated_at.desc().nullslast())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def get_latest_historical_snapshot(db: Session, scope: str) -> DiscoveryScoreSnapshot | None:
    """获取某 scope 最近一个非 building 状态的历史快照（用于无 ready 快照时回退只读查看）。

    优先级：ready（旧版本）> superseded > failed。
    不返回 building 状态（防半成品）。

    Args:
        db: 数据库会话
        scope: 范围

    Returns:
        DiscoveryScoreSnapshot 或 None（首次使用无任何历史快照时）
    """
    normalized = _normalize_scope(scope)
    stmt = (
        select(DiscoveryScoreSnapshot)
        .where(
            DiscoveryScoreSnapshot.scope == normalized,
            DiscoveryScoreSnapshot.status.in_(["ready", "superseded", "failed"]),
        )
        .order_by(DiscoveryScoreSnapshot.generated_at.desc().nullslast())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def _count_snapshot_items(db: Session, snapshot_id: int) -> int:
    """统计某快照下 item 总数。"""
    return int(
        db.execute(
            select(func.count(DiscoveryScoreSnapshotItem.id)).where(
                DiscoveryScoreSnapshotItem.snapshot_id == snapshot_id
            )
        ).scalar_one()
    )


def _coarse_filter(
    db: Session,
    snapshot: DiscoveryScoreSnapshot,
    *,
    min_score: float,
    asset_types: list[str] | None = None,
    stages: list[str] | None = None,
    actions: list[str] | None = None,
    min_data_credibility: float = 0.0,
    limit: int = DEFAULT_TOP_K,
) -> tuple[list[DiscoveryScoreSnapshotItem], dict]:
    """SQL 粗筛：在快照表上完成分数阈值、资产类型、阶段、动作、可信度、排序，保留 Top 300。

    Args:
        db: 数据库会话
        snapshot: 当前 ready 快照
        min_score: priority_score 最低阈值（默认 55）
        asset_types: 资产类型筛选（如 ["stock", "etf"]），None 表示不限
        stages: 阶段筛选（如 ["breakout", "pullback"]），None 表示不限
        actions: 动作筛选（如 ["buy", "watch"]），None 表示不限
        min_data_credibility: 数据可信度最低阈值（0.0~1.0）
        limit: Top-K 上限（默认 300）；0 返回空；<0 视为不限

    Returns:
        (items, stats) 元组
        stats = {
            "total_in_snapshot": int,
            "after_min_score": int,
            "after_asset_types": int,
            "after_stages": int,
            "after_actions": int,
            "after_credibility": int,
            "coarse_match_count": int,
        }
    """
    snapshot_id = snapshot.id
    total_in_snapshot = _count_snapshot_items(db, snapshot_id)

    # 边界：limit=0 → 直接返回空
    if limit == 0:
        stats = {
            "total_in_snapshot": total_in_snapshot,
            "after_min_score": 0,
            "after_asset_types": 0,
            "after_stages": 0,
            "after_actions": 0,
            "after_credibility": 0,
            "coarse_match_count": 0,
        }
        return [], stats

    # 阶段 1：min_score 阈值
    stmt = select(DiscoveryScoreSnapshotItem).where(
        DiscoveryScoreSnapshotItem.snapshot_id == snapshot_id,
        DiscoveryScoreSnapshotItem.priority_score >= float(min_score),
    )
    after_min_score = _count_with_stmt(db, stmt)

    # 阶段 2：asset_types 过滤（join symbols 表）
    if asset_types:
        stmt = stmt.join(
            Symbol, Symbol.id == DiscoveryScoreSnapshotItem.symbol_id, isouter=True
        ).where(Symbol.asset_type.in_(asset_types))
    after_asset_types = _count_with_stmt(db, stmt)

    # 阶段 3：stages 过滤
    if stages:
        stmt = stmt.where(DiscoveryScoreSnapshotItem.stage.in_(stages))
    after_stages = _count_with_stmt(db, stmt)

    # 阶段 4：actions 过滤
    if actions:
        stmt = stmt.where(DiscoveryScoreSnapshotItem.action.in_(actions))
    after_actions = _count_with_stmt(db, stmt)

    # 阶段 5：min_data_credibility 过滤
    # None 视为 0（不限制）；阈值 > 0 时过滤 data_credibility >= 阈值
    credibility_threshold = float(min_data_credibility or 0.0)
    if credibility_threshold > 0:
        stmt = stmt.where(
            DiscoveryScoreSnapshotItem.data_credibility >= credibility_threshold
        )
    after_credibility = _count_with_stmt(db, stmt)

    # 排序与 Top-K 限制
    stmt = stmt.order_by(
        DiscoveryScoreSnapshotItem.priority_score.desc(),
        DiscoveryScoreSnapshotItem.id.asc(),
    )
    if limit > 0:
        stmt = stmt.limit(limit)
    items = list(db.execute(stmt).scalars().all())

    stats = {
        "total_in_snapshot": total_in_snapshot,
        "after_min_score": after_min_score,
        "after_asset_types": after_asset_types,
        "after_stages": after_stages,
        "after_actions": after_actions,
        "after_credibility": after_credibility,
        "coarse_match_count": len(items),
    }
    return items, stats


def _count_with_stmt(db: Session, stmt) -> int:
    """统计 stmt 去除 select entity 后的行数。"""
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    return int(db.execute(count_stmt).scalar_one())


def _normalize_indicator_plan(indicator_plan: dict | None) -> list[dict]:
    """归一化 indicator_plan 为 list[dict] 形式。

    支持两种格式：
    - 新格式：{"indicators": [{"key": "rsi_14", "formula": "...", ...}], "max_indicators": 5}
    - 简单格式：{"key1": {"formula": "expr1"}, "key2": {"formula": "expr2"}}

    返回指标定义列表，每个元素至少包含 key / formula / params / min_value / max_value / enabled。
    """
    if not indicator_plan or not isinstance(indicator_plan, dict):
        return []
    indicators: list[dict] = []
    if "indicators" in indicator_plan and isinstance(indicator_plan["indicators"], list):
        for item in indicator_plan["indicators"]:
            if not isinstance(item, dict):
                continue
            indicators.append(item)
    else:
        # 简单格式：{key: {formula: ...}}
        for key, value in indicator_plan.items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            item.setdefault("key", key)
            indicators.append(item)
    return indicators


def _count_indicators(indicator_plan: dict | None) -> int:
    """统计 indicator_plan 中的指标数量。"""
    return len(_normalize_indicator_plan(indicator_plan))


def _advanced_filter(
    items: list[DiscoveryScoreSnapshotItem],
    *,
    indicator_plan: dict | None,
    db: Session,
    snapshot: DiscoveryScoreSnapshot | None = None,
) -> tuple[list[DiscoveryScoreSnapshotItem], dict]:
    """高级筛选：只对 Top 300 批量加载 K 线，向量化计算最多 5 个自定义指标。

    indicator_plan 支持两种格式：
    - 新格式：{"indicators": [{"key": "rsi_14", "formula": "...", "params": {...},
                 "min_value": 0, "max_value": 100, "enabled": true}],
              "max_indicators": 5}
    - 简单格式：{"key1": {"formula": "expr1"}, "key2": {"formula": "expr2"}}

    约束：
    - 最多 5 个自定义指标（超限明确拒绝并降级，error_code="INDICATOR_PLAN_TOO_MANY"）
    - 全市场逐只复杂自定义公式定义为"预计算指标"由后台快照物化
      （本任务先不实现，返回 degraded_reason="should_precompute"）
    - 向量化计算使用 numpy / pandas
    - AST sandbox 必须限制 ast.Pow 防止 OOM（参照 project_memory 硬约束 #1）
    - bar_count < 5 边界安全处理（参照 project_memory 硬约束 #2）

    Returns:
        (filtered_items, stats) 元组
        stats = {
            "input_count": int,
            "indicator_count": int,
            "after_indicators": int,
            "advanced_match_count": int,
            "degraded_reason": str | None,
            "indicator_errors": list[dict],
        }
    """
    input_count = len(items)
    base_stats = {
        "input_count": input_count,
        "indicator_count": 0,
        "after_indicators": input_count,
        "advanced_match_count": input_count,
        "degraded_reason": None,
        "indicator_errors": [],
    }

    # 无 indicator_plan 或空 → 直接返回 items
    if not indicator_plan or not isinstance(indicator_plan, dict):
        return list(items), base_stats

    indicators = _normalize_indicator_plan(indicator_plan)
    # 过滤掉 enabled=False 的指标
    indicators = [ind for ind in indicators if ind.get("enabled", True) is not False]

    base_stats["indicator_count"] = len(indicators)

    if not indicators:
        return list(items), base_stats

    # 超过 5 个指标 → 返回 items 并 degraded_reason="INDICATOR_PLAN_TOO_MANY"
    if len(indicators) > MAX_CUSTOM_INDICATORS:
        base_stats["degraded_reason"] = "INDICATOR_PLAN_TOO_MANY"
        base_stats["indicator_errors"] = [
            {
                "error_code": "INDICATOR_PLAN_TOO_MANY",
                "message": (
                    f"indicator count {len(indicators)} exceeds limit "
                    f"{MAX_CUSTOM_INDICATORS}"
                ),
            }
        ]
        # 超限时不能直接执行高级筛选，返回 items 由调用方降级处理
        return list(items), base_stats

    # 准备 AST evaluator（构造失败的指标记录错误，不阻塞其它指标）
    evaluators: list[tuple[dict, IndicatorFormulaEvaluator]] = []
    indicator_errors: list[dict] = []
    for ind in indicators:
        formula = str(ind.get("formula", "") or "")
        key = str(ind.get("key", "") or "")
        try:
            evaluator = IndicatorFormulaEvaluator(formula)
            evaluators.append((ind, evaluator))
        except (ValueError, TypeError) as exc:
            indicator_errors.append({
                "indicator_key": key,
                "error_code": "INVALID_FORMULA",
                "message": str(exc),
            })

    if not evaluators:
        # 全部指标公式非法，降级
        base_stats["indicator_errors"] = indicator_errors
        base_stats["degraded_reason"] = "ALL_INDICATORS_INVALID"
        return list(items), base_stats

    # 批量加载 K 线：select * from daily_bars where symbol_id in (?) and trade_date between ? and ?
    symbol_ids = [item.symbol_id for item in items if item.symbol_id is not None]
    if not symbol_ids:
        base_stats["indicator_errors"] = indicator_errors
        return list(items), base_stats

    # 以快照 trade_date 为基准，回看 250 个交易日
    end_date = (
        snapshot.trade_date.date() if snapshot is not None and snapshot.trade_date else None
    )
    if end_date is None:
        end_date = _now().date()
    start_date = end_date - timedelta(days=_KLINE_LOOKBACK_DAYS * 2)

    bar_rows = db.execute(
        select(DailyBar)
        .where(
            DailyBar.symbol_id.in_(symbol_ids),
            DailyBar.trade_date >= start_date,
            DailyBar.trade_date <= end_date,
        )
        .order_by(DailyBar.symbol_id.asc(), DailyBar.trade_date.asc())
    ).scalars().all()

    # 按 symbol_id 分组
    bars_by_symbol: dict[int, list[DailyBar]] = {}
    for bar in bar_rows:
        bars_by_symbol.setdefault(bar.symbol_id, []).append(bar)

    # 逐标的逐指标评估（保留与 pandas 等价的逻辑，但避免在每条 item 上重复建 DataFrame）
    # 注：本任务先实现"逐标的 + 单公式评估"，未启用 pandas groupby 的全市场并行；
    # 真正的全市场向量化预计算由后台快照物化（degraded_reason="should_precompute"）
    # 在 WP-P.5 阶段保留行级降级路径，单标的多公式仍按 AST sandbox 安全求值。
    filtered: list[DiscoveryScoreSnapshotItem] = []
    for item in items:
        if item.symbol_id is None:
            # 无 symbol_id 关联的 item 直接通过（不参与指标筛选）
            filtered.append(item)
            continue

        bars = bars_by_symbol.get(item.symbol_id, [])
        # bar_count < 5 → 该标的该指标视为 None（不参与筛选，整体通过）
        if len(bars) < _MIN_BAR_COUNT:
            # 数据不足，安全降级（参照 project_memory 硬约束 #2）
            filtered.append(item)
            continue

        # 准备上下文：最新一根 K 线的字段值
        latest_bar = bars[-1]
        context = {
            "open": float(latest_bar.open),
            "high": float(latest_bar.high),
            "low": float(latest_bar.low),
            "close": float(latest_bar.close),
            "volume": float(latest_bar.volume or 0.0),
            "amount": float(latest_bar.amount or 0.0),
            "turnover_rate": float(latest_bar.turnover_rate or 0.0),
            "prev_close": float(bars[-2].close) if len(bars) >= 2 else float(latest_bar.close),
            "bar_count": float(len(bars)),
        }

        # 单标的逐指标评估
        all_pass = True
        for ind_def, evaluator in evaluators:
            try:
                result = evaluator.evaluate(context)
            except Exception as exc:  # pragma: no cover - 兜底
                result = None
                indicator_errors.append({
                    "indicator_key": str(ind_def.get("key", "") or ""),
                    "error_code": "EVAL_EXCEPTION",
                    "message": str(exc),
                })

            if result is None:
                # 单指标计算错误不阻塞整体，记入 indicator_errors
                # 该标的此指标视为 None，不参与筛选（视为通过）
                continue

            try:
                result_float = float(result)
            except (TypeError, ValueError):
                result_float = None

            if result_float is None:
                continue

            min_value = ind_def.get("min_value")
            max_value = ind_def.get("max_value")
            if min_value is not None and result_float < float(min_value):
                all_pass = False
                break
            if max_value is not None and result_float > float(max_value):
                all_pass = False
                break

        if all_pass:
            filtered.append(item)

    base_stats["after_indicators"] = len(filtered)
    base_stats["advanced_match_count"] = len(filtered)
    base_stats["indicator_errors"] = indicator_errors
    return filtered, base_stats


def _portfolio_filter(
    items: list[DiscoveryScoreSnapshotItem],
    *,
    portfolio_id: int | None,
    portfolio_rule_id: int | None,
    db: Session,
) -> tuple[list[DiscoveryScoreSnapshotItem], dict]:
    """组合风控只对高级筛选后最终集合执行。

    约束类型：
    1. 已持仓标的去重（避免重复推荐已持仓）
    2. 同行业/同主题数量上限（参照 portfolio 规则 max_sector_position_pct）
    3. 单标的目标仓位上限（参照 portfolio_rule.max_stock_position_pct /
       max_etf_position_pct）
    4. 总仓位上限（参照 Portfolio.total_capital / investable_ratio）

    Args:
        portfolio_id: 组合 ID，None 表示不应用组合过滤
        portfolio_rule_id: 组合规则 ID

    Returns:
        (filtered_items, stats) 元组
        stats = {
            "input_count": int,
            "after_existing_position_filter": int,
            "after_industry_concentration": int,
            "after_position_limit": int,
            "advanced_match_count": int,
            "applied_rules": list[str],
        }
    """
    input_count = len(items)
    applied_rules: list[str] = []

    # portfolio_id 为 None → 直接返回
    if portfolio_id is None:
        stats = {
            "input_count": input_count,
            "after_existing_position_filter": input_count,
            "after_industry_concentration": input_count,
            "after_position_limit": input_count,
            "advanced_match_count": input_count,
            "applied_rules": applied_rules,
        }
        return list(items), stats

    # 1. 已持仓标的去重
    existing_position_stmt = select(Position.symbol_id).where(
        Position.portfolio_id == int(portfolio_id)
    )
    existing_symbol_ids = set(
        int(sid) for sid in db.execute(existing_position_stmt).scalars().all() if sid
    )
    after_existing = [
        item for item in items
        if item.symbol_id is None or item.symbol_id not in existing_symbol_ids
    ]
    applied_rules.append("existing_position_dedup")
    after_existing_count = len(after_existing)

    # 加载 portfolio_rule（若指定）
    rule: PortfolioRule | None = None
    if portfolio_rule_id is not None:
        rule = db.get(PortfolioRule, int(portfolio_rule_id))
        if rule is None:
            # 指定了规则 ID 但未找到 → 降级（不阻塞）
            logger.warning(
                "_portfolio_filter: portfolio_rule_id=%s not found",
                portfolio_rule_id,
            )
        else:
            applied_rules.append("portfolio_rule_loaded")

    # 加载 portfolio
    portfolio: Portfolio | None = db.get(Portfolio, int(portfolio_id))
    if portfolio is None:
        # 指定了 portfolio_id 但未找到 → 降级
        logger.warning(
            "_portfolio_filter: portfolio_id=%s not found", portfolio_id
        )
        stats = {
            "input_count": input_count,
            "after_existing_position_filter": after_existing_count,
            "after_industry_concentration": after_existing_count,
            "after_position_limit": after_existing_count,
            "advanced_match_count": after_existing_count,
            "applied_rules": applied_rules,
        }
        return after_existing, stats

    applied_rules.append("portfolio_loaded")

    # 2. 行业/主题集中度限制（参照 portfolio_rule.max_sector_position_pct）
    # max_sector_position_pct 为百分比（0~100），转换为最大推荐数 = round(总数 * pct / 100)
    # 除以零保护（参照 project_memory 硬约束 #3）
    after_industry = list(after_existing)
    if rule is not None and after_industry:
        max_sector_pct = float(rule.max_sector_position_pct or 0)
        if max_sector_pct > 0 and input_count > 0:
            # 按 input_count 计算总数，避免除零
            max_per_industry = max(1, int(round(input_count * max_sector_pct / 100.0)))
            industry_count: dict[str, int] = {}
            filtered_by_industry: list[DiscoveryScoreSnapshotItem] = []
            for item in after_industry:
                industry = _resolve_symbol_industry(db, item.symbol_id)
                if industry is None:
                    # 无行业信息的标的不受集中度限制
                    filtered_by_industry.append(item)
                    continue
                cur_count = industry_count.get(industry, 0)
                if cur_count < max_per_industry:
                    filtered_by_industry.append(item)
                    industry_count[industry] = cur_count + 1
            after_industry = filtered_by_industry
            applied_rules.append("industry_concentration")
    after_industry_count = len(after_industry)

    # 3. 单标的目标仓位上限 + 4. 总仓位上限
    # 总仓位上限：portfolio.total_capital * investable_ratio * (1 - cash_reserve_ratio)
    # 单标的仓位上限：rule.max_stock_position_pct / max_etf_position_pct（百分比）
    after_position = list(after_industry)
    if rule is not None and portfolio is not None and after_position:
        total_capital = float(portfolio.total_capital or 0)
        investable_ratio = float(portfolio.investable_ratio or 0)
        cash_reserve_ratio = float(portfolio.cash_reserve_ratio or 0)
        # 除以零保护：total_capital == 0 时跳过总仓位限制
        max_total = (
            total_capital * investable_ratio * (1.0 - cash_reserve_ratio)
            if total_capital > 0
            else 0.0
        )

        # 加载当前总持仓市值（用于剩余可分配仓位计算）
        current_market_value = float(
            db.execute(
                select(func.sum(Position.market_value)).where(
                    Position.portfolio_id == int(portfolio_id)
                )
            ).scalar_one()
            or 0.0
        )
        # 剩余可分配仓位
        remaining_capacity = (
            max(0.0, max_total - current_market_value) if max_total > 0 else None
        )

        # 单标的仓位限制：按 asset_type 选择对应百分比
        max_stock_pct = float(rule.max_stock_position_pct or 0)
        max_etf_pct = float(rule.max_etf_position_pct or 0)
        max_open_positions = int(rule.max_open_positions or 0)

        # 当前持仓数量
        current_position_count = int(
            db.execute(
                select(func.count(Position.id)).where(
                    Position.portfolio_id == int(portfolio_id)
                )
            ).scalar_one()
            or 0
        )

        kept: list[DiscoveryScoreSnapshotItem] = []
        accumulated = 0.0
        new_added_count = 0
        for item in after_position:
            symbol = _resolve_symbol(db, item.symbol_id) if item.symbol_id else None
            asset_type = symbol.asset_type if symbol is not None else None

            # 单标的仓位上限：以 max_total * pct/100 估算
            pct_limit = max_etf_pct if asset_type == "etf" else max_stock_pct
            # 除以零保护
            if max_total > 0 and pct_limit > 0:
                estimated_position = max_total * pct_limit / 100.0
            else:
                # 无仓位上限信息 → 不阻塞
                estimated_position = 0.0

            if (
                remaining_capacity is not None
                and estimated_position > 0
                and accumulated + estimated_position > remaining_capacity
            ):
                # 超过总仓位上限
                continue

            if (
                max_open_positions > 0
                and current_position_count + new_added_count >= max_open_positions
            ):
                # 达到 max_open_positions 上限
                continue

            kept.append(item)
            accumulated += estimated_position
            new_added_count += 1
        after_position = kept
        applied_rules.append("position_limit")
    after_position_count = len(after_position)

    stats = {
        "input_count": input_count,
        "after_existing_position_filter": after_existing_count,
        "after_industry_concentration": after_industry_count,
        "after_position_limit": after_position_count,
        "advanced_match_count": after_position_count,
        "applied_rules": applied_rules,
    }
    return after_position, stats


def _resolve_symbol_industry(db: Session, symbol_id: int | None) -> str | None:
    """加载某 symbol 的行业字段。"""
    if symbol_id is None:
        return None
    row = db.execute(
        select(Symbol.industry).where(Symbol.id == int(symbol_id))
    ).first()
    if row is None:
        return None
    return row[0]


def _resolve_symbol(db: Session, symbol_id: int | None) -> Symbol | None:
    """加载 Symbol 行。"""
    if symbol_id is None:
        return None
    return db.get(Symbol, int(symbol_id))


def _build_cache_key(
    *,
    snapshot_id: int,
    scope: str,
    min_score: float,
    asset_types: list[str] | None,
    stages: list[str] | None,
    actions: list[str] | None,
    indicator_plan: dict | None,
    portfolio_id: int | None,
    portfolio_rule_id: int | None,
    limit: int,
) -> str:
    """构建扫描结果缓存键。

    缓存键组成：snapshot_id + scope + min_score + filter_hash +
                 indicator_plan_version + portfolio_id + portfolio_rule_version

    WP-P.6 会用此键实现结果复用；WP-P.4 先实现键的计算。
    """
    filter_parts: list[str] = []
    if asset_types:
        filter_parts.append("at:" + ",".join(sorted(asset_types)))
    if stages:
        filter_parts.append("st:" + ",".join(sorted(stages)))
    if actions:
        filter_parts.append("ac:" + ",".join(sorted(actions)))
    filter_str = "|".join(filter_parts)
    filter_hash = hashlib.md5(filter_str.encode("utf-8")).hexdigest()[:8]

    indicator_hash = "none"
    if indicator_plan:
        # 取 indicator_plan 的稳定哈希（不依赖 dict 顺序）
        try:
            indicator_str = json.dumps(indicator_plan, sort_keys=True, default=str)
            indicator_hash = hashlib.md5(indicator_str.encode("utf-8")).hexdigest()[:8]
        except (TypeError, ValueError):
            indicator_hash = "invalid"

    return (
        f"{_CACHE_KEY_PREFIX}_{snapshot_id}_scope_{_normalize_scope(scope)}"
        f"_min_{min_score}_filter_{filter_hash}_ind_{indicator_hash}"
        f"_pf_{portfolio_id or 'none'}_pr_{portfolio_rule_id or 'none'}_limit_{limit}"
    )


def _find_cached_scan_run(
    db: Session,
    *,
    cache_key: str,
    snapshot_id: int,
) -> ScanRun | None:
    """查找已有相同 cache_key + snapshot_id 的 ScanRun（WP-P.6）。

    匹配条件：
    - cache_key 完全相同
    - snapshot_id 相同（必须，cache_key 内部已含 snapshot_id 但显式校验更安全）
    - status ∈ {done, success, completed}（不含 failed / running）
    - created_at >= snapshot.generated_at（避免命中旧快照的旧结果）

    返回匹配的 ScanRun；多个匹配时取 created_at 最新一个。
    """
    snapshot = db.get(DiscoveryScoreSnapshot, snapshot_id)
    if snapshot is None:
        return None

    stmt = (
        select(ScanRun)
        .where(
            ScanRun.cache_key == cache_key,
            ScanRun.snapshot_id == snapshot_id,
            ScanRun.status.in_(("done", "success", "completed")),
        )
    )
    if snapshot.generated_at is not None:
        stmt = stmt.where(ScanRun.created_at >= snapshot.generated_at)
    stmt = stmt.order_by(ScanRun.created_at.desc().nullslast()).limit(1)
    return db.execute(stmt).scalars().first()


def _reuse_cached_results(
    db: Session,
    cached_scan_run: ScanRun,
) -> tuple[list[dict], dict]:
    """从已有 ScanRun 读取候选结果，不重新写入 ScanResult（WP-P.6）。

    Returns:
        (results, summary) 元组
        results: 候选列表，每项含 symbol_id / symbol / quality_score /
                 timing_score / priority_score / stage / action / data_credibility
        summary: {
            "cache_hit": True,
            "cached_scan_run_id": int,
            "cached_at": datetime,
            "result_count": int,
        }
    """
    # 读取缓存的 ScanResult，按 rank_no 排序
    result_rows = db.execute(
        select(ScanResult)
        .where(ScanResult.scan_run_id == cached_scan_run.id)
        .order_by(ScanResult.rank_no.asc())
    ).scalars().all()

    # 批量加载 symbol 信息
    symbol_ids = [r.symbol_id for r in result_rows if r.symbol_id is not None]
    symbol_map: dict[int, Symbol] = {}
    if symbol_ids:
        sym_rows = db.execute(
            select(Symbol).where(Symbol.id.in_(symbol_ids))
        ).scalars().all()
        symbol_map = {s.id: s for s in sym_rows}

    results: list[dict[str, Any]] = []
    for r in result_rows:
        sym = symbol_map.get(r.symbol_id) if r.symbol_id else None
        item: dict[str, Any] = {
            # 缓存命中场景下不暴露原 ScanResult id / universe_symbol_id
            "id": None,
            "snapshot_id": cached_scan_run.snapshot_id,
            "universe_symbol_id": None,
            "symbol_id": r.symbol_id,
            "quality_score": r.quality_score,
            "timing_score": r.timing_score,
            "priority_score": r.priority_score,
            "stage": r.stage,
            "action": r.action,
            # ScanResult 表不存 data_credibility；缓存命中场景无法恢复，置 None
            "data_credibility": None,
        }
        if sym is not None:
            item["symbol"] = sym.symbol
            item["name"] = sym.name
            item["asset_type"] = sym.asset_type
            item["market"] = sym.market
        results.append(item)

    summary = {
        "cache_hit": True,
        "cached_scan_run_id": cached_scan_run.id,
        "cached_at": cached_scan_run.created_at,
        "result_count": len(results),
    }
    return results, summary


def _serialize_item(
    item: DiscoveryScoreSnapshotItem, *, symbol: Symbol | None = None
) -> dict[str, Any]:
    """将快照 item 序列化为前端可用字典。"""
    payload: dict[str, Any] = {
        "id": item.id,
        "snapshot_id": item.snapshot_id,
        "universe_symbol_id": item.universe_symbol_id,
        "symbol_id": item.symbol_id,
        "quality_score": item.quality_score,
        "timing_score": item.timing_score,
        "priority_score": item.priority_score,
        "stage": item.stage,
        "action": item.action,
        "data_credibility": item.data_credibility,
    }
    if symbol is not None:
        payload["symbol"] = symbol.symbol
        payload["name"] = symbol.name
        payload["asset_type"] = symbol.asset_type
        payload["market"] = symbol.market
    return payload


def run_fast_scan(
    *,
    scope: str,
    min_score: float = 55,
    asset_types: list[str] | None = None,
    stages: list[str] | None = None,
    actions: list[str] | None = None,
    indicator_plan: dict | None = None,
    portfolio_id: int | None = None,
    portfolio_rule_id: int | None = None,
    min_data_credibility: float = 0.0,
    limit: int = DEFAULT_TOP_K,
    cancel_token: CancelToken | None = None,
    enforce_budget: bool = True,
    task_id: str | None = None,
    db: Session | None = None,
) -> dict:
    """执行用户快速扫描（同步，目标 < 5 分钟，理想 < 60s）。

    用户快速扫描链：
    1. 读取 ready 评分快照（绝不读 building 半成品）
    2. SQL 粗筛和排序（分数阈值 / 资产类型 / 阶段 / 动作 / 可信度，保留 Top 300）
    3. Top-K 高级指标过滤（向量化计算，最多 5 个自定义指标）
    4. 组合约束过滤（如 portfolio_rule 限制）
    5. 保存小规模候选结果到 ScanResult
    6. 写入 ScanRun 摘要

    WP-P.6 缓存命中：
    - 在 _build_cache_key 之后调用 _find_cached_scan_run 检测相同参数已有结果
    - 命中则直接返回 _reuse_cached_results 结果，不重新写入 ScanResult
    - 未命中则继续完整流程，并在 ScanRun 写入 cache_key 供下次复用

    WP-P.8 阶段预算与可观测性：
    - 每阶段使用 ScanTimings 计时（snapshot_health_check / sql_coarse_filter /
      advanced_indicator / portfolio_filter / result_persistence / finalize_audit）
    - 每阶段开始前检查 cancel_token；若已取消抛 ScanCancelledError（协作式取消）
    - 总预算超时且 enforce_budget=True 时抛 ScanBudgetExceededError，由 except
      捕获并转换为 status="timeout" 响应（不抛异常给用户）
    - 返回 timings 字段供前端展示具体慢在哪一步
    - 可选 task_id：若提供则将 timings 持久化到 AsyncTaskRecord（供后续查询）

    约束：
    - 不发起任何第三方 HTTP 请求
    - 不调用 market_data_sync / financial_report_task / macro_update_task /
      hot_rank_task / lhb_institution_task / tail_proxy_task
    - 消息分数读取最近缓存，不在用户扫描中同步抓取新闻
    - 自定义指标只处理 Top 300，超限明确拒绝或转后台预计算
    - 缓存命中时绝不重复写入 ScanResult（参照 project_memory 硬约束）
    - ScanResult 写入数量严格受 limit（默认 300）限制
    - 取消令牌不阻塞主流程，仅在每个阶段开始前检查
    - 超时降级 best-effort，不抛异常中断用户响应

    Returns:
        {
            "snapshot_id": int,
            "snapshot_generated_at": datetime,
            "scope": str,
            "total_in_snapshot": int,
            "coarse_match_count": int,
            "advanced_match_count": int,
            "result_rows_written": int,
            "cache_key": str,
            "cache_hit": bool,
            "results": [...],
            "duration_ms": float,
            "degraded_reason": str | None,
            "recommended_action": str | None,
            "filter_stats": {
                "coarse": dict,
                "advanced": dict,
                "portfolio": dict,
            },
            "cached_from_scan_run_id": int | None,  # WP-P.6
            "cached_at": datetime | None,  # WP-P.6
            "timings": dict | None,  # WP-P.8 阶段计时
            "status": str | None,  # WP-P.8 "ok"|"cancelled"|"timeout"|None
            "exceeded_stages": list[dict] | None,  # WP-P.8 超时阶段列表
        }
    """
    started_at = time.perf_counter()

    # HTTP 守门：在 fast_scan 入口调用，测试中可 monkeypatch 验证未发起 HTTP 请求
    _assert_no_http_request()

    # WP-P.8 阶段计时集合
    timings = ScanTimings()
    normalized = _normalize_scope(scope)

    own_session = db is None
    if db is None:
        from app.db.session import get_session_local
        db = get_session_local()
    try:
        # 1. 快照与数据健康预检（budget 10s）
        check_cancel_token(cancel_token)
        timings.cancel_token_checked = True
        timings.add_stage("snapshot_health_check")
        snapshot = get_ready_snapshot(db, scope)
        if snapshot is None:
            timings.finish_stage("snapshot_health_check")
            # WP-P-FIX.2: 无 ready 快照时尝试返回上一历史快照 + 自动启动数据准备任务
            # 1. 查询历史快照（ready 旧版本 / superseded / failed）
            historical_snapshot = get_latest_historical_snapshot(db, scope)
            # 2. 自动启动数据准备任务（fire-and-forget，不阻塞响应）
            data_prep_task_id: str | None = None
            try:
                # 延迟导入避免循环依赖（discovery_data_prep 可能反向导入 discovery_fast_scan）
                from app.services.discovery_data_prep import start_data_prep_task, _is_data_prep_running
                # 并发保护：已有 data_prep 运行时不重复启动
                if _is_data_prep_running(db, normalized) is None:
                    fast_scan_params = {
                        "scope": normalized,
                        "min_score": min_score,
                        "asset_types": asset_types,
                        "stages": stages,
                        "actions": actions,
                        "portfolio_id": portfolio_id,
                        "portfolio_rule_id": portfolio_rule_id,
                        "limit": limit,
                    }
                    task_read = start_data_prep_task(
                        scope=normalized,
                        trigger_fast_scan_after_ready=True,
                        fast_scan_params=fast_scan_params,
                    )
                    data_prep_task_id = task_read.id
            except Exception as e:
                logger.warning("auto start data_prep failed for scope=%s: %s", normalized, e)
                # 启动失败不影响主响应，用户可手动触发

            # 3. 构造响应
            if historical_snapshot is not None:
                # 返回上一历史快照（只读查看旧候选）
                timings.degraded_reason = "using_stale_snapshot"
                return {
                    "snapshot_id": historical_snapshot.id,
                    "snapshot_generated_at": historical_snapshot.generated_at,
                    "scope": normalized,
                    "total_in_snapshot": historical_snapshot.symbol_count or 0,
                    "coarse_match_count": 0,
                    "advanced_match_count": 0,
                    "result_rows_written": 0,
                    "cache_key": None,
                    "cache_hit": False,
                    "results": [],
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    "degraded_reason": "using_stale_snapshot",
                    "recommended_action": "正在使用历史快照（数据可能过期），已自动启动数据准备任务，完成后将自动刷新",
                    "filter_stats": {"coarse": {}, "advanced": {}, "portfolio": {}},
                    "cached_from_scan_run_id": None,
                    "cached_at": None,
                    "timings": timings.to_dict(),
                    "status": "ok",
                    "exceeded_stages": None,
                    "data_prep_task_id": data_prep_task_id,
                    "data_cutoff_at": historical_snapshot.data_cutoff_at.isoformat() if historical_snapshot.data_cutoff_at else None,
                }
            # 4. 首次使用无任何历史快照
            timings.degraded_reason = "no_ready_snapshot"
            return {
                "snapshot_id": None,
                "snapshot_generated_at": None,
                "scope": normalized,
                "total_in_snapshot": 0,
                "coarse_match_count": 0,
                "advanced_match_count": 0,
                "result_rows_written": 0,
                "cache_key": None,
                "cache_hit": False,
                "results": [],
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "degraded_reason": "no_ready_snapshot",
                "recommended_action": "正在为您准备数据，完成后将自动扫描",
                "filter_stats": {"coarse": {}, "advanced": {}, "portfolio": {}},
                "cached_from_scan_run_id": None,
                "cached_at": None,
                "timings": timings.to_dict(),
                "status": "ok",
                "exceeded_stages": None,
                "data_prep_task_id": data_prep_task_id,
            }
        timings.finish_stage(
            "snapshot_health_check", item_count=snapshot.symbol_count or 0
        )

        # 自定义指标数量限制（WP-P.5 完整实现，WP-P.4 先校验）
        if indicator_plan:
            indicator_count = _count_indicators(indicator_plan)
            if indicator_count > MAX_CUSTOM_INDICATORS:
                timings.degraded_reason = "too_many_custom_indicators"
                return {
                    "snapshot_id": snapshot.id,
                    "snapshot_generated_at": snapshot.generated_at,
                    "scope": normalized,
                    "total_in_snapshot": snapshot.symbol_count or 0,
                    "coarse_match_count": 0,
                    "advanced_match_count": 0,
                    "result_rows_written": 0,
                    "cache_key": None,
                    "cache_hit": False,
                    "results": [],
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    "degraded_reason": "too_many_custom_indicators",
                    "recommended_action": (
                        f"自定义指标数量超过上限 {MAX_CUSTOM_INDICATORS}，"
                        "请减少指标或将其转为后台预计算"
                    ),
                    "filter_stats": {"coarse": {}, "advanced": {}, "portfolio": {}},
                    "cached_from_scan_run_id": None,
                    "cached_at": None,
                    "timings": timings.to_dict(),
                    "status": "ok",
                    "exceeded_stages": None,
                }

        # 2. 构建缓存键（WP-P.6 完整实现，WP-P.4 先计算）
        cache_key = _build_cache_key(
            snapshot_id=snapshot.id,
            scope=scope,
            min_score=min_score,
            asset_types=asset_types,
            stages=stages,
            actions=actions,
            indicator_plan=indicator_plan,
            portfolio_id=portfolio_id,
            portfolio_rule_id=portfolio_rule_id,
            limit=limit,
        )

        # 3. WP-P.6 缓存命中检测：相同 cache_key + snapshot_id 直接复用已有结果
        cached_run = _find_cached_scan_run(
            db, cache_key=cache_key, snapshot_id=snapshot.id
        )
        if cached_run is not None:
            cached_results, _cache_summary = _reuse_cached_results(db, cached_run)
            # 缓存命中也记录 finalize 阶段计时（用于可观测性）
            timings.add_stage("finalize_audit")
            timings.finish_stage("finalize_audit")
            timings.check_total_exceeded()
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            # HTTP 守门再次校验
            _assert_no_http_request()
            # 持久化 timings（如果提供了 task_id）
            if task_id:
                _persist_timings_to_task(task_id, timings)
            return {
                "snapshot_id": snapshot.id,
                "snapshot_generated_at": snapshot.generated_at,
                "scope": normalized,
                "total_in_snapshot": cached_run.total_in_snapshot or 0,
                "coarse_match_count": cached_run.coarse_match_count or 0,
                "advanced_match_count": cached_run.advanced_match_count or 0,
                "result_rows_written": 0,  # 命中缓存不重复写
                "cache_key": cache_key,
                "cache_hit": True,  # WP-P.6 关键：返回 True
                "results": cached_results,
                "duration_ms": duration_ms,
                "degraded_reason": cached_run.degraded_reason,
                "recommended_action": None,
                "filter_stats": None,  # 命中缓存无需重新过滤
                "cached_from_scan_run_id": cached_run.id,
                "cached_at": cached_run.created_at,
                "timings": timings.to_dict(),
                "status": "ok",
                "exceeded_stages": (
                    [s.to_dict() for s in timings.get_exceeded_stages()]
                    if timings.get_exceeded_stages()
                    else None
                ),
            }

        # 4. SQL 粗筛和排序（budget 30s）
        check_cancel_token(cancel_token)
        timings.add_stage("sql_coarse_filter")
        coarse_items, coarse_stats = _coarse_filter(
            db,
            snapshot,
            min_score=min_score,
            asset_types=asset_types,
            stages=stages,
            actions=actions,
            min_data_credibility=min_data_credibility,
            limit=limit,
        )
        timings.finish_stage("sql_coarse_filter", item_count=len(coarse_items))

        # 5. Top-K 高级指标过滤（budget 90s，仅有指标时计入计时）
        if indicator_plan and _count_indicators(indicator_plan) > 0:
            check_cancel_token(cancel_token)
            timings.add_stage("advanced_indicator")
            advanced_items, advanced_stats = _advanced_filter(
                coarse_items,
                indicator_plan=indicator_plan,
                db=db,
                snapshot=snapshot,
            )
            timings.finish_stage("advanced_indicator", item_count=len(advanced_items))
        else:
            # 无 indicator_plan 时仍调用以获取 advanced_stats，但不计入计时
            advanced_items, advanced_stats = _advanced_filter(
                coarse_items,
                indicator_plan=indicator_plan,
                db=db,
                snapshot=snapshot,
            )

        # 6. 组合约束过滤（budget 60s，仅有 portfolio_id 时计入计时）
        if portfolio_id is not None:
            check_cancel_token(cancel_token)
            timings.add_stage("portfolio_filter")
            final_items, portfolio_stats = _portfolio_filter(
                advanced_items,
                portfolio_id=portfolio_id,
                portfolio_rule_id=portfolio_rule_id,
                db=db,
            )
            timings.finish_stage("portfolio_filter", item_count=len(final_items))
        else:
            # 无 portfolio_id 时仍调用以获取 portfolio_stats，但不计入计时
            final_items, portfolio_stats = _portfolio_filter(
                advanced_items,
                portfolio_id=portfolio_id,
                portfolio_rule_id=portfolio_rule_id,
                db=db,
            )

        # 7. 候选快照与摘要写入（budget 60s）
        check_cancel_token(cancel_token)
        timings.add_stage("result_persistence")
        result_rows_written = _persist_scan_results(
            db,
            final_items=final_items,
            snapshot=snapshot,
            scope=normalized,
            min_score=min_score,
            portfolio_id=portfolio_id,
            portfolio_rule_id=portfolio_rule_id,
            cache_key=cache_key,
            coarse_stats=coarse_stats,
            portfolio_stats=portfolio_stats,
            degraded_reason=advanced_stats.get("degraded_reason"),
        )
        timings.finish_stage("result_persistence", item_count=result_rows_written)

        # 8. 收尾审计（budget 30s）：序列化 + HTTP 守门 + 总预算检查
        timings.add_stage("finalize_audit")
        # 批量加载 symbol 信息（用于序列化）
        symbol_ids = [item.symbol_id for item in final_items if item.symbol_id is not None]
        symbol_map: dict[int, Symbol] = {}
        if symbol_ids:
            sym_rows = db.execute(
                select(Symbol).where(Symbol.id.in_(symbol_ids))
            ).scalars().all()
            symbol_map = {s.id: s for s in sym_rows}

        # 序列化结果
        results = [
            _serialize_item(item, symbol=symbol_map.get(item.symbol_id))
            for item in final_items
        ]

        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

        # HTTP 守门再次校验（防止 _persist_scan_results 内部意外触发网络请求）
        _assert_no_http_request()
        timings.finish_stage("finalize_audit")

        # 检查总预算是否超时
        total_exceeded = timings.check_total_exceeded()
        exceeded_stages = timings.get_exceeded_stages()
        if total_exceeded and enforce_budget:
            raise ScanBudgetExceededError(
                f"Scan total budget exceeded: "
                f"{timings.total_duration_ms:.0f}ms > "
                f"{timings.total_budget_ms:.0f}ms",
                timings=timings,
                exceeded_stages=exceeded_stages,
            )

        # 阶段超时降级（enforce_budget=False 或总预算未超时但有阶段超时）
        # 若 advanced_stats 已有 degraded_reason 则保留，否则用 "budget_exceeded"
        final_degraded_reason = advanced_stats.get("degraded_reason")
        if not final_degraded_reason and exceeded_stages:
            final_degraded_reason = "budget_exceeded"
            timings.degraded_reason = "budget_exceeded"

        # 持久化 timings（如果提供了 task_id）
        if task_id:
            _persist_timings_to_task(task_id, timings)

        return {
            "snapshot_id": snapshot.id,
            "snapshot_generated_at": snapshot.generated_at,
            "scope": normalized,
            "total_in_snapshot": coarse_stats.get("total_in_snapshot", 0),
            "coarse_match_count": len(coarse_items),
            "advanced_match_count": portfolio_stats.get("advanced_match_count", len(final_items)),
            "result_rows_written": result_rows_written,
            "cache_key": cache_key,
            "cache_hit": False,
            "results": results,
            "duration_ms": duration_ms,
            "degraded_reason": final_degraded_reason,
            "recommended_action": None,
            "filter_stats": {
                "coarse": coarse_stats,
                "advanced": advanced_stats,
                "portfolio": portfolio_stats,
            },
            "cached_from_scan_run_id": None,
            "cached_at": None,
            "timings": timings.to_dict(),
            "status": "ok",
            "exceeded_stages": (
                [s.to_dict() for s in exceeded_stages]
                if exceeded_stages
                else None
            ),
        }
    except ScanCancelledError:
        # 用户取消：返回 cancelled 状态，timings 保留已完成的阶段
        logger.info(
            "Fast scan cancelled by user (scope=%s, stages_completed=%d)",
            normalized,
            len(timings.stages),
        )
        timings.degraded_reason = "cancelled_by_user"
        # 持久化 timings（即使取消也保留可观测性，供前端展示具体慢在哪一步）
        if task_id:
            _persist_timings_to_task(task_id, timings, fast_scan_status="cancelled")
        return {
            "snapshot_id": None,
            "snapshot_generated_at": None,
            "scope": normalized,
            "total_in_snapshot": 0,
            "coarse_match_count": 0,
            "advanced_match_count": 0,
            "result_rows_written": 0,
            "cache_key": None,
            "cache_hit": False,
            "results": [],
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "degraded_reason": "cancelled_by_user",
            "recommended_action": "扫描已取消，可重新发起",
            "filter_stats": {"coarse": {}, "advanced": {}, "portfolio": {}},
            "cached_from_scan_run_id": None,
            "cached_at": None,
            "timings": timings.to_dict(),
            "status": "cancelled",
            "exceeded_stages": None,
        }
    except ScanBudgetExceededError as exc:
        # 总预算超时：返回 timeout 状态，timings 保留所有阶段
        logger.warning(
            "Fast scan budget exceeded (scope=%s): %s; exceeded_stages=%s",
            normalized,
            exc,
            [s.stage for s in exc.exceeded_stages],
        )
        timings.degraded_reason = "budget_exceeded"
        # 持久化 timings（超时也保留可观测性，供前端展示具体慢在哪一步）
        if task_id:
            _persist_timings_to_task(task_id, timings, fast_scan_status="timeout")
        return {
            "snapshot_id": None,
            "snapshot_generated_at": None,
            "scope": normalized,
            "total_in_snapshot": 0,
            "coarse_match_count": 0,
            "advanced_match_count": 0,
            "result_rows_written": 0,
            "cache_key": None,
            "cache_hit": False,
            "results": [],
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "degraded_reason": "budget_exceeded",
            "recommended_action": "扫描超时，请缩小范围或优化指标",
            "filter_stats": {"coarse": {}, "advanced": {}, "portfolio": {}},
            "cached_from_scan_run_id": None,
            "cached_at": None,
            "timings": timings.to_dict(),
            "status": "timeout",
            "exceeded_stages": [s.to_dict() for s in exc.exceeded_stages],
        }
    finally:
        if own_session:
            db.close()


def _persist_scan_results(
    db: Session,
    *,
    final_items: list[DiscoveryScoreSnapshotItem],
    snapshot: DiscoveryScoreSnapshot,
    scope: str,
    min_score: float,
    portfolio_id: int | None,
    portfolio_rule_id: int | None,
    cache_key: str | None = None,
    coarse_stats: dict | None = None,
    portfolio_stats: dict | None = None,
    degraded_reason: str | None = None,
) -> int:
    """保存小规模候选结果到 ScanResult + 写入 ScanRun 摘要（WP-P.6 改造）。

    与 scans.run_scan 的差异：
    - 不调用 calculate_symbol_score（评分已在快照物化）
    - 不调用 upsert_trade_setup（不在用户扫描阶段生成交易计划）
    - 只物化最终候选集（Top-K），不为 5500 只标的各写三类结果
    - ScanRun 写入 cache_key / snapshot_id / 摘要统计字段，供 WP-P.6 缓存复用

    Top-K 限制：
    - final_items 已由 _coarse_filter(limit=300) 限制，此处再防御性截断至 DEFAULT_TOP_K
    - ScanResult 写入数量严格 <= DEFAULT_TOP_K (300)

    返回写入的 ScanResult 数量。
    """
    # 摘要统计（兼容 coarse_stats / portfolio_stats 缺失场景）
    total_in_snapshot = int((coarse_stats or {}).get("total_in_snapshot", 0))
    coarse_match_count = int((coarse_stats or {}).get("coarse_match_count", len(final_items)))
    advanced_match_count = int((portfolio_stats or {}).get(
        "advanced_match_count", len(final_items)
    ))

    # Top-K 防御性截断：保证 ScanResult 数量严格受 DEFAULT_TOP_K 限制
    top_k_items = list(final_items[:DEFAULT_TOP_K])

    if not top_k_items:
        # 仍写一条 ScanRun 摘要，记录本次扫描无候选
        scan_run = ScanRun(
            run_name=f"fast-scan-{scope}-{snapshot.id}",
            scope_snapshot=json.dumps({
                "snapshot_id": snapshot.id,
                "scope": scope,
                "source": "discovery_fast_scan",
            }),
            filters_snapshot=json.dumps({
                "min_score": min_score,
                "portfolio_id": portfolio_id,
                "portfolio_rule_id": portfolio_rule_id,
            }),
            portfolio_id=portfolio_id,
            portfolio_rule_id=portfolio_rule_id,
            status="done",
            started_at=_now(),
            finished_at=_now(),
            # WP-P.6 新增字段
            snapshot_id=snapshot.id,
            cache_key=cache_key,
            cache_hit=0,
            total_in_snapshot=total_in_snapshot,
            coarse_match_count=coarse_match_count,
            advanced_match_count=advanced_match_count,
            result_rows_written=0,
            degraded_reason=degraded_reason,
        )
        db.add(scan_run)
        db.commit()
        return 0

    # 写入 ScanRun 摘要
    scan_run = ScanRun(
        run_name=f"fast-scan-{scope}-{snapshot.id}",
        scope_snapshot=json.dumps({
            "snapshot_id": snapshot.id,
            "scope": scope,
            "source": "discovery_fast_scan",
        }),
        filters_snapshot=json.dumps({
            "min_score": min_score,
            "portfolio_id": portfolio_id,
            "portfolio_rule_id": portfolio_rule_id,
        }),
        portfolio_id=portfolio_id,
        portfolio_rule_id=portfolio_rule_id,
        status="done",
        started_at=_now(),
        finished_at=_now(),
        # WP-P.6 新增字段
        snapshot_id=snapshot.id,
        cache_key=cache_key,
        cache_hit=0,
        total_in_snapshot=total_in_snapshot,
        coarse_match_count=coarse_match_count,
        advanced_match_count=advanced_match_count,
        result_rows_written=0,  # 占位，提交前更新为实际写入数
        degraded_reason=degraded_reason,
    )
    db.add(scan_run)
    db.flush()  # 获取 scan_run.id

    # 写入 ScanResult（每条 top_k_item 一条记录，result_type=executable）
    # 不为所有标的写三类（Quality / Timing / executable）结果，只写最终 executable 候选
    now = _now()
    written = 0
    for rank_no, item in enumerate(top_k_items, start=1):
        if item.symbol_id is None:
            continue
        result = ScanResult(
            scan_run_id=scan_run.id,
            symbol_id=item.symbol_id,
            result_type="executable",
            rank_no=rank_no,
            quality_score=item.quality_score,
            timing_score=item.timing_score,
            priority_score=item.priority_score,
            stage=item.stage,
            action=item.action,
            is_frozen=0,
            created_at=now,
        )
        db.add(result)
        written += 1

    # 回填实际写入数到 ScanRun 摘要
    scan_run.result_rows_written = written
    db.commit()
    return written


def _persist_timings_to_task(
    task_id: str | None,
    timings: ScanTimings,
    *,
    fast_scan_status: str = "ok",
) -> None:
    """将 timings 持久化到 AsyncTaskRecord（WP-P.8）。

    若 fast_scan 由异步任务驱动（task_state_machine 跟踪），传入 task_id 可将
    阶段计时写入 AsyncTaskRecord.result_json，供 /discovery/snapshot/status
    API 后续查询最近一次 fast_scan 的 timings。

    AsyncTaskRecord 没有独立的 stage_durations_json 列，故将 timings 合并到
    result_json 中（key="timings"）。若 result_json 已有内容则保留其它字段。

    Args:
        task_id: AsyncTaskRecord.id；为 None 时直接返回（无操作）
        timings: 已收集完成的 ScanTimings 对象
        fast_scan_status: fast_scan 结果状态（"ok"|"cancelled"|"timeout"）；
            与 AsyncTaskRecord.status（queued/running/done/failed/cancelled）
            解耦，专门供 /discovery/snapshot/status 接口展示
    """
    if not task_id:
        return
    import json

    from app.db.session import get_session_local
    from app.models.async_task import AsyncTaskRecord
    from app.services.async_tasks import _json_loads, _set_task

    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        # 先读取现有 result_json，合并 timings 字段（保留其它已有字段）
        task = db.get(AsyncTaskRecord, task_id)
        existing_result = _json_loads(task.result_json if task else None, {}) or {}
        existing_result["timings"] = timings.to_dict()
        # fast_scan 实际结果状态（ok/cancelled/timeout），与 task.status 解耦
        existing_result["fast_scan_status"] = fast_scan_status
        _set_task(
            db,
            task_id,
            result_json=json.dumps(existing_result, ensure_ascii=False, default=str),
        )
    except Exception as exc:
        # 持久化失败不影响主流程（best-effort）
        logger.warning(
            "Failed to persist timings to task %s: %s",
            task_id,
            exc,
            exc_info=False,
        )
    finally:
        db.close()


__all__ = [
    "MAX_CUSTOM_INDICATORS",
    "DEFAULT_TOP_K",
    "TASK_TYPE_FAST_SCAN",
    "get_ready_snapshot",
    "run_fast_scan",
    "_assert_no_http_request",
    "_coarse_filter",
    "_advanced_filter",
    "_portfolio_filter",
    "_build_cache_key",
    "_find_cached_scan_run",
    "_reuse_cached_results",
    "_count_indicators",
    "_normalize_indicator_plan",
    "_persist_timings_to_task",
]
