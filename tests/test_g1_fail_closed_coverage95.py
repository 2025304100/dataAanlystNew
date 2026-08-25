"""T-A5 Q6.2：95% 正式阻断 + 研究 DEGRADED_DATA 不入生产。

覆盖：
- T-A5-C1：正式 94%（WARN）→ BLOCKED，原因码 COVERAGE_BELOW_95（不含 CRITICAL）
- T-A5-C2：研究 90%（FAIL）→ 允许但 DEGRADED_DATA，eligible=False，无成员级 REJECTED
- T-A5-C3：正式 <92%（FAIL）→ 双阻断码 COVERAGE_BELOW_95 + COVERAGE_CRITICAL_BELOW_92
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g1_cov95_")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["ALEMBIC_DATABASE_URL"] = url
    from alembic.config import Config
    from alembic import command as ac
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    ac.upgrade(cfg, "head")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


def _make_100_members_all_pass() -> list[dict]:
    out: list[dict] = []
    for i in range(1, 101):
        out.append({
            "symbol_id": i,
            "score_found": True,
            "staled": False,
            "not_pit_safe": False,
        })
    return out


class TestCoverage95Block:
    # ------------------------------------------------------------------
    # C1: 正式 94%（WARN）→ BLOCKED，原因码 COVERAGE_BELOW_95
    # ------------------------------------------------------------------
    def test_c1_production_94pct_warn_blocks_only_below_95(self, tmp_alembic_db):
        """T-A5-C1a: coverage_rate=0.94 (WARN) 正式 → BLOCKED，只有 COVERAGE_BELOW_95。"""
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_warn = {
            "coverage_level": "WARN",
            "coverage_pct": 94.0,
            "coverage_rate": 0.94,
            "expected": 100,
            "actual": 94,
        }
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members_all_pass()

        result = evaluate_fail_closed_decision(
            db,
            run_mode="production",
            coverage_result=coverage_warn,
            key_member_result=key_pass,
            staleness_result=stale_pass,
            per_member_score_avail=members,
            portfolio_id=1,
            strategy_snapshot_id="snap_c1a",
            decision_at=datetime(2025, 1, 6, 7, 0, 0),
        )

        assert result["block_all_members"] is True
        assert result["composite_level"] == "BLOCKED"
        assert "COVERAGE_BELOW_95" in result["block_codes"], (
            f"block_codes 应含 COVERAGE_BELOW_95，实际 {result['block_codes']}"
        )
        assert "COVERAGE_CRITICAL_BELOW_92" not in result["block_codes"], (
            f"94% >= 92%，不应含 COVERAGE_CRITICAL_BELOW_92，实际 {result['block_codes']}"
        )
        assert any("94.00%" in r for r in result["block_reasons"]), (
            f"block_reasons 应含可读百分比 94.00%，实际 {result['block_reasons']}"
        )
        assert result["is_result_production_eligible"] is False

    def test_c1_boundary_0949999999_still_warn_blocked(self, tmp_alembic_db):
        """T-A5-C1b: 边界值 coverage_rate=0.949999999（差 1e-9 到 95%）→ WARN → BLOCKED。"""
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_boundary = {
            "coverage_level": "WARN",
            "coverage_pct": 94.9999999,
            "coverage_rate": 0.949999999,
            "expected": 1_000_000_000,
            "actual": 949_999_999,
        }
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members_all_pass()

        result = evaluate_fail_closed_decision(
            db,
            run_mode="production",
            coverage_result=coverage_boundary,
            key_member_result=key_pass,
            staleness_result=stale_pass,
            per_member_score_avail=members,
            portfolio_id=1,
            strategy_snapshot_id="snap_c1b",
            decision_at=datetime(2025, 1, 6, 7, 0, 0),
        )

        assert result["block_all_members"] is True, "0.949999999 仍 < 0.95 应 BLOCKED"
        assert result["composite_level"] == "BLOCKED"
        assert "COVERAGE_BELOW_95" in result["block_codes"]
        assert "COVERAGE_CRITICAL_BELOW_92" not in result["block_codes"]

    def test_c1_boundary_095_exact_pass(self, tmp_alembic_db):
        """T-A5-C1c 控制组：coverage_rate=0.95 恰好 PASS → 不阻断，eligible=True。"""
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_pass_95 = {
            "coverage_level": "PASS",
            "coverage_pct": 95.0,
            "coverage_rate": 0.95,
            "expected": 100,
            "actual": 95,
        }
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members_all_pass()

        result = evaluate_fail_closed_decision(
            db,
            run_mode="production",
            coverage_result=coverage_pass_95,
            key_member_result=key_pass,
            staleness_result=stale_pass,
            per_member_score_avail=members,
            portfolio_id=1,
            strategy_snapshot_id="snap_c1c",
            decision_at=datetime(2025, 1, 6, 7, 0, 0),
        )

        assert result["composite_level"] == "PASS", f"0.95 恰好 PASS，实际 {result['composite_level']}"
        assert result["block_all_members"] is False
        assert result["block_codes"] == []
        assert result["is_result_production_eligible"] is True

    # ------------------------------------------------------------------
    # C2: 研究 90%（FAIL）→ 允许但 DEGRADED_DATA，eligible=False
    # ------------------------------------------------------------------
    def test_c2_research_90pct_fail_degraded_not_eligible(self, tmp_alembic_db):
        """T-A5-C2: coverage_level=FAIL (90%<92%) 研究 → 不阻断，WARN，DEGRADED_DATA，
        per_member_rejections_research_only == []（组合覆盖率问题不打单成员 REJECTED）。"""
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_fail_90 = {
            "coverage_level": "FAIL",
            "coverage_pct": 90.0,
            "coverage_rate": 0.90,
            "expected": 100,
            "actual": 90,
        }
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members_all_pass()

        result = evaluate_fail_closed_decision(
            db,
            run_mode="research",
            coverage_result=coverage_fail_90,
            key_member_result=key_pass,
            staleness_result=stale_pass,
            per_member_score_avail=members,
            portfolio_id=1,
            strategy_snapshot_id="snap_c2",
            decision_at=datetime(2025, 1, 6, 7, 0, 0),
        )

        assert result["block_all_members"] is False, "research 不应全组合阻断"
        assert result["composite_level"] == "WARN", f"期望 WARN，实际 {result['composite_level']}"

        warns = result["degraded_warnings"]
        assert len(warns) >= 1, f"至少 1 条 DEGRADED warning，实际 {len(warns)}"
        has_cov_fail = any(
            w.get("code") == "DEGRADED_DATA"
            and w.get("subtype") == "COVERAGE_FAIL_BELOW_92"
            for w in warns
        )
        assert has_cov_fail, (
            f"degraded_warnings 应含 DEGRADED_DATA+COVERAGE_FAIL_BELOW_92，实际 {warns}"
        )
        pct_warn = next(
            (w for w in warns if w.get("subtype") == "COVERAGE_FAIL_BELOW_92"), None
        )
        assert pct_warn is not None and "90.00%" in str(pct_warn.get("pct")), (
            f"DEGRADED_DATA 应带 pct=90.00%，实际 {pct_warn}"
        )

        assert result["is_result_production_eligible"] is False, (
            "研究 FAIL 覆盖应 eligible=False"
        )
        assert result["block_codes"] == [], "research 下 block_codes 应为空"
        assert result["block_reasons"] == [], "research 下 block_reasons 应为空"

        assert result["per_member_rejections_research_only"] == [], (
            "组合覆盖率问题不应导致 per_member_rejections_research_only 有值（不是单成员缺 Score）"
        )

    # ------------------------------------------------------------------
    # C3: 正式 FAIL <92% → 两条阻断码
    # ------------------------------------------------------------------
    def test_c3_production_91pct_fail_dual_block_codes(self, tmp_alembic_db):
        """T-A5-C3a: coverage_rate=0.91 (91%<92%) 正式 → BLOCKED，
        block_codes 同时包含 COVERAGE_BELOW_95 AND COVERAGE_CRITICAL_BELOW_92。"""
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_fail_91 = {
            "coverage_level": "FAIL",
            "coverage_pct": 91.0,
            "coverage_rate": 0.91,
            "expected": 100,
            "actual": 91,
        }
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members_all_pass()

        result = evaluate_fail_closed_decision(
            db,
            run_mode="production",
            coverage_result=coverage_fail_91,
            key_member_result=key_pass,
            staleness_result=stale_pass,
            per_member_score_avail=members,
            portfolio_id=1,
            strategy_snapshot_id="snap_c3a",
            decision_at=datetime(2025, 1, 6, 7, 0, 0),
        )

        assert result["block_all_members"] is True
        assert result["composite_level"] == "BLOCKED"
        codes = result["block_codes"]
        assert "COVERAGE_BELOW_95" in codes, f"91%<95% 应有 COVERAGE_BELOW_95，实际 {codes}"
        assert "COVERAGE_CRITICAL_BELOW_92" in codes, (
            f"91%<92% 应有 COVERAGE_CRITICAL_BELOW_92，实际 {codes}"
        )
        reasons = result["block_reasons"]
        assert any("91.00%" in r and "95%" in r for r in reasons), (
            f"block_reasons 应含 91.00%<95%，实际 {reasons}"
        )
        assert any("91.00%" in r and "92%" in r for r in reasons), (
            f"block_reasons 应含 91.00%<92%，实际 {reasons}"
        )
        assert result["is_result_production_eligible"] is False

    def test_c3_boundary_0919999999_fail_dual(self, tmp_alembic_db):
        """T-A5-C3b: 边界值 0.919999999（差 1e-9 到 92%）→ FAIL 双重阻断码。"""
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_boundary_fail = {
            "coverage_level": "FAIL",
            "coverage_pct": 91.9999999,
            "coverage_rate": 0.919999999,
            "expected": 1_000_000_000,
            "actual": 919_999_999,
        }
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members_all_pass()

        result = evaluate_fail_closed_decision(
            db,
            run_mode="production",
            coverage_result=coverage_boundary_fail,
            key_member_result=key_pass,
            staleness_result=stale_pass,
            per_member_score_avail=members,
            portfolio_id=1,
            strategy_snapshot_id="snap_c3b",
            decision_at=datetime(2025, 1, 6, 7, 0, 0),
        )

        assert result["block_all_members"] is True
        assert result["composite_level"] == "BLOCKED"
        codes = result["block_codes"]
        assert "COVERAGE_BELOW_95" in codes
        assert "COVERAGE_CRITICAL_BELOW_92" in codes, (
            f"0.919999999 < 0.92 应有 COVERAGE_CRITICAL_BELOW_92，实际 {codes}"
        )
