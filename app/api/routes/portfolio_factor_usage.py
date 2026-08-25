"""G1-WP0-2g: FactorUsage + Strategy Preflight API.

三个端点（Q1/Q8/Q21）：
1. GET  /portfolios/{id}/factor-usage-options  : 返回可选 active 模型、FactorSet、Rule
2. GET  /portfolios/{id}/factor-usage          : 读取当前有效绑定 + 最近历史
3. POST /portfolios/{id}/factor-usage          : 保存并应用（事务落库）
4. POST /portfolios/{id}/strategy-preflight    : 内存预检，不落正式快照
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.decision_engine import (
    CurrentFactorUsageResponse,
    FactorUsageBindRequest,
    FactorUsageOptionsResponse,
    SaveAndApplyResponse,
    StrategyPreflightResponse,
)
from app.services import factor_usage_service as svc


router = APIRouter()


def _actor(x_user: str | None = Header(default=None)) -> str:
    return x_user or "local_user"


@router.get(
    "/portfolios/{portfolio_id}/factor-usage-options",
    response_model=FactorUsageOptionsResponse,
    tags=["portfolio-factor-usage"],
    summary="G1-WP0-2g：获取该组合可绑定的模型/FactorSet/Rule 选项",
)
def factor_usage_options(
    portfolio_id: int,
    db: Session = Depends(get_db),
) -> FactorUsageOptionsResponse:
    """UI 进入策略页时调用，显示可选 active 模型与门禁状态。只读。"""
    return svc.get_factor_usage_options(db, portfolio_id)


@router.get(
    "/portfolios/{portfolio_id}/factor-usage",
    response_model=CurrentFactorUsageResponse,
    tags=["portfolio-factor-usage"],
    summary="G1-WP0-2g：读取当前生效因子绑定与历史",
)
def current_factor_usage(
    portfolio_id: int,
    db: Session = Depends(get_db),
) -> CurrentFactorUsageResponse:
    return svc.get_current_usage(db, portfolio_id)


@router.post(
    "/portfolios/{portfolio_id}/strategy-preflight",
    response_model=StrategyPreflightResponse,
    tags=["portfolio-factor-usage"],
    summary="G1-WP0-2g：策略预检（内存中，不创建正式快照 / 不改 runtime）",
)
def strategy_preflight(
    portfolio_id: int,
    payload: FactorUsageBindRequest,
    db: Session = Depends(get_db),
    actor: str = Depends(_actor),
) -> StrategyPreflightResponse:
    """Q8.3：预检只在内存中执行，返回完整预检证据。

    - 不会写 StrategyExecutionSnapshot 正式表。
    - 会检查 production 模式是否绑定了全局 active 模型（方案 A）。
    """
    return svc.preflight_usage(db, portfolio_id, payload, actor)


@router.post(
    "/portfolios/{portfolio_id}/factor-usage",
    response_model=SaveAndApplyResponse,
    status_code=200,
    tags=["portfolio-factor-usage"],
    summary="G1-WP0-2g：保存并应用；成功后生成不可变 StrategyExecutionSnapshot",
)
def save_and_apply_usage(
    portfolio_id: int,
    payload: FactorUsageBindRequest,
    db: Session = Depends(get_db),
    actor: str = Depends(_actor),
    idempotency_key: str | None = Header(default=None),
) -> SaveAndApplyResponse:
    """Q8.1/Q8.2：单事务保存 + 生成新快照。

    - effective_from = 事务提交成功时间（UTC naive）
    - 前端 Idempotency-Key 仅用于防止用户连点；后端业务幂等使用
      uq_ses_idempotency 唯一索引（Q28）。
    """
    # TODO: Q28 可选：把前端 idempotency_key 写入独立幂等表防连点；
    # 后端主键层仍用 content_hash(portfolio_id + usage_hash + effective_from)
    # 这里仅记录日志，保证请求语义正确。
    _ = idempotency_key
    return svc.save_and_apply_usage(db, portfolio_id, payload, actor)
