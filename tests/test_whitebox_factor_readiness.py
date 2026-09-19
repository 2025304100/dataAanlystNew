"""WPD-04 因子数据 readiness 评估白盒测试。

覆盖 13 个场景，使用 mock warehouse 和 mock factor_coverage，
不依赖真实 DuckDB 和 MySQL。

场景对齐 docs/专业因子库开发计划.md §WPD-04 + Day 2 的 8 因子预期：
- turnover_z20 = available（94.7% 覆盖，A 层日线技术因子）
- ep_ttm / negative_pb = blocked（0% 覆盖，估值表仅 6 条）
- roe_yoy_growth = blocked（0.1451% 覆盖，财报表 336 条）
- main_inflow_5d_ratio = blocked（0% 覆盖，资金流表为空）
- lhb_institution_net_ratio = degraded（event_only，9 个标的）
- hot_rank_attention = degraded（snapshot_only，34 个标的）
- tail_accumulation_proxy = blocked（0% 覆盖，尾盘代理表为空）
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from app.services.factors.readiness import (
    FactorReadiness,
    FactorReadinessReport,
    evaluate_factor_readiness,
    get_factor_readiness_report,
)
from app.services.factors.store import WarehouseHealth
from app.services.factors.trade_calendar import (
    EPOCH_DATE,
    CompleteTradeDayEvidence,
)

pytestmark = pytest.mark.whitebox

UNIVERSE = 2067
COMPLETE_DATE = date(2026, 7, 24)


# ── 测试数据辅助 ─────────────────────────────────────────

def _make_ctd_evidence(
    *,
    fallback_reason=None,
    expected=UNIVERSE,
    observed=UNIVERSE,
    selected=COMPLETE_DATE,
):
    return CompleteTradeDayEvidence(
        selected_trade_date=selected,
        observed_symbols=observed,
        expected_symbols=expected,
        completeness_ratio=1.0 if fallback_reason is None else 0.0,
        fallback_reason=fallback_reason,
        median_baseline=expected,
        evaluated_candidate_dates=[selected],
        candidate_ratios={selected.isoformat(): 1.0},
    )


def _make_warehouse(available=True):
    wh = MagicMock()
    wh.health.return_value = WarehouseHealth(
        available=available,
        path="/tmp/test.duckdb",
        schema_version="3" if available else None,
        raw_daily_bars=41000 if available else 0,
        latest_trade_date="2026-07-24" if available else None,
    )
    return wh


def _make_factor_coverage(
    code,
    *,
    coverage,
    eligible,
    universe=UNIVERSE,
    latest="2026-07-24",
):
    return {
        "factor_code": code,
        "latest_trade_date": latest,
        "universe_symbols": universe,
        "eligible_symbols": eligible,
        "imputed_symbols": 0,
        "coverage": coverage,
    }


def _make_source_table_stats(**overrides):
    base = {
        "raw_daily_bars": {
            "rows": 41000,
            "latest_date": "2026-07-24",
            "date_column": "trade_date",
        },
        "raw_valuation_snapshots": {
            "rows": 6,
            "latest_date": "2026-07-24",
            "date_column": "trade_date",
        },
        "raw_financial_reports": {
            "rows": 336,
            "latest_date": "2026-03-30",
            "date_column": "announcement_date",
        },
        "raw_fund_flows": {
            "rows": 0,
            "latest_date": None,
            "date_column": "trade_date",
        },
        "raw_sentiment": {
            "rows": 50,
            "latest_date": "2026-07-24",
            "date_column": "trade_date",
        },
        "raw_tail_proxy": {
            "rows": 0,
            "latest_date": None,
            "date_column": "trade_date",
        },
    }
    base.update(overrides)
    return base


def _eval(
    code,
    *,
    warehouse=None,
    ctd=None,
    source_stats=None,
    coverage=None,
):
    """便捷封装：调用 evaluate_factor_readiness。"""
    if warehouse is None:
        warehouse = _make_warehouse(available=True)
    if ctd is None:
        ctd = _make_ctd_evidence()
    if source_stats is None:
        source_stats = _make_source_table_stats()
    return evaluate_factor_readiness(
        code,
        db_session=MagicMock(),
        warehouse=warehouse,
        complete_trade_day_evidence=ctd,
        source_table_stats=source_stats,
        factor_coverage=coverage,
    )


# ── 场景 1: turnover_z20 available ───────────────────────

def test_turnover_z20_available():
    """coverage=0.947, universe=2067, eligible=1958 → available, A_continuous, evaluate。"""
    result = _eval(
        "turnover_z20",
        coverage=_make_factor_coverage(
            "turnover_z20", coverage=0.947, eligible=1958
        ),
    )
    assert result.status == "available"
    assert result.layer == "A_continuous"
    assert result.recommended_action == "evaluate"
    assert result.coverage == pytest.approx(0.947, rel=1e-4)
    assert result.universe_symbols == UNIVERSE
    assert result.eligible_symbols == 1958
    assert result.blocking_reasons == []
    assert result.complete_trade_day_evidence is not None


# ── 场景 2: ep_ttm blocked ───────────────────────────────

def test_ep_ttm_blocked():
    """coverage=0.0, 估值表 6 条 → blocked, 含 source_table_insufficient 和 coverage_zero。"""
    result = _eval(
        "ep_ttm",
        coverage=_make_factor_coverage("ep_ttm", coverage=0.0, eligible=0),
    )
    assert result.status == "blocked"
    assert result.layer == "C_blocked"
    assert "source_table_insufficient" in result.blocking_reasons
    assert "coverage_zero" in result.blocking_reasons
    assert result.recommended_action == "await_data"
    assert result.evidence["source_table_rows"]["raw_valuation_snapshots"] == 6


# ── 场景 3: negative_pb blocked ──────────────────────────

def test_negative_pb_blocked():
    """同 ep_ttm，估值表仅 6 条。"""
    result = _eval(
        "negative_pb",
        coverage=_make_factor_coverage("negative_pb", coverage=0.0, eligible=0),
    )
    assert result.status == "blocked"
    assert "source_table_insufficient" in result.blocking_reasons
    assert "coverage_zero" in result.blocking_reasons
    assert result.evidence["source_table_rows"]["raw_valuation_snapshots"] == 6


# ── 场景 4: roe_yoy_growth blocked ───────────────────────

def test_roe_yoy_growth_blocked():
    """coverage=0.001451, 财报表 336 条 → blocked, 含 source_table_insufficient。"""
    result = _eval(
        "roe_yoy_growth",
        coverage=_make_factor_coverage(
            "roe_yoy_growth", coverage=0.001451, eligible=3
        ),
    )
    assert result.status == "blocked"
    assert "source_table_insufficient" in result.blocking_reasons
    assert result.evidence["source_table_rows"]["raw_financial_reports"] == 336
    assert result.recommended_action == "await_data"


# ── 场景 5: main_inflow_5d_ratio blocked ─────────────────

def test_main_inflow_5d_ratio_blocked():
    """资金流表 0 条 → blocked, 含 source_table_empty。"""
    result = _eval(
        "main_inflow_5d_ratio",
        coverage=_make_factor_coverage(
            "main_inflow_5d_ratio", coverage=0.0, eligible=0
        ),
    )
    assert result.status == "blocked"
    assert "source_table_empty" in result.blocking_reasons
    assert result.evidence["source_table_rows"]["raw_fund_flows"] == 0


# ── 场景 6: lhb_institution_net_ratio degraded ───────────

def test_lhb_institution_net_ratio_degraded():
    """coverage=0.004354, eligible=9 → degraded, B_event, event_only。"""
    result = _eval(
        "lhb_institution_net_ratio",
        coverage=_make_factor_coverage(
            "lhb_institution_net_ratio", coverage=0.004354, eligible=9
        ),
    )
    assert result.status == "degraded"
    assert result.layer == "B_event"
    assert result.recommended_action == "event_only"
    assert result.eligible_symbols == 9
    assert result.blocking_reasons == []


# ── 场景 7: hot_rank_attention degraded ──────────────────

def test_hot_rank_attention_degraded():
    """coverage=0.016449, eligible=34 → degraded, B_event, snapshot_only。"""
    result = _eval(
        "hot_rank_attention",
        coverage=_make_factor_coverage(
            "hot_rank_attention", coverage=0.016449, eligible=34
        ),
    )
    assert result.status == "degraded"
    assert result.layer == "B_event"
    assert result.recommended_action == "snapshot_only"
    assert result.eligible_symbols == 34
    assert result.blocking_reasons == []


# ── 场景 8: tail_accumulation_proxy blocked ──────────────

def test_tail_accumulation_proxy_blocked():
    """尾盘代理表 0 条 → blocked, 含 source_table_empty。"""
    result = _eval(
        "tail_accumulation_proxy",
        coverage=_make_factor_coverage(
            "tail_accumulation_proxy", coverage=0.0, eligible=0
        ),
    )
    assert result.status == "blocked"
    assert "source_table_empty" in result.blocking_reasons
    assert result.evidence["source_table_rows"]["raw_tail_proxy"] == 0


# ── 场景 9: warehouse 不可用 ─────────────────────────────

def test_warehouse_unavailable_blocks_all():
    """warehouse.health().available=False → 所有因子 blocked, 含 warehouse_unavailable。"""
    wh = _make_warehouse(available=False)
    ctd = _make_ctd_evidence()
    stats = _make_source_table_stats()
    for code in (
        "turnover_z20",
        "ep_ttm",
        "negative_pb",
        "roe_yoy_growth",
        "main_inflow_5d_ratio",
        "lhb_institution_net_ratio",
        "hot_rank_attention",
        "tail_accumulation_proxy",
    ):
        result = evaluate_factor_readiness(
            code,
            db_session=MagicMock(),
            warehouse=wh,
            complete_trade_day_evidence=ctd,
            source_table_stats=stats,
            factor_coverage=_make_factor_coverage(code, coverage=0.5, eligible=10),
        )
        assert result.status == "blocked"
        assert "warehouse_unavailable" in result.blocking_reasons
        assert result.recommended_action == "blocked"


# ── 场景 10: 无完整交易日 ────────────────────────────────

def test_no_complete_trade_day_blocks_all():
    """fallback_reason='no_universe_data' → 所有因子 blocked。"""
    ctd = _make_ctd_evidence(fallback_reason="no_universe_data")
    wh = _make_warehouse(available=True)
    stats = _make_source_table_stats()
    for code in (
        "turnover_z20",
        "ep_ttm",
        "lhb_institution_net_ratio",
        "tail_accumulation_proxy",
    ):
        result = evaluate_factor_readiness(
            code,
            db_session=MagicMock(),
            warehouse=wh,
            complete_trade_day_evidence=ctd,
            source_table_stats=stats,
            factor_coverage=_make_factor_coverage(code, coverage=0.9, eligible=100),
        )
        assert result.status == "blocked"
        assert "no_complete_trade_day" in result.blocking_reasons


# ── 场景 11: summary 汇总正确 ────────────────────────────

def _build_full_report():
    """构建 8 因子的完整 readiness 报告（用 mock 数据）。"""
    wh = _make_warehouse(available=True)
    ctd = _make_ctd_evidence()
    stats = _make_source_table_stats()
    coverage_map = {
        "turnover_z20": _make_factor_coverage(
            "turnover_z20", coverage=0.947, eligible=1958
        ),
        "ep_ttm": _make_factor_coverage("ep_ttm", coverage=0.0, eligible=0),
        "negative_pb": _make_factor_coverage(
            "negative_pb", coverage=0.0, eligible=0
        ),
        "roe_yoy_growth": _make_factor_coverage(
            "roe_yoy_growth", coverage=0.001451, eligible=3
        ),
        "main_inflow_5d_ratio": _make_factor_coverage(
            "main_inflow_5d_ratio", coverage=0.0, eligible=0
        ),
        "lhb_institution_net_ratio": _make_factor_coverage(
            "lhb_institution_net_ratio", coverage=0.004354, eligible=9
        ),
        "hot_rank_attention": _make_factor_coverage(
            "hot_rank_attention", coverage=0.016449, eligible=34
        ),
        "tail_accumulation_proxy": _make_factor_coverage(
            "tail_accumulation_proxy", coverage=0.0, eligible=0
        ),
    }
    from app.services.factors.definitions import FACTOR_DEFINITIONS

    factors = []
    for definition in FACTOR_DEFINITIONS:
        factors.append(
            evaluate_factor_readiness(
                definition.code,
                db_session=MagicMock(),
                warehouse=wh,
                complete_trade_day_evidence=ctd,
                source_table_stats=stats,
                factor_coverage=coverage_map[definition.code],
            )
        )
    summary = {"available": 0, "degraded": 0, "blocked": 0}
    for f in factors:
        summary[f.status] += 1
    return FactorReadinessReport(
        generated_at="2026-08-01T00:00:00+00:00",
        warehouse_available=True,
        warehouse_path="/tmp/test.duckdb",
        complete_trade_day=COMPLETE_DATE,
        complete_trade_day_evidence={"selected_trade_date": "2026-07-24"},
        factors=factors,
        summary=summary,
        source_table_stats=stats,
    )


def test_summary_counts():
    """8 因子 → {'available': 1, 'degraded': 2, 'blocked': 5}。"""
    report = _build_full_report()
    assert report.summary == {"available": 1, "degraded": 2, "blocked": 5}
    assert len(report.factors) == 8


# ── 场景 12: to_dict 序列化无错误 ─────────────────────────

def test_to_dict_json_serializable():
    """所有字段可 JSON 序列化。"""
    report = _build_full_report()
    payload = report.to_dict()
    # 如果包含不可序列化对象，json.dumps 会抛 TypeError
    json.dumps(payload)
    for factor in report.factors:
        json.dumps(factor.to_dict())


# ── 场景 13: factor_code 不存在 ──────────────────────────

def test_get_factor_not_found():
    """get_factor('xxx') 返回 None。"""
    report = _build_full_report()
    assert report.get_factor("xxx") is None
    assert report.get_factor("turnover_z20") is not None


# ── 额外验证：阻断证据数字 ───────────────────────────────

def test_blocking_evidence_numbers():
    """阻断证据含 估值 6、财报 336、资金流 0、尾盘 0 的具体数字。"""
    report = _build_full_report()
    stats = report.source_table_stats
    assert stats["raw_valuation_snapshots"]["rows"] == 6
    assert stats["raw_financial_reports"]["rows"] == 336
    assert stats["raw_fund_flows"]["rows"] == 0
    assert stats["raw_tail_proxy"]["rows"] == 0

    ep = report.get_factor("ep_ttm")
    assert ep.evidence["source_table_rows"]["raw_valuation_snapshots"] == 6
    roe = report.get_factor("roe_yoy_growth")
    assert roe.evidence["source_table_rows"]["raw_financial_reports"] == 336
    inflow = report.get_factor("main_inflow_5d_ratio")
    assert inflow.evidence["source_table_rows"]["raw_fund_flows"] == 0
    tail = report.get_factor("tail_accumulation_proxy")
    assert tail.evidence["source_table_rows"]["raw_tail_proxy"] == 0


# ── 额外验证：available 因子含完整交易日证据 ─────────────

def test_available_factor_has_complete_trade_day_evidence():
    """available 因子的 complete_trade_day_evidence 不为 None。"""
    result = _eval(
        "turnover_z20",
        coverage=_make_factor_coverage(
            "turnover_z20", coverage=0.947, eligible=1958
        ),
    )
    assert result.status == "available"
    assert result.complete_trade_day_evidence is not None
    assert result.complete_trade_day_evidence["selected_trade_date"] == "2026-07-24"


# ── 额外验证：blocked 因子不含完整交易日证据 ─────────────

def test_blocked_factor_has_no_complete_trade_day_evidence():
    """blocked 因子的 complete_trade_day_evidence 为 None。"""
    result = _eval(
        "ep_ttm",
        coverage=_make_factor_coverage("ep_ttm", coverage=0.0, eligible=0),
    )
    assert result.status == "blocked"
    assert result.complete_trade_day_evidence is None
