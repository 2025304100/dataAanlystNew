"""Task 28 (FILTER_GOVERNANCE alerts) + Task 32 (legacy_baseline replay) unit tests.

运行：
    python -m pytest tests/test_backtest_filter_t28_t32.py -q
或直接：
    python tests/test_backtest_filter_t28_t32.py   # 通过内联 __main__ 断言
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.backtest_filters.alerts_registry import (
    ALERT_CATEGORY_FILTER_GOVERNANCE,
    RULE_STATUS_UNKNOWN_RATIO_HIGH,
    RULE_DELISTING_PRICE_MISSING,
    RULE_PIT_STATUS_BATCH_P95_SLOW,
    register_filter_governance_alerts,
    emit_filter_alerts_post_run,
    get_memory_outbox,
    clear_memory_outbox,
)
from app.services.backtest_filters.config import (
    BacktestFilterConfig,
    compute_config_hash,
    validate_production_fidelity,
)
from app.services.backtest_filters.bridge import (
    pre_rebalance_pipeline,
    PipelineRebalanceInput,
)


# ========================================================================
# Task 28.5 — 2 条 pytest
# ========================================================================

def test_register_rules_count():
    rules = register_filter_governance_alerts()
    assert set(rules) == {
        RULE_STATUS_UNKNOWN_RATIO_HIGH,
        RULE_DELISTING_PRICE_MISSING,
        RULE_PIT_STATUS_BATCH_P95_SLOW,
    }
    assert len(rules) == 3


def test_emit_alerts_triggers():
    """SQLite 内存 DB：构造 session。

    按 Task 28.5 注释：纯函数可直接返回触发数（内存 fallback 兜底，
    不写入真实表），因此用 db=None：
      - 第 1 次：三条阈值都超过 → 3
      - 第 2 次：全正常 → 0
    """
    clear_memory_outbox()
    count1 = emit_filter_alerts_post_run(
        db=None,
        run_id=1,
        unknown_ratio=0.07, unknown_count=70, total_count=1000,
        trade_date=date(2026, 8, 31),
        delisting_price_missing_symbols=[999, 1000],
        status_batch_p95_ms=612, status_batch_p50_ms=220, status_batch_max_ms=880,
        symbols_batch_size=5000, days_in_run=1000,
    )
    assert count1 == 3, f"expected 3, got {count1}; outbox={get_memory_outbox()}"
    assert len(get_memory_outbox()) == 3
    # memory fallback: 检查 severity / rule_code / evidence 完整性
    severity_map = {r["rule_code"]: r["severity"] for r in get_memory_outbox()}
    assert severity_map[RULE_STATUS_UNKNOWN_RATIO_HIGH] == "HIGH"
    assert severity_map[RULE_DELISTING_PRICE_MISSING] == "CRITICAL"
    assert severity_map[RULE_PIT_STATUS_BATCH_P95_SLOW] == "WARNING"
    # evidence 关键字段
    for rec in get_memory_outbox():
        assert "FILTER" in rec["correlation_id"]
        assert rec["retryable"] is True
        assert rec["category"] == ALERT_CATEGORY_FILTER_GOVERNANCE
        assert isinstance(rec["evidence"], dict) and len(rec["evidence"]) > 0

    # 第二次：阈值均未触发 → 0
    clear_memory_outbox()
    count0 = emit_filter_alerts_post_run(
        db=None,
        run_id=2,
        unknown_ratio=0.01, unknown_count=10, total_count=1000,
        trade_date=date(2026, 8, 31),
        delisting_price_missing_symbols=[],
        status_batch_p95_ms=300, status_batch_p50_ms=120, status_batch_max_ms=400,
        symbols_batch_size=5000, days_in_run=1000,
    )
    assert count0 == 0
    assert len(get_memory_outbox()) == 0


# ========================================================================
# Task 32.3 — 2 条 pytest
# ========================================================================

def test_legacy_short_circuit_no_filters():
    """legacy 模式：传入 3 个候选 → eligible 全部返回。

    pit_service / liquidation_service 传 None，应完全不被调用。
    """
    cfg = BacktestFilterConfig(engine_compat_version="legacy_baseline")
    out = pre_rebalance_pipeline(PipelineRebalanceInput(
        trade_date=date(2024, 1, 2),
        raw_candidate_symbol_ids=[1, 2, 3],
        filter_config=cfg, enable_filters=True,
        pit_service=None, liquidation_service=None,
        portfolio_id=1,
    ))
    # eligible_candidates（Task 32 spec 新签名返回 set）
    assert set(out.eligible_candidates) == {1, 2, 3}
    # audit_events（新签名）：每个 symbol 一条 LEGACY_BASELINE_COMPAT_MODE
    assert len(out.audit_events) == 3
    assert all(e.rule_code == "LEGACY_BASELINE_COMPAT_MODE" for e in out.audit_events)
    # 其它空值
    assert out.frozen_skip_set == set()
    assert out.liquidation_orders == []
    assert out.blocking_errors == []
    assert out.effective_config_hash == "legacy_baseline"
    # 不应触发 PIT / 清算：事件 action 都是 include
    assert all(e.action == "include" for e in out.audit_events)
    # 兼容老字段
    assert out.config_hash == "legacy_baseline"
    assert out.should_persist_events is False


def test_legacy_fidelity_reason():
    ok, reason = BacktestFilterConfig(
        engine_compat_version="legacy_baseline"
    ).validate_production_fidelity() if hasattr(
        BacktestFilterConfig, "validate_production_fidelity"
    ) else validate_production_fidelity(
        BacktestFilterConfig(engine_compat_version="legacy_baseline")
    )
    # 上面写法双兼容，下面走真实 API
    cfg = BacktestFilterConfig(engine_compat_version="legacy_baseline")
    ok, reason = validate_production_fidelity(cfg)
    assert ok is False
    assert "legacy_baseline" in reason


# ========================================================================
# Task 32 哈希区分性：filter_v1 vs legacy_baseline 哈希应不同
# ========================================================================

def test_engine_compat_changes_hash():
    h_v1 = compute_config_hash(BacktestFilterConfig(engine_compat_version="filter_v1"))
    h_leg = compute_config_hash(BacktestFilterConfig(engine_compat_version="legacy_baseline"))
    assert h_v1 != h_leg, "engine_compat_version 切换应导致哈希变化"


# ========================================================================
# 内联运行（python tests/test_backtest_filter_t28_t32.py 直接跑）
# ========================================================================
if __name__ == "__main__":
    test_register_rules_count()
    print("[OK] test_register_rules_count")

    test_emit_alerts_triggers()
    print("[OK] test_emit_alerts_triggers")

    test_legacy_short_circuit_no_filters()
    print("[OK] test_legacy_short_circuit_no_filters")

    test_legacy_fidelity_reason()
    print("[OK] test_legacy_fidelity_reason")

    test_engine_compat_changes_hash()
    print("[OK] test_engine_compat_changes_hash")

    print("\n[ALL Task 28 + Task 32 inline tests PASSED]")