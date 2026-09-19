"""Task 28: FILTER_GOVERNANCE 告警 3 规则注册与钩子函数。

纯函数实现：
- register_filter_governance_alerts(): 返回 rule_code 列表（3 条）
- emit_filter_alerts_post_run(...): 按阈值评估并写入 outbox（若可用）；
  无 DB 时 fallback 到内存队列，保证可独立 pytest 通过。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# -- 28.1 常量 -------------------------------------------------------------
ALERT_CATEGORY_FILTER_GOVERNANCE = "FILTER_GOVERNANCE"

RULE_STATUS_UNKNOWN_RATIO_HIGH = "FILTER_STATUS_UNKNOWN_RATIO_HIGH"
RULE_DELISTING_PRICE_MISSING = "FILTER_DELISTING_PRICE_MISSING"
RULE_PIT_STATUS_BATCH_P95_SLOW = "FILTER_PIT_STATUS_BATCH_P95_SLOW"


# -- 规则元数据（注册用） ---------------------------------------------------
@dataclass(frozen=True)
class AlertRuleSpec:
    rule_code: str
    severity: str
    category: str
    threshold_expr: str
    evidence_template: tuple
    fix_link: str
    correlation_id_template: str
    retryable: bool


_RULE_SPECS = (
    AlertRuleSpec(
        rule_code=RULE_STATUS_UNKNOWN_RATIO_HIGH,
        severity="HIGH",
        category=ALERT_CATEGORY_FILTER_GOVERNANCE,
        threshold_expr="unknown_ratio > 0.05",
        evidence_template=("unknown_count", "total_count", "ratio", "trade_date", "run_id"),
        fix_link="DataQuality - SecurityStatus",
        correlation_id_template="FILTER-UNKNOWN-RATIO-{run_id}-{trade_date}",
        retryable=True,
    ),
    AlertRuleSpec(
        rule_code=RULE_DELISTING_PRICE_MISSING,
        severity="CRITICAL",
        category=ALERT_CATEGORY_FILTER_GOVERNANCE,
        threshold_expr="len(delisting_price_missing_symbols) > 0",
        evidence_template=("symbol_ids_with_missing", "trade_dates_involved", "run_ids"),
        fix_link="DataQuality - DailyBar",
        correlation_id_template="FILTER-DELIST-PRICE-{run_id}-{trade_date}",
        retryable=True,
    ),
    AlertRuleSpec(
        rule_code=RULE_PIT_STATUS_BATCH_P95_SLOW,
        severity="WARNING",
        category=ALERT_CATEGORY_FILTER_GOVERNANCE,
        threshold_expr="status_batch_p95_ms > 500",
        evidence_template=("p50_ms", "p95_ms", "max_ms", "symbols_n", "days_n"),
        fix_link="DatabasePerformance - SecurityStatusDaily Index",
        correlation_id_template="FILTER-PIT-SLOW-{run_id}-{trade_date}",
        retryable=True,
    ),
)


# -- 28.2 注册函数 ---------------------------------------------------------
def register_filter_governance_alerts():
    """注册 3 条 FILTER_GOVERNANCE 规则，返回 rule_code 元组。

    纯函数实现：当前不侵入 DB 写入，仅返回 rule_code 集合供验证与
    后续注册逻辑消费。如需真实落表（alert_rules / governance outbox
    规则表），可以在此处基于返回的 rule_specs 追加调用。
    """
    return tuple(spec.rule_code for spec in _RULE_SPECS)


def get_rule_specs():
    """返回完整规则元数据（调试 / UI 展示 / 真实落表用）。"""
    return tuple(_RULE_SPECS)


# -- Fallback 内存队列（无 DB 场景兜底） ------------------------------------
_MEMORY_OUTBOX = []


def get_memory_outbox():
    return list(_MEMORY_OUTBOX)


def clear_memory_outbox():
    _MEMORY_OUTBOX.clear()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_safe(obj):
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


def _write_event_via_governance_outbox(db, payload, rule_spec, correlation_id):
    """优先尝试 governance_outbox。成功返回 True。"""
    if db is None:
        return False
    try:
        from app.services.governance_outbox import emit_governance_event
        emit_governance_event(
            db,
            event_type="ALERT::" + rule_spec.rule_code,
            aggregate_type=ALERT_CATEGORY_FILTER_GOVERNANCE,
            aggregate_id=str(payload.get("run_id", "0")),
            payload={
                "rule_code": rule_spec.rule_code,
                "severity": rule_spec.severity,
                "category": rule_spec.category,
                "correlation_id": correlation_id,
                "retryable": rule_spec.retryable,
                "fix_link": rule_spec.fix_link,
                "evidence": payload.get("evidence", {}),
            },
            correlation_id=correlation_id,
            business_key=rule_spec.rule_code,
        )
        return True
    except Exception as exc:
        logger.debug("governance_outbox fallback skipped: %s", exc)
        return False


# -- 28.3 emit 钩函数 ------------------------------------------------------
def emit_filter_alerts_post_run(
    db,
    run_id,
    unknown_ratio,
    unknown_count,
    total_count,
    trade_date,
    delisting_price_missing_symbols,
    status_batch_p95_ms,
    status_batch_p50_ms,
    status_batch_max_ms,
    symbols_batch_size,
    days_in_run,
):
    """按阈值写入 outbox。返回触发的告警数量（int）。

    触发条件：
      1. unknown_ratio > 0.05                      -> HIGH
      2. len(delisting_price_missing_symbols) > 0  -> CRITICAL
      3. status_batch_p95_ms > 500                 -> WARNING

    写入顺序：先尝试 governance_outbox.emit_governance_event；
    失败或 db=None 时 fallback 到内存队列 _MEMORY_OUTBOX。
    """
    triggered = []

    # Rule 1: STATUS_UNKNOWN_RATIO_HIGH
    if unknown_ratio > 0.05:
        spec = _RULE_SPECS[0]
        evidence = {
            "unknown_count": int(unknown_count),
            "total_count": int(total_count),
            "ratio": float(unknown_ratio),
            "trade_date": _json_safe(trade_date),
            "run_id": int(run_id),
        }
        cid = spec.correlation_id_template.format(
            run_id=run_id, trade_date=_json_safe(trade_date)
        )
        triggered.append({
            "rule_spec": spec, "correlation_id": cid,
            "run_id": run_id, "evidence": evidence,
            "occurred_at": _utcnow().isoformat(),
        })

    # Rule 2: DELISTING_PRICE_MISSING
    if len(delisting_price_missing_symbols) > 0:
        spec = _RULE_SPECS[1]
        evidence = {
            "symbol_ids_with_missing": list(delisting_price_missing_symbols),
            "trade_dates_involved": [_json_safe(trade_date)],
            "run_ids": [int(run_id)],
            "missing_count": len(delisting_price_missing_symbols),
        }
        cid = spec.correlation_id_template.format(
            run_id=run_id, trade_date=_json_safe(trade_date)
        )
        triggered.append({
            "rule_spec": spec, "correlation_id": cid,
            "run_id": run_id, "evidence": evidence,
            "occurred_at": _utcnow().isoformat(),
        })

    # Rule 3: PIT_STATUS_BATCH_P95_SLOW
    if status_batch_p95_ms > 500:
        spec = _RULE_SPECS[2]
        evidence = {
            "p50_ms": float(status_batch_p50_ms),
            "p95_ms": float(status_batch_p95_ms),
            "max_ms": float(status_batch_max_ms),
            "symbols_n": int(symbols_batch_size),
            "days_n": int(days_in_run),
        }
        cid = spec.correlation_id_template.format(
            run_id=run_id, trade_date=_json_safe(trade_date)
        )
        triggered.append({
            "rule_spec": spec, "correlation_id": cid,
            "run_id": run_id, "evidence": evidence,
            "occurred_at": _utcnow().isoformat(),
        })

    # 持久化
    for t in triggered:
        spec = t["rule_spec"]
        payload = {"run_id": t["run_id"], "evidence": t["evidence"]}
        written = _write_event_via_governance_outbox(db, payload, spec, t["correlation_id"])
        if not written:
            record = {
                "rule_code": spec.rule_code,
                "severity": spec.severity,
                "category": spec.category,
                "correlation_id": t["correlation_id"],
                "fix_link": spec.fix_link,
                "retryable": spec.retryable,
                "evidence": t["evidence"],
                "occurred_at": t["occurred_at"],
                "storage": "memory_fallback",
            }
            _MEMORY_OUTBOX.append(record)

    return len(triggered)


# -- 内联自测（python -m 直接运行） ---------------------------------------
if __name__ == "__main__":
    # register: 3 条
    rules = register_filter_governance_alerts()
    assert set(rules) == {
        RULE_STATUS_UNKNOWN_RATIO_HIGH,
        RULE_DELISTING_PRICE_MISSING,
        RULE_PIT_STATUS_BATCH_P95_SLOW,
    }, "rule set mismatch: " + str(set(rules))
    print("[OK] register_filter_governance_alerts returned 3 rules")

    # emit: 3 触发
    clear_memory_outbox()
    c1 = emit_filter_alerts_post_run(
        db=None,
        run_id=1, unknown_ratio=0.07, unknown_count=70, total_count=1000,
        trade_date=date(2026, 8, 31),
        delisting_price_missing_symbols=[999, 1000],
        status_batch_p95_ms=612, status_batch_p50_ms=220, status_batch_max_ms=880,
        symbols_batch_size=5000, days_in_run=1000,
    )
    assert c1 == 3, "expected 3, got " + str(c1)
    assert len(get_memory_outbox()) == 3
    print("[OK] emit triggered 3 alerts")

    # emit: 0 触发
    clear_memory_outbox()
    c0 = emit_filter_alerts_post_run(
        db=None,
        run_id=2, unknown_ratio=0.01, unknown_count=10, total_count=1000,
        trade_date=date(2026, 8, 31),
        delisting_price_missing_symbols=[],
        status_batch_p95_ms=300, status_batch_p50_ms=120, status_batch_max_ms=400,
        symbols_batch_size=5000, days_in_run=1000,
    )
    assert c0 == 0, "expected 0, got " + str(c0)
    assert len(get_memory_outbox()) == 0
    print("[OK] emit triggered 0 alerts")
    print("[Task 28 inline self-tests PASSED]")