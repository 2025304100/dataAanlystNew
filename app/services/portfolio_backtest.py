"""组合整体回测服务（P2-2 + WP7.1 + WP7.3）。

在已有 run_backtest 基础上，提供"组合级"回测入口：
- 自动构造与 auto_trade 信号逻辑一致的 rule_config（基于 Score.action）
- 复用 portfolio 的 active rule 限制仓位与持仓数

标的来源（WP7.1 新增功能开关路由）：
- 当 ``PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=false``（默认）：
  旧逻辑——从组合当前持仓 + 最新 scan 候选池推导 symbol_ids
- 当 ``PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=true``：
  新逻辑——按每个交易日读取当时有效成员（``effective_from <= trade_date
  AND (effective_to IS NULL OR effective_to >= trade_date)``）推导 symbol_ids

WP7.3 兼容与切换：
- 新来源回测时填充 WP7.2 快照字段（成员/规则/成本/引擎等），保证历史可复现
- 旧来源回测时仅填充最小快照集（engine_name/version/source_type）
- 成员资格校验：存在 manual/confirm 成员时默认阻止完整回测，提供 ``only_auto=True`` 选项跳过
- ``compare_new_old_engine`` 同时跑新旧两套来源并对比标的集/交易/指标
- 切换功能开关后历史 BacktestRun 仍可读（快照已落盘）

前提：组合 auto_trade_enabled=1（组内标的标签都是自动下单/程序）。
否则回测的信号源（Score.action）与实际执行逻辑不一致，结果无意义。

设计要点：
- 不重新实现回测引擎，直接调用 backtest.run_backtest
- rule_config 使用 v1 格式（buy_conditions.actions / sell_conditions.score_actions）
- position_config 从 PortfolioRule 映射（max_single_position_pct → value, max_open_positions → max_positions）
- initial_capital 沿用 run_backtest 默认行为（portfolio.total_capital），保持与单标的回测一致
- 新旧标的来源通过同一入口函数路由，避免代码分裂
"""
from __future__ import annotations

import bisect
import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.backtest import BacktestRun, BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.portfolio_candidate import PortfolioCandidate
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    PortfolioMember,
    STATUS_ACTIVE,
)
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.allocation import get_active_rule
from app.services import simulation_matching_engine as sme
from app.services.backtest import build_backtest_detail_context, run_backtest
from app.services.index_data import list_index_prices
from app.services.signal_rules import get_active_signal_rule
from app.services.portfolio_backtest_snapshot import (
    build_cost_config_snapshot,
    build_excluded_members_snapshot,
    build_member_snapshot,
)
from app.services.portfolio_asset_scope import allows_asset_type, ensure_symbol_ids_in_scope

logger = logging.getLogger(__name__)

# Benchmark 名称 → 指数代码映射（前端下拉用中文名，后端查代码）
_BENCHMARK_NAME_TO_SYMBOL: dict[str, str] = {
    "沪深300": "000300",
    "中证500": "000905",
    "创业板指": "399006",
    "上证50": "000016",
    "科创50": "000688",
}

# 反向映射
_BENCHMARK_SYMBOL_TO_NAME: dict[str, str] = {v: k for k, v in _BENCHMARK_NAME_TO_SYMBOL.items()}

# 与 auto_trade_task 保持一致的信号集合
_BUY_ACTIONS = ["open", "buy_dip"]
_SELL_ACTIONS = ["exit", "reduce"]

# WP7.3：事件驱动引擎标识与版本（写入 BacktestRun.engine_name/engine_version）
# 用于历史回测可复现性审计，版本号变更时需同步更新此处
_ENGINE_NAME = "event_driven"
_ENGINE_VERSION = "1.0.0"

# 错误消息（后端固定中文）
_MSG_MANUAL_MEMBERS_BLOCK = (
    "存在 manual/confirm 成员，无法完整回测。"
    "请选择'仅回测自动成员'或调整成员执行模式"
)


def _derive_symbol_ids(db: Session, portfolio_id: int) -> list[int]:
    """从组合当前持仓 + 最新 scan executable 候选推导回测标的列表。

    逻辑：
    1. 当前持仓的 symbol_id（这些可能在回测期间被卖出）
    2. 最新一次成功 scan 的 executable 候选 symbol_id（这些可能在回测期间被买入）
    3. 去重后返回

    若两者都为空，抛 ValueError（无标的可回测）。
    """
    symbol_ids: set[int] = set()

    # 当前持仓
    positions = db.execute(
        select(Position).where(Position.portfolio_id == portfolio_id)
    ).scalars().all()
    for pos in positions:
        symbol_ids.add(pos.symbol_id)

    # 组合专属候选池。候选池已从全局扫描结果中独立出来，因此旧来源也必须
    # 显式读取它，避免“页面有候选、回测却判空”。
    portfolio_candidates = db.execute(
        select(PortfolioCandidate.symbol_id).where(
            PortfolioCandidate.portfolio_id == portfolio_id
        )
    ).scalars().all()
    symbol_ids.update(int(symbol_id) for symbol_id in portfolio_candidates)

    # 最新 scan 候选
    latest_run = db.execute(
        select(ScanRun)
        .where(ScanRun.portfolio_id == portfolio_id, ScanRun.status == "done")
        .order_by(desc(ScanRun.id))
        .limit(1)
    ).scalars().first()
    if latest_run is not None:
        candidates = db.execute(
            select(ScanResult).where(
                ScanResult.scan_run_id == latest_run.id,
                ScanResult.result_type == "executable",
            )
        ).scalars().all()
        for cand in candidates:
            symbol_ids.add(cand.symbol_id)

    if not symbol_ids:
        raise ValueError(
            f"Portfolio {portfolio_id} has no positions, portfolio candidates, or scan candidates. "
            "Cannot run whole-portfolio backtest (no positions and no scan candidates)."
        )

    return sorted(symbol_ids)


def _derive_current_universe_symbol_ids(
    db: Session,
    portfolio_id: int,
    *,
    only_auto: bool = False,
) -> list[int]:
    """Return the current portfolio universe used by the interactive UI.

    A current-configuration historical backtest applies today's portfolio setup
    to historical market data.  Membership/candidate creation timestamps must
    therefore not be compared with the historical date range.
    """
    symbol_ids: set[int] = set(
        db.execute(
            select(Position.symbol_id).where(Position.portfolio_id == portfolio_id)
        ).scalars().all()
    )
    symbol_ids.update(
        db.execute(
            select(PortfolioCandidate.symbol_id).where(
                PortfolioCandidate.portfolio_id == portfolio_id
            )
        ).scalars().all()
    )

    member_conditions = [
        PortfolioMember.portfolio_id == portfolio_id,
        PortfolioMember.status == STATUS_ACTIVE,
        PortfolioMember.effective_to.is_(None),
    ]
    if only_auto:
        member_conditions.append(PortfolioMember.execution_mode == EXECUTION_AUTO)
    symbol_ids.update(
        db.execute(
            select(PortfolioMember.symbol_id).where(and_(*member_conditions))
        ).scalars().all()
    )

    valid_ids = {int(symbol_id) for symbol_id in symbol_ids if int(symbol_id) > 0}
    if not valid_ids:
        raise ValueError(
            f"Portfolio {portfolio_id} has no current positions, portfolio candidates, "
            "or eligible members. Cannot run whole-portfolio backtest."
        )
    return sorted(valid_ids)


# ----------------------------------------------------------------------------
# WP7.1: 按有效日期读取成员
# ----------------------------------------------------------------------------


def get_effective_members_for_date(
    db: Session,
    portfolio_id: int,
    trade_date: datetime,
    only_auto: bool = False,
    exclude_member_ids: list[int] | None = None,
) -> list[PortfolioMember]:
    """读取指定日期当时有效的组合成员。

    条件（参照 spec WP7 line 259）：
    - portfolio_id 匹配
    - status = 'active'
    - effective_from <= trade_date
    - effective_to IS NULL OR effective_to >= trade_date
    - 若 only_auto=True，仅返回 execution_mode='auto'
    - 排除 exclude_member_ids 中的成员

    历史可复现性：
    - 未来才加入的成员（effective_from > trade_date）绝不出现
    - 中途归档的成员（effective_to < trade_date）不返回
    - 同一 trade_date 重复读取结果一致（除非数据被修改）
    """
    conditions = [
        PortfolioMember.portfolio_id == portfolio_id,
        PortfolioMember.status == STATUS_ACTIVE,
        PortfolioMember.effective_from <= trade_date,
        or_(
            PortfolioMember.effective_to.is_(None),
            PortfolioMember.effective_to >= trade_date,
        ),
    ]
    if only_auto:
        conditions.append(PortfolioMember.execution_mode == EXECUTION_AUTO)
    if exclude_member_ids:
        conditions.append(PortfolioMember.id.not_in(list(exclude_member_ids)))

    stmt = (
        select(PortfolioMember)
        .where(and_(*conditions))
        .order_by(PortfolioMember.id)
    )
    return list(db.execute(stmt).scalars().all())


def get_member_symbols_for_date(
    db: Session,
    portfolio_id: int,
    trade_date: datetime,
    only_auto: bool = False,
) -> list[int]:
    """返回指定日期当时有效成员的 symbol_id 列表（去重、排序）。

    等价于 ``[m.symbol_id for m in get_effective_members_for_date(...)]`` 去重。
    """
    members = get_effective_members_for_date(
        db,
        portfolio_id=portfolio_id,
        trade_date=trade_date,
        only_auto=only_auto,
    )
    symbol_ids: set[int] = {m.symbol_id for m in members}
    return sorted(symbol_ids)


