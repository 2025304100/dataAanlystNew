"""T15 · 历史镜像任务 + 状态 API 契约测试（DoD）。

测试策略
========
- `mirror_func` **注入 fake**（不搬真数据、不碰真实 MySQL/DuckDB），
  分块 / 续跑 / 锁持有语义毫秒级可验证。
- 硬规则：**镜像持有 duckdb_write**（运行中 busy、结束释放、被占拒绝不建任务）；
  **分块区间互不重叠**（不得重复行的第二层保证）；**续跑跳过已完成块**。
- 磁盘检查 / 估算只测纯逻辑（monkeypatch shutil.disk_usage）。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.async_task import AsyncTaskRecord
from app.services import async_tasks as AT
from app.services.factors import mirror_task as MT
from app.services.factors.mining import task_lock as TL

_TERMINAL = {"done", "failed", "cancelled"}


@pytest.fixture
def mirror_env(db_session, monkeypatch):
    import app.db.session as session_mod

    monkeypatch.setattr(session_mod, "_cp_factory", None)
    monkeypatch.setattr(session_mod, "_cp_engine", None)
    yield db_session
    AT.request_all_workers_stop()
    AT.wait_workers_stopped(timeout_seconds=15)
    AT.WORKER_STOP_EVENT.clear()   # 模块级全局（T13 教训）


def _fresh_session(db):
    """🚨 独立 session：worker 线程 + 主线程 + watcher 并发时，
    共用 fixture session 会抛 `InvalidRequestError: This session is
    provisioning a new connection; concurrent operations are not permitted`。"""
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=db.get_bind())()


def _wait_terminal(db, task_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        s = _fresh_session(db)
        try:
            row = s.get(AsyncTaskRecord, task_id)
            if row and row.status in _TERMINAL:
                payload = {c.name: getattr(row, c.name)
                           for c in AsyncTaskRecord.__table__.columns}
                return payload
        finally:
            s.close()
        time.sleep(0.05)
    raise AssertionError(f"超时未到终态，最后={getattr(row, 'status', None)}")


def _wait_locks_clear(db, timeout: float = 15.0) -> None:
    """worker finally（释放锁）晚于置 done —— 断言锁前必须轮询（R25 延伸）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _fresh_session(db)
        try:
            locks = TL.get_lock_status(s)
            if not locks.duckdb_write.get("busy"):
                return
        finally:
            s.close()
        time.sleep(0.05)


def _fake_mirror(calls: list, *, fail_on: set | None = None,
                 gate: threading.Event | None = None):
    fail_on = fail_on or set()

    def _mirror(db, *, start_date=None, end_date=None, **kw):
        key = (start_date.isoformat(), end_date.isoformat())
        calls.append(key)
        if gate is not None:
            gate.wait(timeout=30)
        if key in fail_on:
            raise RuntimeError(f"injected failure {key}")

        class _R:
            batch_id = f"bars-fake-{len(calls)}"

            @staticmethod
            def to_dict():
                return {"batch_id": _R.batch_id,
                        "rows_written": 1000, "universe_rows": 1000}

        return _R()
    return _mirror


# ══════════════════════════════════════════════════════════
# 1. 纯逻辑：范围解析 / 分块 / 估算 / 磁盘
# ══════════════════════════════════════════════════════════


