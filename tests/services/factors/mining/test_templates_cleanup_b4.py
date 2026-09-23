"""B4 · drafts / templates / cleanup 路由族。

DoD 对照（开发计划 B4 卡）：
- 草稿（多份并存/暂存/恢复 + prepare 最终准备校验）→ TestDrafts
- 模板（25 入表 + CRUD/复制/启停 + save-as-template 消费）→ TestTemplates
- 中间数据 7 天保留 + POST /runs/{id}/cleanup → TestCleanup

跑法：`.venv/Scripts/python.exe -m pytest tests/services/factors/mining/test_templates_cleanup_b4.py -q`
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.factor_mining import (
    FactorMiningCandidate,
    FactorMiningDraft,
    FactorMiningGeneration,
    FactorMiningRun,
    FactorMiningTemplate,
)
from app.services.factors.mining import service as SVC
from app.services.factors.mining import template_service as TPL


def _client(db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import factor_mining as FM
    from app.db.session import get_db

    app = FastAPI()
    app.include_router(FM.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


# ══════════════════════════════════════════════════════════
# 1) 草稿（多份并存 / prepare）
# ══════════════════════════════════════════════════════════


class TestDrafts:
    def test_save_list_get_and_status(self, db_session):
        client = _client(db_session)
        r1 = client.post("/factor-mining/drafts", json={
            "name": "草稿A", "current_step": 2,
            "steps": {"step1": {"pool": "x"}, "step2": {"start_date": "2026-01-01"}},
        })
        assert r1.status_code == 201
        draft_id = r1.json()["draft_id"]
        r2 = client.post("/factor-mining/drafts", json={
            "name": "草稿B", "current_step": 1, "steps": {},
        })
        r2_id = r2.json()["draft_id"]
        assert r2_id != draft_id          # 多份并存

        lst = client.get("/factor-mining/drafts").json()
        assert len(lst) >= 2                # 草稿列表为数组（多份并存）

        got = client.get(f"/factor-mining/drafts/{draft_id}")
        assert got.status_code == 200 and got.json()["name"] == "草稿A"

        st = client.post(f"/factor-mining/drafts/{draft_id}/status",
                         json={"status": "waiting_data_recheck"})
        assert st.status_code == 200
        assert st.json()["status"] == "waiting_data_recheck"

    def test_prepare_blocks_incomplete_draft(self, db_session):
        client = _client(db_session)
        r = client.post("/factor-mining/drafts", json={
            "name": "未完成", "current_step": 4,
            "steps": {"step2": {"start_date": "2026-01-01"}},
        })
        draft_id = r.json()["draft_id"]
        prep = client.post(f"/factor-mining/drafts/{draft_id}/prepare")
        assert prep.status_code == 200
        body = prep.json()
        assert body["ok"] is False
        assert any("Step" in b or "快照" in b or "校验" in b for b in body["blockers"])

    def test_prepare_missing_draft_404(self, db_session):
        client = _client(db_session)
        assert client.post("/factor-mining/drafts/nope/prepare").status_code == 404


# ══════════════════════════════════════════════════════════
# 2) 模板（25 入表 + CRUD/复制/启停）
# ══════════════════════════════════════════════════════════


class TestTemplates:
    def test_seed_is_25_and_idempotent(self, db_session):
        res1 = TPL.seed_system_templates(db_session)
        n = db_session.query(FactorMiningTemplate).filter_by(scope="system").count()
        assert n >= 25 and res1["created"] == n
        res2 = TPL.seed_system_templates(db_session)
        assert res2["created"] == 0            # 幂等

    def test_crud_copy_enabled(self, db_session):
        TPL.seed_system_templates(db_session)
        lst = TPL.list_templates(db_session, scope="system")
        assert lst["total"] >= 25
        tpl_id = lst["items"][0]["template_id"]
        assert lst["items"][0]["rule_config"]["formula"]

        # 复制 → 个人副本
        copy = TPL.copy_template(db_session, template_id=tpl_id, owner="tester")
        assert copy["scope"] == "personal"
        assert "副本" in copy["name"]

        # 启停
        off = TPL.set_template_enabled(db_session, template_id=tpl_id, enabled=0)
        assert off["enabled"] == 0
        on = TPL.set_template_enabled(db_session, template_id=tpl_id, enabled=1)
        assert on["enabled"] == 1

        # 个人创建（缺 formula → 422）
        created = TPL.create_personal_template(
            db_session, name="自定义", description=None,
            rule_config={"formula": "mean(close,{n1})",
                         "params": {"n1": [5, 10]}}, owner="tester")
        assert created["scope"] == "personal"
        with pytest.raises(ValueError):
            TPL.create_personal_template(db_session, name="坏",
                                         description=None, rule_config={}, owner="t")

    def test_http_seed_list(self, db_session):
        client = _client(db_session)
        seed = client.post("/factor-mining/templates/seed")
        assert seed.status_code == 200 and seed.json()["total_system"] >= 25
        lst = client.get("/factor-mining/templates?scope=system")
        assert lst.status_code == 200 and lst.json()["total"] >= 25
        tpl_id = lst.json()["items"][0]["template_id"]
        assert client.get(f"/factor-mining/templates/{tpl_id}").status_code == 200
        assert client.get("/factor-mining/templates/nope").status_code == 404


# ══════════════════════════════════════════════════════════
# 3) 清理（run 中间数据 + 7 天草稿保留）
# ══════════════════════════════════════════════════════════


def _seed_run_with_children(db_session, run_id: str):
    run = FactorMiningRun(
        id=run_id, status="running", candidate_pool_snapshot_id="snap-b4",
        data_cutoff_at=datetime(2026, 12, 1), start_date=datetime(2026, 1, 5),
        end_date=datetime(2026, 11, 1), rebalance_frequency="daily",
        split_method="ratio", split_algorithm_version="split-1.0.0",
        target_horizon=5, random_seed=7,
    )
    db_session.add(run)
    db_session.flush()
    for i in range(2):
        db_session.add(FactorMiningCandidate(
            id=f"{run_id}-c{i}", run_id=run_id,
            formula_expr="mean(close,5)", canonical_formula="mean(close,5)",
            formula_hash=f"e{i:03d}" + "x" * 29, operation="elite",
            expected_direction="positive"))
    db_session.add(FactorMiningGeneration(
        run_id=run_id, generation=1, population_size=2, probe_data_load_ms=0,
        probe_g2_hit_rate=0.0, cache_validation_passed=1, cache_validation_max_diff=0.0,
        mutation_count=1, crossover_count=0, random_count=0, elite_count=1,
    ))
    db_session.commit()


def _old_ts() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TestCleanup:
    def test_cleanup_run_deletes_intermediates(self, db_session):
        client = _client(db_session)
        _seed_run_with_children(db_session, "run-b4c")
        resp = client.post("/factor-mining/runs/run-b4c/cleanup")
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_candidates"] == 2
        assert body["deleted_generations"] == 1
        assert db_session.get(FactorMiningRun, "run-b4c") is None
        assert db_session.query(FactorMiningCandidate).filter_by(
            run_id="run-b4c").count() == 0
        # 二次清理 → 404
        assert client.post("/factor-mining/runs/run-b4c/cleanup").status_code == 404

    def test_purge_stale_drafts_keeps_fresh(self, db_session):
        old = FactorMiningDraft(
            id="draft-old", name="old", status="draft",
            updated_at=_old_ts() - timedelta(days=30), owner="t")
        fresh = FactorMiningDraft(
            id="draft-fresh", name="fresh", status="draft",
            updated_at=_old_ts(), owner="t")
        submitted = FactorMiningDraft(
            id="draft-sub", name="sub", status="invalidated",
            updated_at=_old_ts() - timedelta(days=30), owner="t")
        db_session.add_all([old, fresh, submitted])
        db_session.commit()

        res = SVC.purge_stale_mining_drafts(db_session, days=7)
        assert res["purged"] == 1
        assert db_session.get(FactorMiningDraft, "draft-old") is None
        assert db_session.get(FactorMiningDraft, "draft-fresh") is not None
        assert db_session.get(FactorMiningDraft, "draft-sub") is not None