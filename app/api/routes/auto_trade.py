"""WP6.6 自动交易双跑与成员级状态 HTTP 路由。

在现有 `POST /portfolios/{id}/auto-trade/execute`（定义于 portfolios.py）基础上，
本模块追加以下端点（路径前缀沿用 `/portfolios/{portfolio_id}/auto-trade/...`）：

- GET  /portfolios/{portfolio_id}/auto-trade/readiness
- GET  /portfolios/{portfolio_id}/auto-trade/dry-run-diff
- GET  /portfolios/{portfolio_id}/auto-trade/member-source-status
- POST /portfolios/{portfolio_id}/auto-trade/rollback-to-old-source
- GET  /portfolios/{portfolio_id}/auto-trade/member-status

设计原则：
- 仅做 UI 展示与可视化所需的数据聚合，不修改任何业务逻辑
- 错误信息使用统一错误协议（UnifiedErrorException / 标准 HTTPException）
- 不暴露敏感信息，时间戳统一返回 ISO 字符串
"""
from __future__ import annotations

import logging
import os
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import PortfolioMember
from app.models.sim_account import SimOrder
from app.models.symbol import Symbol
from app.services.auto_trade_dual_run import (
    ENV_FLAG,
    capture_trade_set_from_new_logic,
    capture_trade_set_from_old_logic,
    diff_trade_sets,
    is_member_source_enabled,
    rollback_to_old_source,
)
from app.services.auto_trade_readiness import (
    get_auto_trade_readiness,
    readiness_to_dict,
)
from app.services.auto_trade_safety import check_data_health
from app.services.portfolio_members import has_position, list_members


logger = logging.getLogger(__name__)

router = APIRouter()


# ----------------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------------


def _get_portfolio_or_404(db: Session, portfolio_id: int) -> Portfolio:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return portfolio


def _ensure_simulated(portfolio: Portfolio) -> None:
    if portfolio.account_type != "simulated":
        raise HTTPException(
            status_code=400,
            detail="Auto trade features are only available for simulated portfolios",
        )


def _iso(dt) -> str | None:
    if dt is None:
        return None
    try:
        # 项目其他模型均为 naive UTC，这里统一加 Z 后缀以便前端解析
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001 - 兜底防御，避免时间格式化失败导致整接口 500
        return str(dt)


def _parse_env_list(name: str) -> list[int]:
    raw = os.environ.get(name, "")
    if not raw:
        return []
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if token.isdigit():
            out.append(int(token))
    return out


# ----------------------------------------------------------------------------
# 1. dry-run-diff：双跑差异对比
# ----------------------------------------------------------------------------


@router.get(
    "/portfolios/{portfolio_id}/auto-trade/dry-run-diff",
    tags=["auto-trade"],
)
def get_dry_run_diff(
    portfolio_id: int,
    db: Session = Depends(get_db),
):
    """WP6.6 双跑差异对比。

    调用 capture_trade_set_from_old_logic + capture_trade_set_from_new_logic + diff_trade_sets，
    返回 {old_set, new_set, diffs: [{symbol_id, side, old_action, new_action, reason, detail}]}。

    说明：
    - 仅做 dry run，不实际下单
    - 不修改任何状态
    - 错误返回标准 HTTPException
    """
    portfolio = _get_portfolio_or_404(db, portfolio_id)
    _ensure_simulated(portfolio)

    try:
        old_set = capture_trade_set_from_old_logic(db, portfolio_id=portfolio_id)
        new_set = capture_trade_set_from_new_logic(db, portfolio_id=portfolio_id)
        diffs = diff_trade_sets(old_set, new_set, db, portfolio_id=portfolio_id)
    except Exception as exc:
        logger.warning(
            "dry-run-diff 失败 portfolio_id=%s: %s", portfolio_id, exc, exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="双跑差异计算失败，请稍后重试",
        ) from exc

    return {
        "portfolio_id": portfolio_id,
        "old_set": old_set.to_dict(),
        "new_set": new_set.to_dict(),
        "diffs": [
            {
                "symbol_id": d.symbol_id,
                "side": d.side,
                "old_action": d.old_action,
                "new_action": d.new_action,
                "reason": d.reason,
                "detail": d.detail,
            }
            for d in diffs
        ],
    }


