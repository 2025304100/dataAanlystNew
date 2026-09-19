"""Canonical JSON + SHA-256 content hash helpers.

Used by:
  - PortfolioFactorUsage.content_hash
  - StrategyExecutionSnapshot.snapshot_hash
  - DecisionEvidence.content_hash
  - Idempotency key builders

Determinism rules:
  1. JSON is serialized with sort_keys=True, separators=(',',':'), no whitespace.
  2. Dict entries with None values are dropped so caller does not need to care
     about empty optional fields.
  3. Datetimes are ISO-8601 (UTC offset stripped when the caller already
     guarantees UTC naive, matching the DB contract).
  4. Decimal uses "format canonical string" (drop trailing zeros and bare `.`)
     so `Decimal('1.1000')` hash-equals `Decimal('1.1')`.
  5. NaN / ±Inf floats are rejected with NonFiniteNumberError —— 禁止用特殊
     浮点值参与 hash，否则出现 "两份不同数据因 NaN 字符串相同而撞 hash"
     以及 JSON 标准里 NaN/Inf 本身不合法的双重问题。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable


class NonFiniteNumberError(ValueError):
    """检测到 NaN / ±Inf 参与 canonical 哈希（契约禁止）。"""


def _check_finite_number(value: Any, *, where: str = "value") -> None:
    """对于数字标量：只要是 NaN / ±Inf → NonFiniteNumberError 明确拒绝。"""
    # bool 是 int 子类但不会出现 NaN/Inf；只拦 float（含 numpy 风格经 int 转的）
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise NonFiniteNumberError(
                f"canonical_json 不允许 NaN/±Inf：{where}={value!r}"
            )


def _drop_none(obj: Any) -> Any:
    """Recursively drop None-valued keys from dicts + 遍历性 NaN 扫描."""
    _check_finite_number(obj, where="<root>")
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_drop_none(v) for v in obj]
    return obj


def _default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        # WP0-2 T4a 契约：Decimal('1.1000') == Decimal('1.1') → 同一字符串表示
        s = format(obj, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(f"Type {type(obj).__name__} not serializable for canonical JSON")


def canonical_json(payload: Any, *, drop_none: bool = True) -> str:
    """Return a deterministic JSON string (no whitespace, sorted keys)."""
    data = _drop_none(payload) if drop_none else payload
    # JSONEncoder 会在遇到 allow_nan=False 时报 ValueError，给出"在哪一层"更清晰
    # 的信息前面 _drop_none 已做过标量级 NaN 拒绝；这里作为兜底。
    try:
        return json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=_default,
            allow_nan=False,
        )
    except ValueError as exc:  # allow_nan=False 会抛出该类
        if "nan" in str(exc).lower() or "inf" in str(exc).lower():
            raise NonFiniteNumberError(
                f"canonical_json 检测到非法 NaN/±Inf：{exc}"
            ) from exc
        raise



def content_hash(*parts: Any) -> str:
    """SHA-256 of concatenated canonical JSON parts, hex digest."""
    hasher = hashlib.sha256()
    for p in parts:
        blob = canonical_json(p) if not isinstance(p, (bytes, bytearray)) else p
        if isinstance(blob, str):
            blob = blob.encode("utf-8")
        hasher.update(blob)
    return hasher.hexdigest()


def idempotency_key(*, portfolio_id: int, strategy_snapshot_id: str,
                    decision_at: datetime | str, trade_date: date | str,
                    run_type: str) -> str:
    """Portfolio-centric idempotency key per Q28.

    The backend key is deterministic and does NOT rely on the frontend
    UUID-only Idempotency-Key header (that header is for double-click
    protection only).
    """
    dec = decision_at.isoformat() if isinstance(decision_at, datetime) else decision_at
    td = trade_date.isoformat() if isinstance(trade_date, date) else trade_date
    return content_hash(
        ["idem_v1", int(portfolio_id), str(strategy_snapshot_id),
         str(dec), str(td), str(run_type)],
    )
