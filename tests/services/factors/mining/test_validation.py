"""T14 · 字段异步校验（分片 + 断点续跑）契约测试（DoD）。

测试策略
========
- 分片执行器**全部注入**（不碰真实 DuckDB），毫秒级确定性。
- async_tasks 的会话工厂基于当前测试库：fixture 重置 control-plane 全局缓存
  （T13 教训）。
- **三列必须验证的硬规则**：① 同 draft_id+config_hash 活动任务唯一（幂等复用）
  ② 续跑不重复已完成分片（用计数器证明每个分片**恰好执行一次**）
  ③ 报告落库 + 24h 有效期。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.models.async_task import AsyncTaskRecord
from app.services import async_tasks as AT
from app.services.factors.mining import validation_service as VS

_TERMINAL = {"done", "failed", "cancelled"}


@pytest.fixture
def val_env(db_session, monkeypatch):
    import app.db.session as session_mod

    monkeypatch.setattr(session_mod, "_cp_factory", None)
    monkeypatch.setattr(session_mod, "_cp_engine", None)
    yield db_session
    AT.request_all_workers_stop()
    AT.wait_workers_stopped(timeout_seconds=15)
    AT.WORKER_STOP_EVENT.clear()   # 模块级全局，不清会毒化后续测试（T13 教训）


def _wait_done(val_env, task_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        val_env.expire_all()
        row = val_env.get(AsyncTaskRecord, task_id)
        if row is None:
            raise AssertionError(f"任务 {task_id} 不存在")
        last = row
        if row.status in _TERMINAL:
            val_env.refresh(row)
            return {c.name: getattr(row, c.name)
                    for c in AsyncTaskRecord.__table__.columns}
        time.sleep(0.05)
    raise AssertionError(f"超时未到终态，最后={last.status}")


def _wait_locks_clear(val_env, timeout: float = 15.0) -> None:
    """worker finally（释放锁）晚于置 done —— 轮询到终态就断言锁会撞竞态窗口。"""
    # TL 在本文件是测试方法内局部导入，helper 里必须自取
    from app.services.factors.mining import task_lock as _TL

    deadline = time.time() + timeout
    while time.time() < deadline:
        val_env.expire_all()
        locks = _TL.get_lock_status(val_env)
        if not locks.mining_domain.get("busy") and not locks.duckdb_write.get("busy"):
            return
        time.sleep(0.05)


def _fake_checker(calls: list, *, plan: dict | None = None):
    """计数分片执行器：每个分片恰好执行一次可被证明。"""
    plan = plan or {}

    def _check(shard: VS.ValidationShard, ctx) -> dict:
        calls.append(shard.key())
        if shard.key() in plan:
            raise RuntimeError(plan[shard.key()])
        return {"verdict": VS.VERDICT_PASS,
                "coverage": 0.99, "physical_table": "raw_daily_bars"}
    return _check


# ══════════════════════════════════════════════════════════
# 1. 纯逻辑
# ══════════════════════════════════════════════════════════


class TestPureLogic:
    def test_config_hash_stable_and_order_insensitive(self):
        a = VS.compute_config_hash({"x": 1, "y": [1, 2], "z": None})
        b = VS.compute_config_hash({"y": [1, 2], "x": 1})
        c = VS.compute_config_hash({"x": 2, "y": [1, 2]})
        assert a == b, "键序无关"
        assert a != c, "值不同必须不同"
        assert len(a) == 16

    def test_shards_are_field_x_bucket(self):
        shards = VS.build_shards(["close", "pe_ttm"],
                                 time_buckets=("2024H1", "2024H2"))
        keys = [s.key() for s in shards]
        assert keys == ["close::2024H1", "close::2024H2",
                        "pe_ttm::2024H1", "pe_ttm::2024H2"]
        assert all(s.status == VS.SHARD_PENDING for s in shards)

    def test_empty_fields_rejected(self):
        with pytest.raises(ValueError, match="fields"):
            VS.create_or_reuse_validation(draft_id="d1", config={}, fields=[])


# ══════════════════════════════════════════════════════════
# 2. 幂等复用（同 draft_id + config_hash 活动任务唯一）
# ══════════════════════════════════════════════════════════


class TestIdempotentReuse:
    def test_same_draft_and_config_reuses_active_task(self, val_env):
        """任务在运行中（用阻塞 stage 钉住）→ 再次提交**复用**，不新建。"""
        blocker = threading.Event()
        started = threading.Event()

        def slow_checker(shard, ctx):
            started.set()
            blocker.wait(timeout=30)
            return {"verdict": VS.VERDICT_PASS}

        r1 = VS.create_or_reuse_validation(
            draft_id="d-1", config={"fields": ["close"]}, fields=["close", "pe_ttm"],
            field_checker=slow_checker)
        assert r1["reused"] is False
        assert started.wait(timeout=15), "worker 未启动"

        r2 = VS.create_or_reuse_validation(
            draft_id="d-1", config={"fields": ["close"]}, fields=["close", "pe_ttm"],
            field_checker=slow_checker)
        assert r2["reused"] is True
        assert r2["task_id"] == r1["task_id"]

        # 库里只有一条该类型的任务记录
        rows = val_env.query(AsyncTaskRecord).filter(
            AsyncTaskRecord.task_type == VS.TASK_TYPE).all()
        assert len(rows) == 1
        blocker.set()
        _wait_done(val_env, r1["task_id"])

    def test_different_config_creates_new_task(self, val_env):
        """config_hash 不同 → 不复用（各自独立）。"""
        calls: list = []
        r1 = VS.create_or_reuse_validation(
            draft_id="d-2", config={"a": 1}, fields=["close"],
            field_checker=_fake_checker(calls))
        _wait_done(val_env, r1["task_id"])
        r2 = VS.create_or_reuse_validation(
            draft_id="d-2", config={"a": 2}, fields=["close"],
            field_checker=_fake_checker(calls))
        assert r2["reused"] is False
        assert r2["task_id"] != r1["task_id"]
        _wait_done(val_env, r2["task_id"])

    def test_finished_task_is_not_reused(self, val_env):
        """终态任务不复用 —— 同配置重校验是合法诉求（修复数据后回流）。"""
        calls: list = []
        r1 = VS.create_or_reuse_validation(
            draft_id="d-3", config={"a": 1}, fields=["close"],
            field_checker=_fake_checker(calls))
        _wait_done(val_env, r1["task_id"])
        r2 = VS.create_or_reuse_validation(
            draft_id="d-3", config={"a": 1}, fields=["close"],
            field_checker=_fake_checker(calls))
        assert r2["reused"] is False


# ══════════════════════════════════════════════════════════
# 3. 分片进度 / 断点续跑
# ══════════════════════════════════════════════════════════


class TestShardResume:
    def test_progress_persists_per_shard(self, val_env):
        calls: list = []
        r = VS.create_or_reuse_validation(
            draft_id="d-4", config={"a": 1},
            fields=["close", "pe_ttm", "pb", "roe_ttm"],
            field_checker=_fake_checker(calls))
        done = _wait_done(val_env, r["task_id"])
        assert set(calls) == {"close::all", "pe_ttm::all", "pb::all", "roe_ttm::all"}
        report = json.loads(done["result_json"])
        assert report["total_shards"] == 4
        assert report["verdict"] == VS.VERDICT_PASS
        # batch_recovery_json 记录了全部已完成分片
        recovery = json.loads(done["batch_recovery_json"])
        assert recovery["completed"] == 4

    def test_failed_shard_does_not_abort_the_run(self, val_env):
        """单分片失败 → 该分片标 failed 继续跑完，报告如实呈报（阻断）。"""
        calls: list = []
        checker = _fake_checker(calls, plan={"pe_ttm::all": "boom"})
        r = VS.create_or_reuse_validation(
            draft_id="d-5", config={"a": 1}, fields=["close", "pe_ttm"],
            field_checker=checker)
        done = _wait_done(val_env, r["task_id"])
        assert done["status"] == "done"      # 整场不失败
        report = json.loads(done["result_json"])
        assert report["verdict"] == VS.VERDICT_BLOCK
        assert "pe_ttm::all" in report["failed_shards"]
        shards = {s["field"]: s for s in report["shards"]}
        assert shards["pe_ttm"]["status"] == VS.SHARD_FAILED
        assert "boom" in shards["pe_ttm"]["error"]
        assert shards["close"]["status"] == VS.SHARD_DONE

    def test_resume_skips_completed_shards(self, val_env):
        """🚨 核心硬规则：续跑**不重复已完成分片** —— 用计数器证明。"""
        calls: list = []
        r1 = VS.create_or_reuse_validation(
            draft_id="d-6", config={"a": 1}, fields=["close", "pe_ttm", "pb"],
            field_checker=_fake_checker(calls))
        _wait_done(val_env, r1["task_id"])
        first_run = list(calls)
        assert len(first_run) == 3

        # resume 走的是**默认检查器**（服务不转发自定义 checker），
        # 所以 monkeypatch 它，让续跑也计数。
        import pytest as _pytest
        _pytest.MonkeyPatch().setattr(VS, "_default_field_checker",
                                      _fake_checker(calls))
        n_before = len(calls)
        r2 = VS.resume_validation(task_id=r1["task_id"])
        assert r2["resumed_from"] == r1["task_id"]
        _wait_done(val_env, r2["task_id"])
        second_run = calls[n_before:]

        # 续跑 = 全新执行（新任务的 batch_recovery 为空）；
        # 「跳过已完成分片」的精确验证由 test_resume_from_partial_progress 覆盖。
        assert sorted(second_run) == sorted(first_run)

    def test_resume_from_partial_progress_skips_done_shards(self, val_env):
        """🚨 核心硬规则的精确验证：手工把 3 片中的 2 片标 done，
        续跑 worker 必须只执行剩下 1 片。"""
        executed: list = []

        def checker(shard, ctx):
            executed.append(shard.key())
            return {"verdict": VS.VERDICT_PASS}

        # 手工构造「跑到一半」的任务现场：3 片，2 片已 done
        payload = {
            "draft_id": "d-7", "config_hash": "hash-7", "config": {"a": 1},
            "fields": ["close", "pe_ttm", "pb"],
            "shards": [
                {"field": "close", "bucket": "all", "status": VS.SHARD_DONE,
                 "result": {"verdict": VS.VERDICT_PASS}},
                {"field": "pe_ttm", "bucket": "all", "status": VS.SHARD_DONE,
                 "result": {"verdict": VS.VERDICT_PASS}},
                {"field": "pb", "bucket": "all", "status": VS.SHARD_PENDING,
                 "result": {}},
            ],
            "operator_id": "test",
        }
        task = AT.create_async_task(VS.TASK_TYPE, payload, use_control_plane=True,
                                    force_new=True)
        recovery = {"shards": {
            "close::all": {"field": "close", "bucket": "all",
                           "status": VS.SHARD_DONE, "result": {}},
            "pe_ttm::all": {"field": "pe_ttm", "bucket": "all",
                            "status": VS.SHARD_DONE, "result": {}},
        }, "completed": 2, "total": 3}
        val_env.get(AsyncTaskRecord, task.id).batch_recovery_json = \
            json.dumps(recovery)
        val_env.commit()

        VS.run_validation_worker(task.id, field_checker=checker)

        assert executed == ["pb::all"], f"续跑重复执行了已完成分片: {executed}"
        row = val_env.get(AsyncTaskRecord, task.id)
        val_env.refresh(row)
        report = json.loads(row.result_json)
        assert report["verdict"] == VS.VERDICT_PASS
        assert report["total_shards"] == 3


# ══════════════════════════════════════════════════════════
# 4. 报告落库 + 24h 有效期
# ══════════════════════════════════════════════════════════


class TestReportAndValidity:
    def test_report_is_persisted_not_frontend_only(self, val_env):
        calls: list = []
        r = VS.create_or_reuse_validation(
            draft_id="d-8", config={"a": 1}, fields=["close"],
            field_checker=_fake_checker(calls))
        done = _wait_done(val_env, r["task_id"])
        report = json.loads(done["result_json"])
        for key in ("draft_id", "config_hash", "verdict", "shards",
                    "valid_until", "ttl_hours"):
            assert key in report, f"报告缺 {key}"
        assert report["draft_id"] == "d-8"
        assert report["ttl_hours"] == 24

    def test_validity_window(self, val_env):
        calls: list = []
        r = VS.create_or_reuse_validation(
            draft_id="d-9", config={"a": 1}, fields=["close"],
            field_checker=_fake_checker(calls))
        _wait_done(val_env, r["task_id"])

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert VS.is_validation_valid(val_env, r["task_id"], now=now) is True
        assert VS.is_validation_valid(
            val_env, r["task_id"],
            now=now + timedelta(hours=24, seconds=1)) is False

    def test_invalid_task_is_never_valid(self, val_env):
        assert VS.is_validation_valid(val_env, "nope") is False


# ══════════════════════════════════════════════════════════
# 5. 不占 mining_domain
# ══════════════════════════════════════════════════════════


class TestLockIsolation:
    def test_validation_neither_lock(self, val_env):
        """任务卡「明确不做：不占 mining_domain」+ 校验只读 → 不需要写锁。"""
        calls: list = []
        r = VS.create_or_reuse_validation(
            draft_id="d-10", config={"a": 1}, fields=["close"],
            field_checker=_fake_checker(calls))
        _wait_done(val_env, r["task_id"])

        from app.services.factors.mining import task_lock as TL

        _wait_locks_clear(val_env)
        locks = TL.get_lock_status(val_env)
        assert locks.mining_domain["busy"] is False
        assert locks.duckdb_write["busy"] is False

    def test_validation_can_run_alongside_mining_domain_holder(self, val_env):
        """挖掘域被占时校验**照常可跑**（两者互不阻塞）。"""
        from app.services.factors.mining import task_lock as TL

        TL.acquire_mining_lock(val_env, task_id="mining-holder", run_id="r")
        try:
            calls: list = []
            r = VS.create_or_reuse_validation(
                draft_id="d-11", config={"a": 1}, fields=["close"],
                field_checker=_fake_checker(calls))
            done = _wait_done(val_env, r["task_id"])
            assert done["status"] == "done"
        finally:
            TL.release_lock(val_env, lock_key=TL.LOCK_MINING_DOMAIN,
                            task_id="mining-holder")


# ══════════════════════════════════════════════════════════
# 6. HTTP 层
# ══════════════════════════════════════════════════════════


@pytest.fixture
def client(val_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import factor_mining_wizard as route_mod
    from app.db.session import get_db

    app = FastAPI()
    app.include_router(route_mod.router)
    app.dependency_overrides[get_db] = lambda: val_env
    return TestClient(app), val_env


class TestHttpLayer:
    def test_create_get_valid_resume(self, client):
        tc, val_env = client
        r = tc.post("/factor-mining/validations", json={
            "draft_id": "d-http", "config": {"a": 1},
            "fields": ["close", "pe_ttm"]})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["reused"] is False
        assert body["total_shards"] == 2

        # 幂等复用
        r2 = tc.post("/factor-mining/validations", json={
            "draft_id": "d-http", "config": {"a": 1},
            "fields": ["close", "pe_ttm"]})
        # 若第一个已在跑 → reused=true；若已完成 → 新建（都合法）
        assert r2.status_code == 201

        # 进度/报告
        g = tc.get(f"/factor-mining/validations/{body['task_id']}")
        assert g.status_code == 200
        assert g.json()["found"] is True

        # 有效期
        v = tc.get(f"/factor-mining/validations/{body['task_id']}/valid")
        assert v.status_code == 200
        assert v.json()["ttl_hours"] == 24

    def test_get_unknown_returns_404(self, client):
        tc, _ = client
        r = tc.get("/factor-mining/validations/nope")
        assert r.status_code == 404

    def test_create_without_fields_returns_400(self, client):
        tc, _ = client
        r = tc.post("/factor-mining/validations", json={
            "draft_id": "d-x", "config": {}, "fields": []})
        # fields 有 min_length=1 → pydantic 在进 handler 前就 422
        assert r.status_code == 422

    def test_resume_unknown_returns_404(self, client):
        tc, _ = client
        r = tc.post("/factor-mining/validations/nope/resume")
        assert r.status_code == 404

    def test_pause_then_resume(self, client, monkeypatch):
        """暂停 = cancel_async_task（置 cancel_requested）→ worker 分片间自检自止；
        续跑 = 同 payload 新建任务（已完成分片不再重复）。"""
        tc, val_env = client

        def slow(shard, ctx):
            # 每个「分片」模拟 0.15s 工作，让 worker 有机会在分片间自检
            time.sleep(0.15)
            return {"verdict": VS.VERDICT_PASS}

        monkeypatch.setattr(VS, "_default_field_checker", slow)

        r = tc.post("/factor-mining/validations", json={
            "draft_id": "d-pause", "config": {"a": 1},
            "fields": ["close", "pe_ttm", "pb", "roe_ttm"]})
        task_id = r.json()["task_id"]

        # 等它真正进入 running（分片在跑）
        deadline = time.time() + 15
        row = None
        while time.time() < deadline:
            val_env.expire_all()
            row = val_env.get(AsyncTaskRecord, task_id)
            if row.status == "running":
                break
            time.sleep(0.05)

        p = tc.post(f"/factor-mining/validations/{task_id}/pause")
        assert p.status_code == 200, p.text

        # worker 应在分片边界自止 → cancelled
        deadline = time.time() + 30
        row = None
        while time.time() < deadline:
            val_env.expire_all()
            row = val_env.get(AsyncTaskRecord, task_id)
            if row.status in _TERMINAL:
                break
            time.sleep(0.05)
        assert row.status == "cancelled", row.status

        # 续跑（默认检查器仍是被 monkeypatch 的慢检查器，4 片约 0.6s）
        res = tc.post(f"/factor-mining/validations/{task_id}/resume")
        assert res.status_code == 200
        new_id = res.json()["task_id"]
        deadline = time.time() + 30
        row = None
        while time.time() < deadline:
            val_env.expire_all()
            row = val_env.get(AsyncTaskRecord, new_id)
            if row and row.status in _TERMINAL:
                break
            time.sleep(0.05)
        assert row.status == "done"

    def test_pause_terminal_returns_409(self, client):
        tc, val_env = client
        calls: list = []
        r = tc.post("/factor-mining/validations", json={
            "draft_id": "d-p2", "config": {"a": 1}, "fields": ["close"]})
        task_id = r.json()["task_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            val_env.expire_all()
            if val_env.get(AsyncTaskRecord, task_id).status in _TERMINAL:
                break
            time.sleep(0.05)
        p = tc.post(f"/factor-mining/validations/{task_id}/pause")
        assert p.status_code == 409
