"""T25 · 候选转正式因子 + 批量操作 契约测试（DoD）。

覆盖
====
- `submit_candidate_as_factor`：C6 草稿链（draft + version）、幂等、回填
- `batch_review_candidates`：approve / reject / add_to_set 三动作逐项处理、
  单项失败不中断批、frozen FactorSet 不可写（`set_not_mutable`）、重复加入跳过
- 路由（`factor_mining.py`）：submit / batch-review 两端点行为 + **注册验证**
  （`api_router` 内确实存在这两个路径 —— 任务卡坑「漏注册 → 404 无报错」）
"""
from __future__ import annotations

import math
from datetime import datetime

import pytest

from app.models.factor_mining import (
    FactorMiningCandidate,
    FactorMiningGeneration,
    FactorMiningRun,
)
from app.services.factors import factor_set_service as FSS
from app.services.factors.mining import service as SVC


@pytest.fixture
def run_row(db_session):
    import uuid as _uuid

    row = FactorMiningRun(
        id=f"run-t25-{_uuid.uuid4().hex[:8]}", status="succeeded",
        candidate_pool_snapshot_id="snap-1",
        data_cutoff_at=datetime(2026, 9, 17), start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 9, 1), rebalance_frequency="weekly",
        split_method="ratio", split_algorithm_version="split-1.0.0",
        max_generation=5, total_trials=30, converged=1,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _seed(db_session, run_id: str, n: int = 2) -> list[str]:
    ids = []
    for i in range(n):
        row = FactorMiningCandidate(
            id=f"cand-{run_id}-{i}", run_id=run_id,
            formula_expr=f"mean(close,{20 + i})",
            canonical_formula=f"mean(close,{20 + i})",
            formula_hash=f"{run_id[-12:]}{i:04d}" + "a" * 20,
            generation=1, operation="elite", category="trend",
            generation_icir=0.6 - 0.1 * i,
            economic_logic="动量逻辑", expected_direction="positive",
        )
        db_session.add(row)
        ids.append(row.id)
    db_session.commit()
    return ids


# ══════════════════════════════════════════════════════════
# 1. submit_candidate_as_factor（C6 链路）
# ══════════════════════════════════════════════════════════


class TestSubmitCandidate:
    def test_submit_creates_draft_and_version(self, db_session, run_row):
        """🚨 红线 C6：必须走 factor_registry（产物 = draft 因子 + valid 版本）。"""
        cand_id = _seed(db_session, run_row.id, n=1)[0]
        out = SVC.submit_candidate_as_factor(db_session, candidate_id=cand_id)
        assert out["status"] == "submitted"
        assert out["factor_code"].startswith("mining_")

        from app.models.factor import Factor
        from app.models.factor_model import FactorVersion

        factor = db_session.query(Factor).filter_by(
            code=out["factor_code"]).one()
        assert factor.lifecycle_status == "draft", "必须是草稿（不创建 active）"
        version = db_session.query(FactorVersion).filter_by(
            id=out["factor_version_id"]).one()
        assert version.formula_expr == "mean(close,20)"
        assert version.validation_status == "valid", "编译成功 → valid"

        # 回填候选
        cand = db_session.get(FactorMiningCandidate, cand_id)
        db_session.refresh(cand)
        assert cand.factor_version_id == out["factor_version_id"]
        assert cand.latest_evaluation_status == "submitted"

    def test_idempotent_second_submit(self, db_session, run_row):
        cand_id = _seed(db_session, run_row.id, n=1)[0]
        first = SVC.submit_candidate_as_factor(db_session, candidate_id=cand_id)
        second = SVC.submit_candidate_as_factor(db_session, candidate_id=cand_id)
        assert first["status"] == "submitted"
        assert second["status"] == "already"
        assert second["factor_version_id"] == first["factor_version_id"]

    def test_unknown_candidate(self, db_session, run_row):
        with pytest.raises(ValueError, match="candidate_not_found"):
            SVC.submit_candidate_as_factor(db_session, candidate_id="nope")

    def test_code_collision_across_candidates(self, db_session, run_row):
        """两个不同候选若哈希前 12 位相同 → 第二个复用既有草稿版本（幂等）。"""
        ids = _seed(db_session, run_row.id, n=2)
        first = SVC.submit_candidate_as_factor(db_session, candidate_id=ids[0])
        # 把第二个候选的哈希改成与第一个同前缀（模拟同哈希冲突）
        cand2 = db_session.get(FactorMiningCandidate, ids[1])
        # 同前缀（前 12 位）但完整哈希不同 → 触发 code 冲突而非唯一约束冲突
        cand2.formula_hash = cand2.formula_hash[:12] + "b" * 28
        db_session.commit()
        second = SVC.submit_candidate_as_factor(db_session, candidate_id=ids[1])
        assert second["status"] == "submitted"
        # code 冲突 → 复用同一个 **Factor**；公式不同 → 新建版本（version_id 不同）
        assert second["factor_version_id"] != first["factor_version_id"]
        from app.models.factor import Factor
        from app.models.factor_model import FactorVersion

        f1 = db_session.query(Factor).filter_by(
            code=first["factor_code"]).one()
        versions = db_session.query(FactorVersion).filter_by(
            factor_id=f1.id).all()
        assert len(versions) == 2
        assert {v.id for v in versions} == {
            first["factor_version_id"], second["factor_version_id"]}


