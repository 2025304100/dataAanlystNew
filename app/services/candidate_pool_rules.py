"""portfolio-factor-backtest-full-linkage #1：候选池 BUY 门禁（纯函数实现）。

Seam：evaluate_candidate_buy_eligibility(...) → CandidateBuyEligibilityResult
  - BUY 必须在候选池内（SCD2 按 trade_date 当日有效，同日多版本取 audit_version 最大）
  - removed_manually_flag=1 必须永久 BLOCK（MANUALLY_REMOVED_BLOCKED），
    除非 require_restore_operation_for_removed=False（显式"恢复候选资格"入口）
  - SELL 永远通过（卖出不限制候选身份）

优先级（同时满足时取最根本原因）：
  ① OUTSIDE_CANDIDATE_POOL（候选已过期 / 未来才生效 → 候选身份根本不存在）
  ② MANUALLY_REMOVED_BLOCKED（候选在池内但标记移除 → 必须人工恢复操作）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Literal, Sequence, TypeVar

from app.services.tri_entry_candidate_snapshot import pick_candidate_snapshot_rows


# ────────────────────────────────────────────────────────────────────────────
# 结果契约（固定字段以便 DecisionEvidence.metadata_json 消费）
# ────────────────────────────────────────────────────────────────────────────
_RejectionReason = Literal[None, "OUTSIDE_CANDIDATE_POOL", "MANUALLY_REMOVED_BLOCKED"]


@dataclass
class CandidateBuyEligibilityResult:
    allowed: bool
    rejection_reason: _RejectionReason
    decision_evidence_meta: dict[str, Any] = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────────
# 取候选行字段（兼容 dataclass / ORM / dict）
# ────────────────────────────────────────────────────────────────────────────
T = TypeVar("T")


def _attr(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _flag_bool_int(x: Any) -> bool:
    """1/0 小整数 → bool；T/F 直接返回。"""
    if isinstance(x, bool):
        return x
    try:
        return bool(int(x))
    except (TypeError, ValueError):
        return False


# ────────────────────────────────────────────────────────────────────────────
# 主 Seam
# ────────────────────────────────────────────────────────────────────────────
_Action = Literal["BUY", "SELL"]


def evaluate_candidate_buy_eligibility(
    *,
    portfolio_id: int,
    symbol_id: int,
    action: _Action,
    candidate_rows: Sequence[T] | Iterable[T],
    current_position_qty: int | float,
    trade_date: date,
    require_restore_operation_for_removed: bool = True,
) -> CandidateBuyEligibilityResult:
    if action == "SELL":
        # 卖出动作不经过候选门禁（先有持仓才能卖，不应强制要求候选）。
        # 不填 candidate_effective_from（卖不需要候选审计），但记录 action=SELL 直通。
        return CandidateBuyEligibilityResult(
            allowed=True,
            rejection_reason=None,
            decision_evidence_meta={"bypass": "SELL action never requires candidate"},
        )

    if action != "BUY":
        raise ValueError(
            f"action must be 'BUY' or 'SELL'; got {action!r}"
        )
    if not isinstance(trade_date, date):
        raise TypeError(
            f"trade_date must be datetime.date; got {type(trade_date).__name__}"
        )

    # ① 先按 trade_date 取 SCD2 快照（同 #4 模块逻辑，复用保持一致性）。
    # dry_run 视角：按"视角日=候选动作日"取数；这里只关心"这一行 + 这一天是否在池"
    snapshot_rows = pick_candidate_snapshot_rows(
        candidate_rows,
        "dry_run",  # 关键：直接使用"按给定 trade_date 当天精确"的语义；dry_run 在本层就是当日快照
        today=trade_date,
    )
    # 从 snapshot 中筛选本 symbol 行（pick_candidate_snapshot_rows 已经是按 symbol 唯一化后的 dict 值列表）
    matched: T | None = None
    for r in snapshot_rows:
        if _attr(r, "symbol_id") == symbol_id:
            # 还要确认 portfolio_id（防止混用行）
            if _attr(r, "portfolio_id") is None or int(_attr(r, "portfolio_id")) == int(portfolio_id):
                matched = r
                break

    if matched is None:
        return CandidateBuyEligibilityResult(
            allowed=False,
            rejection_reason="OUTSIDE_CANDIDATE_POOL",
            decision_evidence_meta={
                "rejection_rule": "symbol not present in candidate snapshot on trade_date",
                "trade_date": trade_date.isoformat(),
                "portfolio_id": int(portfolio_id),
                "symbol_id": int(symbol_id),
            },
        )

    # ② 候选已在池内：先检查 removed_manually_flag
    removed_flag = _flag_bool_int(_attr(matched, "removed_manually_flag"))
    removal_reason = _attr(matched, "removal_reason", None)
    effective_from = _attr(matched, "effective_from", None)
    audit_version = _attr(matched, "audit_version", None)

    base_meta: dict[str, Any] = {
        "portfolio_id": int(portfolio_id),
        "symbol_id": int(symbol_id),
        "trade_date": trade_date.isoformat(),
        "current_position_qty": float(current_position_qty) if isinstance(current_position_qty, (int, float)) else 0.0,
    }
    if effective_from is not None and isinstance(effective_from, date):
        base_meta["candidate_effective_from"] = effective_from.isoformat()
    if audit_version is not None:
        try:
            base_meta["candidate_audit_version"] = int(audit_version)
        except (TypeError, ValueError):
            base_meta["candidate_audit_version"] = audit_version

    if removed_flag and require_restore_operation_for_removed:
        base_meta["removal_reason"] = (
            removal_reason if removal_reason is not None else "MANUAL_REMOVE"
        )
        base_meta["require_restore_operation_for_removed"] = True
        return CandidateBuyEligibilityResult(
            allowed=False,
            rejection_reason="MANUALLY_REMOVED_BLOCKED",
            decision_evidence_meta=base_meta,
        )

    # ③ 通过：允许 BUY
    if removal_reason is not None:
        # 仅 restored 场景记录（removed=0 但 removal_reason 有残留，代表曾经移除过然后恢复）
        base_meta.setdefault("removal_reason", removal_reason)
    base_meta["require_restore_operation_for_removed"] = require_restore_operation_for_removed
    return CandidateBuyEligibilityResult(
        allowed=True, rejection_reason=None, decision_evidence_meta=base_meta
    )
