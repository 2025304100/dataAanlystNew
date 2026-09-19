"""IdempotencyService：WP0-2 TR-02.6 幂等键双保险协议。

两步调用（典型在 save_and_apply_usage_atomic / PUT APIs 中）：
  1. try_acquire_or_get(db, idem_key, request_hash, portfolio_id, correlation_id, entity_type_hint)
     → 返回 {"status": "ACQUIRED" | "REPLAY" | "CONFLICT", replay_response? , conflict_info?}
       - ACQUIRED：获取成功（此 key 之前未出现），调用方继续执行业务流程；
       - REPLAY：同 key + 同 request_hash → 返回原 response（replay_response JSON），调用方不做写操作；
       - CONFLICT：同 key + 不同 request_hash → HTTP 409，返回 conflict_info={"first_seen_hash": str}。
  2. record_response(db, idem_key, request_hash, response_json, entity_type, entity_id,
                     correlation_id, portfolio_id, expire_at)
     → 业务成功后把首次响应与哈希持久化（REPLAY/CONFLICT 均不写）。
"""
from __future__ import annotations

import json as _json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.hash_utils import content_hash
from app.models.idempotency_records import IdempotencyRecord


AcquireStatus = Literal["ACQUIRED", "REPLAY", "CONFLICT"]


@dataclass
class AcquireResult:
    status: AcquireStatus
    replay_response: dict[str, Any] | None = None
    conflict_info: dict[str, Any] | None = None


class IdempotencyService:
    """Stateless 类方法封装（不保存实例状态）。"""

    # ── public: Step 1: acquire / replay / conflict ────────────────────────
    @classmethod
    def try_acquire_or_get(
        cls,
        db: Session,
        idempotency_key: str,
        request_hash: str,
        *,
        portfolio_id: int | None = None,
        correlation_id: str | None = None,
        entity_type_hint: str = "general",
        expire_at: datetime | None = None,
        auto_ttl_hours: int | None = None,
    ) -> AcquireResult:
        """第一步：获取幂等锁，若存在则判断 replay 或 conflict。

        先查 DB：不存在 → ACQUIRED（实际记录留给 record_response 写）；
        存在 → 比对 request_hash：相同=REPLAY，不同=CONFLICT。
        """
        if not idempotency_key:
            raise ValueError("idempotency_key 不能为空")
        if not request_hash:
            raise ValueError("request_hash 不能为空（调用方应先对 payload 做 T4a content_hash）")

        existing = db.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.idempotency_key == str(idempotency_key),
            ),
        ).scalar_one_or_none()

        if existing is None:
            # 不存在：返回 ACQUIRED（实际写入必须通过 record_response，保证 ACQUIRE + 执行 + 响应三原子）
            return AcquireResult(status="ACQUIRED")

        existing_hash = (existing.request_hash or "").strip()
        if existing_hash == str(request_hash):
            # 重放：返回首次响应
            try:
                payload: dict[str, Any] = _json.loads(existing.response_json or "{}")
            except Exception:
                payload = {
                    "idempotency_key": str(idempotency_key),
                    "correlation_id": str(existing.correlation_id or correlation_id or ""),
                    "entity_id": str(existing.entity_id or ""),
                }
            return AcquireResult(status="REPLAY", replay_response=payload)

        # 参数冲突：返回 first_seen_hash
        return AcquireResult(
            status="CONFLICT",
            conflict_info={
                "error": "STATE_IDEMPOTENCY_PARAMS_CONFLICT",
                "idempotency_key": str(idempotency_key),
                "first_seen_hash": existing_hash,
                "provided_hash": str(request_hash),
                "first_entity_id": str(existing.entity_id or ""),
                "first_correlation_id": str(existing.correlation_id or ""),
                "http_status": 409,
            },
        )

    # ── public: Step 2: write response record ───────────────────────────────
    @classmethod
    def record_response(
        cls,
        db: Session,
        idempotency_key: str,
        request_hash: str,
        response_json: dict[str, Any] | str,
        *,
        entity_type: str,
        entity_id: str | None = None,
        correlation_id: str | None = None,
        portfolio_id: int | None = None,
        expire_at: datetime | None = None,
        auto_ttl_hours: int | None = 24,
    ) -> IdempotencyRecord:
        """业务成功后，把首次响应写幂等表（仅 ACQUIRED 之后调用）。

        若 expire_at 为 NULL + auto_ttl_hours>0 → 默认 created_at + auto_ttl_hours；
        若显式 expire_at=None + auto_ttl_hours=None → 长期不过期。
        """
        if not idempotency_key or not request_hash:
            raise ValueError("idempotency_key 与 request_hash 必填")
        if isinstance(response_json, (dict, list, tuple, int, float, str, bool)) or response_json is None:
            try:
                serialised = _json.dumps(response_json, ensure_ascii=False, sort_keys=True)
            except Exception:
                # 不能 JSON 化 → 最后兜底 str
                serialised = _json.dumps({"response_raw": str(response_json)}, ensure_ascii=False)
        else:
            serialised = str(response_json)

        cid = correlation_id or f"corr-{uuid.uuid4().hex}"
        created = datetime.now(timezone.utc).replace(tzinfo=None)
        exp = expire_at
        if exp is None and auto_ttl_hours is not None:
            exp = created + timedelta(hours=int(auto_ttl_hours))

        record = IdempotencyRecord(
            idempotency_key=str(idempotency_key),
            request_hash=str(request_hash),
            response_json=serialised,
            correlation_id=str(cid),
            entity_type=str(entity_type or "general"),
            entity_id=str(entity_id) if entity_id is not None else None,
            portfolio_id=int(portfolio_id) if portfolio_id is not None else None,
            expire_at=exp,
            created_at=created,
            executed_at=created,
        )
        db.add(record)
        try:
            db.flush()
        except Exception:
            # 唯一键冲突 = 重入 race，调用方按 REPLAY 处理；这里抛错但不吞
            raise
        return record

    # ── helper: canonical request hash utility ───────────────────────────────
    @classmethod
    def hash_payload(cls, payload: Any, *, idempotency_key: str | None = None) -> str:
        """便利方法：请求体 → canonical_hash。"""
        parts: list[Any] = ["idem_req_v1"]
        if idempotency_key is not None:
            parts.append(str(idempotency_key))
        parts.append(payload)
        return content_hash(*parts)
