"""A3 · factor-mining 核心路由契约测试（19 处占位清零 + 注册 + 行为冒烟）。

DoD 对照（开发计划 A3 卡）：
- factor_mining.py 的 `NotImplementedError` 归零            → test_1（静态断言）
- 每路由有 DTO 校验 + 契约测试                             → 本文件
- R5 分层纪律（路由不计算）由 test_3/7 的桩注入佐证          → 业务全部委托 service/runner

跑法：`.venv/Scripts/python.exe -m pytest tests/services/factors/mining/test_factor_mining_routes.py -q`
"""
from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("duckdb")

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.factor_mining import (
    FactorMiningCandidate,
    FactorMiningGeneration,
    FactorMiningRun,
)
from app.api.routes import factor_mining as FM
from app.api.routes.factor_mining import mining_runner
from app.db.session import get_db

pytestmark = pytest.mark.whitebox


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(FM.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _seed_run(db_session, run_id: str = "run-c1", status: str = "running"):
    row = FactorMiningRun(
        id=run_id, status=status, candidate_pool_snapshot_id="snap-c1",
        data_cutoff_at=datetime(2026, 11, 10), start_date=datetime(2026, 1, 5),
        end_date=datetime(2026, 11, 1), rebalance_frequency="daily",
        split_method="ratio", split_algorithm_version="split-1.0.0",
        target_horizon=5, max_generation=3, random_seed=7,
        current_generation=1, total_trials=24,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _seed_candidate(db_session, run_id: str, cand_id: str = "cand-c1",
                    *, parent_ids=None):
    row = FactorMiningCandidate(
        id=cand_id, run_id=run_id, formula_expr="mean(close,5)/mean(close,20)-1",
        canonical_formula="mean(close,5)/mean(close,20)-1",
        formula_hash="c" + "e" * 31, generation=1, operation="elite",
        category="trend", generation_icir=0.35, generation_coverage=0.9,
        generation_complexity=3, economic_logic="均线趋势",
        expected_direction="positive",
        parent_ids_json=_safe_json(parent_ids),
    )
    db_session.add(row)
    db_session.commit()
    return row


def _safe_json(values):
    import json
    return json.dumps(list(values or []))


class TestRoutesContract:
    def test_source_has_no_not_implemented(self):
        src = pathlib.Path(FM.__file__).read_text(encoding="utf-8")
        assert "NotImplementedError" not in src.replace("策略实现", "")

    def test_core_routes_registered(self):
        from app.api.router import api_router

        paths = {getattr(r, "path", "") for r in api_router.routes}
        needed = [
            "/api/v1/factor-mining/runs",
            "/api/v1/factor-mining/runs/{run_id}",
            "/api/v1/factor-mining/runs/{run_id}/cancel",
            "/api/v1/factor-mining/runs/{run_id}/pause",
            "/api/v1/factor-mining/runs/{run_id}/resume",
            "/api/v1/factor-mining/runs/{run_id}/stop",
            "/api/v1/factor-mining/runs/{run_id}/discard",
            "/api/v1/factor-mining/runs/{run_id}/generations",
            "/api/v1/factor-mining/runs/{run_id}/candidates",
            "/api/v1/factor-mining/runs/{run_id}/candidates/{candidate_id}/lineage",
            "/api/v1/factor-mining/ai/generate-preview",
            "/api/v1/factor-mining/locks/status",
            "/api/v1/factor-mining/evaluations",
            "/api/v1/factor-mining/runs/{run_id}/evaluate-all",
            "/api/v1/factor-mining/evaluations/{evaluation_id}",
            "/api/v1/factor-mining/candidates/{candidate_id}/save-as-template",
            "/api/v1/factor-mining/candidates/{candidate_id}/grade/manual",
            "/api/v1/factor-mining/candidates/{candidate_id}/grade/restore-auto",
            "/api/v1/factor-mining/candidates/{candidate_id}/grade/evidence",
        ]
        missing = [p for p in needed if p not in paths]
        assert not missing, f"缺失路由: {missing}"


class TestRunsApi:
    def test_create_run_submits_with_contract(self, db_session, monkeypatch):
        @dataclass
        class _Ret:
            task_id: str = "task-c1"
            run_id: str = "run-any"
            status: str = "queued"
            queue_position: int = 0
            started: bool = True

            def to_dict(_self):
                return {"task_id": _self.task_id, "run_id": _self.run_id,
                        "status": _self.status,
                        "queue_position": _self.queue_position,
                        "started": _self.started,
                        "started_hint_zh": "已启动"}

        monkeypatch.setattr(mining_runner, "submit_mining_run",
                            lambda payload, run_id: _Ret(run_id=run_id))
        client = _client(db_session)
        resp = client.post("/factor-mining/runs", json={
            "candidate_pool_snapshot_id": "snap-c1",
            "data_cutoff_at": "2026-11-10T00:00:00",
            "start_date": "2026-01-05T00:00:00",
            "end_date": "2026-11-01T00:00:00",
            "rebalance_frequency": "daily",
            "target_horizon": 5,
        })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        for key in ("run_id", "task_id", "queue_position"):
            assert key in body

    def test_list_and_get_run(self, db_session):
        _seed_run(db_session)
        client = _client(db_session)
        r1 = client.get("/factor-mining/runs")
        assert r1.status_code == 200
        page = r1.json()
        assert page["total"] >= 1 and "items" in page

        r2 = client.get("/factor-mining/runs/run-c1")
        assert r2.status_code == 200
        body = r2.json()
        assert body["id"] == "run-c1"
        assert "lock" in body

    def test_run_actions_migrate_status(self, db_session):
        _seed_run(db_session, status="running")
        client = _client(db_session)
        for action, expect in (("pause", "paused"), ("cancel", "cancelled"),
                               ("stop", "converged")):
            resp = client.post(f"/factor-mining/runs/run-c1/{action}")
            assert resp.status_code == 200, (action, resp.text)
            assert resp.json()["status"] == expect


class TestSplitBudgetApi:
    """P1-9：POST /factor-mining/split-budget 切分预算。

    用 monkeypatch 固定数仓交易日序列（两周 10 个工作日），验证：
    - 日频 total_points=10（全部交易日）；
    - 周频重采样 → 每周最后交易日（2 点）；
    - 月频重采样 → 每月最后交易日（1 点）；
    - 数仓/series 不可用 → available=False + reason_zh 降级。
    """

    def _client(self, db_session):
        return _client(db_session)

    @staticmethod
    def _weekdays() -> list[date]:
        # 2026-01-05(周一) 起两周，每周 5 个交易日
        days: list[date] = []
        base = date(2026, 1, 5)
        for i in range(10):
            days.append(base + timedelta(days=i))
        return days

    @staticmethod
    def _patch_trade_dates(monkeypatch, days: list[date]) -> None:
        fixed = list(days)
        monkeypatch.setattr(
            "app.services.factors.store.FactorWarehouse.list_trade_dates",
            lambda self: fixed,
        )

    def test_daily_budget(self, db_session, monkeypatch):
        self._patch_trade_dates(monkeypatch, self._weekdays())
        resp = self._client(db_session).post("/factor-mining/split-budget", json={
            "start_date": "2026-01-01T00:00:00",
            "end_date": "2026-01-31T00:00:00",
            "frequency": "daily",
            "target_horizon": 5,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is True
        assert body["total_points"] == 10
        assert "meets_floor" in body and "purge_trading_days" in body

    def test_weekly_resample(self, db_session, monkeypatch):
        self._patch_trade_dates(monkeypatch, self._weekdays())
        resp = self._client(db_session).post("/factor-mining/split-budget", json={
            "start_date": "2026-01-01T00:00:00",
            "end_date": "2026-01-31T00:00:00",
            "frequency": "weekly",
            "target_horizon": 5,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 每周只保留最后交易日 → 2 个调仓点
        assert body["total_points"] == 2

    def test_monthly_resample(self, db_session, monkeypatch):
        self._patch_trade_dates(monkeypatch, self._weekdays())
        resp = self._client(db_session).post("/factor-mining/split-budget", json={
            "start_date": "2026-01-01T00:00:00",
            "end_date": "2026-01-31T00:00:00",
            "frequency": "monthly",
            "target_horizon": 5,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["total_points"] == 1

    def test_degraded_when_warehouse_empty(self, db_session, monkeypatch):
        self._patch_trade_dates(monkeypatch, [])
        resp = self._client(db_session).post("/factor-mining/split-budget", json={
            "start_date": "2026-01-01T00:00:00",
            "end_date": "2026-01-31T00:00:00",
            "frequency": "daily",
            "target_horizon": 5,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is False
        assert body["reason_zh"]


class TestResultsApi:
    def test_generations_candidates_lineage(self, db_session):
        _seed_run(db_session)
        _seed_candidate(db_session, "run-c1")
        gen = FactorMiningGeneration(
            run_id="run-c1", generation=0, population_size=6,
            best_icir=0.3, avg_icir=0.2, median_icir=0.21,
            probe_data_load_ms=12,
        )
        db_session.add(gen)
        db_session.commit()

        client = _client(db_session)
        g = client.get("/factor-mining/runs/run-c1/generations")
        assert g.status_code == 200 and len(g.json()) == 1
        assert g.json()[0]["probe_data_load_ms"] == 12

        c = client.get("/factor-mining/runs/run-c1/candidates")
        assert c.status_code == 200 and c.json()["total"] == 1

        l = client.get("/factor-mining/runs/run-c1/candidates/cand-c1/lineage")
        assert l.status_code == 200
        assert l.json()["id"] == "cand-c1"

    def test_ai_preview_delegates(self, db_session, monkeypatch):
        client = _client(db_session)
        monkeypatch.setattr(
            "app.api.routes.factor_mining.mining_service.ai_preview_skeletons",
            lambda payload, db: {"skeletons": [{"formula": "x"}],
                                 "stats": {"accepted": 1}})
        resp = client.post("/factor-mining/ai/generate-preview",
                           json={"count": 3})
        assert resp.status_code == 200
        assert resp.json()["skeletons"][0]["formula"] == "x"


class TestEvaluationsApi:
    def test_evaluate_all_delegates(self, db_session, monkeypatch):
        _seed_run(db_session)
        client = _client(db_session)
        monkeypatch.setattr(
            "app.api.routes.factor_mining.mining_service.evaluate_all_impl",
            lambda db, **kw: {"evaluated": 1, "failed": 0, "status": "succeeded"})
        resp = client.post("/factor-mining/runs/run-c1/evaluate-all", json={"top_n": 5})
        assert resp.status_code == 200
        assert resp.json()["status"] == "succeeded"

    def test_create_evaluation_requires_submitted_candidate(self, db_session):
        _seed_run(db_session)
        _seed_candidate(db_session, "run-c1")
        client = _client(db_session)
        resp = client.post("/factor-mining/evaluations",
                           json={"candidate_id": "cand-c1"})
        assert resp.status_code == 409
        assert resp.json()["detail"]["error_code"] == "CANDIDATE_NOT_SUBMITTED"


class TestTemplateAndGradeApi:
    def test_save_as_template_and_grade_evidence(self, db_session):
        _seed_run(db_session)
        _seed_candidate(db_session, "run-c1")
        client = _client(db_session)

        t = client.post("/factor-mining/candidates/cand-c1/save-as-template")
        assert t.status_code == 200
        assert t.json()["status"] == "created"

        g = client.post("/factor-mining/candidates/cand-c1/grade/manual",
                        json={"grade": "B", "reason": "样本量有限，建议保守观察定级"})
        # 候选未提交（无 factor_version_id）→ 结构化 409
        assert g.status_code == 409
        assert g.json()["detail"]["error_code"] == "CANDIDATE_NOT_SUBMITTED"

        e = client.get("/factor-mining/candidates/cand-c1/grade/evidence")
        assert e.status_code == 200
        assert e.json()["grade"] in ("S", "A", "B", "C", "D")
        assert e.json()["lineage"]["id"] == "cand-c1"

    def test_manual_grade_reason_required(self, db_session):
        _seed_run(db_session)
        _seed_candidate(db_session, "run-c1")
        client = _client(db_session)
        resp = client.post("/factor-mining/candidates/cand-c1/grade/manual",
                           json={"grade": "A", "reason": "短"})
        assert resp.status_code == 422  # Pydantic min_length=10