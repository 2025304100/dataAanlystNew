"""基于 PortfolioMember 的自动交易执行逻辑（WP6.2）。

与旧逻辑（基于 Score.action + latest scan executable，位于 auto_trade_task.py）
双轨并存。由 AUTO_TRADE_MEMBER_SOURCE_ENABLED 开关控制（WP6.4 实现）。

不修改 auto_trade_task.py，本模块独立实现基于成员的执行逻辑。

买入候选（spec line 230/233）：
    active PortfolioMember AND execution_mode=auto AND 当前无持仓
    AND 最新有效信号允许买入 AND 数据健康通过 AND 组合风控通过

卖出侧（spec line 231/234）：
    覆盖所有当前持仓，即使成员暂停或归档，不因成员状态漏掉止损/退出信号

执行模式（spec line 232/235）：
    - manual：只提示信号不下单
    - confirm：只生成待确认订单计划
    - auto：满足规则后程序自动模拟下单

冲突优先级（spec line 233/236）：
    手动锁定 → 组合级风险强制减仓/清仓 → 自动卖出规则 → 自动买入规则 → 普通信号建议
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio, Position
from app.models.daily_bar import DailyBar
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    EXECUTION_CONFIRM,
    EXECUTION_MANUAL,
    PortfolioMember,
    STATUS_ACTIVE,
)
from app.models.score import Score
from app.models.sim_account import SimOrder
from app.models.symbol import Symbol
from app.services.decision_clock import utcnow_naive
from app.services.portfolio_asset_scope import allows_asset_type, ensure_symbol_in_scope
from app.services.portfolio_members import has_position, list_members

logger = logging.getLogger(__name__)


# 触发买入的 action 集合
_BUY_ACTIONS = {"open", "buy_dip"}
# 触发卖出的 action 集合
_SELL_ACTIONS = {"exit", "reduce"}


def _snapshot_member_symbol_ids(snapshot: Any) -> set[int]:
    """Read the immutable snapshot member universe without consulting live members."""
    try:
        payload = json.loads(snapshot.member_snapshot_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return set()
    members = payload.get("members", []) if isinstance(payload, dict) else payload
    if not isinstance(members, list):
        return set()
    symbol_ids: set[int] = set()
    for member in members:
        if not isinstance(member, dict):
            continue
        try:
            symbol_ids.add(int(member["symbol_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return symbol_ids


def _build_snapshot_decision_state_context(
    db: Session | None,
    *,
    portfolio_id: int,
    strategy_snapshot_id: str,
    trade_date: date,
    dry_run: bool,
) -> Any | None:
    """Build the historical/live state that is authoritative for auto decisions.

    Automatic simulation used to let ``DecisionEngine`` read current positions
    while omitting its cash and PIT price inputs.  That made the allocator emit
    a different plan than the same snapshot through dry-run/backtest.  Keep the
    state assembly at the public auto adapter boundary, matching the backtest
    contract: T-day bars provide decision inputs and the first later bar
    provides the declared NEXT_OPEN price assumption.
    """
    if db is None:
        return None

    from app.models.decision_engine import StrategyExecutionSnapshot
    from app.models.sim_account import CashLedger
    from app.services.decision_engine import DecisionStateContext
    from app.services.sim_accounts import cash_balance, ensure_sim_account_seed

    portfolio = db.get(Portfolio, portfolio_id)
    snapshot = db.get(StrategyExecutionSnapshot, strategy_snapshot_id)
    if portfolio is None or snapshot is None:
        return None

    member_symbol_ids = _snapshot_member_symbol_ids(snapshot)
    positions = list(db.execute(
        select(Position).where(Position.portfolio_id == portfolio_id)
    ).scalars().all())
    symbol_ids = member_symbol_ids | {int(position.symbol_id) for position in positions}

    try:
        snapshot_cost = json.loads(snapshot.cost_config_json or "{}")
    except (TypeError, json.JSONDecodeError):
        snapshot_cost = {}

    # A real run must initialize the simulated account before using its cash
    # state.  A dry-run remains read-only: an absent initial ledger represents
    # the portfolio's configured opening cash, just as it would at execution.
    if dry_run:
        has_ledger = db.execute(
            select(CashLedger.id)
            .where(CashLedger.portfolio_id == portfolio_id)
            .limit(1)
        ).scalar_one_or_none() is not None
        available_cash = (
            float(cash_balance(db, portfolio_id))
            if has_ledger else float(portfolio.total_capital or 0.0)
        )
    else:
        ensure_sim_account_seed(db, portfolio)
        available_cash = float(cash_balance(db, portfolio_id))

    if not symbol_ids:
        return DecisionStateContext(
            available_cash=available_cash,
            total_capital=float(portfolio.total_capital or 0.0),
            cost_config=snapshot_cost,
        )

    symbols = {
        int(symbol.id): symbol
        for symbol in db.execute(select(Symbol).where(Symbol.id.in_(symbol_ids))).scalars()
    }
    # Read the immutable historical bar set once. Selecting the nearest prior
    # and next rows in Python keeps this adapter portable across SQLite/MySQL
    # and avoids a correlated alias query whose semantics differ by dialect.
    bars_by_symbol: dict[int, list[DailyBar]] = {}
    for bar in db.execute(
        select(DailyBar)
        .where(DailyBar.symbol_id.in_(symbol_ids))
        .order_by(DailyBar.symbol_id, DailyBar.trade_date, DailyBar.id)
    ).scalars():
        bars_by_symbol.setdefault(int(bar.symbol_id), []).append(bar)
    signal_bars: dict[int, DailyBar] = {}
    previous_bars: dict[int, DailyBar] = {}
    next_bars: dict[int, DailyBar] = {}
    for symbol_id, bars in bars_by_symbol.items():
        same_day = [bar for bar in bars if bar.trade_date == trade_date]
        if same_day:
            signal_bars[symbol_id] = same_day[-1]
        before = [bar for bar in bars if bar.trade_date < trade_date]
        after = [bar for bar in bars if bar.trade_date > trade_date]
        if before:
            previous_bars[symbol_id] = before[-1]
        if after:
            next_bars[symbol_id] = after[0]

    total_capital = float(portfolio.total_capital or 0.0)
    current_by_symbol: dict[int, dict[str, Any]] = {}
    current_asset_pct: dict[str, float] = {}
    current_sector_pct: dict[str, float] = {}
    for position in positions:
        symbol_id = int(position.symbol_id)
        symbol = symbols.get(symbol_id)
        signal_bar = signal_bars.get(symbol_id)
        mark = (
            float(signal_bar.close)
            if signal_bar is not None and signal_bar.close is not None
            else float(position.latest_price or position.avg_cost or 0.0)
        )
        quantity = float(position.quantity or 0.0)
        market_value = mark * quantity
        pct = market_value / total_capital if total_capital > 0 else 0.0
        asset_type = str(
            getattr(symbol, "asset_type", None) or position.asset_type or "stock"
        ).lower()
        sector = str(getattr(symbol, "industry", None) or "unclassified")
        current_by_symbol[symbol_id] = {
            "qty": quantity,
            "pct": pct,
            "market_value": market_value,
            "asset_type": asset_type,
            "sector": sector,
        }
        if quantity > 0:
            current_asset_pct[asset_type] = current_asset_pct.get(asset_type, 0.0) + pct
            current_sector_pct[sector] = current_sector_pct.get(sector, 0.0) + pct

    price_data_by_symbol: dict[int, dict[str, Any]] = {}
    for symbol_id in symbol_ids:
        signal_bar = signal_bars.get(symbol_id)
        if signal_bar is None:
            continue
        next_bar = next_bars.get(symbol_id)
        previous_bar = previous_bars.get(symbol_id)
        intended_open = (
            float(next_bar.open)
            if next_bar is not None and next_bar.open is not None
            else (
                float(next_bar.close)
                if next_bar is not None and next_bar.close is not None
                else float(signal_bar.open)
            )
        )
        price_data_by_symbol[symbol_id] = {
            "open_price": intended_open,
            "close_price": float(signal_bar.close),
            "high_price": float(signal_bar.high),
            "low_price": float(signal_bar.low),
            "volume": float(signal_bar.volume or 0.0),
            "prev_close_price": (
                float(previous_bar.close)
                if previous_bar is not None and previous_bar.close is not None else None
            ),
            "is_suspended_today": bool((signal_bar.volume or 0) <= 0),
            "available_at": signal_bar.created_at,
            "_auto_signal_open": float(signal_bar.open),
        }

    return DecisionStateContext(
        current_by_symbol=current_by_symbol,
        current_asset_pct=current_asset_pct,
        current_sector_pct=current_sector_pct,
        available_cash=available_cash,
        total_capital=total_capital,
        price_data_by_symbol=price_data_by_symbol,
        cost_config=snapshot_cost,
    )


@dataclass
class MemberTradeCandidate:
    """成员交易候选。"""

    member: PortfolioMember | None
    symbol_id: int
    symbol: str | None
    action: str  # open/buy_dip/hold/reduce/exit
    score: float | None
    signal_id: int | None
    signal_snapshot: dict | None
    data_health_ok: bool
    risk_check_ok: bool
    rejection_code: str | None = None
    rejection_detail: str | None = None
    data_cutoff_at: str | None = None  # ISO 8601，决策快照追溯用


@dataclass
class TradeDecision:
    """交易决策。"""

    portfolio_id: int
    member: PortfolioMember | None
    symbol_id: int
    side: str  # buy/sell
    quantity: float
    price: float
    action: str  # open/buy_dip/reduce/exit
    execution_mode: str  # manual/confirm/auto
    signal_id: int | None
    signal_snapshot: dict | None
    rule_version_id: int | None
    client_order_key: str
    decision_snapshot: dict
    rejection_code: str | None = None
    rejection_detail: str | None = None


def _now_utc() -> datetime:
    """当前 UTC 时间（naive，与项目其他模型一致）。

    T-C1 Q1.1：禁止本文件出现 [当前时间裸调用]；统一走 decision_clock.utcnow_naive()。
    """
    return utcnow_naive()


# ----------------------------------------------------------------------------
# 信号查询（复用 Score，WP6 后续可独立信号模块）
# ----------------------------------------------------------------------------


def _get_latest_signal(
    db: Session, symbol_id: int
) -> tuple[dict | None, int | None, dict | None]:
    """获取最新有效信号。

    WP6.2 阶段复用 Score 逻辑（与旧 auto_trade_task 一致）。
    返回 (score_dict, signal_id, signal_snapshot)。
    """
    try:
        score = db.execute(
            select(Score)
            .where(Score.symbol_id == symbol_id)
            .order_by(Score.trade_date.desc(), Score.id.desc())
            .limit(1)
        ).scalars().first()

        if score is None:
            return None, None, None

        score_dict = {
            "action": score.action,
            "score": float(score.priority_score) if score.priority_score is not None else None,
            "stage": score.stage,
        }
        signal_snapshot = {
            "action": score.action,
            "score": score_dict["score"],
            "stage": score.stage,
            "trade_date": score.trade_date.isoformat() if score.trade_date else None,
            "created_at": score.created_at.isoformat() if score.created_at else None,
        }
        return score_dict, score.id, signal_snapshot
    except Exception as exc:
        # 不暴露敏感信息，仅记录概要
        logger.warning("获取信号失败 symbol_id=%s: %s", symbol_id, exc)
        return None, None, None


def _get_symbol_code(db: Session, symbol_id: int) -> str | None:
    """获取标的代码。"""
    symbol = db.get(Symbol, symbol_id)
    return symbol.symbol if symbol else None


# ----------------------------------------------------------------------------
# 数据健康 / 组合风控（WP6.5 实现详细检查，WP6.2 占位返回通过）
# ----------------------------------------------------------------------------


def _check_data_health(
    db: Session,
    symbol_id: int,
    rule_version_id: int | None = None,
) -> tuple[bool, str]:
    """数据健康检查（WP6.5 fail-closed 接入安全门禁）。

    检查 K 线/评分/规则版本是否过期，任一过期则 fail-closed 禁止买入。
    调用 app.services.auto_trade_safety.check_data_health 实现。
    错误消息不暴露敏感信息（脱敏后的概要描述）。
    """
    from app.services.auto_trade_safety import check_data_health

    result = check_data_health(
        db, symbol_id=symbol_id, rule_version_id=rule_version_id
    )
    return result.healthy, result.reason


def _get_data_cutoff_at(
    db: Session,
    symbol_id: int,
    rule_version_id: int | None = None,
) -> str | None:
    """获取数据截止时间（ISO 8601 字符串，用于决策快照追溯）。

    取最新 K 线时间与评分时间的较新者作为 data_cutoff_at。
    即使数据健康检查不通过（过期），仍返回可用的时间戳用于追溯。
    若两者均缺失，返回 None。
    """
    from app.services.auto_trade_safety import check_data_health

    result = check_data_health(
        db, symbol_id=symbol_id, rule_version_id=rule_version_id
    )
    cutoff: datetime | None = None
    if result.kline_latest_at:
        cutoff = result.kline_latest_at
    if result.score_latest_at:
        if cutoff is None or result.score_latest_at > cutoff:
            cutoff = result.score_latest_at
    return cutoff.isoformat() if cutoff else None


def _check_portfolio_risk(db: Session, portfolio_id: int) -> tuple[bool, str]:
    """组合风控检查（WP6.5 占位，详细风控在后续完善）。

    买入组合级风控：仓位/集中度/风险预算等。
    第一阶段返回 True（占位），实际风控规则在 WP6.5 后续完善。
    """
    # TODO: WP6.5 后续接入组合级风控规则（仓位/集中度/预算）
    return True, ""


# ----------------------------------------------------------------------------
# 买入候选（auto 模式专用，spec line 229）
# ----------------------------------------------------------------------------


def get_buy_candidates(
    db: Session,
    *,
    portfolio_id: int,
) -> list[MemberTradeCandidate]:
    """获取买入候选（WP6.2，仅 auto 模式）。

    条件（spec line 229 全部满足）：
        - active PortfolioMember
        - execution_mode=auto
        - 当前无持仓
        - 最新有效信号允许买入（action ∈ {open, buy_dip}）
        - 数据健康通过
        - 组合风控通过

    manual/confirm 模式的成员不进入此列表（由 get_signal_candidates 处理）。
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        return []

    # 1. 获取 active 成员（list_members 默认排除归档）
    members = list_members(
        db,
        portfolio_id=portfolio_id,
        status=STATUS_ACTIVE,
        include_archived=False,
    )

    candidates: list[MemberTradeCandidate] = []
    for member in members:
        # 只处理 auto 模式（confirm/manual 在 get_signal_candidates 中处理）
        if member.execution_mode != EXECUTION_AUTO:
            continue
        symbol = db.get(Symbol, member.symbol_id)
        if symbol is None or not allows_asset_type(portfolio.asset_scope, symbol.asset_type):
            continue

        # 2. 检查当前无持仓
        has_pos, _qty = has_position(
            db, portfolio_id=portfolio_id, symbol_id=member.symbol_id
        )
        if has_pos:
            continue  # 已有持仓，跳过买入

        # 3. 获取最新有效信号
        score, signal_id, signal_snapshot = _get_latest_signal(db, member.symbol_id)
        if score is None:
            continue  # 无信号，跳过

        action = score.get("action", "hold")
        if action not in _BUY_ACTIONS:
            continue  # 信号不允许买入

        # 4. 数据健康检查（WP6.5 接入 fail-closed 检查，含规则版本新鲜度）
        data_health_ok, data_reason = _check_data_health(
            db, member.symbol_id, rule_version_id=member.entry_rule_version_id
        )
        # 数据截止时间（用于决策快照追溯，即使健康检查不通过也记录）
        data_cutoff_at = _get_data_cutoff_at(
            db, member.symbol_id, rule_version_id=member.entry_rule_version_id
        )

        # 5. 组合风控检查
        risk_check_ok, risk_reason = _check_portfolio_risk(db, portfolio_id)

        rejection_code: str | None = None
        rejection_detail: str | None = None
        if not data_health_ok or not risk_check_ok:
            rejection_code = "BLOCKED"
            rejection_detail = f"data_health={data_reason}, risk={risk_reason}"

        candidates.append(
            MemberTradeCandidate(
                member=member,
                symbol_id=member.symbol_id,
                symbol=_get_symbol_code(db, member.symbol_id),
                action=action,
                score=score.get("score"),
                signal_id=signal_id,
                signal_snapshot=signal_snapshot,
                data_health_ok=data_health_ok,
                risk_check_ok=risk_check_ok,
                rejection_code=rejection_code,
                rejection_detail=rejection_detail,
                data_cutoff_at=data_cutoff_at,
            )
        )

    return candidates


