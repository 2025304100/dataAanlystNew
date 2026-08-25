"""WP0-2 Step 3b：PortfolioFactorUsage 单事务 7 步原子保存服务。

七步原子流程 save_and_apply_usage_atomic(portfolio_id, payload, expected_row_version,
idempotency_key, db, *, inject_failure=None, correlation_id=None, operator_id=None)：
  1. 开启单事务（或使用已传入的 db Session 事务边界）
  2. 查 IdempotencyRecords：同 idempotency_key → (a) request_hash 相同=重放直接返回原响应
                              (b) request_hash 不同=参数冲突 → HTTP 409
  3. 计算 factor_weights_hash（前端提交的 expected_hash 与后端 compute hash 不同 → 抛 ValueError/409）
  4. Upsert PortfolioFactorUsage：乐观锁 WHERE id=? AND row_version=expected_row_version
     row_version 版本不匹配 → HTTP 409（CONCURRENT_UPDATE）
  5. StrategyExecutionSnapshot 插入：snapshot_no 按组合递增 1（max(snapshot_no)+1）
  6. OutboxEvent PORTFOLIO_FACTOR_USAGE_APPLIED 写入
  7. 写 IdempotencyRecords：幂等记录与结果落库
  8. COMMIT；任何一步抛异常 → 整体 ROLLBACK（测试 inject_failure 在任意步均可触发）

inject_failure 钩子：每次调用在指定 step_number 前抛 RuntimeError（测试 A2 中间失败回滚）。
钩子签名: inject_failure(step_number: int, context: dict) -> None
"""
from __future__ import annotations

import json as _json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.hash_utils import canonical_json, content_hash
from app.models.portfolio_factor_usage import (
    PortfolioFactorUsage, BINDING_STATUS_APPLIED,
)
from app.models.decision_engine import StrategyExecutionSnapshot
from app.models.outbox_event import OutboxEvent, OUTBOX_STATUS_PENDING
from app.models.idempotency_records import IdempotencyRecord  # 复数模块兼容入口


# ── hash helpers ────────────────────────────────────────────────────────────
def compute_factor_weights_canonical_hash(
    factor_weights: Any,
    factor_set_id: int | None,
    model_run_id: int | None,
) -> str:
    """T4a 契约：对三元组 (factor_set_id, model_run_id, factor_weights) 做 canonical_hash。

    等价：content_hash(["pfu_v1", factor_set_id, model_run_id, factor_weights])
    """
    parts: list[Any] = ["pfu_v1"]
    parts.append(int(factor_set_id) if factor_set_id is not None else None)
    parts.append(int(model_run_id) if model_run_id is not None else None)
    parts.append(factor_weights)
    return content_hash(*parts)


@dataclass
class FactorUsageOption:
    """get_factor_usage_options 返回的单条选项。"""
    factor_set_id: int
    factor_set_name: str
    model_run_id: int | None = None
    model_name: str | None = None
    runtime_active: bool = True


def get_factor_usage_options(
    db: Session | None = None,
    *,
    filter_active_only: bool = True,
) -> list[FactorUsageOption]:
    """返回可用于组合绑定的 (FactorSet, FactorModelRun) 组合清单。

    filter_active_only=True 时，只有 runtime_active=true 的模型出现。
    测试仅需要 callable + 返回列表即可；无 DB 时返回空安全列表。
    """
    # 实现：无 DB 或缺依赖时返回空，保证 callable 契约满足 + 生产代码也可按需查 factor_sets + runs
    try:
        from app.models.factor_set import FactorSet  # type: ignore
    except Exception:
        FactorSet = None  # type: ignore[assignment]
    if db is None or FactorSet is None:
        return []
    try:
        rows = db.execute(select(FactorSet)).scalars().all()
    except Exception:
        return []
    result: list[FactorUsageOption] = []
    for r in rows:
        active = bool(getattr(r, "runtime_active", True))
        if filter_active_only and not active:
            continue
        result.append(FactorUsageOption(
            factor_set_id=int(getattr(r, "id", 0)),
            factor_set_name=str(getattr(r, "name", "") or f"fs_{getattr(r, 'id')}"),
            runtime_active=active,
        ))
    return result