# ----------------------------------------------------------------------------
# 1. readiness：就绪检查（P0-AutoTrade 统一入口）
# ----------------------------------------------------------------------------


@router.get(
    "/portfolios/{portfolio_id}/auto-trade/readiness",
    tags=["auto-trade"],
)
def get_auto_trade_readiness_route(
    portfolio_id: int,
    for_schedule: bool = False,
    db: Session = Depends(get_db),
):
    """P0-AutoTrade：自动交易就绪检查。

    - 不存在组合：404 Portfolio not found。
    - 非模拟组合：仍 200，但会返回 account_ready=false 与对应 blocker。
    - ready=true：允许真实执行；ready=false 只保留 dry-run 与诊断能力。

    Query 参数：
    - for_schedule：若来自调度入口则把 schedule_ready 升级为 blocker。
    """
    _get_portfolio_or_404(db, portfolio_id)

    try:
        result = get_auto_trade_readiness(
            db,
            portfolio_id=portfolio_id,
            for_schedule=bool(for_schedule),
        )
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("readiness 计算失败 portfolio_id=%s", portfolio_id)
        raise HTTPException(
            status_code=500,
            detail="就绪检查失败，请稍后重试",
        ) from exc

    response = readiness_to_dict(result)
    response["portfolio_id"] = portfolio_id
    return response


# ----------------------------------------------------------------------------
# 2. member-source-status：新来源开关状态
# ----------------------------------------------------------------------------


@router.get(
    "/portfolios/{portfolio_id}/auto-trade/member-source-status",
    tags=["auto-trade"],
)
def get_member_source_status(
    portfolio_id: int,
    db: Session = Depends(get_db),
):
    """WP6.6 新来源开关状态。

    返回 {enabled, env_flag, whitelist_match, blacklist_match, env_var_name}。
    - enabled：该组合当前是否启用新来源
    - env_flag：全局环境变量 AUTO_TRADE_MEMBER_SOURCE_ENABLED 的原始值
    - whitelist_match：组合是否在白名单中
    - blacklist_match：组合是否在黑名单中
    """
    _get_portfolio_or_404(db, portfolio_id)

    whitelist = _parse_env_list("AUTO_TRADE_MEMBER_SOURCE_PORTFOLIOS")
    blacklist = _parse_env_list("AUTO_TRADE_MEMBER_SOURCE_EXCLUDED_PORTFOLIOS")

    return {
        "portfolio_id": portfolio_id,
        "enabled": is_member_source_enabled(portfolio_id),
        "env_var_name": ENV_FLAG,
        "env_flag": os.environ.get(ENV_FLAG, "false"),
        "whitelist_match": portfolio_id in whitelist,
        "blacklist_match": portfolio_id in blacklist,
        "whitelist": whitelist,
        "blacklist": blacklist,
    }


# ----------------------------------------------------------------------------
# 3. rollback-to-old-source：回退到旧来源
# ----------------------------------------------------------------------------


@router.post(
    "/portfolios/{portfolio_id}/auto-trade/rollback-to-old-source",
    tags=["auto-trade"],
)
def rollback_to_old_source_route(
    portfolio_id: int,
    db: Session = Depends(get_db),
):
    """WP6.6 回退到旧来源。

    调用 rollback_to_old_source(portfolio_id)：
    - 清除组合级白名单中的该 portfolio_id
    - 全局关闭 AUTO_TRADE_MEMBER_SOURCE_ENABLED

    返回 {ok, message}。
    """
    _get_portfolio_or_404(db, portfolio_id)

    try:
        rollback_to_old_source(portfolio_id)
    except Exception as exc:
        logger.warning(
            "rollback 失败 portfolio_id=%s: %s", portfolio_id, exc, exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="回退旧来源失败，请稍后重试",
        ) from exc

    return {
        "ok": True,
        "portfolio_id": portfolio_id,
        "message": "已回退到旧来源（新来源开关已关闭）",
    }


# ----------------------------------------------------------------------------
# 4. member-status：成员级执行状态
# ----------------------------------------------------------------------------