class TestPureLogic:
    def test_resolve_presets(self):
        s, e, p = MT.resolve_range(preset="5y")
        assert (s, p) == (date(2021, 1, 1), "5y")
        assert e >= date(2026, 1, 1)
        s, _, p = MT.resolve_range(preset="10y")
        assert (s, p) == (date(2016, 7, 12), "10y")
        # full 与 10 年同起点（向导 §10.1）
        s, _, p = MT.resolve_range(preset="full")
        assert s == date(2016, 7, 12)

    def test_resolve_custom(self):
        s, e, p = MT.resolve_range(
            preset="custom", start_date=date(2023, 3, 1), end_date=date(2024, 5, 20))
        assert (s, e, p) == (date(2023, 3, 1), date(2024, 5, 20), "custom")

    @pytest.mark.parametrize("kw", [
        {"preset": "wrong"},
        {"preset": "custom"},
        {"preset": "custom", "start_date": date(2024, 1, 1)},   # 缺 end
        {"preset": "custom", "start_date": date(2024, 5, 1),
         "end_date": date(2024, 1, 1)},                          # 起止倒置
        {"preset": "5y", "start_date": date(2024, 1, 1)},        # preset 不接自定义
    ])
    def test_resolve_invalid(self, kw):
        with pytest.raises(ValueError):
            MT.resolve_range(**kw)

    def test_build_chunks_yearly_contiguous_no_overlap(self):
        chunks = MT.build_chunks(date(2021, 5, 3), date(2024, 2, 10))
        assert chunks == [
            (date(2021, 5, 3), date(2021, 12, 31)),
            (date(2022, 1, 1), date(2022, 12, 31)),
            (date(2023, 1, 1), date(2023, 12, 31)),
            (date(2024, 1, 1), date(2024, 2, 10)),
        ]
        # 连续且互不重叠（「不得重复行」第二层保证）
        for (s1, e1), (s2, e2) in zip(chunks, chunks[1:]):
            assert e1 < s2
        assert chunks[0][0] == date(2021, 5, 3)
        assert chunks[-1][1] == date(2024, 2, 10)

    def test_build_chunks_single_year(self):
        assert MT.build_chunks(date(2023, 1, 1), date(2023, 6, 30)) == \
            [(date(2023, 1, 1), date(2023, 6, 30))]

    def test_estimate_rows_shape(self, db_session):
        est = MT.estimate_rows(db_session, date(2025, 1, 1), date(2025, 12, 31))
        assert est["estimated"] is True
        assert "estimated_rows" in est and "estimated_seconds" in est
        assert "估算" in est["note_zh"]

    def test_check_disk_space_blocks_when_short(self, monkeypatch, tmp_path):
        class _U:
            free = 100

        monkeypatch.setattr(MT.shutil, "disk_usage", lambda _: _U())
        out = MT.check_disk_space(tmp_path, estimated_bytes=1_000_000)
        assert out["ok"] is False
        assert "不足以" in out["note_zh"]

    def test_check_disk_space_ok(self, monkeypatch, tmp_path):
        class _U:
            free = 10**12

        monkeypatch.setattr(MT.shutil, "disk_usage", lambda _: _U())
        out = MT.check_disk_space(tmp_path, estimated_bytes=1_000_000)
        assert out["ok"] is True
        assert out["note_zh"] is None


# ══════════════════════════════════════════════════════════
# 2. 提交 / 执行 / 锁持有
# ══════════════════════════════════════════════════════════


class TestSubmitAndRun:
    def test_happy_path_holds_and_releases_duckdb_write(
        self, mirror_env, db_session
    ):
        """🚨 任务卡硬规则：镜像持有 duckdb_write —— 运行中 busy、结束释放。"""
        calls: list = []
        gate = threading.Event()

        def checker():
            # 运行中探测：锁必须是本任务持有
            locks = TL.get_lock_status(db_session)
            return locks.duckdb_write.get("busy"), locks.duckdb_write.get("taskId")

        holder = {}

        def mirror(db, *, start_date=None, end_date=None, **kw):
            key = (start_date.isoformat(), end_date.isoformat())
            calls.append(key)
            busy, owner = checker()
            holder["busy"] = busy
            holder["owner"] = owner
            holder["task_id_probe"] = kw.get("should_cancel")
            return type("R", (), {"batch_id": "b", "to_dict": lambda s: {
                "batch_id": "b", "rows_written": 10}})()

        result = MT.create_mirror_task(
            preset="custom", start_date=date(2023, 1, 1), end_date=date(2024, 6, 30),
            mirror_func=mirror, skip_disk_check=True)
        read = _wait_terminal(db_session, result["task_id"])
        assert read["status"] == "done"
        # 镜像运行期间确实持有 duckdb_write
        assert holder["busy"] is True
        assert holder["owner"] == result["task_id"]
        # 结束后释放
        _wait_locks_clear(db_session)
        assert TL.get_lock_status(db_session).duckdb_write["busy"] is False
        # 分块按年
        assert calls == [
            (date(2023, 1, 1).isoformat(), date(2023, 12, 31).isoformat()),
            (date(2024, 1, 1).isoformat(), date(2024, 6, 30).isoformat()),
        ]
        report = json.loads(read["result_json"])
        assert report["total_rows"] == 20
        assert report["chunks_total"] == 2

    def test_duckdb_write_busy_rejects_without_creating_task(
        self, mirror_env, db_session
    ):
        """🚨 被占 → 409 语义（这里抛 RuntimeError 带 JSON）且**未创建任务**。"""
        TL.acquire_write_slot(db_session, task_id="other-writer", run_id="r")
        before = db_session.query(AsyncTaskRecord).filter(
            AsyncTaskRecord.task_type == MT.TASK_TYPE).count()
        with pytest.raises(RuntimeError) as ei:
            MT.create_mirror_task(preset="5y", mirror_func=_fake_mirror([]),
                                  skip_disk_check=True)
        detail = json.loads(str(ei.value))
        assert detail["error_code"] == "DUCKDB_WRITE_BUSY"
        assert detail["owner_task_id"] == "other-writer"
        after = db_session.query(AsyncTaskRecord).filter(
            AsyncTaskRecord.task_type == MT.TASK_TYPE).count()
        assert after == before, "被占时不应创建任务记录"
        TL.release_lock(db_session, lock_key=TL.LOCK_DUCKDB_WRITE,
                        task_id="other-writer")

    def test_does_not_hold_mining_domain(self, mirror_env, db_session):
        """镜像不占 mining_domain（任务卡「明确不做」的镜像面）。"""
        calls: list = []
        result = MT.create_mirror_task(
            preset="custom", start_date=date(2023, 1, 1), end_date=date(2023, 3, 31),
            mirror_func=_fake_mirror(calls), skip_disk_check=True)
        _wait_terminal(db_session, result["task_id"])
        locks = TL.get_lock_status(db_session)
        assert locks.mining_domain["busy"] is False