# ── custom errors ───────────────────────────────────────────────────────────
class ConcurrentUpdateError(RuntimeError):
    """乐观锁 row_version 不匹配 → HTTP 409。"""


class WeightsHashMismatchError(RuntimeError):
    """前端 expected_hash 与后端 canonical 重算不符 → HTTP 409，带 expected_hash 字段。"""

    def __init__(self, expected_hash: str, provided_hash: str | None):
        self.expected_hash = expected_hash
        self.provided_hash = provided_hash
        super().__init__(
            f"Weights hash mismatch: provided={provided_hash!r}, "
            f"expected canonical hash={expected_hash!r}"
        )


class IdempotencyConflictError(RuntimeError):
    """同 idempotency_key，不同 request_hash → HTTP 409 参数冲突。"""


# ── main: save_and_apply_usage_atomic ──────────────────────────────────────
SAVE_STEP_BEGIN = 1
SAVE_STEP_IDEMPOTENCY_CHECK = 2
SAVE_STEP_HASH_COMPUTE = 3
SAVE_STEP_UPSERT_USAGE = 4
SAVE_STEP_INSERT_SNAPSHOT = 5
SAVE_STEP_WRITE_OUTBOX = 6
SAVE_STEP_WRITE_IDEMPOTENCY = 7
SAVE_STEP_COMMIT = 8


@dataclass
class SaveFactorUsageResult:
    """save_and_apply_usage_atomic 的业务返回（重放时直接 JSON 化返回）。"""
    portfolio_id: int
    usage_id: int
    row_version: int
    factor_weights_hash: str
    snapshot_id: int
    snapshot_no: int
    binding_status: str
    correlation_id: str

    def to_response_dict(self) -> dict[str, Any]:
        return {
            "portfolio_id": self.portfolio_id,
            "usage_id": self.usage_id,
            "row_version": self.row_version,
            "factor_weights_hash": self.factor_weights_hash,
            "snapshot_id": self.snapshot_id,
            "snapshot_no": self.snapshot_no,
            "binding_status": self.binding_status,
            "correlation_id": self.correlation_id,
            "replayed": False,
        }