# ----------------------------------------------------------------------------
# 信号候选（manual/confirm 模式，只生成信号提示/待确认计划）
# ----------------------------------------------------------------------------


def get_signal_candidates(
    db: Session,
    *,
    portfolio_id: int,
) -> list[MemberTradeCandidate]:
    """获取信号候选（manual/confirm 模式）。

    manual/confirm 模式不实际下单，仅生成信号提示/待确认订单计划。
    不应用数据健康/风控阻断（由用户人工判断）。

    条件：
        - active PortfolioMember
        - execution_mode ∈ {manual, confirm}
        - 当前无持仓
        - 最新有效信号允许买入（action ∈ {open, buy_dip}）
    """
    members = list_members(
        db,
        portfolio_id=portfolio_id,
        status=STATUS_ACTIVE,
        include_archived=False,
    )

    candidates: list[MemberTradeCandidate] = []
    for member in members:
        if member.execution_mode not in (EXECUTION_MANUAL, EXECUTION_CONFIRM):
            continue

        has_pos, _qty = has_position(
            db, portfolio_id=portfolio_id, symbol_id=member.symbol_id
        )
        if has_pos:
            continue

        score, signal_id, signal_snapshot = _get_latest_signal(db, member.symbol_id)
        if score is None:
            continue

        action = score.get("action", "hold")
        if action not in _BUY_ACTIONS:
            continue

        # 数据截止时间（用于决策快照追溯；manual/confirm 不阻断，仅记录）
        data_cutoff_at = _get_data_cutoff_at(
            db, member.symbol_id, rule_version_id=member.entry_rule_version_id
        )

        candidates.append(
            MemberTradeCandidate(
                member=member,
                symbol_id=member.symbol_id,
                symbol=_get_symbol_code(db, member.symbol_id),
                action=action,
                score=score.get("score"),
                signal_id=signal_id,
                signal_snapshot=signal_snapshot,
                data_health_ok=True,
                risk_check_ok=True,
                data_cutoff_at=data_cutoff_at,
            )
        )

    return candidates


