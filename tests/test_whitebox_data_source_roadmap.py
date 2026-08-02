"""WPD-07: White-box tests for the data source roadmap module.

覆盖 7 类数据源的补齐策略、限流预算、可达覆盖说明和阻断逻辑，
确保：
- 估值无历史 point-in-time 时不做伪历史 IC
- 资金流表为空时不运行 main_inflow_5d_ratio
- 龙虎榜只在事件样本内评估
- 热度不承诺历史连续性
- 尾盘代理本期维持 blocked
- 宏观作为 regime 条件，不按个股覆盖评估

对齐 docs/专业因子库开发计划.md §2A.5 数据补齐边界。
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from app.services.factors.data_source_roadmap import (
    CompletionStrategy,
    DATA_SOURCE_POLICIES,
    DataSourceLayer,
    DataSourceRoadmapReport,
    FACTOR_TO_SOURCE_MAP,
    VALUATION_COMPLETION_START_DATE,
    check_capital_flow_continuity,
    check_valuation_point_in_time,
    get_data_source_roadmap,
    get_lhb_event_sample,
    is_factor_blocked_by_roadmap,
)
from app.services.factors.definitions import FACTOR_DEFINITIONS
from app.services.factors.readiness import evaluate_factor_readiness
from app.services.factors.store import WarehouseHealth
from app.services.factors.trade_calendar import CompleteTradeDayEvidence

pytestmark = pytest.mark.whitebox

UNIVERSE = 2067
COMPLETE_DATE = date(2026, 7, 24)


# ── Mock helpers ──────────────────────────────────────────


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


def _make_warehouse_with_conn(fetchone_result, fetchall_result=None):
    """Warehouse with a mock connection supporting context manager.

    All ``conn.execute(...).fetchone()`` calls return *fetchone_result* and
    all ``conn.execute(...).fetchall()`` calls return *fetchall_result*.
    """
    wh = _make_warehouse(available=True)
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = fetchone_result
    conn.execute.return_value.fetchall.return_value = fetchall_result or []
    wh.connection.return_value.__enter__.return_value = conn
    wh.connection.return_value.__exit__.return_value = False
    return wh


def _make_warehouse_unavailable():
    """Warehouse that reports unavailable; connection raises on use."""
    wh = _make_warehouse(available=False)
    wh.connection.side_effect = RuntimeError("warehouse unavailable")
    return wh


def _has_reason(blocking_reasons, substring):
    """Return True if any entry in *blocking_reasons* contains *substring*."""
    return any(substring in r for r in blocking_reasons)


def _make_ctd_evidence(*, fallback_reason=None):
    return CompleteTradeDayEvidence(
        selected_trade_date=COMPLETE_DATE,
        observed_symbols=UNIVERSE,
        expected_symbols=UNIVERSE,
        completeness_ratio=1.0 if fallback_reason is None else 0.0,
        fallback_reason=fallback_reason,
        median_baseline=UNIVERSE,
        evaluated_candidate_dates=[COMPLETE_DATE],
        candidate_ratios={COMPLETE_DATE.isoformat(): 1.0},
    )


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


def _make_factor_coverage(
    code, *, coverage, eligible, universe=UNIVERSE, latest="2026-07-24"
):
    return {
        "factor_code": code,
        "latest_trade_date": latest,
        "universe_symbols": universe,
        "eligible_symbols": eligible,
        "imputed_symbols": 0,
        "coverage": coverage,
    }


# ── 1. Policy definitions (7 tests) ───────────────────────


def test_valuation_policy_incremental_from_date():
    """估值策略：INCREMENTAL_FROM_DATE、C_BLOCKED、起始日 2026-08-01、禁伪历史。"""
    policy = DATA_SOURCE_POLICIES["valuation"]
    assert policy.completion_strategy == CompletionStrategy.INCREMENTAL_FROM_DATE
    assert policy.layer == DataSourceLayer.C_BLOCKED
    assert policy.completion_start_date == VALUATION_COMPLETION_START_DATE
    assert policy.completion_start_date == date(2026, 8, 1)
    assert policy.reachable_coverage.fake_history_forbidden is True


def test_financial_policy_high_liquidity_first():
    """财报策略：HIGH_LIQUIDITY_FIRST、要求 point-in-time。"""
    policy = DATA_SOURCE_POLICIES["financial"]
    assert policy.completion_strategy == CompletionStrategy.HIGH_LIQUIDITY_FIRST
    assert policy.reachable_coverage.requires_point_in_time is True


def test_capital_flow_policy_validate_and_continuity():
    """资金流策略：VALIDATE_AND_CONTINUITY、禁止表为空时运行因子。"""
    policy = DATA_SOURCE_POLICIES["capital_flow"]
    assert policy.completion_strategy == CompletionStrategy.VALIDATE_AND_CONTINUITY
    assert "表为空时运行 main_inflow_5d_ratio" in policy.forbidden_uses


def test_lhb_policy_event_sample_only():
    """龙虎榜策略：EVENT_SAMPLE_ONLY、B_EVENT 层、禁止非事件日填0。"""
    policy = DATA_SOURCE_POLICIES["lhb"]
    assert policy.completion_strategy == CompletionStrategy.EVENT_SAMPLE_ONLY
    assert policy.layer == DataSourceLayer.B_EVENT
    assert "非事件日填0" in policy.forbidden_uses


def test_hot_rank_policy_snapshot_only():
    """热度策略：SNAPSHOT_ONLY、禁止历史连续性承诺。"""
    policy = DATA_SOURCE_POLICIES["hot_rank"]
    assert policy.completion_strategy == CompletionStrategy.SNAPSHOT_ONLY
    assert "历史连续性承诺" in policy.forbidden_uses


def test_tail_proxy_policy_maintain_blocked():
    """尾盘代理策略：MAINTAIN_BLOCKED、限流 calls_per_minute=0、禁止引入分钟库。"""
    policy = DATA_SOURCE_POLICIES["tail_proxy"]
    assert policy.completion_strategy == CompletionStrategy.MAINTAIN_BLOCKED
    assert policy.rate_limit_budget.calls_per_minute == 0
    assert "引入大规模分钟库" in policy.forbidden_uses


def test_macro_policy_regime_condition():
    """宏观策略：REGIME_CONDITION、D_REGIME 层、禁止按个股横截面覆盖评估。"""
    policy = DATA_SOURCE_POLICIES["macro"]
    assert policy.completion_strategy == CompletionStrategy.REGIME_CONDITION
    assert policy.layer == DataSourceLayer.D_REGIME
    assert "按股票横截面覆盖率评估" in policy.forbidden_uses


# ── 2. Factor-to-source mapping (2 tests) ─────────────────


def test_factor_to_source_map_covers_all_8_factors():
    """FACTOR_TO_SOURCE_MAP 恰好 8 条且覆盖 FACTOR_DEFINITIONS 全部因子。"""
    assert len(FACTOR_TO_SOURCE_MAP) == 8
    for definition in FACTOR_DEFINITIONS:
        assert definition.code in FACTOR_TO_SOURCE_MAP


def test_factor_to_source_map_valuation_has_two_factors():
    """ep_ttm 和 negative_pb 均映射到 valuation。"""
    assert FACTOR_TO_SOURCE_MAP["ep_ttm"] == "valuation"
    assert FACTOR_TO_SOURCE_MAP["negative_pb"] == "valuation"


# ── 3. is_factor_blocked_by_roadmap (5 tests) ─────────────


def test_tail_proxy_always_blocked():
    """尾盘代理 MAINTAIN_BLOCKED 始终阻断，reason 含 maintain_blocked_strategy。"""
    result = is_factor_blocked_by_roadmap("tail_accumulation_proxy")
    assert result["is_blocked"] is True
    assert _has_reason(result["blocking_reasons"], "maintain_blocked_strategy")


def test_turnover_z20_not_blocked():
    """turnover_z20 依赖 daily_bars（已可用源），不阻断。"""
    result = is_factor_blocked_by_roadmap("turnover_z20")
    assert result["is_blocked"] is False
    assert result["source_key"] == "daily_bars"


def test_capital_flow_blocked_without_warehouse():
    """资金流在 warehouse=None 时无法验证连续性，安全阻断。"""
    result = is_factor_blocked_by_roadmap(
        "main_inflow_5d_ratio", warehouse=None
    )
    assert result["is_blocked"] is True
    assert _has_reason(result["blocking_reasons"], "continuity_not_verified")


def test_valuation_blocked_before_completion_start():
    """估值在补齐起始日后但尚未积累 PIT 数据时阻断。"""
    wh = _make_warehouse_with_conn((0,))
    result = is_factor_blocked_by_roadmap("ep_ttm", warehouse=wh)
    assert result["is_blocked"] is True
    assert (
        _has_reason(result["blocking_reasons"], "no_accumulated_data_since_completion_start")
        or _has_reason(result["blocking_reasons"], "before_completion_start_date")
    )


def test_unknown_factor_blocked():
    """未知因子代码阻断，reason 含 factor_source_mapping_missing。"""
    result = is_factor_blocked_by_roadmap("nonexistent_factor")
    assert result["is_blocked"] is True
    assert _has_reason(result["blocking_reasons"], "factor_source_mapping_missing")


# ── 4. check_capital_flow_continuity (3 tests) ────────────


def test_capital_flow_empty_table_blocked():
    """资金流表为空（distinct_days=0）→ 阻断，reason=source_table_empty。"""
    wh = _make_warehouse_with_conn((0, None))
    result = check_capital_flow_continuity(wh)
    assert result["is_blocked"] is True
    assert result["reason"] == "source_table_empty"
    assert result["is_continuous"] is False
    assert result["continuous_days"] == 0


def test_capital_flow_insufficient_days_blocked():
    """资金流 distinct_days=30 < 60 → 阻断，reason 含 below_60。"""
    wh = _make_warehouse_with_conn((30, "2026-07-31"))
    result = check_capital_flow_continuity(wh)
    assert result["is_blocked"] is True
    assert result["is_continuous"] is False
    assert result["continuous_days"] == 30
    assert "below_60" in result["reason"]


def test_capital_flow_sufficient_days_not_blocked():
    """资金流 distinct_days=65 >= 60 → 不阻断，is_continuous=True。"""
    wh = _make_warehouse_with_conn((65, "2026-07-31"))
    result = check_capital_flow_continuity(wh)
    assert result["is_blocked"] is False
    assert result["is_continuous"] is True
    assert result["continuous_days"] == 65


# ── 5. check_valuation_point_in_time (3 tests) ────────────


def test_valuation_before_completion_start_forbidden():
    """as_of_date 早于 2026-08-01 → 禁止伪历史。"""
    wh = _make_warehouse()
    result = check_valuation_point_in_time(
        wh, as_of_date=date(2026, 7, 15)
    )
    assert result["has_pit_data"] is False
    assert result["is_before_completion_start"] is True
    assert result["fake_history_forbidden"] is True


def test_valuation_after_start_no_data():
    """as_of_date 达到起始日但无积累数据 → has_pit_data=False。"""
    wh = _make_warehouse_with_conn((0,))
    result = check_valuation_point_in_time(
        wh, as_of_date=date(2026, 8, 5)
    )
    assert result["has_pit_data"] is False
    assert result["reason"] == "no_data_accumulated_yet"
    assert result["is_before_completion_start"] is False


def test_valuation_after_start_with_data():
    """as_of_date 达到起始日且有积累数据 → has_pit_data=True。"""
    wh = _make_warehouse_with_conn((100,))
    result = check_valuation_point_in_time(
        wh, as_of_date=date(2026, 8, 5)
    )
    assert result["has_pit_data"] is True
    assert result["reason"] == "pit_data_available"


# ── 6. get_lhb_event_sample (2 tests) ─────────────────────


def test_lhb_event_sample_with_events():
    """龙虎榜事件日样本：2 个事件日、15 个标的、non_event_policy=missing。"""
    wh = _make_warehouse_with_conn(
        fetchone_result=(15,),
        fetchall_result=[("2026-07-24",), ("2026-07-25",)],
    )
    result = get_lhb_event_sample(
        wh, start_date=date(2026, 7, 1), end_date=date(2026, 7, 31)
    )
    assert result["event_days"] == 2
    assert result["total_symbols"] == 15
    assert result["non_event_policy"] == "missing"
    assert result["event_dates"] == ["2026-07-24", "2026-07-25"]


def test_lhb_event_sample_no_events():
    """龙虎榜无事件日 → event_days=0，reason=no_lhb_events_in_range。"""
    wh = _make_warehouse_with_conn(
        fetchone_result=(0,),
        fetchall_result=[],
    )
    result = get_lhb_event_sample(
        wh, start_date=date(2026, 7, 1), end_date=date(2026, 7, 31)
    )
    assert result["event_days"] == 0
    assert result["reason"] == "no_lhb_events_in_range"


# ── 7. get_data_source_roadmap report (2 tests) ───────────


def test_roadmap_report_structure():
    """报告含 7 条 policy、8 条 factor_to_source_map、summary 数字正确。"""
    wh = _make_warehouse_with_conn((0, None, 0, 0))
    report = get_data_source_roadmap(MagicMock(), warehouse=wh)
    assert isinstance(report, DataSourceRoadmapReport)
    assert len(report.policies) == 7
    assert len(report.factor_to_source_map) == 8
    assert report.summary["total_sources"] == 7
    assert report.summary["total_factors"] == 8
    assert report.summary["warehouse_available"] is True
    assert report.valuation_completion_start_date == date(2026, 8, 1)


def test_roadmap_report_warehouse_unavailable():
    """warehouse 不可用时全部 source_states 为空、summary.warehouse_available=False。"""
    wh = _make_warehouse_unavailable()
    report = get_data_source_roadmap(MagicMock(), warehouse=wh)
    assert len(report.policies) == 7
    assert all(state.is_empty for state in report.source_states.values())
    assert report.summary["warehouse_available"] is False


# ── 8. Readiness integration (1 test) ─────────────────────


def test_readiness_includes_data_source_policy():
    """evaluate_factor_readiness 为 ep_ttm 填充 data_source_policy 字段。"""
    wh = _make_warehouse(available=True)
    ctd = _make_ctd_evidence()
    stats = _make_source_table_stats()
    coverage = _make_factor_coverage("ep_ttm", coverage=0.0, eligible=0)

    result = evaluate_factor_readiness(
        "ep_ttm",
        db_session=MagicMock(),
        warehouse=wh,
        complete_trade_day_evidence=ctd,
        source_table_stats=stats,
        factor_coverage=coverage,
    )
    assert result.data_source_policy is not None
    assert result.data_source_policy["source_key"] == "valuation"
    assert result.data_source_policy["completion_strategy"] == "incremental_from_date"
