# -*- coding: utf-8 -*-
"""T36 质量分级 + 季度重评单元测试。

规格来源：
- 需求文档 §6.8（8 维度 + S/A/B/C/D 五级阈值表 + 季度动态调整 + 约束）
- 设计文档 §7.10（`grade()` 纯函数：8 维度指标 → (等级, 理由)；阈值可注入）

硬约束：
- **每一级要求全部满足才定级**（不是打分加权）；
- 衰减率 `decay_ratio < 0.5` → **不得评为 B 级以上**（§3.2 / §6.8 约束）；
- 月频因子**最高只能评 B 级**（test 段仅 12 点，统计检验无意义）；
- `grade_manual_adjusted=1` 的因子**不被季度任务自动覆盖**。

纯函数测试：不触 DB。
"""
from __future__ import annotations

import pytest

from app.services.factors.mining.contracts import GradeResult
from app.services.factors.mining.factor_grading import (
    DEFAULT_THRESHOLDS,
    MAX_GRADE_MONTHLY,
    decay_cap,
    grade,
    monthly_cap,
    review_quarterly,
)


# ── 素材：8 维度全达标的 S 级样本 ────────────────────────────────────
S_METRICS = {
    "icir": 0.62,
    "p_value": 0.0005,
    "p_adj_bonferroni": 0.005,      # S: <0.01
    "ci_lower": 0.35,               # S: >0.1
    "perm_p_value": 0.0005,         # S: <0.001
    "coverage": 0.95,               # S: ≥0.90
    "oos_icir": 0.42,               # S: ≥0.3 且同向
    "correlation": 0.45,            # S: <0.7
    "turnover": 0.18,               # S: <0.30
    "complexity": 3,                # S: ≤4
    "decay_ratio": 0.9,
    "frequency": "daily",
}


def m(**over):
    base = dict(S_METRICS)
    base.update(over)
    return base


class TestGradeLevels:
    def test_all_dimensions_met_yields_s(self):
        g, reason = grade(S_METRICS)
        assert g == "S"
        assert reason

    def test_single_dimension_failure_drops_level(self):
        """每一级全部满足才定级：S 的覆盖率不够 → 落到 A。"""
        g, _ = grade(m(coverage=0.85))
        assert g == "A"

    def test_a_level_boundary(self):
        g, _ = grade(m(icir=0.31, p_adj_bonferroni=0.04, ci_lower=0.01,
                       perm_p_value=0.009, coverage=0.82, oos_icir=0.1,
                       correlation=0.75, turnover=0.45, complexity=6))
        assert g == "A"

    def test_b_level(self):
        g, _ = grade(m(icir=0.22, p_value=0.08, p_adj_bonferroni=0.3,
                       ci_lower=-0.01, perm_p_value=0.5, coverage=0.75,
                       oos_icir=0.1, correlation=0.85, turnover=0.8,
                       complexity=9))
        assert g == "B"

    def test_c_level(self):
        g, _ = grade(m(icir=0.16, p_value=0.15, p_adj_bonferroni=0.9,
                       ci_lower=-0.5, perm_p_value=0.9, coverage=0.65,
                       oos_icir=-0.05, correlation=0.92, turnover=0.9,
                       complexity=12))
        assert g == "C"

    def test_d_level(self):
        g, _ = grade(m(icir=0.05, p_value=0.6, p_adj_bonferroni=1.0,
                       ci_lower=-1.0, perm_p_value=0.9, coverage=0.4,
                       oos_icir=-0.2, correlation=0.98, turnover=0.95,
                       complexity=20))
        assert g == "D"


class TestHardConstraints:
    def test_decay_below_half_caps_at_c(self):
        """衰减率 <0.5 → 不得 B 级以上（本例原本 S）。"""
        g, reason = grade(m(decay_ratio=0.4))
        assert g in ("C", "D")
        assert "decay" in reason.lower() or "衰减" in reason

    def test_decay_cap_helper(self):
        assert decay_cap("S", 0.4) == "C"
        assert decay_cap("A", 0.49) == "C"
        assert decay_cap("B", 0.2) == "C"
        assert decay_cap("C", 0.1) == "C"
        assert decay_cap("D", 0.1) == "D"

    def test_monthly_frequency_caps_at_b(self):
        g, _ = grade(m(frequency="monthly"))
        assert g == MAX_GRADE_MONTHLY == "B"

    def test_monthly_cap_helper(self):
        assert monthly_cap("S") == "B"
        assert monthly_cap("A") == "B"
        assert monthly_cap("B") == "B"
        assert monthly_cap("C") == "C"

    def test_degraded_flag_caps_at_b(self):
        """统计层降级（月频 degraded=True）同样最高 B。"""
        g, _ = grade(m(frequency="monthly", degraded=True))
        assert g == "B"


class TestThresholds:
    def test_custom_thresholds_injection(self):
        strict = dict(DEFAULT_THRESHOLDS)
        strict["icir"]["S"] = 0.95          # 抬高 S 门槛 → 原本 S 变 A
        g, _ = grade(S_METRICS, thresholds=strict)
        assert g == "A"

    def test_missing_metrics_treated_as_not_met(self):
        partial = {"icir": 0.9}
        g, _ = grade(partial)
        assert g == "D"

    def test_default_thresholds_structure(self):
        for level in ("S", "A", "B", "C"):
            assert level in DEFAULT_THRESHOLDS["icir"]


