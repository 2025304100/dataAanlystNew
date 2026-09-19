"""T25 · G4 端到端：初始种群 → GA → 最终验证 → 提交/入组（含路由注册验证）。

流程（全离线，LLM/评估器注入假实现）：
  run 创建 → 候选落库 → GA 主循环（1 代） → finalize（test-once） →
  提交 Top 候选为因子草稿 → batch-review 加入 FactorSet → 路由注册断言
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.factor_mining import (
    FactorMiningCandidate,
    FactorMiningGeneration,
    FactorMiningRun,
)
from app.services.factors import factor_set_service as FSS
from app.services.factors.mining import genetic_algorithm as GA
from app.services.factors.mining import service as SVC


# ══════════════════════════════════════════════════════════
# 基建
# ══════════════════════════════════════════════════════════


@pytest.fixture
def run_row(db_session):
    row = FactorMiningRun(
        id="run-e2e", status="running", candidate_pool_snapshot_id="snap-1",
        data_cutoff_at=datetime(2026, 9, 17), start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 9, 1), rebalance_frequency="weekly",
        split_method="ratio", split_algorithm_version="split-1.0.0",
        max_generation=3, total_trials=0,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _evaluator(item):
    """朴素评估：公式越短 ICIR 越低（制造确定性排序）。"""
    import math

    return {"icir": round(1.0 / (1 + len(item["formula"]) / 20), 4),
            "coverage": 0.8, "turnover": 0.2, "ic_mean": 0.02,
            "complexity": len(item["formula"]) // 5}


def _fake_final_runner(metrics: dict | None = None):
    def _run(db, *, factor_version_id, factor_values, forward_returns,
             config, created_by="local_user"):
        @dataclass
        class _Outcome:
            run_id: str
            gate_result: str
            rejection_reasons: list
            metrics: dict
            time_split: object
            coverage: object

        @dataclass
        class _Cov:
            pass

        return _Outcome(run_id=f"eval-{factor_version_id}",
                        gate_result="passed", rejection_reasons=[],
                        metrics=metrics or {"ic": 0.1, "icir": 0.4},
                        time_split=None, coverage=_Cov())
    return _run


def _fake_draft_creator():
    registry: dict = {}

    def _create(db, *, digest, formula, category=None, direction="positive",
                logic="", created_by="mining", frequency=None):
        from app.schemas.factor_library import (
            FactorDraftCreate,
            FactorVersionCreate,
        )
        from app.services.factors import factor_registry as FR
        import re as _re

        safe = _re.sub(r"[^a-z0-9_]", "_", digest.lower())[:12] or "cand"
        code = f"mining_{safe}"
        factor = FR.create_factor_draft(db, draft=FactorDraftCreate(
            code=code, name=f"挖掘候选 {digest[:12]}",
            category=category or "trend", direction="higher_better",
            created_by=created_by))
        version = FR.create_factor_version(
            db, factor_id=factor.id, request=FactorVersionCreate(
                formula_expr=formula, direction="higher_better",
                change_note=f"mining:{digest[:12]}", created_by=created_by))
        registry[digest] = version.id
        return int(version.id), code

    return _create


# ══════════════════════════════════════════════════════════
# G4 端到端
# ══════════════════════════════════════════════════════════


class TestG4EndToEnd:
    def test_pipeline_to_factor_submission(self, db_session, run_row):
        """初始种群 → GA → finalize → 提交草稿 → 入 FactorSet。"""
        # ① 初始种群（模拟 T17/T18/T22/T19 产出）与落库
        population = [
            {"formula": f"mean(close,{10 * (i + 1)})",
             "canonical_formula": f"mean(close,{10 * (i + 1)})",
             "formula_hash": f"{i:04d}" + "e" * 28,
             "source": "classic", "operation": "enumerated",
             "category": "trend", "generation": 0,
             "economic_logic": "均线趋势", "expected_direction": "positive"}
            for i in range(6)
        ]
        SVC.persist_candidates(db_session, run_id=run_row.id, generation=0,
                               ranked=population)

        # ② GA 主循环（1 代，注入朴素评估）
        res = GA.run_ga_loop(_cfg := GA.GAConfig(
            population_size=6, max_generations=1, selection_ratio=0.2,
            mutation_rate=0.55, crossover_rate=0.25, random_rate=0.20),
            population, evaluate=_evaluator)
        for gen, record in enumerate(res.history):
            SVC.persist_generation(db_session, run_id=run_row.id,
                                   generation=gen, record=record)
            SVC.sync_run_progress(db_session, run_id=run_row.id,
                                  generation=gen,
                                  total_trials=record["total_trials"])

        # ③ 最终验证（test-once；评估器注入）
        from app.services.factors.mining.contracts import MiningContext
        from app.services.factors.mining.evaluation_adapter import build_split

        ctx = MiningContext(
            run_id=run_row.id, candidate_pool_snapshot_id="snap-1",
            data_cutoff_at=datetime(2026, 9, 17), start_date=date(2026, 1, 5),
            end_date=date(2026, 4, 30), rebalance_frequency="weekly",
            target_horizon=5,
            split=build_split(all_dates=[date(2026, 1, 5) + timedelta(days=i)
                                         for i in range(560)],
                              frequency="weekly", target_horizon=5),
            purge_points=1, embargo_points=1, train_ratio=0.6,
            validation_ratio=0.2, random_seed=42, config_hash="cfg",
            split_algorithm_version="split-1.0.0",
        )
        final = SVC.finalize_run(
            db_session, ctx=ctx, run_id=run_row.id, top_k=3,
            draft_creator=_fake_draft_creator(),
            run_evaluator=_fake_final_runner())
        assert final["status"] == "succeeded"
        assert final["evaluated"] == 3

        # ④ 提交 Top 候选为因子草稿（C6）
        db_session.refresh(run_row)
        top_rows = db_session.query(FactorMiningCandidate).filter_by(
            run_id=run_row.id).order_by(
            FactorMiningCandidate.generation_icir.desc()).limit(2).all()
        submitted = [SVC.submit_candidate_as_factor(
            db_session, candidate_id=r.id) for r in top_rows]
        assert all(s["status"] in ("submitted", "already") for s in submitted)

        # ⑤ batch-review 加入 FactorSet
        from app.schemas.factor_library import FactorSetCreate

        fset = FSS.create_factor_set(db_session, payload=FactorSetCreate(
            name="E2E 精选")).factor_set
        actions = [{"candidate_id": r.id, "action": "add_to_set",
                    "factor_set_id": fset.id} for r in top_rows]
        review = SVC.batch_review_candidates(db_session, actions=actions)
        assert review["counts"]["added"] == 2, review

        # ⑥ run 收官状态
        db_session.refresh(run_row)
        assert run_row.status == "succeeded"
        assert run_row.converged == 1
        assert run_row.total_trials == len(population)

    def test_test_once_lock_in_flow(self, db_session, run_row):
        """finalize 二次调用 → `MINING_TEST_LOCKED`（test-once 贯穿全流程）。"""
        from app.schemas.errors import FactorSevenError

        population = [{"formula": f"mean(close,{20 + i})",
                       "canonical_formula": f"mean(close,{20 + i})",
                       "formula_hash": f"{i:04d}" + "f" * 28,
                       "source": "classic", "generation": 0} for i in range(4)]
        SVC.persist_candidates(db_session, run_id=run_row.id, generation=0,
                               ranked=population)
        res = GA.run_ga_loop(GA.GAConfig(population_size=4, max_generations=1,
                                         selection_ratio=0.25,
                                         mutation_rate=0.55,
                                         crossover_rate=0.25,
                                         random_rate=0.20),
                             population, evaluate=_evaluator)
        for gen, record in enumerate(res.history):
            SVC.persist_generation(db_session, run_id=run_row.id,
                                   generation=gen, record=record)
        from app.services.factors.mining.contracts import MiningContext
        from app.services.factors.mining.evaluation_adapter import build_split

        ctx = MiningContext(
            run_id=run_row.id, candidate_pool_snapshot_id="snap-1",
            data_cutoff_at=datetime(2026, 9, 17), start_date=date(2026, 1, 5),
            end_date=date(2026, 4, 30), rebalance_frequency="weekly",
            target_horizon=5,
            split=build_split(all_dates=[date(2026, 1, 5) + timedelta(days=i)
                                         for i in range(560)],
                              frequency="weekly", target_horizon=5),
            purge_points=1, embargo_points=1, train_ratio=0.6,
            validation_ratio=0.2, random_seed=42, config_hash="cfg",
            split_algorithm_version="split-1.0.0",
        )
        SVC.finalize_run(db_session, ctx=ctx, run_id=run_row.id, top_k=2,
                         draft_creator=_fake_draft_creator(),
                         run_evaluator=_fake_final_runner())
        with pytest.raises(Exception) as ei:
            SVC.finalize_run(db_session, ctx=ctx, run_id=run_row.id, top_k=2,
                             draft_creator=_fake_draft_creator(),
                             run_evaluator=_fake_final_runner())
        assert "MINING_TEST_LOCKED" in str(ei.value) or \
            getattr(ei.value, "error_code", "") == "MINING_TEST_LOCKED"


# ══════════════════════════════════════════════════════════
# 路由注册验证（🚨 任务卡坑：漏注册 → 404 且无报错）
# ══════════════════════════════════════════════════════════


def _seed(db_session, run_id: str, n: int = 1) -> list[str]:
    rows = []
    for i in range(n):
        row = FactorMiningCandidate(
            id=f"cand-e2e-{i}", run_id=run_id,
            formula_expr=f"mean(close,{20 + i})",
            canonical_formula=f"mean(close,{20 + i})",
            formula_hash=f"{i:04d}" + "c" * 28,
            generation=1, operation="elite", category="trend",
            generation_icir=0.6, economic_logic="逻辑",
            expected_direction="positive",
        )
        db_session.add(row)
        rows.append(row.id)
    db_session.commit()
    return rows


class TestRouteRegistration:
    def test_submit_and_batch_review_registered(self):
        """`api_router` 内必须存在两条路径（全局挂载带 /api/v1 前缀）。"""
        from app.api.router import api_router

        paths = {getattr(r, "path", "") for r in api_router.routes}
        assert "/api/v1/factor-mining/candidates/{candidate_id}/submit" in paths
        assert "/api/v1/factor-mining/candidates/batch-review" in paths

    def test_route_roundtrip(self, db_session, run_row):
        """路由行为冒烟：submit 经 HTTP 层成功（get_db 覆盖为测试会话）。"""
        cand_id = _seed(db_session, run_row.id, n=1)[0]

        from fastapi import FastAPI

        from app.api.routes import factor_mining as FM
        from app.db.session import get_db

        app = FastAPI()
        app.include_router(FM.router)
        app.dependency_overrides[get_db] = lambda: db_session
        client = TestClient(app)

        resp = client.post(f"/factor-mining/candidates/{cand_id}/submit")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "submitted"

        resp = client.post("/factor-mining/candidates/batch-review",
                           json={"actions": [
                               {"candidate_id": cand_id,
                                "action": "add_to_set",
                                "factor_set_id": "fs-nonexistent"}]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["results"][0]["status"] == "failed"
