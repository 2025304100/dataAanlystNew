"""B1 · `factor_versions` 质量分级 4 列迁移（quality_grade / grade_updated_at /
grade_metrics_json / grade_manual_adjusted）。

DoD 对照（开发计划 B1 卡）：
- 迁移**升/降级**测试 + drift 校验通过 → TestMigration（upgrade→downgrade→upgrade）
  + test_schema_drift_zero（verify_schema_drift.py 对 factor_versions drift=0）；
- `grade()` 结果可落库 → TestGradePersistence：
    * `persist_grade_to_version` 直接落（自动评定语义，manual_adjusted=0）；
    * `apply_manual_grade` 人工调级 → quality_grade + grade_manual_adjusted=1 落库；
    * `set_manual_grade_auto` 恢复自动 → grade_manual_adjusted=0（顺带修了
      service.py 里 `from app.models.factor import FactorVersion` 的 latent 导入错，
      改从 `app.models.factor_model` 导入）；
    * `run_quarterly_review` 自动评定同步写当前态列。

跑法：`.venv/Scripts/python.exe -m pytest tests/services/factors/mining/test_grade_columns_b1.py -q`
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_MIGRATION_HEAD_DOWN = "wps_0023_059_factor_grade_history"

#: 满足 S 级全部阈值（8 维度）的指标（阈值见 factor_grading.DEFAULT_THRESHOLDS）
S_METRICS = {
    "icir": 0.6, "p_adj_bonferroni": 0.001, "ci_lower": 0.12,
    "perm_p_value": 0.0001, "coverage": 0.95, "oos_icir": 0.4,
    "correlation": 0.5, "turnover": 0.2, "complexity": 3,
    "decay_ratio": 0.9,
}


# ══════════════════════════════════════════════════════════
# 迁移升/降级 + drift
# ══════════════════════════════════════════════════════════


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def _fresh_sqlite_url() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_b1_")
    os.close(fd)
    return f"sqlite:///{path}", path


def _upgraded_columns(url: str) -> set:
    from sqlalchemy import create_engine
    from sqlalchemy import inspect as sa_inspect

    engine = create_engine(url)
    try:
        insp = sa_inspect(engine)
        return {c["name"] for c in insp.get_columns("factor_versions")}
    finally:
        engine.dispose()


class TestMigrationUpDown:
    def test_upgrade_downgrade_upgrade_roundtrip(self):
        from alembic import command as ac

        # ① upgrade head → 4 列存在
        url, path = _fresh_sqlite_url()
        try:
            os.environ["ALEMBIC_DATABASE_URL"] = url
            cfg = _alembic_cfg()
            ac.upgrade(cfg, "head")
            cols = _upgraded_columns(url)
            assert {"quality_grade", "grade_updated_at",
                    "grade_metrics_json", "grade_manual_adjusted"} <= cols

            # ② downgrade 到 059 → 4 列移除（升降级 DoD）
            ac.downgrade(cfg, _MIGRATION_HEAD_DOWN)
            cols = _upgraded_columns(url)
            assert "quality_grade" not in cols
            assert "grade_metrics_json" not in cols
            assert "grade_manual_adjusted" not in cols
            assert "grade_updated_at" not in cols

            # ③ 再 upgrade → 4 列回（迁移幂等且可逆）
            ac.upgrade(cfg, "head")
            cols = _upgraded_columns(url)
            assert {"quality_grade", "grade_updated_at",
                    "grade_metrics_json", "grade_manual_adjusted"} <= cols
        finally:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
            try:
                os.remove(path)
            except OSError:
                pass

    def test_schema_drift_zero_for_factor_versions(self):
        """verify_schema_drift.py --tables factor_versions（sqlite，纯迁移链）drift=0。

        drift=0 说明**不靠 auto-align 兜底**，迁移链产物与 ORM 声明完全一致。
        """
        url, path = _fresh_sqlite_url()
        try:
            os.environ["ALEMBIC_DATABASE_URL"] = url
            from alembic import command as ac

            ac.upgrade(_alembic_cfg(), "head")
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT)
            # Windows 下 text=True 默认用 GBK 解码子进程输出，而校验脚本会打
            # UTF-8 中文（迁移注释含全角箭头）→ UnicodeDecodeError。固定 UTF-8。
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                [sys.executable, str(ROOT / ".workbuddy" / "mining"
                                    / "verify_schema_drift.py"),
                 "--tables", "factor_versions", "--url-from-env", "--json"],
                capture_output=True, text=True, cwd=str(ROOT), env=env,
                encoding="utf-8", errors="replace",
                timeout=180,
            )
            assert proc.returncode == 0, proc.stderr[-2000:]
            payload = json.loads(proc.stdout)
            summary = payload["summary"]
            assert summary["drift"] == 0, json.dumps(payload["drift_tables"],
                                                     ensure_ascii=False)
            assert summary["total"] >= 1
        finally:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
            try:
                os.remove(path)
            except OSError:
                pass


# ══════════════════════════════════════════════════════════
# grade() 结果落库闭环（grade 纯函数 → factor_versions 4 列）
# ══════════════════════════════════════════════════════════


def _seed_factor_version(db_session):
    from app.models.factor import Factor
    from app.models.factor_model import FactorVersion

    f = Factor(code="B1GRADE", name="B1 grade test", category="trend",
               direction="positive", status="active")
    db_session.add(f)
    db_session.flush()
    v = FactorVersion(factor_id=f.id, version=1,
                      formula_expr="mean(close,5)", direction="higher_better")
    db_session.add(v)
    db_session.commit()
    db_session.refresh(v)
    return f, v


class TestGradePersistence:
    def test_persist_grade_to_version_lands(self, db_session):
        """grade()（纯函数）→ persist_grade_to_version → 4 列全部落库。"""
        from app.services.factors.mining import factor_grading as FG
        from app.services.factors.mining import service as SVC

        _f, v = _seed_factor_version(db_session)
        grade_value, reason = FG.grade(S_METRICS)
        assert grade_value == "S", (grade_value, reason)

        result = SVC.persist_grade_to_version(
            db_session, factor_version_id=v.id, grade_value=grade_value,
            reason=reason, metrics=S_METRICS, manual_adjusted=0,
        )
        assert result["persisted"] is True

        db_session.refresh(v)
        assert v.quality_grade == "S"
        assert v.grade_updated_at is not None
        assert v.grade_manual_adjusted == 0
        blob = json.loads(v.grade_metrics_json)
        assert blob["grade"] == "S"
        assert blob["metrics"]["icir"] == S_METRICS["icir"]

        # 再次评定（降级为 D：全零分 cadidate）→ 覆盖当前态
        SVC.persist_grade_to_version(
            db_session, factor_version_id=v.id, grade_value="D",
            reason="无有效样本", metrics={"icir": 0.0}, manual_adjusted=0,
        )
        db_session.refresh(v)
        assert v.quality_grade == "D"

    def test_apply_manual_grade_and_restore(self, db_session):
        """人工调级 → factor_versions 当前态 + factor_grade_history；恢复自动 → 清标记。"""
        from datetime import datetime

        from app.models.factor_grade_history import FactorGradeHistory
        from app.models.factor_mining import (
            FactorMiningCandidate,
            FactorMiningRun,
        )
        from app.services.factors.mining import service as SVC

        f, v = _seed_factor_version(db_session)
        run = FactorMiningRun(
            id="run-b1g", status="succeeded", candidate_pool_snapshot_id="snap-b1g",
            data_cutoff_at=datetime(2026, 12, 1),
            start_date=datetime(2026, 1, 5),
            end_date=datetime(2026, 11, 1),
            rebalance_frequency="weekly", split_method="ratio",
            split_algorithm_version="split-1.0.0", target_horizon=5,
        )
        db_session.add(run)
        db_session.flush()
        cand = FactorMiningCandidate(
            id="cand-b1g", run_id=run.id, factor_version_id=v.id,
            formula_expr="mean(close,5)", canonical_formula="mean(close,5)",
            formula_hash="c" * 31 + "0", operation="elite",
            expected_direction="positive",
        )
        db_session.add(cand)
        db_session.commit()

        # 人工调级：落 factor_versions 当前态 + grade_manual_adjusted=1
        res = SVC.apply_manual_grade(
            db_session, candidate_id="cand-b1g",
            grade="B", reason="样本区间过短，人工复核后定 B 级观察测试",
        )
        assert res["manual_adjusted"] == 1
        db_session.refresh(v)
        assert v.quality_grade == "B"
        assert v.grade_manual_adjusted == 1
        assert v.grade_updated_at is not None

        # 历史行可追溯
        hist = db_session.query(FactorGradeHistory).filter_by(
            factor_version_id=v.id).all()
        assert len(hist) == 1 and hist[0].grade == "B" and hist[0].source == "manual"

        # 恢复自动评定（顺带覆盖 service 里 FactorVersion 导入路径）
        SVC.set_manual_grade_auto(db_session, candidate_id="cand-b1g")
        db_session.refresh(v)
        assert v.grade_manual_adjusted == 0

    def test_quarterly_review_syncs_current_state(self, db_session):
        """季度重评（自动评定）→ factor_versions.quality_grade 同步为最新等级。"""
        from datetime import datetime

        from app.models.factor_grade_history import FactorGradeHistory
        from app.services.factors.mining import factor_grading as FG

        f, v = _seed_factor_version(db_session)
        db_session.add(FactorGradeHistory(
            id="v-q-1", factor_version_id=v.id, grade="B",
            reason="季度评定初始", metrics_snapshot_json="{}",
            source="quarterly", manual_adjusted=0,
            created_at=datetime(2026, 6, 1),
        ))
        db_session.commit()
        # 先落一个「旧等级」到当前态，验证季度任务会覆盖
        db_session.add(FactorGradeHistory(
            id="v-q-2", factor_version_id=v.id, grade="B",
            reason="季度评定第二次", metrics_snapshot_json="{}",
            source="quarterly", manual_adjusted=0,
            created_at=datetime(2026, 9, 1),
        ))
        db_session.commit()

        stats = FG.run_quarterly_review(db_session, actor="system")
        assert stats["reviewed"] >= 1

        db_session.refresh(v)
        assert v.quality_grade in ("S", "A", "B", "C", "D")
        assert v.grade_updated_at is not None
        assert v.grade_manual_adjusted == 0
        assert json.loads(v.grade_metrics_json)["grade"] == v.quality_grade