# ----------------------------------------------------------------------------
# 卖出候选（覆盖所有持仓，spec line 230）
# ----------------------------------------------------------------------------


def get_sell_candidates(
    db: Session,
    *,
    portfolio_id: int,
) -> list[MemberTradeCandidate]:
    """获取卖出候选（WP6.2）。

    卖出侧覆盖所有当前持仓，即使成员暂停或归档，
    不因成员状态漏掉止损/退出信号（spec line 230）。

    对每个持仓：
        1. 查找对应成员（可能 active/paused/archived 或无成员，历史持仓）
        2. 获取最新信号
        3. 如果信号 action ∈ {reduce, exit}，加入卖出候选
    """
    # 1. 获取所有非零持仓
    positions = db.execute(
        select(Position).where(
            and_(
                Position.portfolio_id == portfolio_id,
                Position.quantity != 0,
            )
        )
    ).scalars().all()

    candidates: list[MemberTradeCandidate] = []
    for pos in positions:
        # 2. 查找对应成员（可能无成员，历史持仓）
        # 注意：不限制 status，paused/archived 成员的持仓也要处理
        member = db.execute(
            select(PortfolioMember).where(
                and_(
                    PortfolioMember.portfolio_id == portfolio_id,
                    PortfolioMember.symbol_id == pos.symbol_id,
                    PortfolioMember.effective_to.is_(None),
                )
            )
        ).scalars().first()

        # 即使成员暂停/归档/无成员，仍检查卖出信号
        score, signal_id, signal_snapshot = _get_latest_signal(db, pos.symbol_id)
        if score is None:
            continue

        action = score.get("action", "hold")
        if action not in _SELL_ACTIONS:
            continue

        # WP6.5 卖出风控：数据缺失时生成高优先级告警，但不阻断卖出（止损保护）
        # 调用 check_sell_risk 触发告警，结果忽略（卖出始终允许）
        from app.services.auto_trade_safety import check_sell_risk

        check_sell_risk(
            db, symbol_id=pos.symbol_id, portfolio_id=portfolio_id
        )

        # 数据截止时间（用于决策快照追溯；卖出始终允许，仅记录）
        sell_rule_version_id = (
            member.exit_rule_version_id if member is not None else None
        )
        data_cutoff_at = _get_data_cutoff_at(
            db, pos.symbol_id, rule_version_id=sell_rule_version_id
        )

        candidates.append(
            MemberTradeCandidate(
                member=member,  # 可能为 None
                symbol_id=pos.symbol_id,
                symbol=_get_symbol_code(db, pos.symbol_id),
                action=action,
                score=score.get("score"),
                signal_id=signal_id,
                signal_snapshot=signal_snapshot,
                # 卖出不因数据缺失静默跳过（spec WP6.5 安全门禁）
                # check_sell_risk 已在数据缺失时触发告警，这里始终允许卖出
                data_health_ok=True,
                risk_check_ok=True,
                data_cutoff_at=data_cutoff_at,
            )
        )

    return candidates


# ----------------------------------------------------------------------------
# 幂等订单键（WP6.3）
# ----------------------------------------------------------------------------


def build_client_order_key(
    *,
    portfolio_id: int,
    member_id: int | None,
    signal_date: str,
    side: str,
    rule_version_id: int | None,
) -> str:
    """构建客户端订单键（WP6.3 幂等）。

    格式：portfolio_id:member_id:signal_date:side:rule_version_id
    """
    return f"{portfolio_id}:{member_id or 0}:{signal_date}:{side}:{rule_version_id or 0}"


# ----------------------------------------------------------------------------
# 决策生成（冲突优先级 spec line 233）
# ----------------------------------------------------------------------------


def decide_trades(
    db: Session,
    *,
    portfolio_id: int,
) -> list[TradeDecision]:
    """生成交易决策（WP6.2）。

    按 spec 冲突优先级（spec line 233）：
        1. 手动锁定（manual_lock=True）→ 跳过
        2. 组合级风险强制减仓/清仓 → 生成卖出（WP6.5 实现，WP6.2 占位）
        3. 自动卖出规则（action ∈ {reduce, exit}）→ 生成卖出
        4. 自动买入规则（action ∈ {open, buy_dip}）→ 生成买入
        5. 普通信号建议 → 不下单

    返回所有决策（含被拒绝的，带 rejection_code）。
    决策顺序：卖出在前，买入在后（卖出优先级高于买入）。
    """
    decisions: list[TradeDecision] = []
    signal_date = _now_utc().strftime("%Y-%m-%d")
    # 决策时间戳（UTC ISO 8601，用于 decision_snapshot 追溯）
    decision_timestamp = _now_utc().isoformat()

    # ==================================================================
    # 1. 卖出候选（优先级高于买入）
    # ==================================================================
    sell_candidates = get_sell_candidates(db, portfolio_id=portfolio_id)
    for cand in sell_candidates:
        # 手动锁定：跳过（最高优先级）
        if cand.member is not None and cand.member.manual_lock:
            continue

        # 卖出决策默认使用 auto 模式（即使成员暂停/归档也执行止损）
        # 若成员存在且非 auto，仍以 auto 执行卖出（止损保护）
        execution_mode = EXECUTION_AUTO

        rule_version_id = (
            cand.member.exit_rule_version_id if cand.member is not None else None
        )

        decisions.append(
            TradeDecision(
                portfolio_id=portfolio_id,
                member=cand.member,
                symbol_id=cand.symbol_id,
                side="sell",
                quantity=0,  # 实际数量在执行时计算
                price=0,  # 实际价格在执行时获取
                action=cand.action,
                execution_mode=execution_mode,
                signal_id=cand.signal_id,
                signal_snapshot=cand.signal_snapshot,
                rule_version_id=rule_version_id,
                client_order_key=build_client_order_key(
                    portfolio_id=portfolio_id,
                    member_id=cand.member.id if cand.member else None,
                    signal_date=signal_date,
                    side="sell",
                    rule_version_id=rule_version_id,
                ),
                decision_snapshot={
                    "action": cand.action,
                    "symbol": cand.symbol,
                    "score": cand.score,
                    "signal_id": cand.signal_id,
                    "source": "sell_rule",
                    "member_id": cand.member.id if cand.member else None,
                    "rule_version_id": rule_version_id,
                    "data_cutoff_at": cand.data_cutoff_at,
                    "decision_timestamp": decision_timestamp,
                },
            )
        )

    # ==================================================================
    # 2. 自动买入候选（auto 模式）
    # ==================================================================
    buy_candidates = get_buy_candidates(db, portfolio_id=portfolio_id)
    for cand in buy_candidates:
        # 手动锁定：跳过
        if cand.member.manual_lock:
            continue

        rule_version_id = cand.member.entry_rule_version_id

        decision = TradeDecision(
            portfolio_id=portfolio_id,
            member=cand.member,
            symbol_id=cand.symbol_id,
            side="buy",
            quantity=0,
            price=0,
            action=cand.action,
            execution_mode=cand.member.execution_mode,
            signal_id=cand.signal_id,
            signal_snapshot=cand.signal_snapshot,
            rule_version_id=rule_version_id,
            client_order_key=build_client_order_key(
                portfolio_id=portfolio_id,
                member_id=cand.member.id,
                signal_date=signal_date,
                side="buy",
                rule_version_id=rule_version_id,
            ),
            decision_snapshot={
                "action": cand.action,
                "symbol": cand.symbol,
                "score": cand.score,
                "signal_id": cand.signal_id,
                "source": "buy_rule",
                "member_id": cand.member.id,
                "rule_version_id": rule_version_id,
                "data_cutoff_at": cand.data_cutoff_at,
                "decision_timestamp": decision_timestamp,
            },
        )

        # 数据健康/风控不通过：生成拒绝决策
        if cand.rejection_code:
            decision.rejection_code = cand.rejection_code
            decision.rejection_detail = cand.rejection_detail

        decisions.append(decision)

    # ==================================================================
    # 3. 信号候选（manual/confirm 模式，仅生成提示/计划）
    # ==================================================================
    signal_candidates = get_signal_candidates(db, portfolio_id=portfolio_id)
    for cand in signal_candidates:
        # 手动锁定：跳过
        if cand.member.manual_lock:
            continue

        rule_version_id = cand.member.entry_rule_version_id

        decisions.append(
            TradeDecision(
                portfolio_id=portfolio_id,
                member=cand.member,
                symbol_id=cand.symbol_id,
                side="buy",
                quantity=0,
                price=0,
                action=cand.action,
                execution_mode=cand.member.execution_mode,
                signal_id=cand.signal_id,
                signal_snapshot=cand.signal_snapshot,
                rule_version_id=rule_version_id,
                client_order_key=build_client_order_key(
                    portfolio_id=portfolio_id,
                    member_id=cand.member.id,
                    signal_date=signal_date,
                    side="buy",
                    rule_version_id=rule_version_id,
                ),
                decision_snapshot={
                    "action": cand.action,
                    "symbol": cand.symbol,
                    "score": cand.score,
                    "signal_id": cand.signal_id,
                    "source": "signal_plan",
                    "member_id": cand.member.id,
                    "rule_version_id": rule_version_id,
                    "data_cutoff_at": cand.data_cutoff_at,
                    "decision_timestamp": decision_timestamp,
                },
            )
        )

    return decisions