def validate_member_eligibility(
    members: list[PortfolioMember],
) -> tuple[bool, list[dict[str, Any]]]:
    """校验成员资格：所有成员必须 execution_mode='auto' 且规则有效。

    规则有效指 entry_rule_version_id 不为空。

    Returns:
        (is_valid, excluded_members_with_reason)
        - is_valid: 全部通过时为 True
        - excluded_members_with_reason: 不通过成员的列表，每项形如
          ``{"member_id": int, "symbol_id": int, "reason": str}``
    """
    excluded: list[dict[str, Any]] = []
    for m in members:
        if m.execution_mode != EXECUTION_AUTO:
            excluded.append({
                "member_id": m.id,
                "symbol_id": m.symbol_id,
                "reason": f"execution_mode is {m.execution_mode!r}, expected 'auto'",
            })
            continue
        if m.entry_rule_version_id is None:
            excluded.append({
                "member_id": m.id,
                "symbol_id": m.symbol_id,
                "reason": "entry_rule_version_id is None",
            })
    return (len(excluded) == 0, excluded)


# ----------------------------------------------------------------------------
# WP7.1: 新标的来源逻辑（功能开关开启时使用）
# ----------------------------------------------------------------------------


def _derive_symbol_ids_from_members(
    db: Session,
    portfolio_id: int,
    start_date: date,
    end_date: date,
    *,
    only_auto: bool = False,
    exclude_member_ids: list[int] | None = None,
) -> list[int]:
    """按成员有效期构造历史标的集合（取并集）。

    概念上等价于：对 ``[start_date, end_date]`` 区间内每个交易日调用
    ``get_effective_members_for_date``，取所有返回成员的 symbol_id 并集。

    为提升性能，使用单次查询实现等价语义：
        effective_from <= end_date
        AND (effective_to IS NULL OR effective_to >= start_date)
        AND status = 'active'

    此查询满足：
    - 未来成员（effective_from > end_date）不出现
    - 回测窗口前已归档成员（effective_to < start_date）不出现

    注：现有 ``run_backtest`` 引擎接收静态 ``symbol_ids`` 列表，不支持按日动态
    调整标的集。因此窗口内中途归档的成员仍会出现在整个回测的标的集中；
    精确的按日过滤留待 WP7.5 引擎增强时实现。本函数仍然保证：
    - 未来成员（effective_from > end_date）绝不出现
    - 回测窗口前已归档成员（effective_to < start_date）不出现

    WP7.3 新增参数：
    - ``only_auto``：仅返回 execution_mode='auto' 的成员（用于跳过 manual/confirm）
    - ``exclude_member_ids``：显式排除的成员 ID 列表
    """
    # start_date 当日 00:00:00（含）/ end_date 当日 23:59:59.999999（含）
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    conditions = [
        PortfolioMember.portfolio_id == portfolio_id,
        PortfolioMember.status == STATUS_ACTIVE,
        PortfolioMember.effective_from <= end_dt,
        or_(
            PortfolioMember.effective_to.is_(None),
            PortfolioMember.effective_to >= start_dt,
        ),
    ]
    if only_auto:
        conditions.append(PortfolioMember.execution_mode == EXECUTION_AUTO)
    if exclude_member_ids:
        conditions.append(PortfolioMember.id.not_in(list(exclude_member_ids)))

    stmt = (
        select(PortfolioMember.symbol_id)
        .where(and_(*conditions))
        .distinct()
        .order_by(PortfolioMember.symbol_id)
    )
    return list(db.execute(stmt).scalars().all())


def _get_effective_members_for_window(
    db: Session,
    portfolio_id: int,
    start_date: date,
    end_date: date,
    *,
    only_auto: bool = False,
    exclude_member_ids: list[int] | None = None,
) -> list[PortfolioMember]:
    """获取回测窗口内所有有效成员（PortfolioMember 对象，用于快照构建）。

    查询条件与 ``_derive_symbol_ids_from_members`` 对齐，但返回完整对象
    而非仅 symbol_id，以便 ``build_member_snapshot`` 提取每个成员的
    规则版本、执行模式等属性。
    """
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    conditions = [
        PortfolioMember.portfolio_id == portfolio_id,
        PortfolioMember.status == STATUS_ACTIVE,
        PortfolioMember.effective_from <= end_dt,
        or_(
            PortfolioMember.effective_to.is_(None),
            PortfolioMember.effective_to >= start_dt,
        ),
    ]
    if only_auto:
        conditions.append(PortfolioMember.execution_mode == EXECUTION_AUTO)
    if exclude_member_ids:
        conditions.append(PortfolioMember.id.not_in(list(exclude_member_ids)))

    stmt = (
        select(PortfolioMember)
        .where(and_(*conditions))
        .order_by(PortfolioMember.id)
    )
    return list(db.execute(stmt).scalars().all())


def _resolve_symbol_ids(
    db: Session,
    portfolio_id: int,
    start_date: date,
    end_date: date,
    *,
    only_auto: bool = False,
    exclude_member_ids: list[int] | None = None,
    current_universe: bool = False,
) -> tuple[list[int], str]:
    """根据功能开关路由新旧标的来源逻辑。

    Args:
        only_auto: 仅在 member 来源生效，True 时只取 execution_mode='auto' 成员
        exclude_member_ids: 仅在 member 来源生效，显式排除的成员 ID 列表

    Returns:
        (symbol_ids, source_label)
        - source_label: ``"legacy"``（持仓 + 最新扫描）或 ``"members"``（历史成员），
          用于回测元数据与 UI 显示。

    Raises:
        ValueError: 新逻辑下回测窗口内无任何有效成员。
    """
    if current_universe:
        return _derive_current_universe_symbol_ids(
            db, portfolio_id, only_auto=only_auto
        ), "members"
    if settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED:
        symbol_ids = _derive_symbol_ids_from_members(
            db, portfolio_id, start_date, end_date,
            only_auto=only_auto, exclude_member_ids=exclude_member_ids,
        )
        if not symbol_ids:
            raise ValueError(
                f"Portfolio {portfolio_id} has no effective members in "
                f"[{start_date.isoformat()} ~ {end_date.isoformat()}]. "
                "Cannot run whole-portfolio backtest with member source."
            )
        return symbol_ids, "members"
    symbol_ids = _derive_symbol_ids(db, portfolio_id)
    return symbol_ids, "legacy"


def _build_rule_config(
    db: Session | PortfolioRule | None = None,
    portfolio_id: int | None = None,
    rule: PortfolioRule | None = None,
) -> dict[str, Any]:
    """从 PortfolioRule + SignalRule 构造与 auto_trade 信号逻辑一致的 v1 rule_config。

    信号逻辑（与 auto_trade_task 对齐）：
    - 买入：Score.action ∈ {open, buy_dip}
    - 卖出：Score.action ∈ {exit, reduce}

    仓位限制（从 PortfolioRule 映射）：
    - position_config.value = max_single_position_pct（每标的最大仓位百分比）
    - position_config.max_positions = max_open_positions（最大持仓数）
    - sell_conditions.stop_loss_pct = max_loss_per_trade_pct（个股止损）

    质量/择时阈值（从 SignalRule 映射，避免"策略规则页配置了，但回测没生效"）：
    - buy_conditions.quality_score_min = clamp(SignalRule.quality_score_min, 0~100)
    - buy_conditions.timing_score_min  = clamp(SignalRule.timing_score_min,  0~100)

    stage_limits_json 可选：若有 factors / stock_pool / weighting 则透传，供引擎与 UI 审计。
    """
    # Compatibility for legacy white-box callers that passed only ``rule`` or
    # ``None``. Production callers still pass (db, portfolio_id, rule).
    if rule is None and (portfolio_id is None or not isinstance(portfolio_id, int)):
        if db is not None and not isinstance(db, Session):
            rule = db  # type: ignore[assignment]
        db = None
        portfolio_id = None

    stage_limits: dict[str, Any] | None = None
    if rule is None:
        max_single = 0.1
        max_positions = 5
        stop_loss = 0.08
    else:
        max_single = float(rule.max_single_position_pct or 0.1)
        max_positions = int(rule.max_open_positions or 5)
        max_loss = getattr(rule, "max_loss_per_trade_pct", None)
        stop_loss = float(max_loss or 0.08) if max_loss else 0.08
        try:
            if getattr(rule, "stage_limits_json", None):
                stage_limits = (
                    json.loads(getattr(rule, "stage_limits_json"))
                    if isinstance(getattr(rule, "stage_limits_json"), str)
                    else dict(getattr(rule, "stage_limits_json"))
                )
        except (TypeError, json.JSONDecodeError):
            stage_limits = None

    # 映射 SignalRule：失败/缺失时不抛，兜底默认
    try:
        sig = get_active_signal_rule(db, portfolio_id) if db is not None and portfolio_id is not None else None
        q_min = int(getattr(sig, "quality_tolerance", 0) or 0)
        t_min = int(getattr(sig, "timing_tolerance", 0) or 0)
        # quality_tolerance 语义是"与最佳样本的评分容忍差值"，回测语义是"最低准入分"，
        # 保守映射：min_score = clamp(80 - tolerance, 10, 80)
        quality_score_min = max(10, min(80, 80 - q_min))
        timing_score_min = max(10, min(80, 80 - t_min))
    except Exception:  # noqa: BLE001
        quality_score_min = 50
        timing_score_min = 60

    rule_cfg: dict[str, Any] = {
        "buy_conditions": {
            "actions": _BUY_ACTIONS,
            "quality_score_min": quality_score_min,
            "timing_score_min": timing_score_min,
        },
        "sell_conditions": {
            "score_actions": _SELL_ACTIONS,
            "stop_loss_pct": stop_loss,
        },
        "position_config": {
            "type": "fixed_pct",
            "value": max_single,
            "max_positions": max_positions,
        },
        "execution_config": {
            "entry_timing": "signal_close",
            "exit_timing": "signal_close",
        },
    }
    if stage_limits:
        rule_cfg["stage_limits"] = stage_limits
    return rule_cfg