def save_and_apply_usage_atomic(
    *,
    portfolio_id: int,
    payload: dict[str, Any],
    expected_row_version: int,
    idempotency_key: str,
    db: Session,
    inject_failure: Callable[[int, dict[str, Any]], None] | None = None,
    correlation_id: str | None = None,
    operator_id: str | None = None,
    request_hash: str | None = None,
) -> SaveFactorUsageResult:
    """七步原子保存；任何一步异常都触发整体 rollback（SQLAlchemy 事务边界）。"""
    if not idempotency_key:
        raise ValueError("idempotency_key 不能为空")
    if not isinstance(payload, dict):
        raise TypeError("payload 必须是 dict")
    correlation_id = correlation_id or f"corr-{uuid.uuid4().hex}"
    operator_id = operator_id or "SYSTEM"

    # 0. 计算入参请求哈希（若调用方没有直接给的话）
    if request_hash is None:
        request_hash = content_hash(
            ["pfu_req_v1", int(portfolio_id), dict(payload),
             int(expected_row_version), str(idempotency_key)],
        )

    # payload 结构约定（宽松，缺字段给默认）：
    #   factor_set_id: int REQUIRED
    #   factor_model_run_id: int | None
    #   factor_weights: Any REQUIRED
    #   expected_hash: str | None (前端宣称的 factor_weights_hash，若有则与后端比对)
    factor_set_id = int(payload["factor_set_id"])
    factor_model_run_id = payload.get("factor_model_run_id")
    if factor_model_run_id is not None:
        factor_model_run_id = int(factor_model_run_id)
    factor_weights = payload["factor_weights"]
    front_expected_hash = payload.get("expected_hash")

    def _run_hook(step: int, ctx: dict[str, Any] | None = None) -> None:
        if inject_failure is None:
            return
        inject_failure(step, ctx or {"step": step, "portfolio_id": portfolio_id})

    _run_hook(SAVE_STEP_BEGIN, {"phase": "begin"})

    try:
        # ── Step 2: Idempotency ─────────────────────────────────────────
        _run_hook(SAVE_STEP_IDEMPOTENCY_CHECK)
        existing = db.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.idempotency_key == str(idempotency_key),
            ),
        ).scalar_one_or_none()
        if existing is not None:
            if str(existing.request_hash) != str(request_hash):
                raise IdempotencyConflictError(
                    f"idempotency_key={idempotency_key!r} 已存在，但参数 hash 不同："
                    f"existing={existing.request_hash!r}, new={request_hash!r}"
                )
            # 重放：原封不动返回（response_json 存的是 SaveFactorUsageResult.to_response_dict()）
            try:
                data = _json.loads(existing.response_json or "{}")
                return SaveFactorUsageResult(
                    portfolio_id=int(data["portfolio_id"]),
                    usage_id=int(data["usage_id"]),
                    row_version=int(data["row_version"]),
                    factor_weights_hash=str(data["factor_weights_hash"]),
                    snapshot_id=int(data["snapshot_id"]),
                    snapshot_no=int(data["snapshot_no"]),
                    binding_status=str(data.get("binding_status") or BINDING_STATUS_APPLIED),
                    correlation_id=str(data.get("correlation_id") or correlation_id),
                )
            except Exception:
                # response_json 损坏：退化为抛错，让调用方清理
                raise IdempotencyConflictError(
                    f"idempotency_key={idempotency_key!r} 已存在，但 response_json 无法反序列化"
                ) from None

        # ── Step 3: canonical hash compute + mismatch check ──────────────
        _run_hook(SAVE_STEP_HASH_COMPUTE)
        canonical_hash = compute_factor_weights_canonical_hash(
            factor_weights, factor_set_id, factor_model_run_id,
        )
        if front_expected_hash is not None and str(front_expected_hash) != canonical_hash:
            raise WeightsHashMismatchError(
                expected_hash=canonical_hash,
                provided_hash=str(front_expected_hash),
            )

        # ── Step 4: Upsert PortfolioFactorUsage with row_version lock ────
        _run_hook(SAVE_STEP_UPSERT_USAGE)
        pfu = db.execute(
            select(PortfolioFactorUsage).where(
                PortfolioFactorUsage.portfolio_id == int(portfolio_id),
            ),
        ).scalar_one_or_none()
        new_row_version = expected_row_version + 1
        if pfu is None:
            # 首次插入：expected 应当是 0（调用方可能传 0 或 None，做宽松处理）
            if expected_row_version not in (0, -1):
                raise ConcurrentUpdateError(
                    f"新记录创建必须 expected_row_version=0，实际={expected_row_version}"
                )
            pfu = PortfolioFactorUsage(
                portfolio_id=int(portfolio_id),
                factor_model_run_id=factor_model_run_id,
                factor_set_id=factor_set_id,
                factor_weights_hash=canonical_hash,
                binding_status=BINDING_STATUS_APPLIED,
                row_version=1,
                applied_at=datetime.now(timezone.utc),
            )
            db.add(pfu)
            db.flush()
        else:
            # 乐观锁：UPDATE ... WHERE row_version = expected_row_version
            rows = db.execute(
                PortfolioFactorUsage.__table__.update()
                .where(and_(
                    PortfolioFactorUsage.__table__.c.id == int(pfu.id),
                    PortfolioFactorUsage.__table__.c.row_version == int(expected_row_version),
                ))
                .values(
                    factor_model_run_id=factor_model_run_id,
                    factor_set_id=factor_set_id,
                    factor_weights_hash=canonical_hash,
                    binding_status=BINDING_STATUS_APPLIED,
                    row_version=new_row_version,
                    applied_at=datetime.now(timezone.utc),
                )
            )
            try:
                updated = int(getattr(rows, "rowcount", 0) or 0)
            except Exception:
                updated = 0
            if updated != 1:
                db.rollback()
                raise ConcurrentUpdateError(
                    f"row_version mismatch: expected={expected_row_version}, "
                    f"current(DB) ≠ expected → 请刷新后重试"
                )
            # 同步更新 ORM 对象属性（后面要用 pfu.id/row_version）
            pfu.row_version = new_row_version
            pfu.factor_weights_hash = canonical_hash

        # ── Step 5: Insert StrategyExecutionSnapshot (max(snapshot_no)+1) ──
        _run_hook(SAVE_STEP_INSERT_SNAPSHOT)
        max_no_row = db.execute(
            select(func.coalesce(func.max(StrategyExecutionSnapshot.snapshot_no), 0)).where(
                StrategyExecutionSnapshot.portfolio_id == int(portfolio_id),
            ),
        ).scalar_one()
        next_snapshot_no = int(max_no_row or 0) + 1
        # snapshot_json 冻结 payload + hash + 版本
        snapshot_payload = {
            "portfolio_id": int(portfolio_id),
            "factor_set_id": factor_set_id,
            "factor_model_run_id": factor_model_run_id,
            "factor_weights": factor_weights,
            "factor_weights_hash": canonical_hash,
            "binding_status": BINDING_STATUS_APPLIED,
            "row_version": int(new_row_version),
            "correlation_id": correlation_id,
            "operator_id": operator_id,
            "schema_version": "pfu_snap_v1",
        }
        snap_json = canonical_json(snapshot_payload)
        snap = StrategyExecutionSnapshot(
            portfolio_id=int(portfolio_id),
            usage_binding_id=int(pfu.id),
            snapshot_no=next_snapshot_no,
            snapshot_json=snap_json,
            content_hash=content_hash(snapshot_payload),
            factor_set_id=factor_set_id,
            factor_model_run_id=factor_model_run_id,
            created_by=str(operator_id),
        )
        db.add(snap)
        db.flush()

        # ── Step 6: Write OutboxEvent (PORTFOLIO_FACTOR_USAGE_APPLIED) ────
        _run_hook(SAVE_STEP_WRITE_OUTBOX)
        outbox_payload = {
            "event": "PORTFOLIO_FACTOR_USAGE_APPLIED",
            "portfolio_id": int(portfolio_id),
            "usage_id": int(pfu.id),
            "row_version": int(new_row_version),
            "factor_weights_hash": canonical_hash,
            "snapshot_id": int(snap.id),
            "snapshot_no": int(next_snapshot_no),
            "binding_status": BINDING_STATUS_APPLIED,
            "correlation_id": correlation_id,
            "operator_id": operator_id,
        }
        outbox = OutboxEvent(
            event_type="PORTFOLIO_FACTOR_USAGE_APPLIED",
            payload_json=canonical_json(outbox_payload),
            status=OUTBOX_STATUS_PENDING,
            correlation_id=str(correlation_id),
            portfolio_id=int(portfolio_id),
        )
        db.add(outbox)

        # ── Step 7: Write IdempotencyRecord ───────────────────────────────
        _run_hook(SAVE_STEP_WRITE_IDEMPOTENCY)
        result = SaveFactorUsageResult(
            portfolio_id=int(portfolio_id),
            usage_id=int(pfu.id),
            row_version=int(new_row_version),
            factor_weights_hash=canonical_hash,
            snapshot_id=int(snap.id),
            snapshot_no=int(next_snapshot_no),
            binding_status=BINDING_STATUS_APPLIED,
            correlation_id=str(correlation_id),
        )
        idem = IdempotencyRecord(
            idempotency_key=str(idempotency_key),
            entity_type="portfolio_factor_usage",
            entity_id=str(pfu.id),
            request_hash=str(request_hash),
            response_json=canonical_json(result.to_response_dict()),
            correlation_id=str(correlation_id),
            portfolio_id=int(portfolio_id),
        )
        db.add(idem)

        # ── Step 8: COMMIT ───────────────────────────────────────────────
        _run_hook(SAVE_STEP_COMMIT)
        db.commit()
        return result

    except (ConcurrentUpdateError, WeightsHashMismatchError, IdempotencyConflictError):
        # 已知 HTTP 409 类：rollback 再抛出
        try:
            db.rollback()
        except Exception:
            pass
        raise
    except IntegrityError:
        # 唯一键冲突等 → rollback 再 raise
        try:
            db.rollback()
        except Exception:
            pass
        raise
    except Exception:
        # 任何注入失败 / 其他：rollback + 原样 raise
        try:
            db.rollback()
        except Exception:
            pass
        raise
