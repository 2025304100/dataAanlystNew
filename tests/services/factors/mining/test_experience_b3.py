"""B3 · F1 内生链路 + 列表/详情/归档。

DoD 对照（开发计划 B3 卡 / 验收报告 §19/29）：
- 一次真实挖掘后 F1 库有自动沉淀记录
    → TestAccumulation：submit_candidate 正样本；finalize 分级 D 级负样本
- 列表/详情/归档契约 → TestContract（service + HTTP 路由，全走真实编译 AST）

红线 C5 决策记录（2026-09-20）：挖掘 worker 与 API 同进程，HTTP 自调不可靠；
mining 写入 F1 的**唯一入口**是 `service._store_f1_experience`（语义仍走 F1
服务层：三层指纹去重/参数泛化/负样本豁免，不直写 F1 表），对外契约仍以
`/factor-experience` HTTP 面为准。

跑法：`.venv/Scripts/python.exe -m pytest tests/services/factors/mining/test_experience_b3.py -q`
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.models.factor import Factor
from app.models.factor_experience import FactorExperience
from app.models.factor_mining import FactorMiningCandidate, FactorMiningRun
from app.models.factor_model import FactorVersion
from app.schemas.factor_library import FactorDraftCreate, FactorVersionCreate
from app.services.factors import factor_registry as FR
from app.services.factors.mining import service as SVC


def _make_compiled_version(db, *, code: str = "B3F") -> FactorVersion:
    """经 factor_registry 创建已被**真实编译**的版本（formula_ast_json 有值）。"""
    factor = FR.create_factor_draft(db, draft=FactorDraftCreate(
        code=code, name="B3 因子", category="trend", direction="higher_better",
        created_by="test"))
    version = FR.create_factor_version(db, factor_id=factor.id, request=FactorVersionCreate(
        formula_expr="mean(close,5)", direction="higher_better",
        change_note="b3", created_by="test"))
    db.commit()
    db.refresh(version)
    return version


def _make_run_and_candidate(db, *, cand_id: str, run_id: str, icir: float,
                            logic_source: str, version_id: int | None = None):
    run = FactorMiningRun(
        id=run_id, status="running", candidate_pool_snapshot_id="snap-b3",
        data_cutoff_at=datetime(2026, 12, 1), start_date=datetime(2026, 1, 5),
        end_date=datetime(2026, 11, 1), rebalance_frequency="daily",
        split_method="ratio", split_algorithm_version="split-1.0.0",
        target_horizon=5, random_seed=7,
    )
    db.add(run)
    db.flush()
    cand = FactorMiningCandidate(
        id=cand_id, run_id=run.id, factor_version_id=version_id,
        formula_expr="mean(close,5)", canonical_formula="mean(close,5)",
        formula_hash=cand_id + "x" * 30, operation="elite",
        generation_icir=icir, generation_complexity=2,
        category="trend", logic_source=logic_source,
        expected_direction="positive", economic_logic="趋势均值",
    )
    db.add(cand)
    db.commit()
    return run, cand


# ══════════════════════════════════════════════════════════
# 1) 一次真实挖掘后 F1 自动沉淀
# ══════════════════════════════════════════════════════════


class TestAccumulation:
    def test_submit_candidate_accumulates_positive(self, db_session):
        _make_run_and_candidate(db_session, cand_id="cand-b3p",
                                run_id="run-b3p", icir=0.3, logic_source="ai")
        res = SVC.submit_candidate_as_factor(db_session, candidate_id="cand-b3p")
        assert res["status"] == "submitted"

        rows = db_session.query(FactorExperience).all()
        assert len(rows) == 1, len(rows)
        exp = rows[0]
        assert exp.is_negative_sample == 0
        assert exp.source == "ai_generated"          # logic_source=ai 映射
        assert exp.category == "trend"
        assert "mean(close" in exp.formula_template   # 参数泛化：5 → {n1}
        assert exp.avg_icir == 0.3

    def test_submit_duplicate_is_deduped(self, db_session):
        """三层指纹去重：同一公式二次提交 → duplicate，不重复落库。"""
        _make_run_and_candidate(db_session, cand_id="cand-b3d",
                                run_id="run-b3d", icir=0.2, logic_source="manual")
        SVC.submit_candidate_as_factor(db_session, candidate_id="cand-b3d")
        # 再次提交会因已带 factor_version_id 走 already；直接对另一候选同公式再提交
        _make_run_and_candidate(db_session, cand_id="cand-b3d2",
                                run_id="run-b3d2", icir=0.2, logic_source="manual")
        SVC.submit_candidate_as_factor(db_session, candidate_id="cand-b3d2")
        positives = db_session.query(FactorExperience).filter_by(
            is_negative_sample=0).all()
        assert len(positives) == 1       # 指纹去重（同一 formula template）

    def test_d_grade_writes_negative_sample(self, db_session):
        from dataclasses import dataclass

        version = _make_compiled_version(db_session)
        _run, cand = _make_run_and_candidate(
            db_session, cand_id="cand-b3n", run_id="run-b3n",
            icir=0.02, logic_source="enumerated",
            version_id=int(version.id))
        db_session.flush()

        @dataclass
        class _Outcome:
            metrics: dict

        outcome = _Outcome(metrics={"ic": {"icir": 0.02}})
        SVC._grade_and_govern_outcome(db_session, cand=cand, outcome=outcome)

        negatives = db_session.query(FactorExperience).filter_by(
            is_negative_sample=1).all()
        assert len(negatives) == 1, len(negatives)
        assert negatives[0].category == "trend"


# ══════════════════════════════════════════════════════════
# 2) 列表 / 详情 / 归档契约（service + HTTP）
# ══════════════════════════════════════════════════════════


class TestContract:
    def test_list_get_archive_services(self, db_session):
        version = _make_compiled_version(db_session)
        formula_ast = json.loads(version.formula_ast_json)
        from app.services.factors.experience import service as EXP

        exp_id, status = EXP.store_experience(db_session, payload={
            "formula_ast": formula_ast,
            "category": "trend",
            "source": "enumerated",
            "metrics": [{"metric_type": "icir", "value": 0.25}],
        })
        assert status == "created"

        page = EXP.list_experiences(db_session, page=1, page_size=10)
        assert page["total"] == 1 and page["items"][0]["formula_template"]
        detail = EXP.get_experience(db_session, exp_id)
        assert detail["metrics"][0]["metric_type"] == "icir"
        assert detail["avg_icir"] == 0.25

        archived = EXP.archive_experience(db_session, exp_id)
        assert archived["status"] == "archived"
        assert EXP.get_experience(db_session, exp_id)["status"] == "archived"
        assert EXP.get_experience(db_session, "no-such") is None

    def test_http_contract(self, db_session):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.routes import factor_experience as FE
        from app.db.session import get_db

        version = _make_compiled_version(db_session)
        formula_ast = json.loads(version.formula_ast_json)
        from app.services.factors.experience import service as EXP

        exp_id, _ = EXP.store_experience(db_session, payload={
            "formula_ast": formula_ast, "category": "trend",
            "source": "manual", "metrics": [],
        })

        app = FastAPI()
        app.include_router(FE.router)
        app.dependency_overrides[get_db] = lambda: db_session
        client = TestClient(app)

        page = client.get("/factor-experience?page_size=10")
        assert page.status_code == 200
        assert page.json()["total"] == 1

        detail = client.get(f"/factor-experience/{exp_id}")
        assert detail.status_code == 200
        assert detail.json()["experience_id"] == exp_id

        missing = client.get("/factor-experience/nope")
        assert missing.status_code == 404

        ar = client.post(f"/factor-experience/{exp_id}/archive")
        assert ar.status_code == 200 and ar.json()["status"] == "archived"