"""DEF-9 回归 · list_candidates 等级联表（结果页数据链）。

真实缺陷（2026-09-24 全面测试报告 DEF-9）：等级只存在
`factor_versions.quality_grade`，候选列表接口此前不返回 grade —— 即使
finalize 正常执行，结果页也拿不到等级列。

哨兵：
1. 带 factor_version_id 且版本已定级 → items[].grade 回填等级；
2. factor_version_id 为 null（未 finalize/未提交）→ grade=None；
3. 版本未定级（quality_grade=None）→ grade=None；
4. latest_ic / latest_evaluation_status 随 items 返回（结果页排序口径）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.models.factor import Factor
from app.models.factor_model import FactorVersion
from app.models.factor_mining import FactorMiningCandidate, FactorMiningRun
from app.services.factors.mining import service as SVC

pytestmark = pytest.mark.whitebox


def _make_run(db_session, run_id: str = "run-grade") -> FactorMiningRun:
    row = FactorMiningRun(
        id=run_id, status="succeeded",
        candidate_pool_snapshot_id="snap-grade",
        data_cutoff_at=datetime(2026, 11, 10), start_date=datetime(2026, 1, 5),
        end_date=datetime(2026, 11, 1), rebalance_frequency="daily",
        split_method="ratio", split_algorithm_version="split-1.0.0",
        target_horizon=5, max_generation=3, random_seed=7,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _make_candidate(db_session, run_id: str, cand_id: str, *,
                    version_id: int | None, icir: float = 0.4,
                    latest_ic: float | None = None):
    row = FactorMiningCandidate(
        id=cand_id, run_id=run_id,
        formula_expr="mean(close,5)/mean(close,20)-1",
        canonical_formula="mean(close,5)/mean(close,20)-1",
        formula_hash=f"hash-{cand_id}", generation=1, operation="elite",
        category="trend", generation_icir=icir, generation_coverage=0.9,
        generation_complexity=3, economic_logic="均线趋势",
        expected_direction="positive", factor_version_id=version_id,
        latest_ic=latest_ic,
        latest_evaluation_status="done" if latest_ic is not None else None,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _make_graded_version(db_session, *, grade: str | None) -> FactorVersion:
    factor = Factor(code=f"F-GRADE-{grade or 'NA'}-{id(grade) % 99999}",
                    name="测试因子", category="trend", direction="higher_better",
                    status="active")
    db_session.add(factor)
    db_session.commit()
    version = FactorVersion(factor_id=factor.id, version=1,
                            formula_expr="mean(close,5)")
    db_session.add(version)
    db_session.commit()
    if grade is not None:
        version.quality_grade = grade
        db_session.commit()
    return version


def test_graded_candidate_returns_grade(db_session):
    """哨兵 1：版本已定级 → grade 回填（结果页等级列数据源）。"""
    _make_run(db_session)
    version = _make_graded_version(db_session, grade="A")
    _make_candidate(db_session, "run-grade", "cand-graded",
                    version_id=version.id, latest_ic=0.052)

    out = SVC.list_candidates(db_session, "run-grade")
    item = next(i for i in out["items"] if i["id"] == "cand-graded")
    assert item["grade"] == "A"
    assert item["latest_ic"] == pytest.approx(0.052)
    assert item["latest_evaluation_status"] == "done"


def test_unversioned_candidate_grade_is_none(db_session):
    """哨兵 2：factor_version_id=None → grade=None（不误报等级）。"""
    _make_run(db_session)
    _make_candidate(db_session, "run-grade", "cand-raw", version_id=None)

    out = SVC.list_candidates(db_session, "run-grade")
    item = next(i for i in out["items"] if i["id"] == "cand-raw")
    assert item["grade"] is None


def test_ungraded_version_grade_is_none(db_session):
    """哨兵 3：版本存在但未定级 → grade=None。"""
    _make_run(db_session)
    version = _make_graded_version(db_session, grade=None)
    _make_candidate(db_session, "run-grade", "cand-ungraded",
                    version_id=version.id)

    out = SVC.list_candidates(db_session, "run-grade")
    item = next(i for i in out["items"] if i["id"] == "cand-ungraded")
    assert item["grade"] is None


def test_grade_filter_still_works(db_session):
    """grade 筛选参数不被联表改造破坏（既有契约）。"""
    _make_run(db_session)
    version_a = _make_graded_version(db_session, grade="A")
    version_b = _make_graded_version(db_session, grade="B")
    _make_candidate(db_session, "run-grade", "cand-a", version_id=version_a.id)
    _make_candidate(db_session, "run-grade", "cand-b", version_id=version_b.id)

    out = SVC.list_candidates(db_session, "run-grade", grade="A")
    ids = {i["id"] for i in out["items"]}
    assert "cand-a" in ids
