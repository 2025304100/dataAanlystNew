"""端到端集成档：**零桩**走完「挖掘收官 → 分级 → 证据 → 人工调级」全链。

为什么存在（2026-09-25 全面测试报告 DEF-9/15/16 的回归哨兵）：
  既有真实 GA 档（test_m1_closure.py）用 `finalize_top_k: 0` 显式跳过最终验证，
  于是「finalize → evaluate_full 默认评估器 → grade 回填 → 证据契约」整段
  没有任何不注入桩的覆盖 —— 结果 evaluate_full 生产路径把 factor_values=None
  直透 run_evaluation（None.index 崩）、证据接口契约与前端类型脱节，
  两个 P1/P2 都在单测全绿的情况下上线后被黑盒实测抓获。

本档断言链条（任何一环断掉都会在这里红）：
  ① POST /runs **不传** finalize_top_k → 默认必须执行最终验证（DEF-9）
  ② run succeeded 后 ≥1 个候选带 factor_version_id + grade
  ③ GET  evidence 返回**前端 GradeEvidence 契约形状**（DEF-16）
  ④ POST manual：短理由 4xx / 合法理由 200 + manual_adjusted + 历史留痕
  ⑤ POST restore-auto → manual_adjusted 回落 False

跑法：`.venv/Scripts/python.exe -m pytest tests/services/factors/mining/test_mining_finalize_evidence_e2e.py -q`
（真实 DuckDB fixture + 真实评估器，约 1~3 分钟）
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("duckdb")

from test_m1_closure import (  # fixtures 复用（同目录 import 即被 pytest 收集）
    mining_env,
    warehouse_path,
    _wait_run_terminal,
)

from app.models.factor_mining import FactorMiningCandidate, FactorMiningRun
from app.models.mining_candidate_pool import (
    TrainingCandidatePool,
    TrainingCandidatePoolSnapshot,
)

pytestmark = pytest.mark.whitebox

_APP = None


def _client(db_session):
    from fastapi import FastAPI

    from app.api.routes import factor_mining as FM
    from app.db.session import get_db

    app = FastAPI()
    app.include_router(FM.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _seed_locked_snapshot(db_session, warehouse_path: str):
    """DEF-2 上线后提交必须引用真实锁定快照。"""
    db_session.merge(TrainingCandidatePool(
        id="pool-e2e", name="E2E候选池", source_type="filter",
        status="frozen", member_count=0,
    ))
    db_session.merge(TrainingCandidatePoolSnapshot(
        id="snap-e2e", pool_id="pool-e2e", members_json="[]",
        rule_hash="rh-e2e", data_cutoff_at=datetime(2027, 3, 1),
        is_locked=1, member_count=0,
    ))
    db_session.commit()


def test_finalize_grade_evidence_full_chain(mining_env, warehouse_path):
    db_session = mining_env
    client = _client(db_session)
    _seed_locked_snapshot(db_session, warehouse_path)

    # ── ① 不传 finalize_top_k：默认必须收官（DEF-9 的核心回归点） ──
    resp = client.post("/factor-mining/runs", json={
        "candidate_pool_snapshot_id": "snap-e2e",
        "data_cutoff_at": "2027-03-01T00:00:00",
        "start_date": "2026-01-05T00:00:00",
        "end_date": "2027-02-01T00:00:00",
        "rebalance_frequency": "daily",
        "target_horizon": 5,
        "random_seed": 3,
        "evolution_params": {
            "population_size": 6, "max_generations": 1,
            "convergence_threshold": -1.0, "convergence_generations": 100,
            "random_seed": 3,
        },
        "filter_config": {
            "selected_fields": ["close", "open", "high", "low", "volume"],
            "warehouse_path": warehouse_path,
        },
    })
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]

    # ── ② 终态 + finalize 回填 ──
    run = _wait_run_terminal(db_session, run_id, timeout=420.0)
    assert run.status == "succeeded", f"{run.status} / {run.error_code}"
    cands = db_session.query(FactorMiningCandidate).filter_by(run_id=run_id).all()
    assert cands, "候选必须落库"
    finalized = [c for c in cands if c.factor_version_id]
    assert finalized, (
        "DEF-9 回归：run succeeded 但没有任何候选回填 factor_version_id —— "
        "默认 finalize 未执行或 evaluate_full 生产路径再次断裂")

    # ──  证据接口契约形状（DEF-16 回归点：字段名/类型与前端 GradeEvidence 对齐） ──
    target = next((c for c in finalized if c.generation_icir is not None),
                  finalized[0])
    r_ev = client.get(f"/factor-mining/candidates/{target.id}/grade/evidence")
    assert r_ev.status_code == 200, r_ev.text
    ev = r_ev.json()
    for key in ("candidate_id", "formula", "grade", "reason_zh",
                "thresholds_source", "frequency", "dimensions", "lineage",
                "manual_adjusted", "grade_history"):
        assert key in ev, f"证据契约缺字段 {key}（前端 normalize 依赖）"
    assert str(ev["grade"]) in ("S", "A", "B", "C", "D")
    assert isinstance(ev["dimensions"], list) and ev["dimensions"], \
        "dimensions 必须给出逐维明细（哪怕 current=null，前端要渲染达标表）"
    assert isinstance(ev["manual_adjusted"], bool)
    assert isinstance(ev["grade_history"], list)

    # ──  人工调级：短理由拒绝 / 合法理由成功并留痕 ──
    r_short = client.post(
        f"/factor-mining/candidates/{target.id}/grade/manual",
        json={"grade": "A", "reason": "太短"})
    assert 400 <= r_short.status_code < 500, "短理由必须被拒绝"

    r_manual = client.post(
        f"/factor-mining/candidates/{target.id}/grade/manual",
        json={"grade": "A", "reason": "E2E：样本外表现与经济性人工复核后上调，留痕可审计。"})
    assert r_manual.status_code == 200, r_manual.text
    body = r_manual.json()
    assert int(body.get("manual_adjusted") or 0) == 1

    r_ev2 = client.get(f"/factor-mining/candidates/{target.id}/grade/evidence")
    ev2 = r_ev2.json()
    assert ev2["manual_adjusted"] is True, "调级后证据应反映人工态"
    assert len(ev2["grade_history"]) >= 1, "调级必须写入历史时间轴"

    # ── ⑤ 恢复自动 ──
    r_auto = client.post(
        f"/factor-mining/candidates/{target.id}/grade/restore-auto")
    assert r_auto.status_code == 200, r_auto.text
    ev3 = client.get(
        f"/factor-mining/candidates/{target.id}/grade/evidence").json()
    assert ev3["manual_adjusted"] is False, "恢复自动后 manual_adjusted 必须回落"