def _filter_symbol_ids_by_rule(
    db: Session,
    portfolio_id: int,
    rule: PortfolioRule | None,
    symbol_ids: list[int],
    *,
    as_of_date: date | None = None,
) -> list[int]:
    """把 PortfolioRule.stage_limits_json (stock_pool / factors) 与
    SignalRule.quality_tolerance/timing_tolerance 应用到回测标的池，
    保持与 auto_trade_task 买入侧筛选语义一致。

    - stock_pool：启发式按 Symbol.market / Symbol.symbol 前缀过滤
    - quality/timing 准入分：基于 PIT 截止前最新 Score 过滤（缺失时不粗暴剔除，避免空池）
    - factors：基于因子加权重排序，避免"随机取 N"时因子完全不起作用
    - as_of_date：C-08 PIT 截止，仅使用 trade_date <= as_of_date 的 Score。
      None 表示不限（仅研究/空窗口兼容，生产链路强制传值）。
    """
    if not symbol_ids:
        return symbol_ids

    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        return []

    # 1. 解析 PortfolioRule stage_limits（stock_pool + factors）
    stage_obj: dict[str, Any] = {}
    if rule is not None and getattr(rule, "stage_limits_json", None):
        try:
            stage_obj = (
                json.loads(rule.stage_limits_json)
                if isinstance(rule.stage_limits_json, str)
                else dict(rule.stage_limits_json)
            )
        except (TypeError, json.JSONDecodeError):
            stage_obj = {}

    stock_pool: str = str(stage_obj.get("stock_pool") or "全A")
    factors_cfg: list[dict[str, Any]] | None = None
    if isinstance(stage_obj.get("factors"), list):
        factors_cfg = [f for f in stage_obj["factors"] if isinstance(f, dict)]
    factor_model_run_id: str | None = stage_obj.get("factor_model_run_id") or None
    if factor_model_run_id:
        factor_model_run_id = str(factor_model_run_id).strip() or None

    # 2. SignalRule → 质量/择时准入分（与 _build_rule_config、auto_trade_task 同源）
    quality_score_min: float = 0.0
    timing_score_min: float = 0.0
    try:
        sig = get_active_signal_rule(db, portfolio_id)
        q_tol = int(getattr(sig, "quality_tolerance", 0) or 0)
        t_tol = int(getattr(sig, "timing_tolerance", 0) or 0)
        quality_score_min = float(max(10, min(80, 80 - q_tol)))
        timing_score_min = float(max(10, min(80, 80 - t_tol)))
    except Exception:  # noqa: BLE001
        quality_score_min = 0.0
        timing_score_min = 0.0

    def _quality_timing_check(sc: Score | None) -> bool:
        if sc is None:
            return True  # 缺 Score 不粗暴剔除，避免空回测池
        # 绑定 validated FactorModelRun：优先用 factor_quality/timing_score
        if factor_model_run_id and getattr(sc, "factor_model_run_id", None) == factor_model_run_id:
            fq = getattr(sc, "factor_quality_score", None)
            ft = getattr(sc, "factor_timing_score", None)
            if isinstance(fq, (int, float)) and math.isfinite(float(fq)) and float(fq) < quality_score_min:
                return False
            if isinstance(ft, (int, float)) and math.isfinite(float(ft)) and float(ft) < timing_score_min:
                return False
            return True
        # 回退 manual/ridge 混合分
        if getattr(sc, "quality_score", None) is not None and float(sc.quality_score) < quality_score_min:
            return False
        if getattr(sc, "timing_score", None) is not None and float(sc.timing_score) < timing_score_min:
            return False
        return True

    def _candidate_weighted_score(sc: Score | None) -> float:
        if sc is None:
            return 0.0
        # 绑定了 validated FactorModelRun 且本 Score 是该模型产出 → 直接用 model_alpha_score
        if factor_model_run_id and getattr(sc, "factor_model_run_id", None) == factor_model_run_id:
            ma = getattr(sc, "model_alpha_score", None)
            if isinstance(ma, (int, float)) and math.isfinite(float(ma)):
                return float(ma)
        base = float(getattr(sc, "priority_score", 0) or 0)
        if not factors_cfg:
            return base
        acc = 0.0
        total_w = 0.0
        for f in factors_cfg:
            if not bool(f.get("active", True)):
                continue
            w = float(f.get("weight") or 0)
            if w <= 0:
                continue
            fname = str(f.get("name") or "")
            v: float | None = None
            if fname == "质量":
                v = getattr(sc, "quality_score", None)
            elif fname == "动量":
                v = getattr(sc, "momentum_score", None) or getattr(sc, "trend_score", None)
            elif fname == "低波":
                vol = getattr(sc, "volatility_score", None)
                if isinstance(vol, (int, float)) and 0 <= float(vol) <= 100:
                    v = 100.0 - float(vol)
            elif fname == "价值":
                val = getattr(sc, "factor_quality_score", None)
                if isinstance(val, (int, float)):
                    v = float(val)
                else:
                    v = getattr(sc, "breadth_score", None) or getattr(sc, "trend_score", None)
            elif fname == "成长":
                v = (
                    getattr(sc, "pullback_score", None)
                    or getattr(sc, "event_score", None)
                    or getattr(sc, "trend_score", None)
                )
            elif fname == "规模":
                v = getattr(sc, "liquidity_score", None) or getattr(sc, "breadth_score", None)
            if v is None:
                v = getattr(sc, "quality_score", None) or getattr(sc, "priority_score", None)
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                acc += float(v) * w
                total_w += w
        if total_w > 0:
            return acc / total_w
        return base

    # WP0-5 / C-08 合规：选股池不再使用"沪深300/中证500"指数代码前缀的近似伪池。
    # 证券范围严格使用三段统一口径：
    #   (1) 买入候选 = PortfolioCandidate(pid, as_of_date, auto_authorized=True, removed_manually=False)
    #   (2) 卖出/风控 = 当前持仓 holdings.quantity>0
    #   (3) 组合证券范围 = 候选 ∪ 持仓
    # 股票指数成员筛选已在 PortfolioCandidate 入口层显式做过，回测链路不得重复做、
    # 不得用 symbol.symbol 前缀做"属于某指数"的启发式判断。
    def _pass_stock_pool(sym: Symbol) -> bool:  # pragma: no cover - 保持兼容，不会再被调用
        if sym.asset_type == "etf":
            return True
        # C-08a: 任何非 ETF 标的默认通过；真排除由 PortfolioCandidate 交集保证（外层）
        return True

    # 3. 逐条 symbol_id 拉取 Symbol + Score，执行过滤 + 加权排序
    ranked: list[tuple[int, float]] = []
    # C-08 PIT 截止：仅取 trade_date <= as_of_date 的 Score；
    # 若某个 symbol 无历史 Score，仍在 as_of_date 之后则视为缺 Score（fail-soft 进入保留逻辑）
    score_base_where = [Score.symbol_id.in_(symbol_ids)]
    if as_of_date is not None:
        score_base_where.append(Score.trade_date <= as_of_date)
    # 取每个 symbol_id 最新 Score（PIT 限制内）
    latest_date_subq = (
        select(
            Score.symbol_id.label("symbol_id"),
            func.max(Score.trade_date).label("max_date"),
        )
        .where(and_(*score_base_where))
        .group_by(Score.symbol_id)
        .subquery()
    )
    stmt = (
        select(Score)
        .join(
            latest_date_subq,
            and_(
                Score.symbol_id == latest_date_subq.c.symbol_id,
                Score.trade_date == latest_date_subq.c.max_date,
            ),
        )
    )
    latest_scores: dict[int, Score] = {}
    for sc in db.execute(stmt).scalars().all():
        cur = latest_scores.get(sc.symbol_id)
        if cur is None:
            latest_scores[sc.symbol_id] = sc
            continue
        # 如果绑定了 factor_model_run_id，优先取"该模型产出"的 Score（优先级比 priority_score 更高）
        if factor_model_run_id:
            cur_match = bool(getattr(cur, "factor_model_run_id", None) == factor_model_run_id)
            new_match = bool(getattr(sc, "factor_model_run_id", None) == factor_model_run_id)
            if new_match and not cur_match:
                latest_scores[sc.symbol_id] = sc
                continue
            if new_match == cur_match:
                # 同匹配度：priority_score 大者优先
                if float(getattr(sc, "priority_score", 0) or 0) > float(
                    getattr(cur, "priority_score", 0) or 0
                ):
                    latest_scores[sc.symbol_id] = sc
        else:
            if float(getattr(sc, "priority_score", 0) or 0) > float(
                getattr(cur, "priority_score", 0) or 0
            ):
                latest_scores[sc.symbol_id] = sc

    for sid in symbol_ids:
        sym = db.get(Symbol, sid)
        if sym is None:
            continue
        if not allows_asset_type(portfolio.asset_scope, sym.asset_type):
            continue
        if not _pass_stock_pool(sym):
            continue
        sc = latest_scores.get(sid)
        if not _quality_timing_check(sc):
            continue
        weighted = _candidate_weighted_score(sc)
        ranked.append((sid, weighted))

    ranked.sort(key=lambda t: t[1], reverse=True)
    return [r[0] for r in ranked]