def _latest_order_for_member(db: Session, member_id: int) -> SimOrder | None:
    return db.execute(
        select(SimOrder)
        .where(SimOrder.member_id == member_id)
        .order_by(desc(SimOrder.id))
        .limit(1)
    ).scalars().first()


def _symbol_code(db: Session, symbol_id: int) -> str | None:
    symbol = db.get(Symbol, symbol_id)
    return symbol.symbol if symbol else None


@router.get(
    "/portfolios/{portfolio_id}/auto-trade/member-status",
    tags=["auto-trade"],
)
def get_member_status(
    portfolio_id: int,
    db: Session = Depends(get_db),
):
    """WP6.6 成员级执行状态。

    列出组合所有成员及其执行模式/是否持仓/最近订单归因/风控状态/数据健康。

    返回：
    {
        "portfolio_id": int,
        "members": [
            {
                "member_id", "symbol_id", "symbol", "status", "execution_mode",
                "manual_lock", "has_position", "position_quantity",
                "latest_order": {order_id, side, status, created_at, source_type,
                                 signal_id, execution_mode, client_order_key,
                                 rejection_code, rejection_detail},
                "data_health": {"healthy": bool, "reason": str, "kline_latest_at": str?,
                                "score_latest_at": str?},
                "risk_blocked": bool,  # latest_order 有 rejection_code 即视为风控阻断
                "data_expired": bool,  # data_health.healthy=False 即数据过期
            }
        ]
    }
    """
    portfolio = _get_portfolio_or_404(db, portfolio_id)
    _ensure_simulated(portfolio)

    members = list_members(
        db,
        portfolio_id=portfolio_id,
        include_archived=True,
    )

    items: list[dict] = []
    for member in members:
        has_pos, qty = has_position(
            db, portfolio_id=portfolio_id, symbol_id=member.symbol_id
        )
        latest_order = _latest_order_for_member(db, member.id)

        # 数据健康（基于成员入场规则版本；无入场规则版本时仅检查 K线/评分新鲜度）
        rule_version_id = member.entry_rule_version_id
        try:
            health = check_data_health(
                db, symbol_id=member.symbol_id, rule_version_id=rule_version_id
            )
            data_health = {
                "healthy": health.healthy,
                "reason": health.reason,
                "kline_latest_at": _iso(health.kline_latest_at),
                "score_latest_at": _iso(health.score_latest_at),
                "rule_version_id": health.rule_version_id,
            }
        except Exception as exc:  # noqa: BLE001 - 数据健康失败不应阻断整个接口
            logger.warning(
                "member-status 数据健康检查失败 member_id=%s: %s", member.id, exc,
            )
            data_health = {
                "healthy": False,
                "reason": "数据健康检查失败",
                "kline_latest_at": None,
                "score_latest_at": None,
                "rule_version_id": rule_version_id,
            }

        latest_order_dict = None
        risk_blocked = False
        if latest_order is not None:
            risk_blocked = bool(latest_order.rejection_code)
            latest_order_dict = {
                "order_id": latest_order.id,
                "side": latest_order.side,
                "status": latest_order.status,
                "created_at": _iso(latest_order.created_at),
                "source_type": latest_order.source_type,
                "signal_id": latest_order.signal_id,
                "execution_mode": latest_order.execution_mode,
                "client_order_key": latest_order.client_order_key,
                "rejection_code": latest_order.rejection_code,
                "rejection_detail": latest_order.rejection_detail,
            }

        items.append({
            "member_id": member.id,
            "symbol_id": member.symbol_id,
            "symbol": _symbol_code(db, member.symbol_id),
            "status": member.status,
            "execution_mode": member.execution_mode,
            "source_type": member.source_type,
            "manual_lock": bool(member.manual_lock),
            "has_position": bool(has_pos),
            "position_quantity": float(qty) if qty else 0.0,
            "latest_order": latest_order_dict,
            "data_health": data_health,
            "risk_blocked": risk_blocked,
            "data_expired": not data_health["healthy"],
        })

    return {
        "portfolio_id": portfolio_id,
        "members": items,
        "total": len(items),
    }


__all__ = ["router"]