# ══════════════════════════════════════════════════════════
# 3. 断点续跑（watermark 分块）
# ══════════════════════════════════════════════════════════


class TestChunkResume:
    def test_failure_midway_then_resume_skips_done_chunks(
        self, mirror_env, db_session, monkeypatch
    ):
        """🚨 核心硬规则：中断续跑不重复已完成块。

        断言口径：**已完成块**看 recovery 的 completed_chunks（不是 mirror
        的调用记录 —— 失败块也被"调用过"，但不算完成）。
        """
        calls: list = []
        fail_on = {(date(2022, 1, 1).isoformat(), date(2022, 12, 31).isoformat())}
        r1 = MT.create_mirror_task(
            preset="custom", start_date=date(2021, 1, 1), end_date=date(2023, 6, 30),
            mirror_func=_fake_mirror(calls, fail_on=fail_on), skip_disk_check=True)
        read1 = _wait_terminal(db_session, r1["task_id"])
        assert read1["status"] == "failed"

        # 失败现场：只有 chunk1（2021）完成；chunk2（2022）注入失败
        recovery = json.loads(read1["batch_recovery_json"])
        assert recovery["completed_chunks"] == [
            [date(2021, 1, 1).isoformat(), date(2021, 12, 31).isoformat()]]

        # 续跑：mirror_func 恢复正常；**只应执行剩余 2 块**
        calls.clear()
        import app.services.factors.bar_mirror as bm

        monkeypatch.setattr(bm, "mirror_daily_bars", _fake_mirror(calls))
        r2 = MT.resume_mirror_task(task_id=r1["task_id"])
        assert r2["resumed_from"] == r1["task_id"]
        _wait_terminal(db_session, r2["task_id"])

        # 已完成块不得重复执行
        assert calls == [
            (date(2022, 1, 1).isoformat(), date(2022, 12, 31).isoformat()),
            (date(2023, 1, 1).isoformat(), date(2023, 6, 30).isoformat()),
        ]
        _wait_locks_clear(db_session)

    def test_cancel_between_chunks_preserves_progress(
        self, mirror_env, db_session, monkeypatch
    ):
        """取消在块边界自止：已完成块保留，resume 接续且不重复执行。"""
        calls: list = []
        started_running = threading.Event()
        gate = threading.Event()

        def mirror(db, *, start_date=None, end_date=None, **kw):
            key = (start_date.isoformat(), end_date.isoformat())
            calls.append(key)
            if len(calls) == 1:
                # 通知主线程「已在块 1 内」，然后等主线程把 cancel_requested 置位
                started_running.set()
                gate.wait(timeout=30)
            return type("R", (), {"batch_id": "b", "to_dict": lambda s: {
                "batch_id": "b", "rows_written": 5}})()

        r = MT.create_mirror_task(
            preset="custom", start_date=date(2021, 1, 1), end_date=date(2023, 6, 30),
            mirror_func=mirror, skip_disk_check=True)

        # 等 worker 进入 running 且阻塞在块 1 内 → 置 cancel_requested
        assert started_running.wait(timeout=15)
        s = _fresh_session(db_session)
        try:
            row = s.get(AsyncTaskRecord, r["task_id"])
            row.cancel_requested = 1
            s.commit()
        finally:
            s.close()

        # 放行块 1 → worker 在下一块边界自止
        gate.set()
        read = _wait_terminal(db_session, r["task_id"], timeout=40)
        assert read["status"] == "cancelled", read["status"]
        recovery = json.loads(read["batch_recovery_json"])
        assert recovery["completed_chunks"] == [
            [date(2021, 1, 1).isoformat(), date(2021, 12, 31).isoformat()]]

        # resume：剩余 2 块照常，已完成块不重复
        calls.clear()
        import app.services.factors.bar_mirror as bm

        monkeypatch.setattr(bm, "mirror_daily_bars", _fake_mirror(calls))
        r2 = MT.resume_mirror_task(task_id=r["task_id"])
        _wait_terminal(db_session, r2["task_id"])
        assert calls == [
            (date(2022, 1, 1).isoformat(), date(2022, 12, 31).isoformat()),
            (date(2023, 1, 1).isoformat(), date(2023, 6, 30).isoformat()),
        ]
        _wait_locks_clear(db_session)

    def test_get_mirror_task_progress(self, mirror_env, db_session):
        calls: list = []
        r = MT.create_mirror_task(
            preset="custom", start_date=date(2023, 1, 1), end_date=date(2023, 6, 30),
            mirror_func=_fake_mirror(calls), skip_disk_check=True)
        _wait_terminal(db_session, r["task_id"])
        db_session.expire_all()
        progress = MT.get_mirror_task_progress(db_session, r["task_id"])
        assert progress["found"] is True
        assert progress["status"] == "done"
        assert progress["completed_chunks"] == 1
        assert progress["total_chunks"] == 1
        assert progress["rows_written"] == 1000

    def test_list_mirror_tasks(self, mirror_env, db_session):
        calls: list = []
        r = MT.create_mirror_task(
            preset="custom", start_date=date(2023, 1, 1), end_date=date(2023, 3, 31),
            mirror_func=_fake_mirror(calls), skip_disk_check=True)
        _wait_terminal(db_session, r["task_id"])
        items = MT.list_mirror_tasks(db_session)
        assert any(i["task_id"] == r["task_id"] for i in items)

    def test_get_mirror_status_handles_missing_warehouse(
        self, mirror_env, db_session, tmp_path, monkeypatch
    ):
        """数仓不存在 → available=False + 原因（不能 500）。"""
        from app.services.factors import config as config_mod

        real_config = config_mod.get_factor_system_config

        def fake_config(_db):
            cfg = real_config(_db)
            # FactorSystemConfigSnapshot 是 frozen dataclass → 用 replace 换路径
            from dataclasses import replace

            return replace(cfg, warehouse_path=tmp_path / "nope.duckdb")

        # mirror_task 在函数体内 `from ... import get_factor_system_config`
        # → 必须 patch **源模块**的属性
        monkeypatch.setattr(config_mod, "get_factor_system_config", fake_config)
        out = MT.get_mirror_status(db_session)
        assert out["available"] is False
        assert out["error"]


