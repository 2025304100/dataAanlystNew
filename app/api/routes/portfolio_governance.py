"""G4：对账 + 7 状态状态机 治理 REST API。

路由（全部挂在 `settings.api_prefix` 下，路径以 `/portfolios/{pid}` 为前缀）：

  GET  /portfolios/{pid}/status
    → 返回当前 7 状态 + 允许转移列表 + last_decision_trade_date + last_reconciled_trade_date

  POST /portfolios/{pid}/reconcile
    body: { trade_date: date }
    → 触发 T 日对账；返回 ReconciliationReport（status/PASSED|BLOCKED + diffs 明细）
    → PASSED 时 last_reconciled_trade_date 单调推进

  POST /portfolios/{pid}/confirm-reconciliation
    body: { trade_date: date, acknowledge_all_diffs_cleared: bool,
            force_skip_re_reconcile?: bool, operator_id?: string }
    → 人工对账修复确认（先重跑对账 → 对账清零 → RECONCILIATION_BLOCKED → READY）

  POST /portfolios/{pid}/transition-state
    body: { to_state: string, reason?: string } (X-User 作为 operator_id)
    → 管理员级状态转移（例如 ANY→ADMIN_PAUSED / INTERRUPTED→READY / ADMIN_PAUSED→READY）

  GET  /portfolios/{pid}/audit-events
    query: action? / occurred_from? / occurred_to? / operator? / attributes_q?
           page=1 / page_size=20
    → 单组合审计事件查询（分页 + 过滤 + attributes_json 文本包含检索）

  GET  /audit-events
    query: action? / occurred_from? / occurred_to? / operator? / attributes_q?
           portfolio_id? / symbol_id? / business_key?
           page=1 / page_size=20
    → 全局审计事件查询（需 X-Role=admin/auditor；本地 AUDITOR_ALLOW_LOCAL_DEV=1 松校验）

错误约定（统一错误码 code 会写入 Response JSON 里的 error_code 字段，若无则看 status_code）：
  - 401 AUTH_MISSING                  ：X-User 未传（需本地打开 STRICT_AUTH=1；否则 local_user 填充）
  - 403 FORBIDDEN_PORTFOLIO_ACCESS    ：无该组合访问权限
  - 403 FORBIDDEN_AUDITOR_ROLE_REQUIRED：全局审计接口需 admin/auditor
  - 400 INVALID_FILTER                ：过滤参数非法（start＞end / page＜1 / page_size＞200）
  - 404 组合不存在
  - 400 参数非法（trade_date 格式 / to_state 不在 7 状态枚举 / ack 未勾选）
  - 409 状态机非法跳转（Response detail = ILLEGAL_STATE_TRANSITION；并已写审计事件）
  - 428 预条件不满足：confirm-reconciliation 但当前不是 RECONCILIATION_BLOCKED
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func as sa_func, or_, select, true as sa_true
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.portfolio import Portfolio
from app.services.data_governance_audit import (
    ALLOWED_AUDIT_ACTIONS,
    DataGovernanceAuditEvent,
)
from app.services.portfolio_reconciliation import (
    ReconciliationReport,
    reconcile_trade_date,
)
from app.services.portfolio_state_machine import (
    VALID_STATES,
    StateTransitionError,
    StateTransitionResult,
    confirm_reconciliation_fixed,
    transition_portfolio_state,
    _get_status as _psm_get_status,
)

router = APIRouter(tags=["portfolio-governance"])


# ──────────────────────────────────────────────────────────── 通用
def _actor(x_user: str | None = Query(default=None, alias="X-User")) -> str:
    return x_user or "local_user"


def _load_portfolio(db: Session, portfolio_id: int) -> Portfolio:
    p = db.get(Portfolio, portfolio_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id} not found")
    return p


def _allowed_transitions_from(from_state: str) -> list[str]:
    """基于状态机转移表，返回当前状态允许的下一个状态列表 + ADMIN_PAUSED 总是允许。"""
    matrix: dict[str, list[str]] = {
        "PENDING_INITIAL_REVIEW": ["READY", "ADMIN_PAUSED"],
        "READY": ["RUNNING_AUTO_SIMULATION", "RUNNING_BACKTEST", "ADMIN_PAUSED"],
        "RUNNING_AUTO_SIMULATION": ["READY", "RECONCILIATION_BLOCKED",
                                     "INTERRUPTED", "ADMIN_PAUSED"],
        "RUNNING_BACKTEST": ["READY", "RECONCILIATION_BLOCKED",
                             "INTERRUPTED", "ADMIN_PAUSED"],
        "RECONCILIATION_BLOCKED": ["READY", "ADMIN_PAUSED"],
        "INTERRUPTED": ["READY", "ADMIN_PAUSED"],
        "ADMIN_PAUSED": ["READY"],
    }
    return matrix.get(from_state, ["ADMIN_PAUSED"])


def _corr_id() -> str:
    from uuid import uuid4
    return f"c_api_{uuid4().hex[:12]}"


# ──────────────────────────────────────────────────────────── Schemas
class ReconcileRequest(BaseModel):
    # ``trade_date`` is the canonical API field.  The UI historically called
    # this ``as_of_trade_date`` and omitted it when opening the tab, so keep
    # both forms during the contract migration.
    trade_date: date | None = Field(default=None, description="要对账的交易日 T")
    as_of_trade_date: date | None = Field(default=None, description="兼容前端字段")


class TransitionStateRequest(BaseModel):
    to_state: str | None = Field(default=None, description="目标状态（规范字段）")
    reason: str | None = Field(default=None, description="转移原因（规范字段）")
    # Legacy frontend contract.  These aliases are normalized in the route.
    target_state: str | None = Field(default=None, description="兼容前端字段")
    trigger_reason: str | None = Field(default=None, description="兼容前端字段")
    review_note: str | None = Field(default=None, description="兼容前端字段")
    noop_if_already: bool = False


class ConfirmReconciliationRequest(BaseModel):
    trade_date: date | None = Field(default=None, description="要确认修复的交易日")
    acknowledge_all_diffs_cleared: bool | None = Field(
        default=None,
        description="防误触：必须显式设为 True，才能确认差异已修复",
    )
    force_skip_re_reconcile: bool = Field(
        False,
        description="管理员模式：跳过重跑对账直接推进 last_reconciled_trade_date（慎用）",
    )
    operator_id: str | None = Field(
        default=None,
        description="操作者；缺省取 X-User header",
    )
    # Legacy UI aliases.
    ack: bool | None = None
    force_skip: bool | None = None
    force_rerun_before: bool | None = None
    review_note: str | None = None


class ReconciliationDiffResponse(BaseModel):
    symbol_id: int | None
    kind: str
    expected: Any = None
    actual: Any = None
    detail: str | None = None


class ReconciliationResponse(BaseModel):
    portfolio_id: int
    trade_date: date
    decision_run_id: str | None
    status: Literal["PASSED", "BLOCKED"]
    expected_position_count: int = 0
    actual_position_count: int = 0
    expected_cash: float | None = None
    actual_cash: float | None = None
    differences: list[ReconciliationDiffResponse] = Field(default_factory=list)
    audit_event_id: int | None = None
    last_reconciled_trade_date: date | None = None
    # Compatibility fields consumed by the existing governance tab.
    as_of_at: datetime | None = None
    differences_found: bool = False
    zero_sum_check_passed: bool = False
    items: list[dict[str, Any]] = Field(default_factory=list)
    correlation_id: str | None = None


class PortfolioStatusResponse(BaseModel):
    portfolio_id: int
    current_state: str
    last_decision_trade_date: date | None = None
    last_reconciled_trade_date: date | None = None
    allowed_transitions: list[str] = Field(default_factory=list)
    is_auto_simulation_eligible: bool = False


class StateTransitionResponse(BaseModel):
    portfolio_id: int
    from_state: str
    to_state: str
    status: Literal["OK", "NOOP"]
    audit_event_id: int | None = None  # 非法跳转时为 ILLEGAL_STATE_TRANSITION 行 id
    # Compatibility fields consumed by the existing governance tab.
    transition_applied: bool = False
    noop_detected: bool = False
    illegal_transition_rejected: bool = False
    trigger_reason: str | None = None
    correlation_id: str | None = None


class ConfirmReconciliationResponse(BaseModel):
    portfolio_id: int
    trade_date: date
    operator_id: str
    from_state: str | None = None
    to_state: str | None = None
    last_reconciled_trade_date: date | None = None
    acknowledged_diffs_cleared: bool = False
    portfolio_now_ready: bool = False


# ──────────────────────────────────────────────────────────── GET /portfolios/{pid}/status
@router.get(
    "/portfolios/{portfolio_id}/status",
    response_model=PortfolioStatusResponse,
    status_code=200,
    summary="G4：查询组合运行状态（7 状态 + 允许转移 + 决策/对账游标）",
)
def get_portfolio_status(
    portfolio_id: int,
    db: Session = Depends(get_db),
) -> PortfolioStatusResponse:
    p = _load_portfolio(db, portfolio_id)
    current = _psm_get_status(db, p)
    return PortfolioStatusResponse(
        portfolio_id=portfolio_id,
        current_state=current,
        last_decision_trade_date=p.last_decision_trade_date,
        last_reconciled_trade_date=p.last_reconciled_trade_date,
        allowed_transitions=_allowed_transitions_from(current),
        is_auto_simulation_eligible=(current == "READY"),
    )


# ──────────────────────────────────────────────────────────── POST /portfolios/{pid}/reconcile
@router.post(
    "/portfolios/{portfolio_id}/reconcile",
    response_model=ReconciliationResponse,
    status_code=200,
    summary="G4：触发 T 日对账（有差异写审计；无差异推进 last_reconciled_trade_date）",
)
def reconcile_portfolio(
    portfolio_id: int,
    payload: ReconcileRequest,
    db: Session = Depends(get_db),
    actor: str = Depends(_actor),
) -> ReconciliationResponse:
    p = _load_portfolio(db, portfolio_id)
    corr = _corr_id()
    effective_trade_date = payload.trade_date or payload.as_of_trade_date or p.last_decision_trade_date or date.today()
    report: ReconciliationReport = reconcile_trade_date(
        db, portfolio_id, effective_trade_date,
        operator_id=actor, correlation_id=corr,
    )
    # 先 commit 以刷新 last_reconciled_trade_date 列
    db.commit()
    db.refresh(p)
    items = [
        {
            "dimension": d.kind,
            "expected_value": d.expected,
            "actual_value": d.actual,
            "diff_value": (d.actual - d.expected)
            if isinstance(d.actual, (int, float)) and isinstance(d.expected, (int, float))
            else (None if d.expected == d.actual else 1),
            "explain_note": d.detail,
        }
        for d in report.differences
    ]
    return ReconciliationResponse(
        portfolio_id=report.portfolio_id,
        trade_date=report.trade_date,
        decision_run_id=report.decision_run_id,
        status=report.status,
        expected_position_count=report.expected_position_count,
        actual_position_count=report.actual_position_count,
        expected_cash=report.expected_cash,
        actual_cash=report.actual_cash,
        differences=[
            ReconciliationDiffResponse(
                symbol_id=d.symbol_id, kind=d.kind,
                expected=d.expected, actual=d.actual, detail=d.detail,
            )
            for d in report.differences
        ],
        audit_event_id=getattr(report.audit, "event_id", None) if report.audit else None,
        last_reconciled_trade_date=p.last_reconciled_trade_date,
        as_of_at=datetime.now(),
        differences_found=report.status != "PASSED" or bool(report.differences),
        zero_sum_check_passed=report.status == "PASSED" and not report.differences,
        items=items,
        correlation_id=corr,
    )


# ──────────────────────────────────────────────────────────── POST /portfolios/{pid}/confirm-reconciliation
@router.post(
    "/portfolios/{portfolio_id}/confirm-reconciliation",
    response_model=ConfirmReconciliationResponse,
    status_code=200,
    summary="G4：人工确认对账修复（ack=True + 重跑对账清零 → RBLOCKED→READY + 推进对账游标）",
)
def confirm_portfolio_reconciliation(
    portfolio_id: int,
    payload: ConfirmReconciliationRequest,
    db: Session = Depends(get_db),
    actor: str = Depends(_actor),
) -> ConfirmReconciliationResponse:
    p = _load_portfolio(db, portfolio_id)
    corr = _corr_id()
    operator = (payload.operator_id or actor).strip()
    if not operator:
        raise HTTPException(status_code=400, detail="operator_id is required")

    current_state = _psm_get_status(db, p)
    if current_state != "RECONCILIATION_BLOCKED":
        # 仅在 RBLOCKED 时走确认接口；READY 状态不需要人工确认
        raise HTTPException(
            status_code=428,
            detail=(
                f"当前组合状态为 {current_state}，无需人工对账修复确认；"
                "仅在 RECONCILIATION_BLOCKED 状态可调用本接口。"
            ),
        )

    acknowledged = payload.acknowledge_all_diffs_cleared
    if acknowledged is None:
        acknowledged = payload.ack
    if acknowledged is not True:
        raise HTTPException(
            status_code=400,
            detail=(
                "acknowledge_all_diffs_cleared=True 是必须的防误触字段；"
                "请确认所有对账差异已在下游（补单 / 调账 / 重新撮合）中修复后再勾选。"
            ),
        )

    from_state = current_state
    try:
        result = confirm_reconciliation_fixed(
            db, portfolio_id, payload.trade_date or p.last_decision_trade_date or date.today(),
            operator_id=operator,
            acknowledge_all_diffs_cleared=True,
            force_skip_re_reconcile=bool(
                payload.force_skip_re_reconcile if payload.force_skip is None else payload.force_skip
            ),
            correlation_id=corr,
        )
    except StateTransitionError as e:
        # 重跑对账仍有差异 → 409
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        # ack 未勾选或 operator 非法 → 400
        raise HTTPException(status_code=400, detail=str(e)) from e

    db.commit()
    db.refresh(p)
    return ConfirmReconciliationResponse(
        portfolio_id=result.portfolio_id,
        trade_date=result.trade_date,
        operator_id=result.operator_id,
        from_state=from_state,
        to_state="READY",
        last_reconciled_trade_date=p.last_reconciled_trade_date,
        acknowledged_diffs_cleared=result.acknowledged_diffs_cleared,
        portfolio_now_ready=result.portfolio_now_ready,
    )


# ──────────────────────────────────────────────────────────── POST /portfolios/{pid}/transition-state
@router.post(
    "/portfolios/{portfolio_id}/transition-state",
    response_model=StateTransitionResponse,
    status_code=200,
    summary="G4：管理员级状态转移（ANY→ADMIN_PAUSED/INTERRUPTED→READY/ADMIN→READY 等）",
)
def admin_transition_portfolio_state(
    portfolio_id: int,
    payload: TransitionStateRequest,
    db: Session = Depends(get_db),
    actor: str = Depends(_actor),
) -> StateTransitionResponse:
    _load_portfolio(db, portfolio_id)
    corr = _corr_id()
    to_state = (payload.to_state or payload.target_state or "").strip()
    if to_state not in VALID_STATES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"非法 to_state: {to_state!r}；允许值：{sorted(VALID_STATES)}"
            ),
        )
    try:
        res: StateTransitionResult = transition_portfolio_state(
            db, portfolio_id, to_state,
            operator_id=actor,
            correlation_id=corr,
            reason=payload.reason or payload.trigger_reason,
        )
    except StateTransitionError as e:
        # ILLEGAL_STATE_TRANSITION 审计事件在底层已写 → 返回 409
        raise HTTPException(status_code=409, detail=str(e)) from e
    db.commit()
    return StateTransitionResponse(
        portfolio_id=res.portfolio_id,
        from_state=res.from_state,
        to_state=res.to_state,
        status=res.status,
        transition_applied=res.status == "OK",
        noop_detected=res.status == "NOOP",
        trigger_reason=payload.reason or payload.trigger_reason,
        correlation_id=corr,
    )


# ===========================================================================
# G4：审计事件查询 API（分页 + 过滤 + attributes_json 检索 + 权限校验）
# ===========================================================================
_STRICT_AUTH_DEFAULT = os.environ.get("STRICT_AUTH", "0") in {"1", "true", "True", "yes"}
_AUDITOR_LOCAL_DEV_DEFAULT = os.environ.get("AUDITOR_ALLOW_LOCAL_DEV", "0") in {"1", "true", "True", "yes"}
_AUDITOR_ROLES = {"admin", "auditor"}

# 可变全局变量，允许测试脚本直接 monkey patch（如 test_g4_audit_events_api T_G4_AUD_05）
# 值含义：None = 走 env fallback；True/False = 强制覆盖
_STRICT_AUTH_OVERRIDE: bool | None = None
_AUDITOR_LOCAL_DEV_ALLOW: bool | None = None


def _strict_auth_enabled() -> bool:
    """优先级：1) _STRICT_AUTH_OVERRIDE 2) 环境变量 STRICT_AUTH 3) _STRICT_AUTH_DEFAULT"""
    if _STRICT_AUTH_OVERRIDE is not None:
        return _STRICT_AUTH_OVERRIDE
    v = os.environ.get("STRICT_AUTH")
    if v is None:
        return _STRICT_AUTH_DEFAULT
    return v in {"1", "true", "True", "yes"}


def _auditor_local_dev_allowed() -> bool:
    """优先级：1) _AUDITOR_LOCAL_DEV_ALLOW 模块全局 2) AUDITOR_ALLOW_LOCAL_DEV env 3) default"""
    if _AUDITOR_LOCAL_DEV_ALLOW is not None:
        return _AUDITOR_LOCAL_DEV_ALLOW
    v = os.environ.get("AUDITOR_ALLOW_LOCAL_DEV")
    if v is None:
        return _AUDITOR_LOCAL_DEV_DEFAULT
    return v in {"1", "true", "True", "yes"}


def _require_actor(x_user: str | None = Query(default=None, alias="X-User")) -> str:
    """认证：X-User（作为 query param 传入；契约保持不变）非空即可；
    STRICT_AUTH 下空 X-User 返回 401 AUTH_MISSING。"""
    if not x_user:
        if _strict_auth_enabled():
            raise HTTPException(
                status_code=401,
                detail={
                    "error_code": "AUTH_MISSING",
                    "message": "X-User query parameter is required when STRICT_AUTH=1",
                },
            )
        return "local_user"
    return str(x_user).strip() or "local_user"


def _header_role(x_role: str | None = Query(default=None, alias="X-Role")) -> str | None:
    return (x_role or "").strip().lower() or None


def _raise_filter_error(message: str) -> None:
    raise HTTPException(
        status_code=400,
        detail={"error_code": "INVALID_FILTER", "message": message},
    )


def _validate_page(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        _raise_filter_error("page must be >= 1")
    if page_size < 1 or page_size > 200:
        _raise_filter_error("page_size must be between 1 and 200")
    return page, page_size


def _validate_range(occurred_from: datetime | None, occurred_to: datetime | None) -> None:
    if occurred_from and occurred_to and occurred_from > occurred_to:
        _raise_filter_error("occurred_from must be <= occurred_to")


class AuditEventRead(BaseModel):
    id: int
    action: str
    portfolio_id: int | None = None
    symbol_id: int | None = None
    business_key: str | None = None
    occurred_at: datetime
    operator_id: str
    correlation_id: str | None = None
    before: Any = None
    after: Any = None
    attributes: Any = None
    note: str | None = None


class AuditEventPage(BaseModel):
    items: list[AuditEventRead]
    total: int
    page: int
    page_size: int
    filter_action: str | None = None
    filter_operator_id: str | None = None
    filter_occurred_from: datetime | None = None
    filter_occurred_to: datetime | None = None
    filter_attributes_q: str | None = None
    # 权限校验提示：若 AUDITOR_ALLOW_LOCAL_DEV 打开，会提示"松校验模式"
    permissions_warning: str | None = None


def _ev_to_read(ev: DataGovernanceAuditEvent) -> AuditEventRead:
    import json
    def _loads(x: str | None) -> Any:
        if not x:
            return None
        try:
            return json.loads(x)
        except Exception:
            return x
    return AuditEventRead(
        id=ev.id, action=ev.action,
        portfolio_id=ev.portfolio_id, symbol_id=ev.symbol_id,
        business_key=ev.business_key, occurred_at=ev.occurred_at,
        operator_id=ev.operator_id, correlation_id=ev.correlation_id,
        before=_loads(ev.before_json), after=_loads(ev.after_json),
        attributes=_loads(ev.attributes_json), note=ev.note,
    )


def _build_audit_query(
    *,
    action: str | None, operator_id_: str | None,
    occurred_from: datetime | None, occurred_to: datetime | None,
    attributes_q: str | None,
    portfolio_id: int | None,
    symbol_id: int | None,
    business_key: str | None,
):
    predicates = []
    if action:
        if action not in ALLOWED_AUDIT_ACTIONS:
            _raise_filter_error(
                f"action filter not in allowed set {sorted(ALLOWED_AUDIT_ACTIONS)}"
            )
        predicates.append(DataGovernanceAuditEvent.action == action)
    if operator_id_:
        predicates.append(DataGovernanceAuditEvent.operator_id == operator_id_)
    if occurred_from:
        predicates.append(DataGovernanceAuditEvent.occurred_at >= occurred_from)
    if occurred_to:
        predicates.append(DataGovernanceAuditEvent.occurred_at <= occurred_to)
    if portfolio_id is not None:
        predicates.append(DataGovernanceAuditEvent.portfolio_id == int(portfolio_id))
    if symbol_id is not None:
        predicates.append(DataGovernanceAuditEvent.symbol_id == int(symbol_id))
    if business_key:
        predicates.append(DataGovernanceAuditEvent.business_key == business_key)
    if attributes_q:
        # 跨字段文本检索（before_json / after_json / attributes_json / note 包含）
        like = f"%{attributes_q}%"
        predicates.append(or_(
            DataGovernanceAuditEvent.before_json.like(like),
            DataGovernanceAuditEvent.after_json.like(like),
            DataGovernanceAuditEvent.attributes_json.like(like),
            (DataGovernanceAuditEvent.note or "").like(like) if False
            else DataGovernanceAuditEvent.note.like(like),
        ))
    return predicates


def _ensure_portfolio_access(db: Session, portfolio_id: int, actor: str) -> None:
    """松校验组合访问权限（若系统缺角色/归属关系，默认放行；显式黑名单才拒绝）。

    扩展点：此处可以接 `Portfolio.assigned_user_id == actor` 或 RBAC ACL。
    当前默认全部允许，未来有鉴权层后在这里插 403 FORBIDDEN_PORTFOLIO_ACCESS。
    """
    return None


# ──────────────────────────────────────────────────────────── GET /portfolios/{pid}/audit-events
@router.get(
    "/portfolios/{portfolio_id}/audit-events",
    response_model=AuditEventPage,
    status_code=200,
    summary="G4：查询单组合审计事件（分页 + 过滤 + attributes_json 检索）",
)
def get_portfolio_audit_events(
    portfolio_id: int,
    action: str | None = Query(default=None, description="过滤 action（必须 ∈ 8 枚举）"),
    operator_id: str | None = Query(default=None, alias="operator", description="按操作者过滤"),
    occurred_from: datetime | None = Query(default=None, description="≥ 发生时间"),
    occurred_to: datetime | None = Query(default=None, description="≤ 发生时间"),
    attributes_q: str | None = Query(
        default=None,
        description="文本包含检索：对 before_json/after_json/attributes_json/note 任一字段的子串匹配",
    ),
    page: int = Query(default=1, ge=1, description="页码 ≥1"),
    page_size: int = Query(default=20, ge=1, le=200, description="每页大小 1..200"),
    db: Session = Depends(get_db),
    actor: str = Depends(_require_actor),
) -> AuditEventPage:
    page, page_size = _validate_page(page, page_size)
    _validate_range(occurred_from, occurred_to)
    p = _load_portfolio(db, portfolio_id)
    _ensure_portfolio_access(db, portfolio_id, actor)

    predicates = _build_audit_query(
        action=action, operator_id_=operator_id,
        occurred_from=occurred_from, occurred_to=occurred_to,
        attributes_q=attributes_q,
        portfolio_id=p.id,
        symbol_id=None, business_key=None,
    )
    total = db.execute(
        select(sa_func.count()).select_from(DataGovernanceAuditEvent).where(and_(sa_true(), *predicates))
    ).scalar_one() or 0

    items_query = (
        select(DataGovernanceAuditEvent)
        .where(and_(sa_true(), *predicates))
        .order_by(DataGovernanceAuditEvent.occurred_at.desc(),
                  DataGovernanceAuditEvent.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = db.execute(items_query).scalars().all()
    return AuditEventPage(
        items=[_ev_to_read(r) for r in rows],
        total=int(total),
        page=page,
        page_size=page_size,
        filter_action=action,
        filter_operator_id=operator_id,
        filter_occurred_from=occurred_from,
        filter_occurred_to=occurred_to,
        filter_attributes_q=attributes_q,
        permissions_warning=None,
    )


# ──────────────────────────────────────────────────────────── GET /audit-events（全局）
@router.get(
    "/audit-events",
    response_model=AuditEventPage,
    status_code=200,
    summary="G4：全局审计事件查询（需 admin/auditor 角色；本地 AUDITOR_ALLOW_LOCAL_DEV=1 松校验）",
)
def get_global_audit_events(
    action: str | None = Query(default=None, description="过滤 action（必须 ∈ 8 枚举）"),
    operator_id: str | None = Query(default=None, alias="operator"),
    occurred_from: datetime | None = Query(default=None),
    occurred_to: datetime | None = Query(default=None),
    attributes_q: str | None = Query(default=None),
    portfolio_id: int | None = Query(default=None),
    symbol_id: int | None = Query(default=None),
    business_key: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    actor: str = Depends(_require_actor),
    role: str | None = Depends(_header_role),
) -> AuditEventPage:
    page, page_size = _validate_page(page, page_size)
    _validate_range(occurred_from, occurred_to)

    permissions_warning: str | None = None
    has_role = role in _AUDITOR_ROLES
    if not has_role:
        if _auditor_local_dev_allowed():
            permissions_warning = (
                "Warning: AUDITOR_ALLOW_LOCAL_DEV=1 opened (non-production)；"
                "production MUST require X-Role ∈ {admin, auditor}."
            )
        else:
            raise HTTPException(
                status_code=403,
                detail={
                    "error_code": "FORBIDDEN_AUDITOR_ROLE_REQUIRED",
                    "message": "X-Role header must be one of {admin, auditor} for global audit queries.",
                },
            )

    predicates = _build_audit_query(
        action=action, operator_id_=operator_id,
        occurred_from=occurred_from, occurred_to=occurred_to,
        attributes_q=attributes_q,
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        business_key=business_key,
    )
    total = db.execute(
        select(sa_func.count()).select_from(DataGovernanceAuditEvent).where(and_(sa_true(), *predicates))
    ).scalar_one() or 0
    items_query = (
        select(DataGovernanceAuditEvent)
        .where(and_(sa_true(), *predicates))
        .order_by(DataGovernanceAuditEvent.occurred_at.desc(),
                  DataGovernanceAuditEvent.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = db.execute(items_query).scalars().all()
    return AuditEventPage(
        items=[_ev_to_read(r) for r in rows],
        total=int(total),
        page=page,
        page_size=page_size,
        filter_action=action,
        filter_operator_id=operator_id,
        filter_occurred_from=occurred_from,
        filter_occurred_to=occurred_to,
        filter_attributes_q=attributes_q,
        permissions_warning=permissions_warning,
    )


# ===========================================================================
# G4：auto_simulation cron 门禁（cron worker 入口）
# ===========================================================================
class AutoSimulationPreflightRequest(BaseModel):
    trade_date: date = Field(..., description="T 日决策日（一般是 today；cron 在 T 日 20:30 调用）")
    decision_at: datetime | None = Field(
        default=None,
        description="模拟决策时间；不传则用 now()；用于门禁 time<20:00 的判定",
    )


class AutoSimulationPreflightResponse(BaseModel):
    portfolio_id: int
    trade_date: date
    proceed: bool
    skip_reason: Literal[
        "PORTFOLIO_NOT_READY",
        "SCHEDULE_TOO_EARLY",
        "DUAL_EXECUTION_RISK_PROHIBITED",
        "RECONCILIATION_GAP_WARNING",
        None,
    ] = None
    skip_detail: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    # proceed=True 时才有，用于后续 evaluate 时一致推进的 transition_id
    transition_trace: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)


@router.post(
    "/portfolios/{portfolio_id}/auto-simulation/preflight",
    response_model=AutoSimulationPreflightResponse,
    status_code=200,
    summary=(
        "G4：T 日 auto_simulation 启动前门禁（3 层检查：READY / 20:00 时间窗 / 对账连续性）。"
        "通过时 READY→RUNNING_AUTO_SIMULATION；失败返回 skip_reason 并写入 AUTO_SIMULATION_RESULT=FAILED 审计。"
    ),
)
def preflight_auto_simulation(
    portfolio_id: int,
    payload: AutoSimulationPreflightRequest,
    db: Session = Depends(get_db),
    actor: str = Depends(_require_actor),
) -> AutoSimulationPreflightResponse:
    """cron worker 在调用 evaluate(persist=True, run_type=auto_simulation) 前，**必须**先调用本接口。

    3 层门禁（任一 fail 则 proceed=False，不阻断其它组合调度）：
      1. 状态机 READY 检查 → 否则 PORTFOLIO_NOT_READY
      2. schedule gate ensure_auto_simulation_schedule → SCHEDULE_TOO_EARLY / DUAL_EXECUTION_RISK_PROHIBITED
      3. 对账连续性检查：last_reconciled_trade_date >= last_decision_trade_date - 1 day（或两者其一为空视为初日通过）
         —— 不 fail，仅 warnings+skip_reason=RECONCILIATION_GAP_WARNING（proceed 仍 True，
           但审计 AUTO_SIMULATION_RESULT=FAILED 写 warning，下游对账系统要追踪）
    """
    from app.services.portfolio_state_machine import (
        StateTransitionError,
        transition_portfolio_state,
    )
    from app.services.decision_schedule_gate import ensure_auto_simulation_schedule
    from app.services.data_governance_audit import audit_auto_simulation_result

    p = _load_portfolio(db, portfolio_id)
    current_state = _psm_get_status(db, p)
    corr = _corr_id()
    warnings: list[str] = []
    decision_at = payload.decision_at or datetime.now()

    # ── Check 1: 状态机 READY ───────────────────────────────────────────────
    if current_state != "READY":
        audit_auto_simulation_result(
            db, portfolio_id, payload.trade_date, result_status="FAILED",
            decision_run_id=f"preflight_skip_{portfolio_id}_{payload.trade_date.isoformat()}",
            operator_id=actor,
            correlation_id=corr,
            error_code="PORTFOLIO_NOT_READY",
        )
        db.commit()
        return AutoSimulationPreflightResponse(
            portfolio_id=portfolio_id, trade_date=payload.trade_date,
            proceed=False,
            skip_reason="PORTFOLIO_NOT_READY",
            skip_detail=(
                f"当前状态为 {current_state}，auto_simulation 必须从 READY 启动；"
                "可能是对账阻塞(RECONCILIATION_BLOCKED)或管理员暂停(ADMIN_PAUSED)。"
            ),
            from_state=current_state, to_state=None,
        )

    # ── Check 2: 调度时间 + 同日重复 ────────────────────────────────────────
    gate = ensure_auto_simulation_schedule(
        decision_at=decision_at,
        portfolio_id=portfolio_id,
        db=db,
    )
    if gate.allowed is False:
        ec = gate.error_code
        mapping = {
            "SCHEDULE_TOO_EARLY": "SCHEDULE_TOO_EARLY",
            "DUAL_EXECUTION_RISK_PROHIBITED": "DUAL_EXECUTION_RISK_PROHIBITED",
        }
        mapped = mapping.get(ec, None) or "SCHEDULE_TOO_EARLY"
        error_code_map = {
            "SCHEDULE_TOO_EARLY": "SCHEDULE_TOO_EARLY",
            "DUAL_EXECUTION_RISK_PROHIBITED": "DUAL_EXECUTION_DETECTED",
        }
        audit_auto_simulation_result(
            db, portfolio_id, payload.trade_date, result_status="FAILED",
            decision_run_id=f"preflight_skip_{portfolio_id}_{payload.trade_date.isoformat()}",
            operator_id=actor, correlation_id=corr,
            error_code=error_code_map.get(mapped, "PREFLIGHT_GATE_FAILED"),
        )
        db.commit()
        return AutoSimulationPreflightResponse(
            portfolio_id=portfolio_id, trade_date=payload.trade_date,
            proceed=False, skip_reason=mapped,  # type: ignore[arg-type]
            skip_detail=gate.message,
            from_state=current_state, to_state=None,
        )

    # ── Check 3: 对账连续性 ─────────────────────────────────────────────────
    last_dec = p.last_decision_trade_date
    last_rec = p.last_reconciled_trade_date
    skip_reason: Any = None
    if last_dec is not None and last_rec is not None:
        delta_days = (last_dec - last_rec).days
        if delta_days > 1:
            msg = (
                f"决策/对账游标间隔 {delta_days} 天 > 1（last_decision={last_dec}, "
                f"last_rec={last_rec}）；可能漏做对账，需人工复核。"
            )
            warnings.append(msg)
            # FR-P1-3 HG3：即使 proceed=True，也必须写 AUTO_SIMULATION_RESULT=FAILED 审计（告警追踪）
            audit_auto_simulation_result(
                db, portfolio_id, payload.trade_date, result_status="FAILED",
                decision_run_id=f"preflight_warn_{portfolio_id}_{payload.trade_date.isoformat()}",
                operator_id=actor,
                correlation_id=corr,
                error_code="RECONCILIATION_GAP_WARNING",
                error_message=msg,
            )
            skip_reason = "RECONCILIATION_GAP_WARNING"

    # ── 通过：READY → RUNNING_AUTO_SIMULATION ──────────────────────────────
    try:
        tr = transition_portfolio_state(
            db, portfolio_id, "RUNNING_AUTO_SIMULATION",
            operator_id=actor,
            correlation_id=corr,
            reason="auto_simulation preflight passed",
        )
    except StateTransitionError as e:
        # 理论上 Check1 已验证 READY，但并发下可能有问题；写审计失败
        audit_auto_simulation_result(
            db, portfolio_id, payload.trade_date, result_status="FAILED",
            decision_run_id=f"preflight_skip_{portfolio_id}_{payload.trade_date.isoformat()}",
            operator_id=actor, correlation_id=corr,
            error_code="STATE_TRANSITION_FAILED_RACE",
        )
        db.commit()
        return AutoSimulationPreflightResponse(
            portfolio_id=portfolio_id, trade_date=payload.trade_date,
            proceed=False, skip_reason="PORTFOLIO_NOT_READY",
            skip_detail=str(e),
            from_state=current_state, to_state=None,
        )
    db.commit()
    return AutoSimulationPreflightResponse(
        portfolio_id=portfolio_id, trade_date=payload.trade_date,
        proceed=True, skip_reason=skip_reason,  # type: ignore[arg-type]
        skip_detail=(warnings[0] if warnings else None),
        from_state=tr.from_state, to_state=tr.to_state,
        transition_trace={
            "correlation_id": corr,
            "gate_schedule_data_cutoff_at": (
                gate.effective_data_cutoff_at.isoformat() if gate.effective_data_cutoff_at else None
            ),
            "gate_schedule_trade_date": (
                gate.effective_data_as_of_trade_date.isoformat() if gate.effective_data_as_of_trade_date else None
            ),
            "gate_allowed": True,
            "gate_error_code": gate.error_code,
        },
        warnings=warnings,
    )