def _compute_data_cutoff_at(
    db: Session,
    symbol_ids: list[int],
    end_date: date,
) -> datetime | None:
    """计算数据截止时间：取 symbol_ids 在 end_date 之前的最新 K 线/Score 日期。

    优先取 DailyBar.trade_date 与 Score.trade_date 的较大者，
    转为 datetime 返回；无任何数据时返回 None。
    """
    if not symbol_ids:
        return None
    bar_cutoff = db.execute(
        select(func.max(DailyBar.trade_date)).where(
            DailyBar.symbol_id.in_(symbol_ids),
            DailyBar.trade_date <= end_date,
        )
    ).scalar_one_or_none()
    score_cutoff = db.execute(
        select(func.max(Score.trade_date)).where(
            Score.symbol_id.in_(symbol_ids),
            Score.trade_date <= end_date,
        )
    ).scalar_one_or_none()
    candidates = [d for d in (bar_cutoff, score_cutoff) if d is not None]
    if not candidates:
        return None
    latest = max(candidates)
    if isinstance(latest, datetime):
        return latest
    # date → datetime（取当日 00:00:00）
    return datetime.combine(latest, datetime.min.time())


def run_portfolio_backtest(
    db: Session,
    portfolio_id: int,
    *,
    start_date: date,
    end_date: date,
    run_name: str | None = None,
    benchmark: str | None = None,
    # WP0-5a 新契约：任务启动时锁定 snapshot；缺省则自动创建任务级锁 + 回退绑定
    strategy_snapshot_id: str | None = None,
    score_weight_mode: str | None = None,
    factor_model_run_id: str | None = None,
    # WP0-5 TR-05.3 契约参数（8 字段，C-05 修复）
    initial_capital: float | None = None,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
    slippage_bps: int = 5,
    price_type: str = "NEXT_OPEN",
    volume_limit_pct: float = 0.10,
    rebalance_frequency: str = "on_signal",
    pit_mode: str = "legacy_research",
    **_legacy_kwargs: Any,
) -> dict[str, Any]:
    """对组合执行整体回测。

    WP0-5 契约收紧：
      - 证券范围（统一三段口径）：
        (1) 买入候选 = PortfolioCandidate(pid, as_of_date, auto_authorized=True, removed_manually=False)
        (2) 卖出/风控范围 = 当前持仓 quantity>0
        (3) 组合证券范围 = 两者 ∪
        不再暴露 only_auto / current_universe 前端特殊分支（C-03 修复）。
      - 请求体 8 参数全部落地：initial_capital 覆盖 total_capital；
        commission_rate / stamp_tax_rate / slippage_bps 写入 CostModelConfig；
        price_type 传 match_order_plan；volume_limit_pct 作为流动性阈值；
        rebalance_frequency 写入 BacktestRun 快照；（C-05 修复）

    Args:
        db: 数据库会话
        portfolio_id: 目标组合 ID
        start_date: 回测起始日期
        end_date: 回测结束日期
        run_name: 回测名称，None 时自动生成
        benchmark: 基准中文名（如 沪深300）

    Returns:
        BacktestRun 的字典形式（含 id, status, trade_count 等字段，以及
        WP7.1 source_label 和 WP7.3 source_type 标签）

    Raises:
        ValueError: 组合不存在/非模拟/未开启自动交易/无标的可回测/
            total_capital<=0 / 价格类型非法
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    if portfolio.account_type != "simulated":
        raise ValueError(f"Portfolio {portfolio_id} is not simulated")
    if not portfolio.auto_trade_enabled:
        raise ValueError(
            f"Portfolio {portfolio_id} auto_trade_enabled is 0. "
            "Whole-portfolio backtest requires auto_trade_enabled=1 "
            "(signals are based on Score.action which mirrors auto-trade logic)."
        )
    # WP0-5 / C-05：initial_capital 显式优先（覆盖 portfolio 默认 total_capital），
    #   否则仍用组合默认，避免默认 0 阻断
    effective_capital: float = (
        float(initial_capital) if isinstance(initial_capital, (int, float)) and initial_capital > 0
        else float(portfolio.total_capital or 0.0)
    )
    if effective_capital <= 0:
        raise ValueError(
            f"Portfolio {portfolio_id} initial_capital is {effective_capital}; total_capital/initial_capital must be positive. "
            "Configure initial_capital before running backtest."
        )

    # WP0-5 / 价格类型：T_CLOSE 仅 research 允许（research_pit / legacy_research）；
    # production_pit 强制 NEXT_OPEN
    if price_type not in {"NEXT_OPEN", "T_CLOSE"}:
        raise ValueError(f"price_type 仅允许 NEXT_OPEN/T_CLOSE，传入={price_type}")
    if pit_mode == "production_pit" and price_type != "NEXT_OPEN":
        raise ValueError("production_pit 场景仅允许 NEXT_OPEN（正式生产口径），禁止 T_CLOSE")
    if rebalance_frequency not in {"daily", "weekly", "monthly", "on_signal"}:
        raise ValueError(
            "rebalance_frequency 仅允许 daily/weekly/monthly/on_signal，"
            f"传入={rebalance_frequency}"
        )

    # WP0-5a / C-02 修复：不再硬编码 score_weight_mode="manual"/factor_model_run_id=None
    # 优先级：
    #   1) 调用方显式参数 (strategy_snapshot_id / score_weight_mode / factor_model_run_id)
    #   2) 若指定 strategy_snapshot_id：从 strategy_execution_snapshots 读取绑定
    #   3) 否则读 portfolio_factor_usages 当前绑定；再回退全局 FactorRuntimeState
    #   4) 最终兜底：manual / None（研究模式兼容，同时写 DEGRADED_DATA 标记）
    from app.models.decision_engine import (
        PortfolioFactorUsage as _PFU, StrategyExecutionSnapshot as _SES,
    )
    from app.models.factor_runtime import FactorRuntimeState as _FRS
    effective_score_mode: str = score_weight_mode if score_weight_mode else ""
    effective_model_id: str | None = (
        factor_model_run_id.strip() if isinstance(factor_model_run_id, str) else None
    ) or None
    bound_factor_set_id: int | None = None

    # 页面只表达“使用当前已应用策略”，不能要求前端维护 snapshot ID。
    # 因此在任务启动前锁定最新 save_and_apply 快照；production_pit 没有
    # 可验证快照时 fail-closed，避免出现无证据的“生产回测”。
    if not strategy_snapshot_id:
        current_snapshot = db.execute(
            select(_SES)
            .where(
                _SES.portfolio_id == portfolio_id,
                _SES.snapshot_type == "save_and_apply",
            )
            .order_by(_SES.effective_from.desc(), _SES.created_at.desc())
            .limit(1)
        ).scalars().first()
        if current_snapshot is not None:
            strategy_snapshot_id = str(current_snapshot.id)
        elif pit_mode == "production_pit":
            raise ValueError(
                "production_pit 回测需要已应用的策略快照；请先保存并应用策略规则。"
            )

    # C-02 / C-12 修复：提前读取 PortfolioRule.stage_limits_json 中的绑定
    # (必须在 _resolve_symbol_ids 之前完成，避免回测窗口成员校验 raise 时跳过解析)
    rule_for_bind = get_active_rule(db, portfolio_id)
    if rule_for_bind is not None:
        sl = getattr(rule_for_bind, "stage_limits_json", None)
        if isinstance(sl, str):
            import json as _j2
            try:
                sl = _j2.loads(sl)
            except Exception:
                sl = None
        if isinstance(sl, dict):
            maybe_model = sl.get("factor_model_run_id")
            if isinstance(maybe_model, str) and maybe_model.strip():
                effective_model_id = effective_model_id or maybe_model.strip()
                if not effective_score_mode or effective_score_mode == "manual":
                    effective_score_mode = "ridge"
            maybe_set = sl.get("factor_set_id")
            if bound_factor_set_id is None and isinstance(maybe_set, (int, str)) and str(maybe_set).strip():
                try:
                    bound_factor_set_id = int(maybe_set)
                except (TypeError, ValueError):
                    bound_factor_set_id = None

    # 尝试使用显式 strategy_snapshot_id
    locked_snap: _SES | None = None
    if strategy_snapshot_id:
        locked_snap = db.get(_SES, strategy_snapshot_id)
        if locked_snap is None:
            raise ValueError(
                f"strategy_snapshot_id={strategy_snapshot_id} 不存在；"
                "请先调用 /portfolios/{id}/factor-usage 的 save_and_apply 后再回测。"
            )
        # save_and_apply 快照是不可变的；本次运行持久化该 ID 即已完成
        # 任务级锁定。旧实现尝试构造另一套 ORM 字段，遇到模型演进时会
        # 静默回退，反而掩盖错误并削弱可复现性。
        if locked_snap:
            vr = locked_snap.versions_json if isinstance(locked_snap.versions_json, dict) else {}
            if not effective_score_mode and vr.get("score_weight_mode"):
                effective_score_mode = str(vr["score_weight_mode"])
            if not effective_model_id and locked_snap.factor_model_run_id:
                effective_model_id = locked_snap.factor_model_run_id
            if bound_factor_set_id is None and locked_snap.factor_set_id:
                bound_factor_set_id = locked_snap.factor_set_id

    # 若未解析到 mode/id：读取 PortfolioFactorUsage 最新绑定
    if not effective_score_mode or not effective_model_id:
        usage = db.execute(
            select(_PFU)
            .where(_PFU.portfolio_id == portfolio_id)
            .order_by(_PFU.created_at.desc())
            .limit(1)
        ).scalars().first()
        if usage is not None:
            if not effective_model_id and usage.factor_model_run_id:
                effective_model_id = usage.factor_model_run_id
            if not effective_score_mode and usage.weight_mode:
                effective_score_mode = usage.weight_mode
            if bound_factor_set_id is None and usage.factor_set_id:
                bound_factor_set_id = usage.factor_set_id

    # 全局兜底
    if not effective_model_id:
        runtime = db.execute(select(_FRS).order_by(_FRS.version.desc()).limit(1)).scalars().first()
        if runtime is not None and runtime.active_model_run_id:
            effective_model_id = runtime.active_model_run_id
            if not effective_score_mode:
                effective_score_mode = "ridge"

    if not effective_score_mode:
        # 研究模式兜底 + 标签（Q6.2）；正式 PIT 链路在 preflight 阶段必须阻断
        effective_score_mode = "manual"

    # WP0-5 / C-03 修复：成员范围使用统一三段口径。
    # 不再暴露 only_auto / current_universe 的前端特殊分支，避免 PIT 历史成员资格被绕过。
    # - 候选：PortfolioCandidate(pid, [start_date, end_date] 窗口内任一 as_of 有效 ∩ auto_authorized ∩ 未手动移除)
    # - 持仓：回测窗口内 Position 历史或当前持仓（有 quantity>0 记录的 symbol）
    # - 组合证券范围 = 两者 ∪
    # - excluded_member_ids / excluded_pairs：保留用于快照审计（manual/confirm 执行模式的成员仍被标记），
    #   但不再仅根据 only_auto 开关过滤（C-03：统一按 auto_authorized_flag 与 execution_mode 双口径）
    excluded_member_ids: list[int] = []
    excluded_pairs: list[tuple[PortfolioMember, str]] = []
    # 仍读取 member 表用于审计（不用于候选过滤）
    if settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED:
        all_members = _get_effective_members_for_window(
            db, portfolio_id, start_date, end_date,
        )
        is_valid, excluded_list = validate_member_eligibility(all_members)
        # C-03：不再抛 ValueError 阻断（only_auto 分支被移除），只把 manual/confirm 成员记入 excluded_pairs
        # 供快照审计；真正的买入白名单通过 PortfolioCandidate.auto_authorized_flag 精确控制
        for item in excluded_list:
            member = next((m for m in all_members if int(m.id) == int(item["member_id"])), None)
            if member is not None:
                excluded_pairs.append((member, item.get("reason") or "unknown"))
                excluded_member_ids.append(int(member.id))

    # 推导标的列表（统一三段口径，不再带 only_auto/current_universe 特殊分支）
    # C-02 兼容：若 caller 传 source_type="legacy"（在 _legacy_kwargs 中），临时关闭 members 来源；
    #   若 _resolve_symbol_ids 抛 ValueError（无有效 members），回退到 legacy 候选+持仓，避免无法触及 run_backtest 传参校验
    _tmp_force_legacy = (
        isinstance(_legacy_kwargs.get("source_type"), str)
        and str(_legacy_kwargs["source_type"]).lower() == "legacy"
    )
    _orig_flag = settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED
    try:
        if _tmp_force_legacy:
            settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = False
        try:
            symbol_ids, source_label = _resolve_symbol_ids(
                db, portfolio_id, start_date, end_date,
                only_auto=False, exclude_member_ids=None,
                current_universe=False,
            )
        except ValueError as _v_exc:
            if settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED:
                # 回退：使用 legacy 候选池（不要求历史 member）兜底
                settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = False
                try:
                    symbol_ids, source_label = _resolve_symbol_ids(
                        db, portfolio_id, start_date, end_date,
                        only_auto=False, exclude_member_ids=None,
                        current_universe=False,
                    )
                finally:
                    settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = True
            else:
                raise _v_exc
    finally:
        settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = _orig_flag
    ensure_symbol_ids_in_scope(db, portfolio, symbol_ids)

    # 构造 rule_config（从 PortfolioRule + SignalRule 读取，避免"策略规则没打通"）
    rule = get_active_rule(db, portfolio_id)
    # C-12 RED → GREEN：研究模式兜底，从 rule.stage_limits_json 读取历史绑定
    #   (PortfolioRule 中若显式写入 factor_model_run_id，回测应使用它
    #    以保持历史规则兼容性；正式链路通过 strategy_snapshot_id 锁定)
    if not effective_model_id and rule is not None:
        sl = getattr(rule, "stage_limits_json", None)
        if isinstance(sl, str):
            import json as _j
            try:
                sl = _j.loads(sl)
            except Exception:
                sl = None
        if isinstance(sl, dict):
            maybe = sl.get("factor_model_run_id")
            if isinstance(maybe, str) and maybe.strip():
                effective_model_id = maybe.strip()
                if not effective_score_mode or effective_score_mode == "manual":
                    effective_score_mode = "ridge"
    rule_config = _build_rule_config(db, portfolio_id, rule)

    # P2-FIX：把 PortfolioRule 的 stock_pool / factors，与 SignalRule 的准入分，
    # 应用到回测标的池（与 auto_trade_task 同源），确保"策略规则与回测打通"。
    # C-08 PIT：传 end_date 作为 Score 查询截止，避免回测窗口结束日期之后的数据污染回测历史，
    #   与"按日决策时 data_cutoff_at 按日切 Score"的 WP0-7 完整方案语义一致（fail-soft 兼容）。
    symbol_ids = _filter_symbol_ids_by_rule(db, portfolio_id, rule, symbol_ids, as_of_date=end_date)
    if not symbol_ids:
        raise ValueError("策略规则过滤后无可回测标的（stock_pool/质量/择时阈值过严或PIT截止后无有效Score），请调整策略规则后再回测。")

    # 回测名称
    if run_name is None:
        run_name = f"组合整体回测 {start_date.isoformat()}~{end_date.isoformat()}"

    # WP7.3 构建快照字段
    snapshot_kwargs: dict[str, Any] = {
        "engine_name": _ENGINE_NAME,
        "engine_version": _ENGINE_VERSION,
        "source_type": "member" if source_label == "members" else "legacy_scan",
    }
    # WP0-5 TR-05.3：显式 8 参数快照（审计用，与请求 diff 一致）
    snapshot_kwargs["rebalance_frequency"] = rebalance_frequency
    snapshot_kwargs["volume_limit_pct"] = float(volume_limit_pct)
    snapshot_kwargs["price_type"] = price_type
    snapshot_kwargs["pit_mode"] = pit_mode
    # WP0-5a：任务级锁定的快照 ID / factor_set_id；写入 BacktestRun 扩展字段（migration 0032）
    if locked_snap is not None:
        snapshot_kwargs["strategy_snapshot_id"] = locked_snap.id
    if bound_factor_set_id is not None:
        snapshot_kwargs["factor_set_id"] = bound_factor_set_id
    if source_label == "members":
        # 回测窗口内的实际成员（不再按 only_auto/current_universe 过滤）
        used_members = _get_effective_members_for_window(
            db, portfolio_id, start_date, end_date,
            only_auto=False, exclude_member_ids=None,
        )
        member_snapshot = build_member_snapshot(used_members)
        excluded_snapshot = build_excluded_members_snapshot(excluded_pairs)
        # WP0-5 C-05：成本快照使用显式请求的 commission_rate / stamp_tax_rate / slippage_bps，
        #  不再依赖 portfolio 字段（成本由调用方在请求体声明，保证 TR-05.3 8 参数逐字段可追溯）
        cost_snapshot = {
            "commission_rate": float(commission_rate),
            "stamp_duty_rate": float(stamp_tax_rate),
            # 与 BacktestCostConfig / sme.CostModelConfig 对齐：slippage_buy_bps/sell_bps 共用单一显式值
            "slippage_buy_bps": int(slippage_bps),
            "slippage_sell_bps": int(slippage_bps),
            "slippage": int(slippage_bps),
            "min_commission": 5.0,  # A 股默认最低 5 元
            "volume_limit_pct": float(volume_limit_pct),
        }
        data_cutoff_at = _compute_data_cutoff_at(db, symbol_ids, end_date)
        snapshot_kwargs.update({
            "member_snapshot_json": json.dumps(member_snapshot, ensure_ascii=False),
            "symbol_ids_json": json.dumps(symbol_ids),
            "excluded_members_json": json.dumps(excluded_snapshot, ensure_ascii=False),
            "portfolio_rule_version_id": rule.id if rule is not None else None,
            "score_mode": "auto_trade_signal",
            "data_cutoff_at": data_cutoff_at,
        })
        cost_config_for_run = cost_snapshot
    else:
        cost_config_for_run = {
            "commission_rate": float(commission_rate),
            "stamp_duty_rate": float(stamp_tax_rate),
            "slippage_buy_bps": int(slippage_bps),
            "slippage_sell_bps": int(slippage_bps),
            "slippage": int(slippage_bps),
            "min_commission": 5.0,
            "volume_limit_pct": float(volume_limit_pct),
        }

    # WP0-5a / C-02 修复：使用解析后的绑定字段，不再硬编码 manual/None
    #   - mode=ridge 且有 active_model_run_id 时按 model 查询
    #   - mode=manual 时按 legacy priority_score 查询（研究模式兜底/Q6.2 兼容）
    # WP0-5 C-05：effective_capital 显式传给 run_backtest 作为 initial_capital（覆盖 portfolio.total_capital）
    run = run_backtest(
        db=db,
        portfolio_id=portfolio_id,
        symbol_ids=symbol_ids,
        start_date=start_date,
        end_date=end_date,
        rule_config=rule_config,
        cost_config=cost_config_for_run,
        run_name=run_name,
        score_weight_mode=effective_score_mode,
        factor_model_run_id=effective_model_id,
        initial_capital=effective_capital,
        **snapshot_kwargs,
    )

    # Q29：保留已经由统一 DecisionEngine 持久化的 backtest DecisionRun 关联。
    # 回测本身不在读取阶段临时创建证据；若当前引擎尚未产生 DecisionRun，
    # 则显式返回空关联，供 UI 标注“无证据链”，而不是伪造 ID。
    decision_run_ids: list[str] = []
    evidence_summary: dict[str, Any] = {"total": 0, "by_action": {}, "linked_trade_count": 0}
    decision_snapshot: dict[str, Any] | None = None
    try:
        from app.models.decision_engine import (
            DecisionEvidence as _DecisionEvidence,
            DecisionRun as _DecisionRun,
            StrategyExecutionSnapshot as _DecisionSnapshot,
        )
        decision_stmt = select(_DecisionRun.id).where(
            _DecisionRun.portfolio_id == portfolio_id,
            _DecisionRun.run_type == "backtest",
            _DecisionRun.trade_date >= start_date,
            _DecisionRun.trade_date <= end_date,
        )
        if getattr(run, "strategy_snapshot_id", None):
            decision_stmt = decision_stmt.where(
                _DecisionRun.strategy_snapshot_id == run.strategy_snapshot_id
            )
        decision_run_ids = [str(v) for v in db.execute(
            decision_stmt.order_by(_DecisionRun.trade_date, _DecisionRun.created_at)
        ).scalars().all()]
        run.decision_run_ids_json = json.dumps(decision_run_ids, ensure_ascii=False)
        if decision_run_ids:
            action_rows = db.execute(
                select(_DecisionEvidence.action, func.count(_DecisionEvidence.id))
                .where(_DecisionEvidence.decision_run_id.in_(decision_run_ids))
                .group_by(_DecisionEvidence.action)
            ).all()
            by_action = {str(action): int(count) for action, count in action_rows}
            evidence_summary["by_action"] = by_action
            evidence_summary["total"] = sum(by_action.values())
            evidence_summary["linked_trade_count"] = int(db.scalar(
                select(func.count()).select_from(BacktestTrade).where(
                    BacktestTrade.run_id == run.id,
                    (BacktestTrade.decision_evidence_id.is_not(None))
                    | (BacktestTrade.exit_evidence_id.is_not(None)),
                )
            ) or 0)
        if run.strategy_snapshot_id:
            snap = db.get(_DecisionSnapshot, run.strategy_snapshot_id)
            if snap is not None:
                decision_snapshot = {
                    "id": str(snap.id),
                    "snapshot_no": snap.snapshot_no,
                    "factor_model_run_id": snap.factor_model_run_id,
                    "factor_set_id": snap.factor_set_id,
                    "rule_id": snap.rule_id,
                    "rule_version": snap.rule_version,
                    "snapshot_hash": snap.snapshot_hash,
                    "snapshot_type": snap.snapshot_type,
                }
        db.flush()
    except Exception:
        # 关联是增强字段，不能让历史/研究模式回测因证据表缺失而失败。
        decision_run_ids = []

    result = {
        "run_id": run.id,
        "portfolio_id": portfolio_id,
        "symbol_ids": symbol_ids,
        "symbol_count": len(symbol_ids),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        # WP0-5 C-05：使用显式 effective_capital（不再写 portfolio 默认值）
        "initial_capital": float(effective_capital),
        "status": run.status,
        "run_name": run.run_name,
        # WP7.1: 标的来源标签（"legacy"=持仓+最新扫描 / "members"=历史有效成员）
        "symbol_source": source_label,
        # WP7.3: 同步暴露 source_type 供 UI 显示
        "source_type": snapshot_kwargs["source_type"],
        # WP7.3: 仍返回 excluded 计数，便于 UI 提示 manual/confirm 执行模式成员虽被标记但不参与候选过滤
        "excluded_member_count": len(excluded_pairs),
        # C-05 参数回显：前端提交后可 diff 确认请求已真实落地
        "commission_rate": float(commission_rate),
        "stamp_tax_rate": float(stamp_tax_rate),
        "slippage_bps": int(slippage_bps),
        "price_type": price_type,
        "volume_limit_pct": float(volume_limit_pct),
        "rebalance_frequency": rebalance_frequency,
        "pit_mode": pit_mode,
        # P2-FIX(TDD RED→GREEN): 同步返回 BacktestRun 已有的绩效/明细字段，
        # 避免前端只看到“回测已启动”但指标卡全 0、曲线空白；同时保持和
        # BacktestRunRead/BacktestRunDetail 一致的字段名，前端可直接复用渲染。
        "total_return": float(run.total_return) if run.total_return is not None else None,
        "total_return_pct": float(run.total_return_pct) if run.total_return_pct is not None else None,
        "max_drawdown": float(run.max_drawdown) if run.max_drawdown is not None else None,
        "max_drawdown_pct": float(run.max_drawdown_pct) if run.max_drawdown_pct is not None else None,
        "sharpe_ratio": float(run.sharpe_ratio) if run.sharpe_ratio is not None else None,
        "win_rate": float(run.win_rate) if run.win_rate is not None else None,
        "profit_factor": float(run.profit_factor) if run.profit_factor is not None else None,
        "trade_count": int(run.trade_count) if run.trade_count is not None else None,
        "avg_holding_days": float(run.avg_holding_days) if run.avg_holding_days is not None else None,
        "error_message": run.error_message,
        "created_at": run.created_at.isoformat() if run.created_at is not None else None,
        "started_at": run.started_at.isoformat() if run.started_at is not None else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at is not None else None,
        "decision_run_ids": decision_run_ids,
        "evidence_summary": evidence_summary,
        "rejected_count": int(
            evidence_summary.get("by_action", {}).get("REJECTED", 0)
            + evidence_summary.get("by_action", {}).get("DATA_BLOCKED", 0)
        ),
        "decision_snapshot": decision_snapshot,
    }
    # WP0-6b / Q29.3：基准曲线落库并返回 warnings（使用 effective_capital 归一化）
    equity_raw = _parse_equity_curve(run.equity_curve_json)
    equity_enriched, bm_warnings = _enrich_equity_curve_with_benchmark(
        db,
        equity_raw,
        initial_capital=float(effective_capital),
        benchmark_name=benchmark,
    )
    # Persist the benchmark health contract on the run itself so API reads do
    # not need to infer status from transient warning payloads.
    benchmark_warning_codes = {str(w.get("code")) for w in bm_warnings}
    if benchmark:
        if "BENCHMARK_INCOMPLETE" in benchmark_warning_codes:
            run.benchmark_status = "BENCHMARK_INCOMPLETE"
        elif "SOURCE_MISSING" in benchmark_warning_codes:
            run.benchmark_status = "SOURCE_MISSING"
        elif "BENCHMARK_GAP" in benchmark_warning_codes:
            run.benchmark_status = "BENCHMARK_INCOMPLETE"
        else:
            run.benchmark_status = "FULL"
        run.benchmark_gap_days = max(
            [int(w.get("missing_days") or 0) for w in bm_warnings] or [0]
        )
    else:
        run.benchmark_status = "SOURCE_MISSING"
        run.benchmark_gap_days = None
    # 持久化基准曲线（后复权归一化口径），避免响应侧每次临时重算
    try:
        import json as _json
        bench_points = [
            {"date": p.get("date"), "benchmark": p.get("benchmark")}
            for p in equity_enriched
            if isinstance(p, dict) and "date" in p
        ]
        # benchmark_equity_json: BacktestRun 扩展字段（migration 0032）
        run.benchmark_equity_json = _json.dumps(
            bench_points, ensure_ascii=False, separators=(",", ":")
        )
        db.flush()
    except Exception:
        pass
    result["equity_curve"] = equity_enriched
    # Build ledger-dependent metrics on the server, but never include the
    # unbounded ledger in the result summary.  The browser reads it only via
    # the paginated /backtest/runs/{run_id}/trades endpoint.
    result_trades = _build_trades_for_result(db, run.id)
    result["metrics"] = {}  # GREEN: 占位，下一行 enrich 覆盖
    result["diagnostics"] = _build_diagnostics_for_result(db, run)
    result["warnings"] = list(bm_warnings)
    result["errors"] = []
    # GREEN: enrich（依赖 equity_curve/trades 构造完成的 result）—— 必须在 return 前执行
    result["metrics"] = _build_enriched_metrics_for_result(
        run,
        equity_curve=result["equity_curve"],
        trades=result_trades,
        initial_capital=float(portfolio.total_capital),
    )
    # 当 benchmark 不完整时，将超额收益/跟踪误差/IR 置 null 并显示不可计算 (Q3.2)
    has_benchmark_incomplete = any(
        w.get("code") == "BENCHMARK_INCOMPLETE" for w in result["warnings"]
    ) or any(
        w.get("code") == "BENCHMARK_GAP" for w in result["warnings"]
    ) or any(
        w.get("code") == "SOURCE_MISSING" for w in result["warnings"]
    )
    if has_benchmark_incomplete:
        for k in ("excess_return_pct", "tracking_error", "information_ratio", "beta"):
            result["metrics"][k] = None
    return result


def _enrich_equity_curve_with_benchmark(
    db: Session,
    equity_curve: list[dict],
    *,
    initial_capital: float,
    benchmark_name: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """WP0-6b / Q3 合规：回填真实指数 benchmark，**禁用伪造5%年化曲线**。

    规则 (Q3.1/Q3.2):
      - 基准缺口不阻断组合回测，只降低可信度并标红；
      - 连续缺失超过5个交易日时标记 BENCHMARK_INCOMPLETE；
      - 主源失败（IndexPrice 无匹配段）：保留缺口为 None，不用当前数据伪造历史。
      - AkShare/Baostock 双源统一写入后复权口径（不在本期 enrich 阶段做拉取，
        只消费已落库的 index_price 表；Q3 健康检查由 index_prices_sync_task 负责）。

    Returns:
      (enriched_curve, warnings)
        warnings: [{"code": str, "message": str, "severity": "warn"|"error", "missing_days": int, ...}]
    """
    warnings: list[dict] = []
    if not equity_curve:
        return [], warnings

    # 仅当有 benchmark 名称时尝试真实数据；否则直接返回原曲线（无基准）
    benchmark_symbol = _BENCHMARK_NAME_TO_SYMBOL.get(benchmark_name or "", "")
    if not benchmark_symbol:
        # 无指定基准：保留原曲线，不伪造
        return [dict(p) if isinstance(p, dict) else {} for p in equity_curve], warnings

    bars: list | None = None
    try:
        start_date = date.fromisoformat(equity_curve[0]["date"])
        end_date = date.fromisoformat(equity_curve[-1]["date"])
        query_start = start_date - timedelta(days=30)
        query_end = end_date + timedelta(days=1)

        bars = list_index_prices(
            db, benchmark_symbol,
            start_date=query_start, end_date=query_end, limit=10000,
        )
    except Exception:
        bars = None

    sorted_bars: list = sorted(bars, key=lambda b: b.trade_date) if bars else []
    bar_dates = [b.trade_date for b in sorted_bars]
    bar_closes = [float(b.close) for b in sorted_bars]
    base_close: float | None = None

    enriched: list[dict] = []
    consecutive_missing = 0
    max_consecutive_missing = 0
    total_missing = 0

    for point in equity_curve:
        new_p = dict(point) if isinstance(point, dict) else {}
        eq_date_str = new_p.get("date", "")
        # 若已有有效非空 benchmark：信任原值，不覆盖
        if isinstance(new_p.get("benchmark"), (int, float)) and not (
            isinstance(new_p.get("benchmark"), bool)
        ) and float(new_p["benchmark"]) > 0:
            consecutive_missing = 0
            enriched.append(new_p)
            continue
        # 无真实 bars：缺口 = None
        if not sorted_bars or not eq_date_str:
            consecutive_missing += 1
            total_missing += 1
            max_consecutive_missing = max(max_consecutive_missing, consecutive_missing)
            new_p["benchmark"] = None
            enriched.append(new_p)
            continue
        try:
            eq_date = date.fromisoformat(eq_date_str)
        except ValueError:
            consecutive_missing += 1
            total_missing += 1
            max_consecutive_missing = max(max_consecutive_missing, consecutive_missing)
            new_p["benchmark"] = None
            enriched.append(new_p)
            continue

        idx = bisect.bisect_right(bar_dates, eq_date) - 1
        if idx < 0:
            close = None
        else:
            close = bar_closes[idx] if idx < len(bar_closes) else None
        # Q3.2：同日之前最近有效 bar 不超过 10 个日历日；否则视为缺口
        if close is None:
            gap_days = -1
        else:
            gap_days = (eq_date - bar_dates[idx]).days if idx >= 0 else 999
        if (
            close is None
            or close <= 0
            or gap_days > 10
        ):
            consecutive_missing += 1
            total_missing += 1
            max_consecutive_missing = max(max_consecutive_missing, consecutive_missing)
            new_p["benchmark"] = None
            enriched.append(new_p)
            continue

        if base_close is None:
            base_close = float(close)

        scaled = initial_capital * (float(close) / base_close)
        new_p["benchmark"] = round(float(scaled), 4)
        consecutive_missing = 0
        enriched.append(new_p)

    # 汇总缺口警告 (Q3.2)
    pct = float(total_missing) / float(len(equity_curve)) if equity_curve else 0.0
    if not sorted_bars:
        warnings.append({
            "code": "SOURCE_MISSING",
            "severity": "warn",
            "message": f"基准[{benchmark_name}/{benchmark_symbol}]没有可用指数行情，禁止填充伪造曲线。",
            "missing_days": len(equity_curve),
            "total_days": len(equity_curve),
        })
    if total_missing > 0:
        warnings.append({
            "code": "BENCHMARK_GAP",
            "severity": "warn",
            "message": (
                f"基准[{benchmark_name}/{benchmark_symbol}]存在缺口："
                f"{total_missing}/{len(equity_curve)} 天 ({round(pct*100,1)}%)，"
                f"最长连续缺失 {max_consecutive_missing} 天。"
                "超额收益/跟踪误差/IR 暂时不可计算。"
            ),
            "missing_days": total_missing,
            "total_days": len(equity_curve),
            "max_consecutive_missing": max_consecutive_missing,
        })
    if max_consecutive_missing >= 5:
        # 连续缺失超过 5 个交易日 → 基准曲线标记 BENCHMARK_INCOMPLETE
        warnings.append({
            "code": "BENCHMARK_INCOMPLETE",
            "severity": "warn",
            "message": (
                f"基准连续缺失 {max_consecutive_missing} 天 >= 5 天阈值，"
                "基准对比可信度降低，结果页应标红。"
            ),
            "max_consecutive_missing": max_consecutive_missing,
        })
    return enriched, warnings


def _build_enriched_metrics_for_result(
    run: BacktestRun,
    *,
    equity_curve: list[dict],
    trades: list[dict],
    initial_capital: float,
) -> dict[str, Any]:
    """在 BacktestRun 原生指标基础上，补充前端 4 项刚需指标，避免显示为 --。

    补充项（全部用 equity_curve + trades 在响应侧即时计算，不写库，保持无损）：
      - annual_return_pct     → 年化收益率（对数收益复利近似，兼容 <1 年）
      - annual_volatility     → 年化波动率（日收益率 std × √252，单位 %）
      - drawdown_days         → 最长回撤修复天数（水下连续天数最大值，整数天）
      - turnover_rate         → 区间双边换手率（Σ(每次交易 entry_cost+exit_cost)
                                 / 2 / initial_capital / 年数，次数近似 → 统一按
                                 "双边交易额/初始资金/年数"口径，单位 次/年）

    若某一项数据不足以计算 → 返回 None，由前端兜底或显示 --。
    """
    import math
    # 原生 9 项（与 run_portfolio_backtest 构造处完全一致）
    base: dict[str, Any] = {
        "total_return": float(run.total_return) if run.total_return is not None else None,
        "total_return_pct": float(run.total_return_pct) if run.total_return_pct is not None else None,
        "max_drawdown": float(run.max_drawdown) if run.max_drawdown is not None else None,
        "max_drawdown_pct": float(run.max_drawdown_pct) if run.max_drawdown_pct is not None else None,
        "sharpe_ratio": float(run.sharpe_ratio) if run.sharpe_ratio is not None else None,
        "win_rate": float(run.win_rate) if run.win_rate is not None else None,
        "profit_factor": float(run.profit_factor) if run.profit_factor is not None else None,
        "trade_count": int(run.trade_count) if run.trade_count is not None else 0,
        "avg_holding_days": float(run.avg_holding_days) if run.avg_holding_days is not None else None,
    }

    eq_arr: list[float] = []
    for p in (equity_curve or []):
        try:
            eq_arr.append(float(p.get("equity")))
        except Exception:
            continue
    n = len(eq_arr)

    # --- annual_return_pct ---------------------------------------------------
    if n >= 2 and eq_arr[0] > 0:
        from datetime import date as _date
        start_date = end_date = None
        try:
            if isinstance((equity_curve[0] or {}).get("date"), str):
                start_date = _date.fromisoformat(equity_curve[0]["date"])
                end_date = _date.fromisoformat(equity_curve[-1]["date"])
        except Exception:
            pass
        total_days = (end_date - start_date).days if start_date and end_date and end_date > start_date else (n - 1)
        total_years = max(total_days / 365.25, 1 / 12.0)  # 至少 1 个月，避免极短区间爆炸
        total_ret = eq_arr[-1] / eq_arr[0] - 1.0
        annual = (1.0 + total_ret) ** (1.0 / total_years) - 1.0
        base["annual_return_pct"] = round(float(annual), 6)
    else:
        base["annual_return_pct"] = None

    # --- annual_volatility --------------------------------------------------
    annual_vol_pct: float | None = None
    if n >= 5:
        daily_pct: list[float] = []
        for i in range(1, n):
            if eq_arr[i - 1] > 0:
                daily_pct.append(eq_arr[i] / eq_arr[i - 1] - 1.0)
        if daily_pct:
            mean = sum(daily_pct) / len(daily_pct)
            var = sum((r - mean) ** 2 for r in daily_pct) / len(daily_pct)
            std_daily = math.sqrt(var)
            annual_vol = std_daily * math.sqrt(252)
            annual_vol_pct = round(float(annual_vol * 100.0), 2)
    base["annual_volatility"] = annual_vol_pct  # 单位：百分比（%），例如 18.5 = 18.5%

    # --- drawdown_days（最长水下连续天数） --------------------------------
    drawdown_days: int | None = None
    if n >= 2:
        peak = eq_arr[0]
        under_water_days = 0
        max_under_water = 0
        for v in eq_arr[1:]:
            if v > peak:
                peak = v
                under_water_days = 0
            elif v < peak:
                under_water_days += 1
                if under_water_days > max_under_water:
                    max_under_water = under_water_days
            # v == peak → 维持状态不变（既不新增也不重置）
        drawdown_days = int(max_under_water)
    base["drawdown_days"] = drawdown_days

    # --- turnover_rate ------------------------------------------------------
    turnover_per_year: float | None = None
    total_years_denom: float | None = None
    if equity_curve and len(equity_curve) >= 2:
        from datetime import date as _date
        try:
            start_date = _date.fromisoformat(equity_curve[0]["date"])
            end_date = _date.fromisoformat(equity_curve[-1]["date"])
            total_years_denom = max((end_date - start_date).days / 365.25, 1 / 12.0)
        except Exception:
            total_years_denom = max((n - 1) / 252.0, 1 / 12.0)
    if total_years_denom and initial_capital > 0:
        bilateral_notional = 0.0
        for t in (trades or []):
            entry_cost = 0.0
            exit_cost = 0.0
            try:
                entry_cost = float(t.get("entry_cost") or (float(t.get("entry_price") or 0) * float(t.get("quantity") or 0)))
            except Exception:
                entry_cost = 0.0
            try:
                exit_cost = float(t.get("exit_cost") or (float(t.get("exit_price") or 0) * float(t.get("quantity") or 0)))
            except Exception:
                exit_cost = 0.0
            bilateral_notional += entry_cost + exit_cost
        turnover_per_year = round(bilateral_notional / initial_capital / total_years_denom, 3)
    base["turnover_rate"] = turnover_per_year  # 单位：次/年（双边），例如 3.5 = 年化双边换手 3.5 倍

    return base


def _parse_equity_curve(equity_curve_json: str | None) -> list[dict]:
    """把 BacktestRun.equity_curve_json 反序列化为前端可直接渲染的 point 列表。

    - 非法 JSON / None → 返回 []，不抛异常（由 BacktestRun 层写入时已清洗 NaN/inf，
      这里主要兜底历史回测遗留的空值）。
    """
    if not equity_curve_json:
        return []
    try:
        parsed = json.loads(equity_curve_json)
        return list(parsed) if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _build_trades_for_result(db: Session, run_id: int) -> list[dict[str, Any]]:
    """精简版 BacktestTrade 列表（对齐 BacktestTradeRead 字段名，便于 schema 复用）。

    说明：这里返回的是 dict（不直接依赖 ORM rel），避免 response_model 序列化时触发
    懒加载；字段与 BacktestTrade.model_dump() 对齐。
    """
    from app.schemas.backtest import BacktestTradeRead
    rows = list(db.execute(
        select(BacktestTrade)
        .where(BacktestTrade.run_id == run_id)
        .order_by(BacktestTrade.entry_date, BacktestTrade.id)
    ).scalars().all())
    result: list[dict[str, Any]] = []
    for t in rows:
        try:
            result.append(BacktestTradeRead.model_validate(t).model_dump(mode="json"))
        except Exception:
            # 防止单个坏 row 毁掉整个响应
            continue
    return result


def _build_diagnostics_for_result(db: Session, run: BacktestRun) -> dict[str, Any]:
    """复用单标的回测 detail diagnostics（含 skip_reasons / sample_misses / 执行配置）。"""
    try:
        trades = list(db.execute(
            select(BacktestTrade)
            .where(BacktestTrade.run_id == run.id)
            .order_by(BacktestTrade.entry_date, BacktestTrade.id)
        ).scalars().all())
        extra = build_backtest_detail_context(db, run, trades)
    except Exception:
        return {}
    diag = extra.get("diagnostics") or {}
    if isinstance(diag, dict):
        return {k: v for k, v in diag.items()}
    return {}


def compare_new_old_engine(
    db: Session,
    portfolio_id: int,
    start_date: date,
    end_date: date,
    *,
    initial_capital: float | None = None,
    run_name_prefix: str = "compare",
) -> dict[str, Any]:
    """WP7.3 新旧引擎对比：使用相同日期/资金/成本对比新旧来源回测结果。

    新旧来源由 ``PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED`` 开关控制：
    - 旧来源（legacy）：持仓 + 最新 scan 候选池
    - 新来源（members）：按有效日期读取历史成员

    本函数临时切换开关并跑两次回测，对比：
    - 标的集差异（新增/移除）
    - 关键指标差异（总收益/最大回撤/Sharpe）
    - 文本解释

    Args:
        initial_capital: 仅用于返回值展示，实际回测沿用 portfolio.total_capital
        run_name_prefix: 两次回测 run_name 前缀，便于审计

    Returns:
        {
            "old": {"symbol_ids": [...], "trades": [...], "metrics": {...}, "run_id": int},
            "new": {"symbol_ids": [...], "trades": [...], "metrics": {...}, "run_id": int},
            "diff": {
                "symbol_ids_added": [...],
                "symbol_ids_removed": [...],
                "metrics_diff": {...},
                "explanation": "..."
            }
        }

    Raises:
        ValueError: 任一来源回测失败（如组合不存在/无标的/成员资格不通过）
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    if initial_capital is None:
        initial_capital = float(portfolio.total_capital)

    original_flag = settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED

    def _run_one(label: str, enabled: bool) -> dict[str, Any]:
        settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = enabled
        try:
            result = run_portfolio_backtest(
                db=db,
                portfolio_id=portfolio_id,
                start_date=start_date,
                end_date=end_date,
                run_name=f"{run_name_prefix}-{label}",
                # 对比时强制只回测 auto 成员，避免 manual/confirm 阻止
                only_auto=enabled,
            )
        finally:
            settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = original_flag

        run = db.get(BacktestRun, result["run_id"])
        trades = list(db.execute(
            select(BacktestTrade)
            .where(BacktestTrade.run_id == run.id)
            .order_by(BacktestTrade.entry_date, BacktestTrade.id)
        ).scalars().all())
        return {
            "run_id": run.id,
            "symbol_ids": result["symbol_ids"],
            "source_type": result["source_type"],
            "trades": [
                {
                    "symbol_id": t.symbol_id,
                    "entry_date": t.entry_date.isoformat() if t.entry_date else None,
                    "exit_date": t.exit_date.isoformat() if t.exit_date else None,
                    "pnl": float(t.pnl) if t.pnl is not None else None,
                    "pnl_pct": float(t.pnl_pct) if t.pnl_pct is not None else None,
                }
                for t in trades
            ],
            "metrics": {
                "total_return": float(run.total_return) if run.total_return is not None else None,
                "total_return_pct": float(run.total_return_pct) if run.total_return_pct is not None else None,
                "max_drawdown": float(run.max_drawdown) if run.max_drawdown is not None else None,
                "max_drawdown_pct": float(run.max_drawdown_pct) if run.max_drawdown_pct is not None else None,
                "sharpe_ratio": float(run.sharpe_ratio) if run.sharpe_ratio is not None else None,
                "trade_count": int(run.trade_count) if run.trade_count is not None else 0,
            },
        }

    old_result = _run_one("legacy", enabled=False)
    new_result = _run_one("members", enabled=True)

    old_set = set(old_result["symbol_ids"])
    new_set = set(new_result["symbol_ids"])
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)

    old_m = old_result["metrics"]
    new_m = new_result["metrics"]
    metrics_diff: dict[str, Any] = {}
    for key in ("total_return", "total_return_pct", "max_drawdown",
                "max_drawdown_pct", "sharpe_ratio", "trade_count"):
        ov = old_m.get(key)
        nv = new_m.get(key)
        if ov is None or nv is None:
            metrics_diff[key] = {"old": ov, "new": nv, "delta": None}
        else:
            metrics_diff[key] = {"old": ov, "new": nv, "delta": nv - ov}

    # 文本解释
    parts: list[str] = []
    if added:
        parts.append(f"新引擎比旧引擎多了 {len(added)} 个标的")
    if removed:
        parts.append(f"新引擎比旧引擎少了 {len(removed)} 个标的")
    if not added and not removed:
        parts.append("新旧引擎标的集完全一致")
    if old_m["trade_count"] != new_m["trade_count"]:
        parts.append(
            f"交易笔数从 {old_m['trade_count']} 变为 {new_m['trade_count']}"
        )
    if (old_m["total_return"] is not None and new_m["total_return"] is not None
            and old_m["total_return"] != new_m["total_return"]):
        parts.append(
            f"总收益从 {old_m['total_return']:.2f} 变为 {new_m['total_return']:.2f}"
        )
    explanation = "；".join(parts) if parts else "新旧引擎结果一致"

    return {
        "old": old_result,
        "new": new_result,
        "diff": {
            "symbol_ids_added": added,
            "symbol_ids_removed": removed,
            "metrics_diff": metrics_diff,
            "explanation": explanation,
        },
    }


__all__ = [
    "run_portfolio_backtest",
    "get_effective_members_for_date",
    "get_member_symbols_for_date",
    "validate_member_eligibility",
    "compare_new_old_engine",
]