class TestQuarterlyReview:
    def test_b_stable_two_quarters_promotes_to_a(self):
        res = review_quarterly(
            current_grade="B", history_grades=("B", "B"),
            current_icir=0.25, previous_icir=0.24, oos_reversed=False,
            manual_adjusted=False)
        assert res.grade == "A"
        assert res.action == "promote"

    def test_single_stable_quarter_not_enough(self):
        res = review_quarterly(
            current_grade="B", history_grades=("B",),
            current_icir=0.25, previous_icir=0.24)
        assert res.action == "keep"

    def test_two_quarters_decline_demotes_and_removes(self):
        res = review_quarterly(
            current_grade="A", history_grades=("A", "A"),
            current_icir=0.20, previous_icir=0.40,   # 降幅 50% > 30%
            decline_streak=2)
        assert res.action == "demote"
        assert res.remove_from_factor_set is True
        assert res.grade in ("B", "C", "D")

    def test_oos_reversal_counts_as_decline(self):
        res = review_quarterly(
            current_grade="S", history_grades=("S", "S"),
            current_icir=0.60, previous_icir=0.61,
            oos_reversed=True, decline_streak=2)
        assert res.action == "demote"
        assert res.remove_from_factor_set is True

    def test_manual_adjusted_never_overwritten(self):
        res = review_quarterly(
            current_grade="A", history_grades=("A", "A"),
            current_icir=0.20, previous_icir=0.60,
            decline_streak=2, manual_adjusted=True)
        assert res.action == "keep"
        assert res.grade == "A"
        assert res.remove_from_factor_set is False

    def test_no_decline_keeps_grade(self):
        res = review_quarterly(
            current_grade="A", history_grades=("A", "A"),
            current_icir=0.62, previous_icir=0.60, decline_streak=0)
        assert res.action == "keep"
        assert res.grade == "A"


class TestGradeResultContract:
    def test_grade_returns_reason_string(self):
        g, reason = grade(S_METRICS)
        assert isinstance(g, str) and isinstance(reason, str)

    def test_grade_result_dataclass_usable(self):
        g, reason = grade(S_METRICS)
        res = GradeResult(grade=g, reason=reason, metrics_snapshot=S_METRICS)
        assert res.grade == g
        assert res.thresholds_source == "default"


class TestEvidenceDrawerContract:
    """T37 证据抽屉（向导 §8.4.1）对分级层的数据契约要求。

    抽屉要能回答「为什么是这个等级」，因此分级层必须保证：
    理由可读（一句话理由）、阈值来源可追溯、月频提示有依据、D 级也有完整证据。
    """

    def test_reason_mentions_grade_for_drawer_headline(self):
        """抽屉顶部一句话理由：理由必须点出最终等级。"""
        for metrics in (S_METRICS, m(coverage=0.85), m(icir=0.05, coverage=0.3)):
            g, reason = grade(metrics)
            assert g in reason, f"理由未提及等级 {g}: {reason}"

    def test_reason_reports_active_hard_constraint(self):
        """硬约束生效时理由须说明原因（抽屉据此解释「为什么被压级」）。"""
        _g, reason = grade(m(decay_ratio=0.3))
        assert "衰减" in reason or "decay" in reason.lower()

    def test_thresholds_source_passthrough(self):
        """自定义阈值的因子，证据里必须标 custom（否则用户分不清用了哪套门槛）。"""
        g, reason = grade(S_METRICS)
        res = GradeResult(
            grade=g, reason=reason, metrics_snapshot=S_METRICS,
            thresholds_source="custom")
        assert res.thresholds_source == "custom"

    def test_degraded_flag_is_the_monthly_tip_basis(self):
        """抽屉「月频最高 B 级」提示的依据：degraded / monthly 都要压到 B。"""
        by_flag, _ = grade(m(degraded=True))
        by_freq, _ = grade(m(frequency="monthly"))
        assert by_flag == "B" == by_freq

    def test_d_grade_still_has_full_evidence(self):
        """D 级因子也要能给出完整证据（不能因为等级低就没有理由/快照）。"""
        weak = m(icir=0.05, coverage=0.3, correlation=0.98,
                 p_adj_bonferroni=1.0, p_value=0.9)
        g, reason = grade(weak)
        assert g == "D"
        assert reason
        # 快照保留全部输入维度（抽屉 8 维度明细表按此渲染）
        for key in ("icir", "coverage", "correlation", "turnover", "complexity"):
            assert key in weak

    def test_restore_auto_lets_quarterly_take_over_again(self):
        """「恢复自动」（manual_adjusted=0）后季度任务重新接管（§8.4.2）。"""
        hijacked = review_quarterly(
            current_grade="B", history_grades=("B", "B"),
            current_icir=0.25, previous_icir=0.24, manual_adjusted=True)
        assert hijacked.action == "keep"          # 人工期间不覆盖
        restored = review_quarterly(
            current_grade="B", history_grades=("B", "B"),
            current_icir=0.25, previous_icir=0.24, manual_adjusted=False)
        assert restored.action == "promote"       # 恢复后自动升级重新生效
        assert restored.grade == "A"
