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
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio, Position
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
from app.services.portfolio_members import has_position, list_members

logger = logging.getLogger(__name__)


# 触发买入的 action 集合
_BUY_ACTIONS = {"open", "buy_dip"}
# 触发卖出的 action 集合
_SELL_ACTIONS = {"exit", "reduce"}


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
    """当前 UTC 时间（naive，与项目其他模型一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def _check_data_health(db: Session, symbol_id: int) -> tuple[bool, str]:
    """数据健康检查。

    检查 K 线/评分/规则版本是否过期。
    WP6.5 实现详细检查，WP6.2 返回 True（占位）。
    """
    # TODO: WP6.5 实现详细数据健康检查
    return True, ""


def _check_portfolio_risk(db: Session, portfolio_id: int) -> tuple[bool, str]:
    """组合风控检查。

    WP6.5 实现详细检查，WP6.2 返回 True（占位）。
    """
    # TODO: WP6.5 实现详细风控检查
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

        # 4. 数据健康检查
        data_health_ok, data_reason = _check_data_health(db, member.symbol_id)

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
                data_health_ok=True,
                risk_check_ok=True,
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
    """计算仓位大小。

    WP6.2 占位：使用最小一手（100 股）。
    WP6.5 接入 compute_position_budget 做实际仓位计算。
    """
    # TODO: WP6.5 接入 compute_position_budget 做实际仓位计算
    # A 股最小 100 股/手
    return 100.0


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

    # 获取实际价格
    price = _get_latest_price(db, decision.symbol_id)
    if price is None or price <= 0:
        raise ValueError(f"无法获取标的 {symbol.symbol} 的最新价格")

    # 计算仓位
    quantity = _calculate_position_size(db, decision, price)
    if quantity <= 0:
        raise ValueError(f"标的 {symbol.symbol} 计算仓位为 0")

    # 卖出时使用持仓数量
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
            quantity = max(held_qty / 2, 100)  # 至少一手

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


def execute_member_source(
    db: Session,
    *,
    portfolio_id: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """执行基于成员的自动交易（WP6.2 主入口）。

    参数：
        portfolio_id: 组合 ID
        dry_run: True 时只返回决策，不实际下单

    返回：
        {
            "portfolio_id": int,
            "buy_decisions": [...],     # auto 模式买入决策
            "sell_decisions": [...],    # 卖出决策
            "signal_decisions": [...],  # manual/confirm 信号决策
            "rejected_decisions": [...], # 被拒绝的决策（数据/风控阻断）
            "executed_orders": [...],   # 实际下单/计划
            "errors": [...],             # 单笔失败原因
        }
    """
    decisions = decide_trades(db, portfolio_id=portfolio_id)

    result: dict[str, Any] = {
        "portfolio_id": portfolio_id,
        "buy_decisions": [],
        "sell_decisions": [],
        "signal_decisions": [],
        "rejected_decisions": [],
        "executed_orders": [],
        "errors": [],
    }

    for decision in decisions:
        decision_dict = {
            "side": decision.side,
            "symbol_id": decision.symbol_id,
            "action": decision.action,
            "execution_mode": decision.execution_mode,
            "signal_id": decision.signal_id,
            "client_order_key": decision.client_order_key,
            "rejection_code": decision.rejection_code,
            "rejection_detail": decision.rejection_detail,
        }

        # 被拒绝的决策（数据/风控阻断）
        if decision.rejection_code:
            result["rejected_decisions"].append(decision_dict)
            continue

        # 分类决策
        if decision.side == "sell":
            result["sell_decisions"].append(decision_dict)
        else:
            # 买入：auto 进 buy_decisions，manual/confirm 进 signal_decisions
            if decision.execution_mode == EXECUTION_AUTO:
                result["buy_decisions"].append(decision_dict)
            else:
                result["signal_decisions"].append(decision_dict)

        # dry_run 模式：只返回决策，不实际下单
        if dry_run:
            continue

        # 按 execution_mode 路由执行
        try:
            if decision.execution_mode == EXECUTION_AUTO:
                # auto 模式：实际下单
                order = _execute_order(db, decision)
                result["executed_orders"].append(
                    {
                        "order_id": order.id,
                        "side": decision.side,
                        "symbol_id": decision.symbol_id,
                        "client_order_key": decision.client_order_key,
                        "status": "filled",
                    }
                )
            elif decision.execution_mode == EXECUTION_CONFIRM:
                # confirm 模式：只生成待确认订单计划，不实际下单
                result["executed_orders"].append(
                    {
                        "side": decision.side,
                        "symbol_id": decision.symbol_id,
                        "client_order_key": decision.client_order_key,
                        "status": "pending_confirmation",
                    }
                )
            elif decision.execution_mode == EXECUTION_MANUAL:
                # manual 模式：只提示信号不下单
                result["executed_orders"].append(
                    {
                        "side": decision.side,
                        "symbol_id": decision.symbol_id,
                        "client_order_key": decision.client_order_key,
                        "status": "signal_only",
                    }
                )
        except Exception as exc:
            # 单笔失败隔离，不阻断其他订单
            # 不暴露敏感信息，仅记录概要
            result["errors"].append(
                {
                    "decision": decision_dict,
                    "error": str(exc),
                }
            )
            logger.warning(
                "execute_member_source 执行失败 symbol_id=%s side=%s: %s",
                decision.symbol_id,
                decision.side,
                exc,
                exc_info=True,
            )

    return result


__all__ = [
    "MemberTradeCandidate",
    "TradeDecision",
    "build_client_order_key",
    "decide_trades",
    "execute_member_source",
    "get_buy_candidates",
    "get_sell_candidates",
    "get_signal_candidates",
]