# ----------------------------------------------------------------------------
# 执行（按 execution_mode 路由）
# ----------------------------------------------------------------------------


def _get_latest_price(db: Session, symbol_id: int) -> float | None:
    """获取最新价格（复用 sim_accounts.latest_price_for_symbol）。"""
    from app.services.sim_accounts import latest_price_for_symbol

    return latest_price_for_symbol(db, symbol_id)


def _calculate_position_size(
    db: Session, decision: TradeDecision, price: float
) -> float:
    """WP0-4b / C-01 修复：统一仓位计算。

    不再硬编码固定 100.0 股。改为：
    1) 读取 PortfolioRule + portfolio.total_capital
    2) 调用 sequential_clamp_allocate (Q10)，约束:
       总仓位 → 资产类型 → 行业 → 单票 → 现金 → 最小手数
    3) allocation 失败或无有效数量时保持 HOLD(0) 由调用方决定是否阻断。

    向下兼容：当规则缺失时退化为 0 (Q6 fail-closed)，
    研究模式下可显式 fallback 到 compute_position_budget 推荐值。
    """
    from app.services.decision_engine import (
        sequential_clamp_allocate, LoadedSnapshot, SignalResult,
    )

    portfolio = db.get(Portfolio, decision.portfolio_id)
    if portfolio is None:
        return 0.0
    # 规则：active rule or latest by id
    from app.models.portfolio import PortfolioRule
    rule_row = db.execute(
        select(PortfolioRule).where(
            PortfolioRule.portfolio_id == decision.portfolio_id,
            PortfolioRule.is_active == 1,
        ).order_by(PortfolioRule.id.desc()).limit(1)
    ).scalars().first()
    rule_id = int(rule_row.id) if rule_row is not None else None
    rule_version = int(getattr(rule_row, "version", 0) or 0) if rule_row else 0

    cost_cfg = {
        "min_lot_size": 100,
        "slippage_buy_bps": 5,
        "slippage_sell_bps": 5,
    }

    # 构造临时 snapshot-like 容器（兼容 sequential_clamp 的 LoadedSnapshot 协议）
    class _SnapShim:
        def __init__(self):
            self.portfolio_id = decision.portfolio_id
            self.portfolio = portfolio
            self.factor_model_run_id = ""
            self.factor_set_id = None
            self.rule_id = rule_id
            self.rule_version = rule_version
            self.benchmark_code = getattr(portfolio, "benchmark_code", "000300")
            self.cost_config = cost_cfg
            self.members = [{"symbol_id": decision.symbol_id}]

    snap = LoadedSnapshot(
        snapshot=_SnapShim(),  # type: ignore[arg-type]
        factor_model_run_id="",
        factor_set_id=None,
        rule_id=rule_id,
        rule_version=rule_version,
        members=[{"symbol_id": decision.symbol_id}],
        gate_policy_version="research-v0",
        cost_config=cost_cfg,
        versions={"run_mode": "research"},
    )

    # direction 映射：decision.action/side
    if decision.side == "sell" or decision.action in {"exit", "reduce"}:
        direction = "EXIT" if decision.action == "exit" else "REDUCE"
    else:
        direction = "BUY"

    _stage = "growth"
    if isinstance(decision.decision_snapshot, dict):
        _stage = str(decision.decision_snapshot.get("stage", "growth"))
    signal = SignalResult(items=[{
        "symbol_id": int(decision.symbol_id),
        "direction": direction,
        "price": float(price),
        "stage": _stage,
    }])

    fallback_net_value = float(getattr(portfolio, "total_capital", 0) or 0)
    alloc = sequential_clamp_allocate(
        db, snap, signal,
        cutoff_utc=_now_utc(),  # cutoff 不直接限制 pct；仅作协议占位
        portfolio_id=decision.portfolio_id,
        fallback_portfolio_net_value=fallback_net_value,
        fallback_prices={int(decision.symbol_id): float(price)},
    )

    if not alloc.items:
        return 0.0
    item = alloc.items[0]
    qty = item.get("target_quantity")
    if qty is None or qty <= 0:
        # 不足最小手数(Q10.2)：由调用方判断阻断
        if item.get("rejection_subtype") == "ORDER_BELOW_LOT_SIZE":
            decision.rejection_code = "ORDER_BELOW_LOT_SIZE"
            decision.rejection_detail = "目标仓位不足最小交易单位，已被 sequential_clamp 阻断 (Q10.2)"
        # 卖出动作：至少返回 100 股占位（由上层 exit/reduce 语义覆盖实际数量）
        if direction in {"EXIT", "REDUCE"}:
            return 100.0
        return 0.0
    return float(qty)


