"""白盒 - akshare 接口探测的任务化 + 心跳轮询（PT-DEF-18 回归）。

为什么这么测：探测过去的形态是"同步请求 + 双方各猜一个秒数"（后端 50s、前端 30s），
慢而成功的上游会被误判失败，且客户端先跑还会白占后端探测线程池。改成提交任务 + 轮询
心跳后，正确性判据变成"任务是否落了终态、心跳是否在走"，**不再把任何秒数写进断言**。

worker 一律内联执行（patch `_start_worker`）：本文件绝不留下跨用例存活的后台线程
—— 那正是 PT-DEF-15 实测踩过并修过的坑。
"""
from __future__ import annotations

import time
from concurrent.futures import TimeoutError as FutureTimeout

import pandas as pd
import pytest
from fastapi import HTTPException

from app.api.routes import akshare_apis as m
from app.models.akshare_api_config import AkshareApiConfig
from app.services.async_tasks import _set_task, create_async_task

pytestmark = pytest.mark.whitebox

API_KEY = "stock_zh_a_hist"


@pytest.fixture(autouse=True)
def _bind_db(db_session):
    """本文件所有用例都跑在逐用例 tmp 库上（顺带失效控制平面缓存）。

    探测任务走 `use_control_plane=True`：如果只改绑主库不清控制平面，就会读到
    上一个用例已删除的 tmp 文件，报 no such table: async_tasks。
    """
    yield


@pytest.fixture(autouse=True)
def _inline_worker(monkeypatch):
    """worker 改成同步内联跑 + 给一个可控的假 akshare 属性。"""
    monkeypatch.setattr(
        m, "_start_worker", lambda task_id, worker_func: worker_func(task_id),
    )
    monkeypatch.setattr(
        m.ak, API_KEY, lambda **kw: pd.DataFrame([{"close": 1.0}]), raising=False,
    )
    m._ACTIVE_PROBE_TASKS.clear()
    yield
    m._ACTIVE_PROBE_TASKS.clear()


def _fake_run_probe(monkeypatch, value=None, exc: Exception | None = None):
    """替换真正的上游调用：本文件测的是任务编排，不打网络。"""
    def fake(func, probe_args, api_key):
        if exc is not None:
            raise exc
        return value

    monkeypatch.setattr(m, "_run_probe", fake)


# ---------------------------------------------------------------------------


def test_submit_returns_task_receipt_without_result():
    """提交只回任务回执：不阻塞等上游，所以前端不再需要"猜一个超时秒数"。"""
    accepted = m.submit_probe(API_KEY)

    assert accepted.task_id
    assert accepted.api_key == API_KEY
    assert accepted.reused is False
    assert accepted.status in ("queued", "running")


def test_unknown_api_key_is_404():
    with pytest.raises(HTTPException) as exc:
        m.submit_probe("no_such_api_key_xyz")
    assert exc.value.status_code == 404


def test_status_404_for_unknown_task():
    with pytest.raises(HTTPException) as exc:
        m.get_probe_status("deadbeefdeadbeefdeadbeefdeadbeef")
    assert exc.value.status_code == 404


def test_same_key_reuses_still_running_task(monkeypatch):
    """重复点击不重复打上游：同 key 仍有未终态任务时复用其 task_id。

    内联 worker 下 submit 一返回就已经终态，所以这里直接种一个 running 任务，
    把"复用"这条分支单独测出来（而不是靠让线程真的卡住）。
    """
    _fake_run_probe(monkeypatch, value=pd.DataFrame([{"close": 1.0}]))
    inflight = create_async_task(
        m.TASK_TYPE_EXTERNAL_API_PROBE, {"api_key": API_KEY},
        use_control_plane=True, force_new=True,
    )
    from app.db.session import get_control_session_local

    cp = get_control_session_local()()
    try:
        _set_task(cp, inflight.id, status="running", stage="calling", heartbeat_at=m._probe_now())
    finally:
        cp.close()

    with m._ACTIVE_PROBE_LOCK:
        m._ACTIVE_PROBE_TASKS[API_KEY] = inflight.id

    second = m.submit_probe(API_KEY)
    assert second.task_id == inflight.id
    assert second.reused is True
    assert second.status == "running"


