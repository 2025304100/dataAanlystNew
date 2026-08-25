"""T-D1 Q27.1 / Q27.2 / Q27.3：组合四维度状态 + 总状态聚合。

规则（严格 tasks.md 定义，不可写 if-elif 分支）：
  1. Portfolio 表 NOT 存一个总的 status。只存 4 个独立维度：
        status_score / status_data / status_model / status_reconciliation
     每个维度 ∈ PortfolioStatus 六状态。
  2. 总状态 composite_status = resolve_composite_status(四维度)：
        取 max(block_level) → 反查 PortfolioStatus.from_block_level
     严禁写成 if status_reconciliation==X elif status_model==Y。

T-D2 约定（Q27.3）：16 组合"覆盖矩阵" = 4 维度 × 4 种子集
    {READY, RUNNING, DATA_INCOMPLETE_PAUSED, RECONCILIATION_BLOCKED}
  共 4⁴ = 256 组合。出于性能 + 覆盖度折中：
    - 我们在 `resolve_composite_status_from_row` 层把 256 组合**穷举**写进 pytest，
    - 同时额外断言：任何一维 = RECONCILIATION_BLOCKED(6) 时，composite 必是 6；
    - 严格不写 if-elif，只用 max()。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, NamedTuple, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.portfolio import (
    Portfolio,
    PORTFOLIO_STATUS_BLOCK_LEVEL,
    PortfolioStatus,
    PORTFOLIO_STATUS_VALUES,
)

StatusDimStr = str


@dataclass(frozen=True)
class ResolvedCompositeStatus:
    """聚合输出：总状态 + 4 维度原始值（常被 API JSON 整体返回）。"""

    composite_status: PortfolioStatus
    status_score: PortfolioStatus
    status_data: PortfolioStatus
    status_model: PortfolioStatus
    status_reconciliation: PortfolioStatus
    highest_block_level: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "composite_status": self.composite_status.value,
            "status_score": self.status_score.value,
            "status_data": self.status_data.value,
            "status_model": self.status_model.value,
            "status_reconciliation": self.status_reconciliation.value,
            "highest_block_level": self.highest_block_level,
        }


def _to_status_enum(raw: StatusDimStr | PortfolioStatus | None) -> PortfolioStatus:
    if raw is None:
        return PortfolioStatus.READY
    if isinstance(raw, PortfolioStatus):
        return raw
    raw_s = str(raw)
    if raw_s not in PORTFOLIO_STATUS_VALUES:
        # 防御：非法值应该抛出一个可见的 ValueError，让 migration/审计先发现。
        raise ValueError(
            f"Invalid portfolio status value {raw_s!r}. "
            f"Expected one of {sorted(PORTFOLIO_STATUS_VALUES)}."
        )
    return PortfolioStatus(raw_s)


def resolve_composite_status_from_row(
    status_score: StatusDimStr | PortfolioStatus | None,
    status_data: StatusDimStr | PortfolioStatus | None,
    status_model: StatusDimStr | PortfolioStatus | None,
    status_reconciliation: StatusDimStr | PortfolioStatus | None,
) -> ResolvedCompositeStatus:
    """纯函数版：四维度 → 总状态。不连 DB、不读 ORM，T-D2 16 组合全矩阵用这个。

    严格实现（tasks.md D2 禁止写 if-elif）：
        max( level(score), level(data), level(model), level(reconciliation) )
        → PortfolioStatus.from_block_level(max_level)
    """
    s_sc = _to_status_enum(status_score)
    s_da = _to_status_enum(status_data)
    s_mo = _to_status_enum(status_model)
    s_re = _to_status_enum(status_reconciliation)

    max_level: int = max(
        PORTFOLIO_STATUS_BLOCK_LEVEL[s_sc.value],
        PORTFOLIO_STATUS_BLOCK_LEVEL[s_da.value],
        PORTFOLIO_STATUS_BLOCK_LEVEL[s_mo.value],
        PORTFOLIO_STATUS_BLOCK_LEVEL[s_re.value],
    )
    composite = PortfolioStatus.from_block_level(max_level)
    return ResolvedCompositeStatus(
        composite_status=composite,
        status_score=s_sc,
        status_data=s_da,
        status_model=s_mo,
        status_reconciliation=s_re,
        highest_block_level=max_level,
    )


def resolve_composite_status(portfolio: Portfolio) -> ResolvedCompositeStatus:
    """ORM 对象版：直接从 Portfolio 实例聚合。"""
    return resolve_composite_status_from_row(
        status_score=portfolio.status_score,
        status_data=portfolio.status_data,
        status_model=portfolio.status_model,
        status_reconciliation=portfolio.status_reconciliation,
    )


def resolve_composite_status_by_id(db: Session, portfolio_id: int) -> ResolvedCompositeStatus:
    """T-D2：Q27.3 resolve_composite_status(portfolio_id) 入口。

    Steps：
      1. SELECT status_score, status_data, status_model, status_reconciliation FROM portfolios WHERE id=?
      2. 返回 resolve_composite_status_from_row 结果。
         portfolio 不存在 → raise LookupError（返回具体 portfolio_id 帮助排障）。
    """
    row = db.execute(
        select(
            Portfolio.status_score,
            Portfolio.status_data,
            Portfolio.status_model,
            Portfolio.status_reconciliation,
        ).where(Portfolio.id == int(portfolio_id))
    ).one_or_none()
    if row is None:
        raise LookupError(f"Portfolio id={portfolio_id} not found for resolve_composite_status_by_id")
    return resolve_composite_status_from_row(
        status_score=row.status_score,
        status_data=row.status_data,
        status_model=row.status_model,
        status_reconciliation=row.status_reconciliation,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# T-D3 Q27.4：自动恢复 vs 人工恢复收敛
#
# 自动恢复维度（仅在外部事件确认完成时置 READY）：
#   * status_data：DATA_INCOMPLETE_PAUSED → 数据补齐完成 → READY
#   * status_score：SCORE_STALE → Score 补齐完成 → READY
#
# 纯人工维度（**绝不**允许自动恢复，必须显式调用 confirm_* API）：
#   * status_model：MODEL_INACTIVE → 必须人工 confirm_active_model_binding → READY
#   * status_reconciliation：RECONCILIATION_BLOCKED → 必须人工 confirm_reconciliation_passed → READY
#     注意：即便是"自动对账 run success"也只是数据层的 DATA_INCOMPLETE_PAUSED 的信号，
#         RECONCILIATION_BLOCKED 本身仍然锁定，必须结合"Q18.3 人工复核通过"双条件。
#
# 策略快照变化（任何 strategy_snapshot_id 变化 → 人工确认接受新快照）：本次不在 DB 层
#     单独建维度列，由调用方在生成新 strategy_snapshot 时显式将 status_model 置为
#     MODEL_INACTIVE（等价语义：模型绑定失效 → 走人工 confirm_active_model_binding 链路）。
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class AutoRecoveryResult:
    status_score_before: PortfolioStatus
    status_data_before: PortfolioStatus
    status_model_before: PortfolioStatus
    status_reconciliation_before: PortfolioStatus
    status_score_after: PortfolioStatus
    status_data_after: PortfolioStatus
    status_model_after: PortfolioStatus
    status_reconciliation_after: PortfolioStatus
    auto_changed_fields: tuple[str, ...]  # 例 ("status_score", "status_data")
    manual_only_blocking_left: tuple[str, ...]  # 例 ("status_model",)

    @property
    def anything_changed(self) -> bool:
        return len(self.auto_changed_fields) > 0


def auto_recover_dimensions_from_row(
    *,
    status_score: StatusDimStr | PortfolioStatus | None,
    status_data: StatusDimStr | PortfolioStatus | None,
    status_model: StatusDimStr | PortfolioStatus | None,
    status_reconciliation: StatusDimStr | PortfolioStatus | None,
    data_completed: bool = False,
    score_completed: bool = False,
) -> AutoRecoveryResult:
    """T-D3 纯函数层：按维度自动恢复策略计算新状态。

    绝不触碰 manual-only 的 status_model / status_reconciliation。
    """
    sc_b = _to_status_enum(status_score)
    da_b = _to_status_enum(status_data)
    mo_b = _to_status_enum(status_model)
    re_b = _to_status_enum(status_reconciliation)

    sc_a = sc_b
    da_a = da_b
    changed: list[str] = []

    if score_completed and sc_b == PortfolioStatus.SCORE_STALE:
        sc_a = PortfolioStatus.READY
        changed.append("status_score")

    if data_completed and da_b == PortfolioStatus.DATA_INCOMPLETE_PAUSED:
        da_a = PortfolioStatus.READY
        changed.append("status_data")

    manual_only_left: list[str] = []
    if mo_b != PortfolioStatus.READY:
        manual_only_left.append("status_model")
    if re_b != PortfolioStatus.READY:
        manual_only_left.append("status_reconciliation")

    return AutoRecoveryResult(
        status_score_before=sc_b,
        status_data_before=da_b,
        status_model_before=mo_b,
        status_reconciliation_before=re_b,
        status_score_after=sc_a,
        status_data_after=da_a,
        status_model_after=mo_b,  # 人工维度不碰
        status_reconciliation_after=re_b,  # 人工维度不碰
        auto_changed_fields=tuple(changed),
        manual_only_blocking_left=tuple(manual_only_left),
    )


def try_auto_recover_dimensions(
    db: Session,
    portfolio_id: int,
    *,
    data_completed: bool = False,
    score_completed: bool = False,
) -> AutoRecoveryResult:
    """T-D3 DB 入口：外部事件（数据补齐 / Score 补齐）触发后调用。

    - 只会自动恢复：status_data(DATA_INCOMPLETE_PAUSED) / status_score(SCORE_STALE)
    - 绝不自动恢复：status_model(MODEL_INACTIVE) / status_reconciliation(RECONCILIATION_BLOCKED)
      （后者必须人工 confirm_reconciliation_passed，哪怕自动对账 run success。）
    """
    p = db.get(Portfolio, int(portfolio_id))
    if p is None:
        raise LookupError(f"Portfolio id={portfolio_id} not found for try_auto_recover_dimensions")
    result = auto_recover_dimensions_from_row(
        status_score=p.status_score,
        status_data=p.status_data,
        status_model=p.status_model,
        status_reconciliation=p.status_reconciliation,
        data_completed=bool(data_completed),
        score_completed=bool(score_completed),
    )
    if result.anything_changed:
        if "status_score" in result.auto_changed_fields:
            p.status_score = result.status_score_after
        if "status_data" in result.auto_changed_fields:
            p.status_data = result.status_data_after
        db.flush()
    return result


@dataclass(frozen=True)
class ManualConfirmResult:
    portfolio_id: int
    status_model_before: PortfolioStatus
    status_model_after: PortfolioStatus
    status_reconciliation_before: PortfolioStatus
    status_reconciliation_after: PortfolioStatus
    model_confirmed: bool
    reconciliation_confirmed: bool
    changed_fields: tuple[str, ...]


def confirm_active_model_binding(
    db: Session,
    portfolio_id: int,
    *,
    confirm_model_binding: bool = True,
    confirm_reconciliation_passed: bool = False,
) -> ManualConfirmResult:
    """T-D3 人工入口：`POST /portfolios/{id}/confirm-active-model-binding`。

    - confirm_model_binding=True → status_model(MODEL_INACTIVE) → READY
      （含语义：接受新 strategy_snapshot_id，"策略快照变化→人工确认"合并入本调用。）
    - confirm_reconciliation_passed=True → status_reconciliation(RECONCILIATION_BLOCKED) → READY
      （语义：Q18.3 人工复核通过；即便是自动对账成功了也必须单独加这个显式标志）

    两个确认可在一次调用中同时完成（通过两个 bool）；但绝不会恢复 data/score 维度——
    data/score 必须走自动恢复 try_auto_recover_dimensions 的事件驱动链路。
    """
    p = db.get(Portfolio, int(portfolio_id))
    if p is None:
        raise LookupError(f"Portfolio id={portfolio_id} not found for confirm_active_model_binding")
    mo_b = _to_status_enum(p.status_model)
    re_b = _to_status_enum(p.status_reconciliation)
    mo_a = mo_b
    re_a = re_b
    changed: list[str] = []
    model_confirmed = bool(confirm_model_binding)
    rec_confirmed = bool(confirm_reconciliation_passed)

    if model_confirmed and mo_b != PortfolioStatus.READY:
        mo_a = PortfolioStatus.READY
        changed.append("status_model")
    if rec_confirmed and re_b != PortfolioStatus.READY:
        re_a = PortfolioStatus.READY
        changed.append("status_reconciliation")

    if changed:
        if "status_model" in changed:
            p.status_model = mo_a
        if "status_reconciliation" in changed:
            p.status_reconciliation = re_a
        db.flush()

    return ManualConfirmResult(
        portfolio_id=int(portfolio_id),
        status_model_before=mo_b,
        status_model_after=mo_a,
        status_reconciliation_before=re_b,
        status_reconciliation_after=re_a,
        model_confirmed=model_confirmed,
        reconciliation_confirmed=rec_confirmed,
        changed_fields=tuple(changed),
    )


__all__ = [
    "PortfolioStatus",
    "ResolvedCompositeStatus",
    "resolve_composite_status",
    "resolve_composite_status_from_row",
    "resolve_composite_status_by_id",
    "AutoRecoveryResult",
    "auto_recover_dimensions_from_row",
    "try_auto_recover_dimensions",
    "ManualConfirmResult",
    "confirm_active_model_binding",
    "GateCheckResult",
    "order_entry_gate_check",
    "calc_task_idempotency_key",
    "try_acquire_idempotency",
    "IdempotencyAcquireResult",
]


# ═══════════════════════════════════════════════════════════════════════════════
# T-D5 Q28.1：幂等键后端主键公式 + 专用幂等表
#
#   key = sha256(f"{portfolio_id}|{strategy_snapshot_id}|{decision_at.isoformat()}|{trade_date.isoformat()}|{run_type}").hexdigest()
#
# 关键点：
#   * 决策时刻 decision_at 必须是 UTC naive（SQLite / MySQL 直接落表不带 tz）；
#   * isoformat 固定字符串，禁止 strftime("%Y-%m-%d %H:%M") 之类格式漂移；
#   * 5 字段任一变化 → sha256 雪崩效应 64 hex 不同；
#   * 同 key 写入第 2 次：先 SELECT 取 existing_task_id 返回，不触发 IntegrityError（走 try_acquire_idempotency）；
#     若直接 INSERT 第二次 → DB UniqueConstraint 抛 IntegrityError。
# ═══════════════════════════════════════════════════════════════════════════════

_IDEMPOTENCY_DELIM = "|"


def calc_task_idempotency_key(
    *,
    portfolio_id: int,
    strategy_snapshot_id: str,
    decision_at: datetime,
    trade_date: date,
    run_type: str,
) -> str:
    """T-D5：按契约计算 idempotency_key（64 hex sha256）。

    参数会被强制规范化：
      - portfolio_id → int
      - strategy_snapshot_id → str.strip()（空字符串不允许，抛 ValueError）
      - decision_at → datetime（tz-aware 转 UTC naive）
      - trade_date → date
      - run_type → str.strip()（空字符串不允许，抛 ValueError）
    """
    pid_int = int(portfolio_id)
    if pid_int < 0:
        raise ValueError(f"portfolio_id must be >= 0, got {pid_int}")
    sid = str(strategy_snapshot_id).strip()
    if not sid:
        raise ValueError("strategy_snapshot_id must not be empty")
    rt = str(run_type).strip()
    if not rt:
        raise ValueError("run_type must not be empty")

    if isinstance(decision_at, datetime):
        dt = decision_at
    else:
        raise TypeError(f"decision_at must be datetime, got {type(decision_at).__name__}")
    # tz-aware → UTC naive
    if dt.tzinfo is not None:
        try:
            import datetime as _dt
            dt_utc = dt.astimezone(_dt.timezone.utc)
            dt_naive = dt_utc.replace(tzinfo=None)
        except Exception as exc:  # pragma: no cover - defensive
            raise ValueError(f"decision_at timezone normalize failed: {exc}") from exc
    else:
        dt_naive = dt

    td = trade_date if isinstance(trade_date, date) and not isinstance(trade_date, datetime) else None
    if td is None:
        if isinstance(trade_date, datetime):
            td = trade_date.date()
        else:
            raise TypeError(f"trade_date must be date, got {type(trade_date).__name__}")

    payload = _IDEMPOTENCY_DELIM.join([
        str(pid_int),
        sid,
        dt_naive.isoformat(timespec="seconds"),
        td.isoformat(),
        rt,
    ])
    import hashlib
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotencyAcquireResult:
    acquired: bool  # True=本调用首次插入；False=已存在重放
    idempotency_key: str
    existing_task_id: str | None
    param_hash: str | None
    row: Any  # TaskIdempotency ORM row


def try_acquire_idempotency(
    db: Session,
    *,
    portfolio_id: int,
    strategy_snapshot_id: str,
    decision_at: datetime,
    trade_date: date,
    run_type: str,
    task_type: str,
    existing_task_id: str | None = None,
    param_hash: str | None = None,
) -> IdempotencyAcquireResult:
    """T-D5 幂等获取：先 SELECT，不存在就 INSERT；存在就直接返回原记录（重放安全）。"""
    from app.models.decision_engine import TaskIdempotency

    key = calc_task_idempotency_key(
        portfolio_id=portfolio_id,
        strategy_snapshot_id=strategy_snapshot_id,
        decision_at=decision_at,
        trade_date=trade_date,
        run_type=run_type,
    )
    existing = db.execute(
        select(TaskIdempotency).where(TaskIdempotency.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        return IdempotencyAcquireResult(
            acquired=False,
            idempotency_key=key,
            existing_task_id=existing.existing_task_id,
            param_hash=existing.param_hash,
            row=existing,
        )
    row = TaskIdempotency(
        idempotency_key=key,
        portfolio_id=int(portfolio_id),
        strategy_snapshot_id=str(strategy_snapshot_id).strip(),
        decision_at=(
            decision_at.replace(tzinfo=None)
            if getattr(decision_at, "tzinfo", None) is None
            else decision_at.astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None)
        ),
        trade_date=trade_date if isinstance(trade_date, date) and not isinstance(trade_date, datetime) else trade_date.date(),
        run_type=str(run_type).strip(),
        task_type=str(task_type).strip(),
        existing_task_id=(str(existing_task_id).strip() if existing_task_id is not None else None),
        param_hash=(str(param_hash).strip() if param_hash is not None else None),
    )
    db.add(row)
    db.flush()
    return IdempotencyAcquireResult(
        acquired=True,
        idempotency_key=key,
        existing_task_id=row.existing_task_id,
        param_hash=row.param_hash,
        row=row,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# T-D4 Q27.5：阻断状态禁止新买单，风险退出 SELL 仍按可验证数据执行
#
# 行为矩阵（总状态 composite block_level）：
#   * 新买单类 action in {BUY, REBALANCE_BUY, CASH_SWAP_IN}
#       - level < 3 (READY=1 / RUNNING=2)             → ALLOW
#       - level ≥ 3 (SCORE_STALE=3 / DATA_INCOMPLETE_PAUSED=4 /
#                   MODEL_INACTIVE=5 / RECONCILIATION_BLOCKED=6)  → BLOCK
#   * 风险退出类 action in {SELL_STOP_LOSS, SELL_RISK_EXIT, EXIT}
#       - 无论阻断级别多少（即使 RECONCILIATION_BLOCKED=6）→ 仍允许下单
#       - 但必须先走 T-A8 数据可验证 check；若 data_verifiable_for_sell=False
#         → 以 UNABLE_TO_VERIFY_STOP_LOSS 拒绝（不生成订单）
#   * 其他 action（HOLD / MODEL_INACTIVE / 等非买卖显式动作）→ 一律放行交给上层裁决
# ═══════════════════════════════════════════════════════════════════════════════

BUY_ENTRY_ACTIONS: frozenset[str] = frozenset({"BUY", "REBALANCE_BUY", "CASH_SWAP_IN"})
SELL_EXIT_ACTIONS: frozenset[str] = frozenset({"SELL_STOP_LOSS", "SELL_RISK_EXIT", "EXIT"})
REJECTION_UNABLE_TO_VERIFY_STOP_LOSS = "UNABLE_TO_VERIFY_STOP_LOSS"
REJECTION_COMPOSITE_BLOCK_LEVEL_TOO_HIGH = "COMPOSITE_BLOCK_LEVEL_TOO_HIGH"


@dataclass(frozen=True)
class GateCheckResult:
    allowed: bool
    requested_action: str
    composite_before: ResolvedCompositeStatus
    block_reason: str | None
    rejection_reason_code: str | None

    @property
    def blocked(self) -> bool:
        return not self.allowed


def order_entry_gate_check(
    db: Session,
    portfolio_id: int,
    requested_action: str,
    *,
    data_verifiable_for_sell: bool = True,
) -> GateCheckResult:
    """T-D4 订单入口门禁。

    Args:
        data_verifiable_for_sell: 当 requested_action 属于 SELL 风险退出类时，
            T-A8 数据可验证标志。True=今日价格/行情齐全可验证是否已触达止损线；
            False=数据缺失且无法验证 → 拒绝并返回 UNABLE_TO_VERIFY_STOP_LOSS。
    """
    composite = resolve_composite_status_by_id(db, int(portfolio_id))
    action = str(requested_action).strip().upper()

    if action in BUY_ENTRY_ACTIONS:
        if composite.highest_block_level >= 3:
            return GateCheckResult(
                allowed=False,
                requested_action=action,
                composite_before=composite,
                block_reason=(
                    f"composite block_level={composite.highest_block_level} >= 3 "
                    f"(status={composite.composite_status.value}); new buy actions are blocked"
                ),
                rejection_reason_code=REJECTION_COMPOSITE_BLOCK_LEVEL_TOO_HIGH,
            )
        return GateCheckResult(
            allowed=True,
            requested_action=action,
            composite_before=composite,
            block_reason=None,
            rejection_reason_code=None,
        )

    if action in SELL_EXIT_ACTIONS:
        # 允许生成 SELL（即便是 RECONCILIATION_BLOCKED 6 级），但必须 T-A8 可验证
        if not bool(data_verifiable_for_sell):
            return GateCheckResult(
                allowed=False,
                requested_action=action,
                composite_before=composite,
                block_reason=(
                    f"sell-exit action {action} enabled but T-A8 data_verifiable=False; "
                    f"missing market data to verify whether stop-loss was hit"
                ),
                rejection_reason_code=REJECTION_UNABLE_TO_VERIFY_STOP_LOSS,
            )
        return GateCheckResult(
            allowed=True,
            requested_action=action,
            composite_before=composite,
            block_reason=None,
            rejection_reason_code=None,
        )

    # 其他 action：门禁不拦，交给上层业务逻辑
    return GateCheckResult(
        allowed=True,
        requested_action=action,
        composite_before=composite,
        block_reason=None,
        rejection_reason_code=None,
    )