# ══════════════════════════════════════════════════════════
# 5. HTTP 层
# ══════════════════════════════════════════════════════════


@pytest.fixture
def http(mirror_env, db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import data_mirror as route_mod
    from app.db.session import get_db

    app = FastAPI()
    app.include_router(route_mod.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app), db_session, route_mod


class TestHttpLayer:
    def test_create_progress_cancel(self, http, monkeypatch):
        tc, db, route_mod = http
        # 默认 mirror_func 会碰真库 → monkeypatch 掉（路由层走 worker 的默认导入）
        calls: list = []
        import app.services.factors.bar_mirror as bm

        monkeypatch.setattr(bm, "mirror_daily_bars", _fake_mirror(calls))

        r = tc.post("/data-mirror/tasks", json={
            "preset": "custom", "start_date": "2023-01-01",
            "end_date": "2023-06-30", "skip_disk_check": True})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["estimated"]["estimated"] is True
        assert len(body["chunks"]) == 1

        _wait_terminal(db, body["task_id"])

        g = tc.get(f"/data-mirror/tasks/{body['task_id']}")
        assert g.status_code == 200
        assert g.json()["status"] == "done"
        assert g.json()["rows_written"] == 1000

        s = tc.get("/data-mirror/status")
        assert s.status_code == 200

        lst = tc.get("/data-mirror/tasks")
        assert any(i["task_id"] == body["task_id"] for i in lst.json()["items"])

    def test_create_busy_returns_409(self, http):
        tc, db, _ = http
        TL.acquire_write_slot(db, task_id="writer", run_id="r")
        r = tc.post("/data-mirror/tasks", json={
            "preset": "custom", "start_date": "2023-01-01",
            "end_date": "2023-06-30", "skip_disk_check": True})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error_code"] == "DUCKDB_WRITE_BUSY"
        TL.release_lock(db, lock_key=TL.LOCK_DUCKDB_WRITE, task_id="writer")

    def test_create_invalid_preset_returns_400(self, http):
        tc, _, _ = http
        r = tc.post("/data-mirror/tasks", json={
            "preset": "nope", "skip_disk_check": True})
        assert r.status_code == 400

    def test_cancel_unknown_returns_404(self, http):
        tc, _, _ = http
        r = tc.post("/data-mirror/tasks/nope/cancel")
        assert r.status_code == 404

    def test_status_missing_warehouse_not_500(self, http, tmp_path,
                                              monkeypatch):
        tc, _, route_mod = http
        monkeypatch.setattr(
            route_mod.MT, "get_mirror_status",
            lambda db: {"available": False, "error": "missing"})
        r = tc.get("/data-mirror/status")
        assert r.status_code == 200
        assert r.json()["available"] is False