# ══════════════════════════════════════════════════════════
# 2. batch_review_candidates
# ══════════════════════════════════════════════════════════


class TestBatchReview:
    def test_approve_reject_mixed(self, db_session, run_row):
        ids = _seed(db_session, run_row.id, n=3)
        result = SVC.batch_review_candidates(db_session, actions=[
            {"candidate_id": ids[0], "action": "approve"},
            {"candidate_id": ids[1], "action": "reject",
             "reason": "经济逻辑不成立"},
            {"candidate_id": ids[2], "action": "approve"},
        ])
        assert result["counts"]["approved"] == 2
        assert result["counts"]["rejected"] == 1
        cand2 = db_session.get(FactorMiningCandidate, ids[1])
        assert cand2.elimination_status == "manual_rejected"

    def test_unknown_action_failed(self, db_session, run_row):
        ids = _seed(db_session, run_row.id, n=1)
        result = SVC.batch_review_candidates(db_session, actions=[
            {"candidate_id": ids[0], "action": "teleport"}])
        assert result["counts"]["failed"] == 1

    def test_unknown_candidate_failed_not_fatal(self, db_session, run_row):
        ids = _seed(db_session, run_row.id, n=1)
        result = SVC.batch_review_candidates(db_session, actions=[
            {"candidate_id": "ghost", "action": "approve"},
            {"candidate_id": ids[0], "action": "approve"},
        ])
        statuses = {r["candidate_id"]: r["status"] for r in result["results"]}
        assert statuses["ghost"] == "failed"
        assert statuses[ids[0]] == "submitted"


# ══════════════════════════════════════════════════════════
# 3. add_to_set（FactorSet 联动）
# ══════════════════════════════════════════════════════════


@pytest.fixture
def factor_set(db_session):
    from app.schemas.factor_library import FactorSetCreate

    result = FSS.create_factor_set(db_session, payload=FactorSetCreate(
        name="挖掘精选", description="T25 测试"))
    return result.factor_set