def _execute_order(db: Session, decision: TradeDecision) -> SimOrder:
    """执行订单（调用现有 place_sim_order）。

    填充归因字段（WP6.1 已扩展的 SimOrder 字段）。
    """
    from app.services.sim_accounts import place_sim_order

    portfolio = db.get(Portfolio, decision.portfolio_id)
    if portfolio is None:
        raise ValueError(f"Portfolio {decision.portfolio_id} not found")

    symbol = db.get(Symbol, decision.symbol_id)
    if symbol is None:
        raise ValueError(f"Symbol {decision.symbol_id} not found")
    # A legacy out-of-scope holding may always be sold to unwind risk, but it
    # must never produce a new buy after a portfolio is narrowed to one asset.
    if decision.side == "buy":
        ensure_symbol_in_scope(portfolio, symbol)

    # 获取实际价格
    price = _get_latest_price(db, decision.symbol_id)
    if price is None or price <= 0:
        raise ValueError(f"无法获取标的 {symbol.symbol} 的最新价格")

    # 卖出数量只由历史持仓和最小手数决定；不得用固定数量兜底。
    if decision.side == "sell":
        pos = db.execute(
            select(Position).where(
                and_(
                    Position.portfolio_id == decision.portfolio_id,
                    Position.symbol_id == decision.symbol_id,
                )
            )
        ).scalars().first()
        held_qty = float(pos.quantity if pos and pos.quantity else 0)
        if held_qty <= 0:
            raise ValueError(f"标的 {symbol.symbol} 无持仓可卖")
        if decision.action == "exit":
            quantity = held_qty  # 全部卖出
        else:  # reduce
            from app.services.sim_accounts import lot_size_for_symbol

            lot_size = int(lot_size_for_symbol(symbol) or 1)
            quantity = (int(held_qty / 2) // lot_size) * lot_size
            if quantity <= 0:
                raise ValueError(
                    f"标的 {symbol.symbol} 减仓后不足最小交易单位 ({lot_size})"
                )
    else:
        # 买入仓位必须来自统一 allocator；失败时保持 fail-closed。
        quantity = _calculate_position_size(db, decision, price)
        if quantity <= 0:
            raise ValueError(f"标的 {symbol.symbol} 计算仓位为 0")

    order, _trade = place_sim_order(
        db=db,
        portfolio=portfolio,
        symbol=symbol,
        side=decision.side,
        quantity=quantity,
        price=price,
        order_type="market",
        note=f"Member source auto-trade: action={decision.action}, mode={decision.execution_mode}",
        enforce_rules=True,
        apply_fees=True,
    )

    # 填充归因字段
    order.member_id = decision.member.id if decision.member else None
    order.source_type = "member"
    order.source_id = decision.member.id if decision.member else None
    order.signal_id = decision.signal_id
    order.signal_snapshot_json = (
        json.dumps(decision.signal_snapshot, ensure_ascii=False)
        if decision.signal_snapshot
        else None
    )
    order.rule_version_id = decision.rule_version_id
    order.execution_mode = decision.execution_mode
    order.client_order_key = decision.client_order_key
    order.decision_snapshot_json = json.dumps(
        decision.decision_snapshot, ensure_ascii=False
    )

    db.commit()
    db.refresh(order)
    return order


def _execute_snapshot_order_plans(
    db: Session,
    *,
    portfolio_id: int,
    strategy_snapshot_id: str,
    trade_date: date,
    dry_run: bool,
) -> dict[str, Any]:
    """Execute the immutable DecisionEngine plan set for auto simulation.

    This is the stage-1 bridge used by the scheduled entry point.  Candidate
    selection, action and quantity are owned by DecisionEngine; this adapter
    only translates a plan into the existing simulated-account order API.
    """
    from app.models.decision_engine import DecisionEvidence, StrategyExecutionSnapshot
    from app.services.decision_engine import default_engine
    from app.services.sim_accounts import (
        apply_sim_order_fill,
        cash_balance,
        ensure_sim_account_seed,
        latest_price_for_symbol,
        place_sim_order,
    )
    from app.services.simulation_matching_engine import (
        CostModelConfig,
        MarketBar,
        OrderPlan as MatchingOrderPlan,
        OrderSide,
        match_order_plan,
    )
    from dataclasses import replace

    result: dict[str, Any] = {
        "portfolio_id": portfolio_id,
        "strategy_snapshot_id": strategy_snapshot_id,
        "decision_run_id": None,
        "buy_decisions": [],
        "sell_decisions": [],
        "signal_decisions": [],
        "rejected_decisions": [],
        "executed_orders": [],
        "pending_orders": [],
        "skipped_orders": [],
        "errors": [],
        "skipped_due_to_cancel": False,
    }
    state_context = _build_snapshot_decision_state_context(
        db,
        portfolio_id=portfolio_id,
        strategy_snapshot_id=strategy_snapshot_id,
        trade_date=trade_date,
        dry_run=bool(dry_run),
    )
    evaluated = default_engine.evaluate(
        db,
        portfolio_id=portfolio_id,
        strategy_snapshot_id=strategy_snapshot_id,
        trade_date=trade_date,
        run_type="auto_simulation",
        dry_run=bool(dry_run),
        state_context=state_context,
        match_mode="NEXT_OPEN",
    )
    snapshot_row = (
        db.get(StrategyExecutionSnapshot, strategy_snapshot_id)
        if db is not None else None
    )
    try:
        snapshot_cost = json.loads(snapshot_row.cost_config_json or "{}") if snapshot_row else {}
    except (TypeError, json.JSONDecodeError):
        snapshot_cost = {}
    default_commission_rate = float(snapshot_cost.get("commission_rate", 0.0003) or 0.0003)
    buy_commission_rate = float(
        snapshot_cost.get("buy_commission_pct", default_commission_rate) or 0.0
    )
    sell_commission_rate = float(
        snapshot_cost.get("sell_commission_pct", default_commission_rate) or 0.0
    )
    matching_cfg = CostModelConfig(
        commission_rate=default_commission_rate,
        min_commission=float(snapshot_cost.get("min_commission", 5.0) or 0.0),
        stamp_tax_rate=float(
            snapshot_cost.get("stamp_tax_rate", snapshot_cost.get("stamp_duty_pct", 0.001))
            or 0.0
        ),
        transfer_fee_rate=float(snapshot_cost.get("transfer_fee_rate", 0.00001) or 0.0),
        slippage_buy_bps=int(float(
            snapshot_cost.get("slippage_buy_bps", snapshot_cost.get("buy_slippage_bps", 5)) or 0
        )),
        slippage_sell_bps=int(float(
            snapshot_cost.get("slippage_sell_bps", snapshot_cost.get("sell_slippage_bps", 5)) or 0
        )),
        volume_limit_pct=(
            float(snapshot_cost["volume_limit_pct"])
            if snapshot_cost.get("volume_limit_pct") is not None else None
        ),
    )
    result["decision_run_id"] = str(evaluated.decision_run_id)

    def _json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    def _json_trace(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return [dict(item) for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []

    def _snapshot_payload(item: dict[str, Any], **execution: Any) -> str:
        payload = dict(item)
        payload["execution"] = execution
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _write_execution_evidence(
        evidence: Any,
        *,
        status: str,
        requested_quantity: float,
        filled_quantity: float,
        reason: str | None = None,
        detail: str | None = None,
        executed_price: float | None = None,
        slippage_bps: float | None = None,
        costs: Any | None = None,
        trace_entry: dict[str, Any] | None = None,
        retry_status: str | None = None,
    ) -> None:
        if evidence is None:
            return
        remaining = max(0.0, float(requested_quantity) - float(filled_quantity))
        versions = _json_dict(evidence.versions_json)
        versions.update({
            "order_plan_status": status,
            "order_plan_requested_quantity": float(requested_quantity),
            "order_plan_filled_quantity": float(filled_quantity),
            "order_plan_remaining_quantity": remaining,
            "order_plan_unfilled_reason": reason,
        })
        if retry_status is not None:
            versions["order_plan_next_retry_status"] = retry_status
        if costs is not None:
            versions.update({
                "order_plan_commission": float(costs.commission),
                "order_plan_stamp_tax": float(costs.stamp_tax),
                "order_plan_transfer_fee": float(costs.transfer_fee),
                "order_plan_total_cost": float(costs.total_cost),
            })
        evidence.versions_json = json.dumps(versions, ensure_ascii=False, sort_keys=True)
        evidence.executed_price = executed_price
        evidence.slippage_bps = slippage_bps
        evidence.rejection_reason = reason
        evidence.rejection_detail = detail
        if trace_entry is not None:
            trace = _json_trace(evidence.rejections_trace_json)
            trace.append(dict(trace_entry))
            evidence.rejections_trace_json = json.dumps(
                trace, ensure_ascii=False, sort_keys=True,
            )

    def _record_unfilled_order(
        *,
        portfolio: Portfolio,
        symbol: Symbol,
        plan: Any,
        item: dict[str, Any],
        status: str,
        rejection_code: str,
        rejection_detail: str,
        submitted_price: float,
    ) -> SimOrder:
        order = SimOrder(
            portfolio_id=portfolio.id,
            symbol_id=symbol.id,
            side="buy" if plan.action == "BUY" else "sell",
            order_type="market",
            quantity=float(plan.target_quantity),
            submitted_price=float(submitted_price),
            status=status,
            filled_quantity=0.0,
            filled_price=0.0,
            filled_amount=0.0,
            fee=0.0,
            note=f"DecisionOrderPlan {plan.order_plan_id}: {status}",
            source_type="decision_engine",
            client_order_key=str(plan.order_plan_id),
            decision_snapshot_json=_snapshot_payload(
                item,
                status=status,
                requested_quantity=float(plan.target_quantity),
                filled_quantity=0.0,
                remaining_quantity=float(plan.target_quantity),
                rejection_code=rejection_code,
                rejection_detail=rejection_detail,
            ),
            rejection_code=rejection_code,
            rejection_detail=rejection_detail,
            decision_evidence_id=str(plan.evidence_id),
        )
        db.add(order)
        db.flush()
        return order

    def _load_pending_retry_plans() -> tuple[list[Any], dict[str, SimOrder]]:
        """Rehydrate partial plans and select the next unattempted market bar.

        Partial fills are durable state, not a new decision.  The original
        order/evidence identity is retained while only the remaining quantity
        is sent through the matcher on the next available session.
        """
        if db is None:
            return [], {}
        rows = db.execute(
            select(SimOrder).where(
                SimOrder.portfolio_id == portfolio_id,
                SimOrder.status == "partial",
                SimOrder.source_type == "decision_engine",
                SimOrder.decision_evidence_id.is_not(None),
            ).order_by(SimOrder.id.asc())
        ).scalars().all()
        plans: list[Any] = []
        by_plan_id: dict[str, SimOrder] = {}
        for order in rows:
            evidence = db.get(DecisionEvidence, str(order.decision_evidence_id))
            if evidence is None or str(evidence.strategy_snapshot_id) != str(strategy_snapshot_id):
                continue
            remaining = max(0.0, float(order.quantity or 0.0) - float(order.filled_quantity or 0.0))
            if remaining <= 0:
                continue
            try:
                payload = json.loads(order.decision_snapshot_json or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                continue
            action = str(payload.get("action") or ("BUY" if order.side == "buy" else "SELL")).upper()
            if action not in {"BUY", "SELL"}:
                continue
            plan_id = str(payload.get("order_plan_id") or order.client_order_key or "")
            if not plan_id or plan_id in by_plan_id:
                continue
            try:
                original_execution_date = date.fromisoformat(str(payload.get("execution_date")))
            except (TypeError, ValueError):
                original_execution_date = evidence.trade_date
            try:
                signal_date = date.fromisoformat(str(payload.get("signal_date")))
            except (TypeError, ValueError):
                signal_date = evidence.trade_date
            trace = _json_trace(evidence.rejections_trace_json)
            if any(
                entry.get("reason") == "PARTIAL_FILL_RETRY"
                and str(entry.get("trigger_trade_date")) == trade_date.isoformat()
                for entry in trace
            ):
                # Replaying the same scheduler cycle must not advance the
                # pending order to a later bar or create another fill.
                continue
            attempted_dates = {
                str(entry.get("execution_date"))
                for entry in trace
                if entry.get("reason") == "PARTIAL_FILL_RETRY" and entry.get("execution_date")
            }
            next_dates = db.execute(
                select(DailyBar.trade_date)
                .where(
                    DailyBar.symbol_id == int(order.symbol_id),
                    DailyBar.trade_date > original_execution_date,
                )
                .distinct()
                .order_by(DailyBar.trade_date.asc())
            ).scalars().all()
            retry_date = next(
                (candidate for candidate in next_dates if candidate.isoformat() not in attempted_dates),
                None,
            )
            if retry_date is None:
                continue
            plan = SimpleNamespace(
                decision_run_id=str(payload.get("decision_run_id") or evidence.decision_run_id),
                evidence_id=str(order.decision_evidence_id),
                order_plan_id=plan_id,
                symbol_id=int(order.symbol_id),
                action=action,
                target_quantity=remaining,
                direction=action,
                intended_price=payload.get("intended_price"),
                reason_code=payload.get("reason_code") or "PARTIAL_FILL_RETRY",
                signal_date=signal_date,
                execution_date=retry_date,
                rejection_trace=trace,
            )
            plans.append(plan)
            by_plan_id[plan_id] = order
        return plans, by_plan_id

    pending_retry_plans, pending_retry_orders = _load_pending_retry_plans()
    # A decision replay may return the original plan as well.  Process the
    # durable retry once, then let the normal plan loop's idempotency branch
    # skip the duplicate value object.
    plans_to_process: list[Any] = list(pending_retry_plans)
    seen_plan_ids = {str(plan.order_plan_id) for plan in pending_retry_plans}
    for plan in evaluated.order_plans:
        if str(plan.order_plan_id) not in seen_plan_ids:
            plans_to_process.append(plan)
            seen_plan_ids.add(str(plan.order_plan_id))

    for plan in plans_to_process:
        pending_order = pending_retry_orders.get(str(plan.order_plan_id))
        requested_quantity = float(
            pending_order.quantity if pending_order is not None else plan.target_quantity
        )
        filled_before = float(pending_order.filled_quantity or 0.0) if pending_order is not None else 0.0
        is_retry = pending_order is not None
        item = {
            "symbol_id": int(plan.symbol_id),
            "action": str(plan.action),
            "quantity": float(plan.target_quantity),
            "requested_quantity": requested_quantity,
            "filled_before": filled_before,
            "intended_price": plan.intended_price,
            "reason_code": plan.reason_code,
            "evidence_id": str(plan.evidence_id),
            "order_plan_id": str(plan.order_plan_id),
            "signal_date": plan.signal_date.isoformat(),
            "execution_date": plan.execution_date.isoformat(),
        }
        if plan.action == "BUY":
            result["buy_decisions"].append(item)
        elif plan.action == "SELL":
            result["sell_decisions"].append(item)
        else:
            result["signal_decisions"].append(item)
        if plan.action not in {"BUY", "SELL"} or float(plan.target_quantity or 0) <= 0:
            continue
        try:
            # G6 manual-stop path: a new BUY must pass the durable composite
            # governance gate before it can reach matching or the account
            # ledger. A partially-filled existing plan is a retry, not a new
            # buy, and remains eligible to finish under its original audit id.
            if plan.action == "BUY" and not is_retry and db is not None:
                from app.services.portfolio_status import order_entry_gate_check
                from app.services.portfolio_state_machine import _get_status

                portfolio_state = _get_status(db, db.get(Portfolio, portfolio_id))
                gate = order_entry_gate_check(db, portfolio_id, "BUY")
                state_blocked = portfolio_state in {
                    "ADMIN_PAUSED",
                    "RECONCILIATION_BLOCKED",
                    "DATA_INCOMPLETE_PAUSED",
                }
                if state_blocked or not gate.allowed:
                    rejection_code = (
                        "PORTFOLIO_STATE_NEW_BUY_BLOCKED"
                        if state_blocked else str(gate.rejection_reason_code)
                    )
                    rejection_detail = (
                        f"portfolio_state={portfolio_state}; manual stop of new buys is active"
                        if state_blocked else str(gate.block_reason or "new buy blocked by governance gate")
                    )
                    item.update({
                        "rejection_code": rejection_code,
                        "rejection_detail": rejection_detail,
                    })
                    if dry_run:
                        result["rejected_decisions"].append(dict(item))
                        continue
                    portfolio = db.get(Portfolio, portfolio_id)
                    symbol = db.get(Symbol, int(plan.symbol_id))
                    evidence = db.get(DecisionEvidence, str(plan.evidence_id))
                    if portfolio is None or symbol is None:
                        raise ValueError("portfolio or symbol not found")
                    order = _record_unfilled_order(
                        portfolio=portfolio,
                        symbol=symbol,
                        plan=plan,
                        item=item,
                        status="rejected",
                        rejection_code=rejection_code,
                        rejection_detail=rejection_detail,
                        submitted_price=float(plan.intended_price or 0.0),
                    )
                    _write_execution_evidence(
                        evidence,
                        status="REJECTED_GOVERNANCE_GATE",
                        requested_quantity=requested_quantity,
                        filled_quantity=0.0,
                        reason=rejection_code,
                        detail=rejection_detail,
                    )
                    db.commit()
                    result["rejected_decisions"].append({**item, "order_id": order.id})
                    continue
            if dry_run:
                continue
            existing = db.execute(
                select(SimOrder).where(SimOrder.client_order_key == str(plan.order_plan_id))
            ).scalars().first()
            if existing is not None:
                if not is_retry or existing.status != "partial":
                    result["skipped_orders"].append({
                        "order_plan_id": str(plan.order_plan_id),
                        "existing_order_id": existing.id,
                        "reason": "idempotent_skip",
                    })
                    continue
                # The rehydrated plan is deliberately allowed through.  The
                # account adapter below accumulates its fill on this row.
                pending_order = existing
            portfolio = db.get(Portfolio, portfolio_id)
            symbol = db.get(Symbol, int(plan.symbol_id))
            if portfolio is None or symbol is None:
                raise ValueError("portfolio or symbol not found")
            price = plan.intended_price or latest_price_for_symbol(db, int(plan.symbol_id))
            if price is None or price <= 0:
                raise ValueError("no usable execution price")
            # The matching engine is the single source of market-rule
            # decisions.  Auto simulation may only use the account API after
            # this preflight succeeds; the API remains responsible for the
            # transactional position/cash ledger update.
            bar = db.execute(
                select(DailyBar)
                .where(
                    DailyBar.symbol_id == int(plan.symbol_id),
                    DailyBar.trade_date == plan.execution_date,
                )
            ).scalars().first()
            matching_bar = MarketBar(
                trade_date=plan.execution_date,
                open=float(bar.open) if bar is not None and bar.open is not None else float(price),
                high=float(bar.high) if bar is not None and bar.high is not None else float(price),
                low=float(bar.low) if bar is not None and bar.low is not None else float(price),
                close=float(bar.close) if bar is not None and bar.close is not None else float(price),
                volume=float(bar.volume) if bar is not None and bar.volume is not None else None,
                amount=float(bar.amount) if bar is not None and bar.amount is not None else None,
                halted=bool(bar is not None and (bar.volume or 0) <= 0),
            )
            matching_plan = MatchingOrderPlan(
                order_plan_id=str(plan.order_plan_id),
                symbol_id=int(plan.symbol_id),
                portfolio_id=portfolio.id,
                trade_date=plan.execution_date,
                price_type="NEXT_OPEN",
                side=OrderSide.BUY if plan.action == "BUY" else OrderSide.SELL,
                target_quantity=int(float(plan.target_quantity)),
                min_lot_size=100 if getattr(symbol, "market", None) in {"SH", "SZ", "BJ"} else 1,
                intended_price=float(price),
            )
            plan_matching_cfg = replace(
                matching_cfg,
                commission_rate=(
                    buy_commission_rate if plan.action == "BUY" else sell_commission_rate
                ),
            )
            match_result = match_order_plan(matching_plan, matching_bar, cfg=plan_matching_cfg)
            if match_result.final_status not in {"FILLED", "PARTIAL_FILL"}:
                evidence = db.get(DecisionEvidence, str(plan.evidence_id))
                if evidence is not None:
                    reason = match_result.reason_codes[0] if match_result.reason_codes else match_result.final_status
                    detail = match_result.note or ",".join(match_result.reason_codes)
                    if is_retry:
                        # A retry rejection keeps the original partial order
                        # open for a later session instead of erasing its
                        # already-filled quantity.
                        _write_execution_evidence(
                            evidence,
                            status="PARTIAL_FILL_PENDING",
                            requested_quantity=requested_quantity,
                            filled_quantity=filled_before,
                            reason=reason,
                            detail=detail,
                            trace_entry={
                                "date": plan.execution_date.isoformat(),
                                "execution_date": plan.execution_date.isoformat(),
                                "trigger_trade_date": trade_date.isoformat(),
                                "reason": "PARTIAL_FILL_RETRY",
                                "retry_result": reason,
                                "remaining_quantity": max(0.0, requested_quantity - filled_before),
                            },
                            retry_status="PENDING_RETRY" if match_result.retry_on_next_session else None,
                        )
                        pending_order.rejection_code = "PARTIAL_FILL_PENDING"
                        pending_order.rejection_detail = detail
                        db.commit()
                        result["pending_orders"].append({
                            **item,
                            "order_id": pending_order.id,
                            "reason": reason,
                            "remaining_quantity": max(0.0, requested_quantity - filled_before),
                            "retry_status": "PENDING_RETRY",
                        })
                        continue
                    evidence.rejection_reason = reason
                    evidence.rejection_detail = detail
                    _write_execution_evidence(
                        evidence,
                        status="REJECTED",
                        requested_quantity=requested_quantity,
                        filled_quantity=0.0,
                        reason=reason,
                        detail=detail,
                    )
                    db.commit()
                result["rejected_decisions"].append({
                    **item,
                    "rejection_code": match_result.reason_codes[0] if match_result.reason_codes else match_result.final_status,
                    "rejection_detail": match_result.note or ",".join(match_result.reason_codes),
                })
                continue

            # A matcher result can be executable while the simulated account
            # lacks cash. Check the actual fill quantity before either the
            # full or partial account path, and materialize a terminal
            # rejection instead of leaking a place_sim_order API exception.
            ensure_sim_account_seed(db, portfolio)
            required_cash = (
                float(match_result.filled_quantity)
                * float(match_result.executed_price or price)
                + float(match_result.total_cost)
            )
            available_cash = float(cash_balance(db, portfolio.id))
            if plan.action == "BUY" and required_cash > available_cash + 1e-9:
                detail = (
                    f"required={required_cash:.8f}, available={available_cash:.8f}; "
                    "order rejected for insufficient cash"
                )
                evidence = db.get(DecisionEvidence, str(plan.evidence_id))
                if is_retry:
                    order = pending_order
                    order.rejection_code = "INSUFFICIENT_CASH_PENDING"
                    order.rejection_detail = detail
                    _write_execution_evidence(
                        evidence,
                        status="PARTIAL_FILL_PENDING",
                        requested_quantity=requested_quantity,
                        filled_quantity=filled_before,
                        reason="INSUFFICIENT_CASH",
                        detail=detail,
                        trace_entry={
                            "date": plan.execution_date.isoformat(),
                            "execution_date": plan.execution_date.isoformat(),
                            "trigger_trade_date": trade_date.isoformat(),
                            "reason": "PARTIAL_FILL_RETRY",
                            "retry_result": "INSUFFICIENT_CASH",
                            "required_cash": required_cash,
                            "available_cash": available_cash,
                            "resolution": "PENDING_RETRY",
                        },
                        retry_status="PENDING_RETRY",
                    )
                    db.commit()
                    result["pending_orders"].append({
                        **item,
                        "order_id": order.id,
                        "reason": "INSUFFICIENT_CASH",
                        "remaining_quantity": max(0.0, requested_quantity - filled_before),
                        "retry_status": "PENDING_RETRY",
                    })
                    continue
                order = _record_unfilled_order(
                    portfolio=portfolio,
                    symbol=symbol,
                    plan=plan,
                    item=item,
                    status="rejected",
                    rejection_code="INSUFFICIENT_CASH",
                    rejection_detail=detail,
                    submitted_price=float(match_result.executed_price or price),
                )
                _write_execution_evidence(
                    evidence,
                    status="REJECTED_INSUFFICIENT_CASH",
                    requested_quantity=requested_quantity,
                    filled_quantity=0.0,
                    reason="INSUFFICIENT_CASH",
                    detail=detail,
                    trace_entry={
                        "date": plan.execution_date.isoformat(),
                        "reason": "INSUFFICIENT_CASH",
                        "required_cash": required_cash,
                        "available_cash": available_cash,
                        "resolution": "REJECTED",
                    },
                )
                db.commit()
                result["rejected_decisions"].append({
                    **item,
                    "rejection_code": "INSUFFICIENT_CASH",
                    "rejection_detail": detail,
                    "order_id": order.id,
                })
                continue

            if match_result.final_status == "PARTIAL_FILL":
                # Persist the actually filled leg through the account adapter,
                # then keep the requested quantity and remainder on the same
                # immutable order/evidence identity for the next session.
                ensure_sim_account_seed(db, portfolio)
                filled_quantity = float(match_result.filled_quantity)
                remaining_quantity = max(
                    0.0,
                    requested_quantity - filled_before - filled_quantity,
                )
                if is_retry:
                    order = pending_order
                    _trade = apply_sim_order_fill(
                        db=db,
                        portfolio=portfolio,
                        symbol=symbol,
                        order=order,
                        side="buy" if plan.action == "BUY" else "sell",
                        quantity=filled_quantity,
                        price=float(match_result.executed_price or price),
                        fee_override=float(match_result.total_cost),
                        note=f"DecisionOrderPlan {plan.order_plan_id} partial retry",
                    )
                else:
                    order, _trade = place_sim_order(
                        db=db,
                        portfolio=portfolio,
                        symbol=symbol,
                        side="buy" if plan.action == "BUY" else "sell",
                        quantity=filled_quantity,
                        price=float(match_result.executed_price or price),
                        order_type="market",
                        note=f"DecisionOrderPlan {plan.order_plan_id} partial fill",
                        enforce_rules=False,
                        apply_fees=False,
                        fee_override=float(match_result.total_cost),
                        execution_price_is_final=True,
                    )
                order.quantity = requested_quantity
                order.status = "partial"
                order.client_order_key = str(plan.order_plan_id)
                order.source_type = "decision_engine"
                order.source_id = None
                order.decision_evidence_id = str(plan.evidence_id)
                order.rejection_code = "PARTIAL_FILL_PENDING"
                order.rejection_detail = (
                    f"requested={requested_quantity}, filled={float(order.filled_quantity or 0.0)}, "
                    f"remaining={remaining_quantity}; next session retry"
                )
                order.decision_snapshot_json = _snapshot_payload(
                    item,
                    status="PARTIAL_FILL_PENDING",
                    requested_quantity=requested_quantity,
                    filled_quantity=float(order.filled_quantity or 0.0),
                    remaining_quantity=remaining_quantity,
                    retry_on_next_session=True,
                )
                evidence = db.get(DecisionEvidence, str(plan.evidence_id))
                _write_execution_evidence(
                    evidence,
                    status="PARTIAL_FILL_PENDING",
                    requested_quantity=requested_quantity,
                    filled_quantity=float(order.filled_quantity or 0.0),
                    reason="PARTIAL_FILL_PENDING",
                    detail=order.rejection_detail,
                    executed_price=float(match_result.executed_price or price),
                    slippage_bps=float(match_result.slippage_bps or 0.0),
                    costs=match_result,
                    trace_entry={
                        "date": plan.execution_date.isoformat(),
                        "execution_date": plan.execution_date.isoformat(),
                        "trigger_trade_date": trade_date.isoformat(),
                        "reason": "PARTIAL_FILL_RETRY" if is_retry else "PARTIAL_FILL",
                        "requested_quantity": requested_quantity,
                        "filled_quantity": filled_quantity,
                        "remaining_quantity": remaining_quantity,
                        "next_status": "PENDING_RETRY",
                    },
                    retry_status="PENDING_RETRY",
                )
                db.commit()
                result["executed_orders"].append({
                    "order_id": order.id,
                    "order_plan_id": str(plan.order_plan_id),
                    "symbol_id": int(plan.symbol_id),
                    "status": str(order.status),
                    "filled_quantity": float(order.filled_quantity or 0.0),
                    "remaining_quantity": remaining_quantity,
                })
                result["pending_orders"].append({
                    "order_plan_id": str(plan.order_plan_id),
                    "order_id": order.id,
                    "reason": "PARTIAL_FILL_RETRY" if is_retry else "PARTIAL_FILL",
                    "filled_quantity": float(order.filled_quantity or 0.0),
                    "remaining_quantity": remaining_quantity,
                    "retry_status": "PENDING_RETRY",
                })
                continue

            if is_retry:
                order = pending_order
                _trade = apply_sim_order_fill(
                    db=db,
                    portfolio=portfolio,
                    symbol=symbol,
                    order=order,
                    side="buy" if plan.action == "BUY" else "sell",
                    quantity=float(plan.target_quantity),
                    price=float(match_result.executed_price or price),
                    fee_override=float(match_result.total_cost),
                    note=f"DecisionOrderPlan {plan.order_plan_id} retry fill",
                )
            else:
                order, _trade = place_sim_order(
                    db=db,
                    portfolio=portfolio,
                    symbol=symbol,
                    side="buy" if plan.action == "BUY" else "sell",
                    quantity=float(plan.target_quantity),
                    price=float(match_result.executed_price or price),
                    order_type="market",
                    note=f"DecisionOrderPlan {plan.order_plan_id}",
                    # Market rules were already evaluated by the shared matcher;
                    # re-running the legacy validator against the slipped final
                    # price can incorrectly reject a valid limit-up/down fill.
                    enforce_rules=False,
                    # match_order_plan already applied directional slippage and
                    # the complete cost model; avoid calculating either twice.
                    apply_fees=False,
                    fee_override=float(match_result.total_cost),
                    execution_price_is_final=True,
                )
            order.client_order_key = str(plan.order_plan_id)
            order.source_type = "decision_engine"
            # SimOrder.source_id is an integer legacy attribution field; the
            # immutable evidence/plan identifiers live in the decision JSON.
            order.source_id = None
            order.decision_evidence_id = str(plan.evidence_id)
            order.decision_snapshot_json = json.dumps(item, ensure_ascii=False)
            evidence = db.get(DecisionEvidence, str(plan.evidence_id))
            if evidence is not None:
                _write_execution_evidence(
                    evidence,
                    status="FILLED",
                    requested_quantity=requested_quantity,
                    filled_quantity=(
                        float(order.filled_quantity or 0.0)
                        if is_retry else float(match_result.filled_quantity)
                    ),
                    executed_price=float(order.filled_price),
                    slippage_bps=float(match_result.slippage_bps or 0.0),
                    costs=match_result,
                    trace_entry=(
                        {
                            "date": plan.execution_date.isoformat(),
                            "execution_date": plan.execution_date.isoformat(),
                            "trigger_trade_date": trade_date.isoformat(),
                            "reason": "PARTIAL_FILL_RETRY",
                            "filled_quantity": float(match_result.filled_quantity),
                            "remaining_quantity": 0.0,
                        }
                        if is_retry else None
                    ),
                )
            db.commit()
            result["executed_orders"].append({
                "order_id": order.id,
                "order_plan_id": str(plan.order_plan_id),
                "symbol_id": int(plan.symbol_id),
                "status": str(order.status),
                "filled_quantity": float(order.filled_quantity or 0.0),
            })
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            result["errors"].append({"order_plan_id": str(plan.order_plan_id), "error": str(exc)})
    return result


def _resolve_snapshot_execution_context(
    db: Session | None,
    *,
    portfolio_id: int,
    strategy_snapshot_id: str | None,
    trade_date: date | None,
) -> tuple[str, date]:
    """Resolve the only execution context accepted by automatic simulation.

    The public member-source entry used to fall back to ``decide_trades`` when
    one of these values was absent. That duplicates selection and allocation
    outside DecisionEngine, so it is now a hard failure. ``db=None`` is kept
    only for the isolated plan-adapter test seam, which must supply both
    immutable values explicitly.
    """
    if db is None:
        if not strategy_snapshot_id or trade_date is None:
            raise ValueError(
                "STRATEGY_SNAPSHOT_REQUIRED: automatic simulation requires an "
                "applied strategy snapshot and trade date"
            )
        return str(strategy_snapshot_id), trade_date

    # Imported lazily to avoid the service-level dual-run/member-source cycle.
    from app.services.auto_trade_dual_run import resolve_applied_snapshot_id

    resolved_snapshot_id = resolve_applied_snapshot_id(
        db,
        portfolio_id=portfolio_id,
        strategy_snapshot_id=strategy_snapshot_id,
    )
    if resolved_snapshot_id is None:
        raise ValueError(
            "STRATEGY_SNAPSHOT_REQUIRED: automatic simulation requires an "
            "applied save_and_apply strategy snapshot"
        )
    if trade_date is None:
        from app.services.decision_clock import utc_naive_to_shanghai

        trade_date = utc_naive_to_shanghai(utcnow_naive()).date()
    return resolved_snapshot_id, trade_date


def execute_member_source(
    db: Session,
    *,
    portfolio_id: int,
    dry_run: bool = False,
    strategy_snapshot_id: str | None = None,
    trade_date: date | None = None,
) -> dict[str, Any]:
    """Run automatic simulation from one applied snapshot plan set.

    Candidate selection, signal interpretation and allocation belong to
    DecisionEngine. This public entry only resolves the immutable execution
    context and translates returned DecisionOrderPlan values into
    simulated-account orders. It fails closed when no applied snapshot exists.
    """
    resolved_snapshot_id, resolved_trade_date = _resolve_snapshot_execution_context(
        db,
        portfolio_id=portfolio_id,
        strategy_snapshot_id=strategy_snapshot_id,
        trade_date=trade_date,
    )
    return _execute_snapshot_order_plans(
        db,
        portfolio_id=portfolio_id,
        strategy_snapshot_id=resolved_snapshot_id,
        trade_date=resolved_trade_date,
        dry_run=dry_run,
    )


# ----------------------------------------------------------------------------
# Legacy diagnostic/idempotency helpers
# ----------------------------------------------------------------------------


def find_existing_order_by_client_key(
    db: Session,
    *,
    client_order_key: str,
) -> SimOrder | None:
    """按 client_order_key 查找已存在的订单（WP6.3 幂等）。

    如果存在，说明同一业务事件已下过单，跳过。
    """
    return db.execute(
        select(SimOrder).where(SimOrder.client_order_key == client_order_key)
    ).scalars().first()


def execute_order_idempotent(
    db: Session,
    *,
    decision: TradeDecision,
) -> tuple[SimOrder | None, str]:
    """Execute a legacy TradeDecision idempotently for diagnostics only.

    Automatic simulation must use run_idempotent_member_source, which delegates
    to DecisionEngine and immutable DecisionOrderPlan values. This helper
    remains for historical diagnostics and is not an automatic execution entry.

    返回 (order, status)：
    - status="executed"：新下单
    - status="skipped_existing"：已存在，跳过
    - status="failed"：下单失败

    如果 client_order_key 已存在，直接返回已存在的订单，不重复下单。
    失败时不占用 client_order_key（未创建 SimOrder），可重试。
    """
    # 1. 检查 client_order_key 是否已存在
    existing = find_existing_order_by_client_key(
        db, client_order_key=decision.client_order_key
    )
    if existing is not None:
        logger.info(
            "幂等跳过：client_order_key=%s 已存在 order_id=%s",
            decision.client_order_key,
            existing.id,
        )
        return existing, "skipped_existing"

    # 2. 执行下单（失败不占用 client_order_key）
    try:
        order = _execute_order(db, decision)
        return order, "executed"
    except Exception as e:
        # 不暴露敏感信息，仅记录概要
        logger.warning(
            "下单失败 client_order_key=%s: %s",
            decision.client_order_key,
            e,
            exc_info=True,
        )
        return None, "failed"


def _create_pending_confirmation_order(
    db: Session,
    decision: TradeDecision,
) -> SimOrder:
    """创建待确认占位订单（confirm 模式，用于幂等追踪）。

    confirm 模式不实际成交，但需要占位 SimOrder 以支持 client_order_key 幂等检查。
    占位订单 status=pending_confirmation，quantity/price 为 0，不产生现金流。
    """
    order = SimOrder(
        portfolio_id=decision.portfolio_id,
        symbol_id=decision.symbol_id,
        side=decision.side,
        order_type="market",
        quantity=0,
        submitted_price=0,
        status="pending_confirmation",
        filled_quantity=0,
        filled_price=0,
        filled_amount=0,
        fee=0,
        note=f"Pending confirmation: action={decision.action}, mode={decision.execution_mode}",
        member_id=decision.member.id if decision.member else None,
        source_type="member",
        source_id=decision.member.id if decision.member else None,
        signal_id=decision.signal_id,
        rule_version_id=decision.rule_version_id,
        execution_mode=decision.execution_mode,
        client_order_key=decision.client_order_key,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def run_idempotent_member_source(
    db: Session,
    *,
    portfolio_id: int,
    dry_run: bool = False,
    strategy_snapshot_id: str | None = None,
    trade_date: date | None = None,
) -> dict[str, Any]:
    """Idempotently execute the plan set returned by DecisionEngine.

    DecisionOrderPlan.order_plan_id is the simulated order client key, so
    retries of the same applied snapshot/date reuse order identity without
    reopening the legacy member-side decision path.
    """
    resolved_snapshot_id, resolved_trade_date = _resolve_snapshot_execution_context(
        db,
        portfolio_id=portfolio_id,
        strategy_snapshot_id=strategy_snapshot_id,
        trade_date=trade_date,
    )
    return _execute_snapshot_order_plans(
        db,
        portfolio_id=portfolio_id,
        strategy_snapshot_id=resolved_snapshot_id,
        trade_date=resolved_trade_date,
        dry_run=dry_run,
    )


__all__ = [
    "MemberTradeCandidate",
    "TradeDecision",
    "build_client_order_key",
    "decide_trades",
    "execute_member_source",
    "execute_order_idempotent",
    "find_existing_order_by_client_key",
    "get_buy_candidates",
    "get_sell_candidates",
    "get_signal_candidates",
    "run_idempotent_member_source",
]