def test_terminal_task_is_not_reused_so_new_probe_actually_runs():
    """上次的探测已终态时，再点必须开新任务 —— 否则"重探一次"永远拿到旧结论。"""
    done = create_async_task(
        m.TASK_TYPE_EXTERNAL_API_PROBE, {"api_key": API_KEY},
        use_control_plane=True, force_new=True,
    )
    from app.db.session import get_control_session_local

    cp = get_control_session_local()()
    try:
        _set_task(cp, done.id, status="done", stage="done", finished_at=m._probe_now())
    finally:
        cp.close()

    with m._ACTIVE_PROBE_LOCK:
        m._ACTIVE_PROBE_TASKS[API_KEY] = done.id

    fresh = m.submit_probe(API_KEY)
    assert fresh.task_id != done.id
    assert fresh.reused is False


def test_successful_probe_lands_terminal_state_and_updates_config(db_session, monkeypatch):
    """成功探测：任务落 done、结果可轮询到、配置行 last_probe_* 被写入。"""
    _fake_run_probe(monkeypatch, value=pd.DataFrame([{"close": 1.0}]))

    accepted = m.submit_probe(API_KEY)
    st = m.get_probe_status(accepted.task_id)

    assert st.status == "done", f"任务未落终态：{st.model_dump()}"
    assert st.result is not None and st.result.success is True
    assert st.result.latency_ms is not None
    assert st.heartbeat_at is not None
    assert st.heartbeat_stale is False
    assert st.api_key == API_KEY

    db_session.expire_all()
    cfg = db_session.query(AkshareApiConfig).filter_by(api_key=API_KEY).one_or_none()
    assert cfg is not None and cfg.last_probe_success is True


def test_empty_result_counts_as_failure_with_reason(db_session, monkeypatch):
    """上游返回空表：不是异常，但结论必须是 success=False 且带可读原因。"""
    _fake_run_probe(monkeypatch, value=pd.DataFrame())

    accepted = m.submit_probe(API_KEY)
    st = m.get_probe_status(accepted.task_id)

    assert st.status == "done"
    assert st.result is not None
    assert st.result.success is False
    assert st.result.error == "Empty result"


def test_probe_exception_is_recorded_as_failure(db_session, monkeypatch):
    _fake_run_probe(monkeypatch, exc=RuntimeError("upstream 503"))

    accepted = m.submit_probe(API_KEY)
    st = m.get_probe_status(accepted.task_id)

    assert st.status == "failed"
    assert st.result is not None and st.result.success is False
    assert "upstream 503" in (st.result.error or "")