class TestAddToSet:
    def test_add_after_submit(self, db_session, run_row, factor_set):
        """🚨 任务卡坑 2：版本 `validation_status="valid"` ∈ 允许集 → 可入组。"""
        ids = _seed(db_session, run_row.id, n=1)
        SVC.submit_candidate_as_factor(db_session, candidate_id=ids[0])
        result = SVC.batch_review_candidates(db_session, actions=[
            {"candidate_id": ids[0], "action": "add_to_set",
             "factor_set_id": factor_set.id}])
        assert result["counts"]["added"] == 1, result

    def test_not_submitted_rejected(self, db_session, run_row, factor_set):
        """未提交的候选不能入组（无 factor_version_id）。"""
        ids = _seed(db_session, run_row.id, n=1)
        result = SVC.batch_review_candidates(db_session, actions=[
            {"candidate_id": ids[0], "action": "add_to_set",
             "factor_set_id": factor_set.id}])
        item = result["results"][0]
        assert item["status"] == "failed"
        assert item["error_code"] == "not_submitted"

    def test_duplicate_add_skipped(self, db_session, run_row, factor_set):
        """同 factor_id 重复加入 → 被拒（唯一约束）→ 第二次计 failed/skip。"""
        ids = _seed(db_session, run_row.id, n=1)
        SVC.submit_candidate_as_factor(db_session, candidate_id=ids[0])
        first = SVC.batch_review_candidates(db_session, actions=[
            {"candidate_id": ids[0], "action": "add_to_set",
             "factor_set_id": factor_set.id}])
        second = SVC.batch_review_candidates(db_session, actions=[
            {"candidate_id": ids[0], "action": "add_to_set",
             "factor_set_id": factor_set.id}])
        assert first["counts"]["added"] == 1
        assert second["counts"]["added"] == 0

    def test_frozen_set_not_writable(self, db_session, run_row, factor_set):
        """🚨 任务卡坑 3：frozen FactorSet 不可写（`set_not_mutable` 按项返回）。"""
        factor_set.status = "frozen"
        db_session.commit()
        ids = _seed(db_session, run_row.id, n=1)
        SVC.submit_candidate_as_factor(db_session, candidate_id=ids[0])
        result = SVC.batch_review_candidates(db_session, actions=[
            {"candidate_id": ids[0], "action": "add_to_set",
             "factor_set_id": factor_set.id}])
        item = result["results"][0]
        assert item["status"] == "failed"
        assert item["error_code"] == "set_not_mutable"


# ══════════════════════════════════════════════════════════
# 4. 路由（实现 + **注册验证** —— 任务卡坑「漏注册 → 404 无报错」）
# ══════════════════════════════════════════════════════════


@pytest.fixture
def api_client(db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import factor_mining as FM
    from app.db.session import get_db

    app = FastAPI()
    app.include_router(FM.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


class TestRoutes:
    def test_submit_route(self, api_client, db_session, run_row):
        cand_id = _seed(db_session, run_row.id, n=1)[0]
        resp = api_client.post(f"/factor-mining/candidates/{cand_id}/submit")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "submitted"

    def test_batch_review_route(self, api_client, db_session, run_row):
        ids = _seed(db_session, run_row.id, n=1)
        resp = api_client.post("/factor-mining/candidates/batch-review",
                               json={"actions": [
                                   {"candidate_id": ids[0],
                                    "action": "approve"}]})
        assert resp.status_code == 200
        assert resp.json()["counts"]["approved"] == 1

    def test_batch_review_route_empty_actions_400(self, api_client):
        resp = api_client.post("/factor-mining/candidates/batch-review",
                               json={"actions": []})
        assert resp.status_code == 400

    def test_submit_unknown_candidate_404(self, api_client):
        resp = api_client.post("/factor-mining/candidates/ghost/submit")
        assert resp.status_code == 404

    def test_router_registration_in_api_router(self):
        """🚨 任务卡坑：漏注册 → 404 且无报错。直接断言 `api_router` 内存在两条路径。"""
        from app.api.router import api_router

        paths = {getattr(r, "path", "") for r in api_router.routes}
        assert "/api/v1/factor-mining/candidates/{candidate_id}/submit" in paths
        assert "/api/v1/factor-mining/candidates/batch-review" in paths
