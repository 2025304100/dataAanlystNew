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

import json
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.backtest import BacktestRun, BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    PortfolioMember,
    STATUS_ACTIVE,
)
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.services.allocation import get_active_rule
from app.services.backtest import run_backtest
from app.services.portfolio_backtest_snapshot import (
    build_cost_config_snapshot,
    build_excluded_members_snapshot,
    build_member_snapshot,
)

logger = logging.getLogger(__name__)

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
            f"Portfolio {portfolio_id} has no positions and no scan candidates. "
            "Cannot run whole-portfolio backtest."
        )

    return sorted(symbol_ids)


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


def _build_rule_config(rule: PortfolioRule | None) -> dict[str, Any]:
    """从 PortfolioRule 构造与 auto_trade 信号逻辑一致的 v1 rule_config。

    信号逻辑（与 auto_trade_task 对齐）：
    - 买入：Score.action ∈ {open, buy_dip}
    - 卖出：Score.action ∈ {exit, reduce}

    仓位限制（从 PortfolioRule 映射）：
    - position_config.value = max_single_position_pct（每标的最大仓位百分比）
    - position_config.max_positions = max_open_positions（最大持仓数）
    """
    if rule is None:
        # 无 active rule 时使用保守默认值
        max_single = 0.1
        max_positions = 5
    else:
        max_single = float(rule.max_single_position_pct or 0.1)
        max_positions = int(rule.max_open_positions or 5)

    return {
        "buy_conditions": {
            "actions": _BUY_ACTIONS,
        },
        "sell_conditions": {
            "score_actions": _SELL_ACTIONS,
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
    only_auto: bool = False,
) -> dict[str, Any]:
    """对组合执行整体回测。

    Args:
        db: 数据库会话
        portfolio_id: 目标组合 ID
        start_date: 回测起始日期
        end_date: 回测结束日期
        run_name: 回测名称，None 时自动生成
        only_auto: WP7.3 仅回测 execution_mode='auto' 成员。
            - source_label=="members" 且 only_auto=False 时，若存在 manual/confirm
              成员则抛出 ValueError（默认禁止完整回测）
            - only_auto=True 时跳过 manual/confirm 成员，并将其记入
              ``excluded_members_json`` 快照

    Returns:
        BacktestRun 的字典形式（含 id, status, trade_count 等字段，以及
        WP7.1 source_label 和 WP7.3 source_type 标签）

    Raises:
        ValueError: 组合不存在/非模拟/未开启自动交易/无标的可回测/
            total_capital<=0/存在 manual/confirm 成员且 only_auto=False
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
    if not portfolio.total_capital or float(portfolio.total_capital) <= 0:
        raise ValueError(
            f"Portfolio {portfolio_id} total_capital is {portfolio.total_capital}. "
            "Configure total_capital before running backtest."
        )

    # WP7.3 成员资格校验（仅 member 来源生效）
    # - only_auto=False 且存在 manual/confirm 成员 → 阻止完整回测
    # - only_auto=True → 跳过 manual/confirm 成员，记入 excluded_members
    excluded_member_ids: list[int] = []
    excluded_pairs: list[tuple[PortfolioMember, str]] = []
    if settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED:
        all_members = _get_effective_members_for_window(
            db, portfolio_id, start_date, end_date,
        )
        is_valid, excluded_list = validate_member_eligibility(all_members)
        if not is_valid and not only_auto:
            # 默认禁止完整回测：让用户感知 manual/confirm 成员的存在
            raise ValueError(_MSG_MANUAL_MEMBERS_BLOCK)
        if not is_valid and only_auto:
            # 收集被排除的成员（manual/confirm/无规则版本），用于快照记录
            excluded_member_ids = [item["member_id"] for item in excluded_list]
            excluded_id_set = set(excluded_member_ids)
            for m in all_members:
                if m.id in excluded_id_set:
                    reason = next(
                        (item["reason"] for item in excluded_list if item["member_id"] == m.id),
                        "unknown",
                    )
                    excluded_pairs.append((m, reason))

    # 推导标的列表（按功能开关路由新旧逻辑）
    # - PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=false（默认）：持仓 + 最新扫描
    # - PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=true：按有效日期读取成员
    symbol_ids, source_label = _resolve_symbol_ids(
        db, portfolio_id, start_date, end_date,
        only_auto=only_auto, exclude_member_ids=excluded_member_ids or None,
    )

    # 构造 rule_config
    rule = get_active_rule(db, portfolio_id)
    rule_config = _build_rule_config(rule)

    # 回测名称
    if run_name is None:
        run_name = f"组合整体回测 {start_date.isoformat()}~{end_date.isoformat()}"

    # WP7.3 构建快照字段
    snapshot_kwargs: dict[str, Any] = {
        "engine_name": _ENGINE_NAME,
        "engine_version": _ENGINE_VERSION,
        "source_type": "member" if source_label == "members" else "legacy_scan",
    }
    if source_label == "members":
        # 取回测实际使用的成员（已应用 only_auto/exclude 过滤）
        used_members = _get_effective_members_for_window(
            db, portfolio_id, start_date, end_date,
            only_auto=only_auto, exclude_member_ids=excluded_member_ids or None,
        )
        member_snapshot = build_member_snapshot(used_members)
        excluded_snapshot = build_excluded_members_snapshot(excluded_pairs)
        cost_snapshot = build_cost_config_snapshot(portfolio)
        data_cutoff_at = _compute_data_cutoff_at(db, symbol_ids, end_date)
        snapshot_kwargs.update({
            "member_snapshot_json": json.dumps(member_snapshot, ensure_ascii=False),
            "symbol_ids_json": json.dumps(symbol_ids),
            "excluded_members_json": json.dumps(excluded_snapshot, ensure_ascii=False),
            "portfolio_rule_version_id": rule.id if rule is not None else None,
            "score_mode": "auto_trade_signal",
            "data_cutoff_at": data_cutoff_at,
        })
        # cost_snapshot 写入 BacktestRun.cost_config_json 字段（已存在）
        # 通过 cost_config 参数传给 run_backtest，由其统一序列化
        cost_config_for_run = cost_snapshot
    else:
        cost_config_for_run = None

    # 调用已有 run_backtest（initial_capital 由 run_backtest 内部从 portfolio.total_capital 取）
    run = run_backtest(
        db=db,
        portfolio_id=portfolio_id,
        symbol_ids=symbol_ids,
        start_date=start_date,
        end_date=end_date,
        rule_config=rule_config,
        cost_config=cost_config_for_run,
        run_name=run_name,
        **snapshot_kwargs,
    )

    return {
        "run_id": run.id,
        "portfolio_id": portfolio_id,
        "symbol_ids": symbol_ids,
        "symbol_count": len(symbol_ids),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "initial_capital": float(portfolio.total_capital),
        "status": run.status,
        "run_name": run.run_name,
        # WP7.1: 标的来源标签（"legacy"=持仓+最新扫描 / "members"=历史有效成员）
        # 用于双跑切换期间的诊断与未来 UI 显示（spec line 274）。
        # 注：PortfolioBacktestResult schema 暂未声明此字段，Pydantic v2 默认忽略，
        # 不影响 API 响应结构；后续 WP7.x UI 改造时再正式加入 schema。
        "symbol_source": source_label,
        # WP7.3: 同步暴露 source_type 供 UI 显示
        "source_type": snapshot_kwargs["source_type"],
        # WP7.3: 仅回测自动成员时返回被排除的成员数，便于 UI 提示
        "excluded_member_count": len(excluded_pairs),
    }


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