def test_heartbeat_ticks_while_waiting_then_hard_limit_reports_timeout(monkeypatch):
    """慢上游的正确表现：先持续刷心跳（不误判失败），超硬上限才明确判超时。

    压小上限只是为了**驱动这段机制本身**跑起来，断言里没有任何时长魔法。
    """
    monkeypatch.setattr(m, "_PROBE_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(m, "_PROBE_HARD_LIMIT_SECONDS", 0.2)

    class _Stuck:
        def submit(self, fn, *a, **kw):
            return self

        def result(self, timeout=None):
            time.sleep(timeout or 0.05)
            raise FutureTimeout

        def cancel(self):
            pass

    monkeypatch.setattr(m, "_PROBE_EXECUTOR", _Stuck())

    accepted = m.submit_probe(API_KEY)
    st = m.get_probe_status(accepted.task_id)

    assert st.status == "failed"
    assert st.stage == "timeout"
    assert "hard limit" in (st.message or "")
    assert st.heartbeat_at is not None  # 心跳走过，不是从头没动静的僵尸任务


def test_stale_heartbeat_never_leaves_the_ui_spinning_forever():
    """worker 没了但任务停在 running：界面必须拿到"结束或可疑"，不能无限转圈。

    实测行为比我自己算的 stale 更强：读取任务时框架巡查会把心跳过旧的 running
    直接判为 `interrupted`。所以这里断言的是那条**安全性质**：
    绝不会出现 status=running 却没有 heartbeat_stale 标记的组合。
    """
    from datetime import timedelta

    from app.db.session import get_control_session_local
    from app.models.async_task import AsyncTaskRecord

    task = create_async_task(
        m.TASK_TYPE_EXTERNAL_API_PROBE, {"api_key": API_KEY},
        use_control_plane=True, force_new=True,
    )
    cp = get_control_session_local()()
    try:
        row = cp.get(AsyncTaskRecord, task.id)
        row.status = "running"
        row.stage = "calling"
        row.is_terminal_locked = 0
        row.heartbeat_at = m._probe_now() - timedelta(minutes=10)
        cp.commit()
    finally:
        cp.close()

    st = m.get_probe_status(task.id)

    assert not (st.status == "running" and not st.heartbeat_stale), (
        f"心跳已停 10 分钟却仍报正常 running：界面会无限等待。实际状态={st.status}, "
        f"stale={st.heartbeat_stale}, 距心跳={st.seconds_since_heartbeat}s"
    )
    assert st.seconds_since_heartbeat and st.seconds_since_heartbeat > 60


def test_unparsable_result_json_does_not_hide_task_state(monkeypatch):
    """result_json 损坏时不能"当作没这个任务"：状态照旧返回，结果如实留空。"""
    from app.db.session import get_control_session_local
    from app.models.async_task import AsyncTaskRecord

    _fake_run_probe(monkeypatch, value=pd.DataFrame([{"close": 1.0}]))
    accepted = m.submit_probe(API_KEY)

    cp = get_control_session_local()()
    try:
        row = cp.get(AsyncTaskRecord, accepted.task_id)
        row.result_json = "{ this is not json"
        cp.commit()
    finally:
        cp.close()

    st = m.get_probe_status(accepted.task_id)
    assert st.status == "done"       # 任务状态不被坏 JSON 拖垮
    assert st.result is None         # 结果缺失要说"缺失"，不能编造一个成功


def test_probe_still_works_after_app_shutdown_closed_the_executor(monkeypatch):
    """PT-DEF-27：应用关停关掉模块级探测线程池后，同进程内后续探测必须还能跑。

    不是凭空担心：这个红灯就是在组合跑里真实出现的 —— 先有 TestClient 走完一次
    lifespan（main.py 关停时 shutdown 线程池），之后探测 worker 再也提不进任务，
    报 cannot schedule new futures after shutdown，任务被记成 failed 而不是 done。
    uvicorn --reload 属于同一机制（同进程再过一次 lifespan）。
    """
    _fake_run_probe(monkeypatch, value=pd.DataFrame([{"close": 1.0}]))

    m.shutdown_probe_executor()
    assert getattr(m._PROBE_EXECUTOR, "_shutdown", False) is True, "前置：池应已被关停"

    accepted = m.submit_probe(API_KEY)
    st = m.get_probe_status(accepted.task_id)

    assert st.status == "done", f"线程池被关过就没法再探测：{st.model_dump()}"
    assert st.result is not None and st.result.success is True
    # 重建后的新池要留给后续用例，不能又是个已关闭的对象
    assert getattr(m._PROBE_EXECUTOR, "_shutdown", False) is False


def test_probe_success_is_visible_in_logs(db_session, monkeypatch, caplog):
    """【P0 运维可观测】探测完成必须进 INFO 日志（旧同步时代的规定动作，不能随重构丢）。

    移植自 test_whitebox_probe_thread_pool.py：那两条是针对旧 `probe_api()` 写的，
    探测任务化后入口已不存在（它们直接报 AttributeError），所以这里按新入口重建。
    """
    import logging

    _fake_run_probe(monkeypatch, value=pd.DataFrame([{"close": 1.0}]))
    with caplog.at_level(logging.INFO, logger="app.api.routes.akshare_apis"):
        accepted = m.submit_probe(API_KEY)
    st = m.get_probe_status(accepted.task_id)
    assert st.status == "done"
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(f"probe {API_KEY} done" in msg for msg in infos), (
        f"探测完成没进 INFO 日志，运维无法从日志定位：{infos[:6]}"
    )


def test_probe_hard_limit_is_visible_in_warning_logs(monkeypatch, caplog):
    """【P0 运维可观测】超硬上限必须进 WARNING 日志（历史问题：超时只写库不留痕）。"""
    import logging

    monkeypatch.setattr(m, "_PROBE_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(m, "_PROBE_HARD_LIMIT_SECONDS", 0.2)

    class _Stuck:
        def submit(self, fn, *a, **kw):
            return self

        def result(self, timeout=None):
            time.sleep(timeout or 0.05)
            raise FutureTimeout

        def cancel(self):
            pass

    monkeypatch.setattr(m, "_PROBE_EXECUTOR", _Stuck())
    with caplog.at_level(logging.WARNING, logger="app.api.routes.akshare_apis"):
        accepted = m.submit_probe(API_KEY)
    st = m.get_probe_status(accepted.task_id)
    assert st.stage == "timeout"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(API_KEY in msg and "HARD LIMIT" in msg for msg in warnings), (
        f"超硬上限没留 WARNING 日志，超时事件会隐形：{warnings[:6]}"
    )
