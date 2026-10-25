"""The supervisor survives a native child failure and keeps task truthfulness."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.async_task import AsyncTaskRecord

pytestmark = pytest.mark.whitebox


def assert_pid_stopped(pid):
    from app.services.factors.process_memory import process_is_running
    assert process_is_running(pid) is False


def test_native_child_exit_does_not_kill_parent_or_mark_training_done(db_session, tmp_path):
    from app.services.factors.pipeline_process import supervise_factor_pipeline
    task = AsyncTaskRecord(id="native-exit-acceptance", task_type="factor_pipeline", status="queued", stage="queued",
                           payload_json=json.dumps({"train_model": True, "window_days": 250, "validation_days": 50}))
    db_session.add(task)
    db_session.commit()
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)
    result = supervise_factor_pipeline(task.id, session_factory=factory, warehouse_path=tmp_path / "isolated.duckdb",
        child_command=[sys.executable, "-c", "import os; os._exit(37)"], poll_seconds=0.05)
    db_session.expire_all()
    actual = db_session.get(AsyncTaskRecord, task.id)
    assert result["exit_code"] == 37
    assert actual.status == "failed"
    assert actual.is_terminal_locked == 1
    assert "FACTOR_WORKER_PROCESS_EXITED" in actual.errors_json
    assert json.loads(actual.payload_json)["window_days"] == 250
    from app.services.async_tasks import _task_to_dict
    assert _task_to_dict(actual)['error_code'] == 'FACTOR_WORKER_PROCESS_EXITED'


def task_for(db, name):
    task = AsyncTaskRecord(id=name, task_type="factor_pipeline", status="queued", stage="queued",
        payload_json=json.dumps({"train_model": True, "window_days": 250, "validation_days": 50}))
    db.add(task)
    db.commit()
    return task.id, sessionmaker(bind=db.get_bind(), autoflush=False)


def test_cancel_request_stops_child_and_heartbeat_is_real(db_session, tmp_path):
    from app.services.factors.pipeline_process import supervise_factor_pipeline
    task_id, factory = task_for(db_session, "cancel-process")
    actual_pid_file = tmp_path / "actual-worker-pid.txt"
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(supervise_factor_pipeline, task_id, session_factory=factory,
            warehouse_path=tmp_path / "cancel.duckdb", child_command=[sys.executable, "-c", "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)", str(actual_pid_file)],
            poll_seconds=0.03, cancel_grace_seconds=0.05)
        deadline = time.monotonic() + 5
        first_heartbeat = None
        while time.monotonic() < deadline:
            with factory() as db:
                task = db.get(AsyncTaskRecord, task_id)
                if task.heartbeat_at and first_heartbeat and task.heartbeat_at > first_heartbeat and actual_pid_file.exists():
                    task.cancel_requested = 1
                    db.commit()
                    break
                first_heartbeat = task.heartbeat_at or first_heartbeat
            time.sleep(0.05)
        else:
            pytest.fail("Supervisor did not publish progressing heartbeats")
        result = future.result(timeout=8)
    db_session.expire_all()
    task = db_session.get(AsyncTaskRecord, task_id)
    assert task.status == "cancelled" and task.is_terminal_locked == 1
    assert result["exit_code"] is not None
    assert json.loads(task.payload_json)["validation_days"] == 50
    assert_pid_stopped(int(actual_pid_file.read_text()))


def test_zero_exit_without_terminal_receipt_is_failed(db_session, tmp_path):
    from app.services.factors.pipeline_process import supervise_factor_pipeline
    task_id, factory = task_for(db_session, "silent-exit")
    supervise_factor_pipeline(task_id, session_factory=factory, warehouse_path=tmp_path / "silent.duckdb",
        child_command=[sys.executable, "-c", "pass"], poll_seconds=0.02)
    db_session.expire_all()
    task = db_session.get(AsyncTaskRecord, task_id)
    assert task.status == "failed" and "FACTOR_WORKER_NO_RESULT" in task.errors_json


def test_parent_hands_off_warehouse_and_existing_instance_reopens_after_child(tmp_path):
    from app.services.factors.store import FactorWarehouse, FactorWarehouseUnavailable, external_warehouse_worker
    path = tmp_path / "handoff.duckdb"
    warehouse = FactorWarehouse(path)
    with warehouse.connection() as connection:
        connection.execute("CREATE TABLE evidence (value INTEGER)")
        connection.execute("INSERT INTO evidence VALUES (1)")
    with external_warehouse_worker(path):
        with pytest.raises(FactorWarehouseUnavailable, match="isolated worker"):
            with warehouse.connection():
                pass
        result = subprocess.run([sys.executable, "-c", "import duckdb,sys; c=duckdb.connect(sys.argv[1]); c.execute('INSERT INTO evidence VALUES (2)'); c.close()", str(path)], capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 0, result.stderr
    with warehouse.connection(read_only=True) as connection:
        assert connection.execute("SELECT value FROM evidence ORDER BY value").fetchall() == [(1,), (2,)]


def test_worker_without_explicit_binding_fails_before_configuration_fallback(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("FACTOR_PIPELINE_")}
    env["DATABASE_URL"] = f"sqlite:///{tmp_path / 'must-not-create.db'}"
    result = subprocess.run([sys.executable, "-m", "app.services.factors.pipeline_worker", "anything"],
        capture_output=True, env=env, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 2
    assert not (tmp_path / "must-not-create.db").exists()


@pytest.mark.parametrize('engine,accepted', [('InnoDB', True), ('MyISAM', False), (None, False)])
def test_worker_requires_transactional_mysql_tables(engine, accepted):
    from types import SimpleNamespace
    from app.services.factors.pipeline_worker import require_transactional_factor_database
    class InspectionSession:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name='mysql'))
        def execute(self, statement):
            assert 'information_schema.TABLES' in str(statement)
            return SimpleNamespace(all=lambda: [('factor_model_runs', engine), ('async_tasks', 'InnoDB')])
    if accepted:
        require_transactional_factor_database(InspectionSession())
    else:
        with pytest.raises(RuntimeError, match='InnoDB'):
            require_transactional_factor_database(InspectionSession())


def test_duckdb_connection_obeys_explicit_memory_and_thread_budget(tmp_path, monkeypatch):
    from app.services.factors.store import FactorWarehouse
    monkeypatch.setenv("FACTOR_WAREHOUSE_MEMORY_MB", "64")
    monkeypatch.setenv("FACTOR_WAREHOUSE_THREADS", "1")
    warehouse = FactorWarehouse(tmp_path / "budget.duckdb")
    with warehouse.connection() as connection:
        assert connection.execute("SELECT current_setting('threads')").fetchone()[0] == 1
        # DuckDB reports 64 decimal MB as approximately 61 MiB.
        assert connection.execute("SELECT current_setting('memory_limit')").fetchone()[0] in {"61.0 MiB", "64.0 MB", "64.0 MiB"}


def test_cooperative_pipeline_cancellation_reads_request_flag(db_session):
    from app.services.factors.pipeline_task import _cancelled
    task_id, _ = task_for(db_session, "cooperative-cancel")
    task = db_session.get(AsyncTaskRecord, task_id)
    task.status = "running"
    task.cancel_requested = 1
    db_session.commit()
    assert _cancelled(task_id) is True


def test_real_worker_uses_explicit_task_database_and_keeps_pipeline_failure(db_session, tmp_path):
    from app.models.factor_runtime import FactorSystemConfig
    from app.schemas.async_task import FactorPipelineCreate
    from app.services.factors.pipeline_process import supervise_factor_pipeline
    task_id, factory = task_for(db_session, "real-explicit-worker")
    path = tmp_path / "real.duckdb"
    db_session.add(FactorSystemConfig(id=1, feature_enabled=0, warehouse_path=str(path), updated_by="acceptance"))
    db_session.get(AsyncTaskRecord, task_id).payload_json = FactorPipelineCreate(train_model=False).model_dump_json()
    db_session.commit()
    result = supervise_factor_pipeline(task_id, session_factory=factory, warehouse_path=path,
        poll_seconds=0.1, timeout_seconds=60)
    db_session.expire_all()
    task = db_session.get(AsyncTaskRecord, task_id)
    assert result["exit_code"] == 1
    assert task.status == "failed"
    assert "FACTOR_PIPELINE_DISABLED" in task.errors_json
    assert "FACTOR_WORKER_NO_RESULT" not in task.errors_json


def test_duckdb_oom_is_confined_to_child(db_session, tmp_path, monkeypatch):
    from app.services.factors.pipeline_process import supervise_factor_pipeline
    monkeypatch.setenv("FACTOR_WAREHOUSE_MEMORY_MB", "32")
    task_id, factory = task_for(db_session, "duckdb-oom-process")
    path = tmp_path / "oom.duckdb"
    code = ("import os; from app.services.factors.store import FactorWarehouse; "
            "w=FactorWarehouse(os.environ['FACTOR_PIPELINE_WAREHOUSE_PATH']); "
            "ctx=w.connection(); c=ctx.__enter__(); "
            "c.execute('SELECT list(i) FROM range(10000000) t(i)').fetchone()")
    result = supervise_factor_pipeline(task_id, session_factory=factory, warehouse_path=path,
        child_command=[sys.executable, "-c", code], poll_seconds=0.1, timeout_seconds=45)
    db_session.expire_all()
    assert result["exit_code"] != 0
    assert "OutOfMemory" in Path(result["log_path"]).read_text(encoding="utf-8")
    task = db_session.get(AsyncTaskRecord, task_id)
    assert task.status == "failed"
    assert json.loads(task.payload_json)["window_days"] == 250


def test_oom_classification_is_not_reported_as_a_database_lock():
    from app.services.factors.pipeline_task import _classify_pipeline_error
    code, message = _classify_pipeline_error(MemoryError("DuckDB OutOfMemoryException: failed to allocate"), "factors")
    assert code == "FACTOR_MEMORY_BUDGET_EXCEEDED"
    assert "缩" in message


def test_dispatch_wrapper_keeps_original_factory_after_global_rebind(db_session, tmp_path, monkeypatch):
    from app.models.factor_runtime import FactorSystemConfig
    from app.schemas.async_task import FactorPipelineCreate
    from app.services.factors import pipeline_task
    task_id, factory = task_for(db_session, "bound-before-worker")
    path = tmp_path / "bound.duckdb"
    db_session.add(FactorSystemConfig(id=1, feature_enabled=0, warehouse_path=str(path), updated_by="acceptance"))
    db_session.get(AsyncTaskRecord, task_id).payload_json = FactorPipelineCreate(train_model=False).model_dump_json()
    db_session.commit()
    def forbidden_global_factory():
        raise AssertionError("The worker must not consult the globally rebound DB")
    monkeypatch.setattr(pipeline_task, "get_session_local", forbidden_global_factory)
    pipeline_task._run_factor_pipeline_process(task_id, session_factory=factory)
    db_session.expire_all()
    task = db_session.get(AsyncTaskRecord, task_id)
    assert task.status == "failed" and "FACTOR_PIPELINE_DISABLED" in task.errors_json


def test_supervisor_preserves_child_success_receipt(db_session, tmp_path):
    from app.services.factors.pipeline_process import supervise_factor_pipeline
    task_id, factory = task_for(db_session, "successful-child")
    code = ("import os; from sqlalchemy import create_engine,text; "
            "e=create_engine(os.environ['FACTOR_PIPELINE_DATABASE_URL']); "
            "c=e.connect(); c.execute(text(\"UPDATE async_tasks SET status='done',stage='done',is_terminal_locked=1 WHERE id='successful-child'\")); c.commit(); c.close()")
    result = supervise_factor_pipeline(task_id, session_factory=factory, warehouse_path=tmp_path / "success.duckdb",
        child_command=[sys.executable, "-c", code], poll_seconds=0.02)
    db_session.expire_all()
    task = db_session.get(AsyncTaskRecord, task_id)
    assert result["exit_code"] == 0 and task.status == "done"
    assert not task.errors_json or json.loads(task.errors_json) == []
    assert json.loads(task.result_json)["process_resources"]["rss_limit_bytes"] > 0


def test_python_heap_rss_limit_terminates_child_and_preserves_requested_window(db_session, tmp_path, monkeypatch):
    from app.services.factors.pipeline_process import supervise_factor_pipeline
    monkeypatch.setenv("FACTOR_PIPELINE_RSS_LIMIT_MB", "64")
    task_id, factory = task_for(db_session, "python-heap-budget")
    result = supervise_factor_pipeline(task_id, session_factory=factory, warehouse_path=tmp_path / "rss.duckdb",
        # Touch every page; zero-filled allocation can remain demand-zero and
        # not become resident on Windows/Linux until the process writes it.
        child_command=[sys.executable, "-c", "import time; allocation=bytearray(128*1024*1024); allocation[::4096]=b'x'*(len(allocation)//4096); time.sleep(10)"],
        poll_seconds=0.02, timeout_seconds=15)
    db_session.expire_all()
    task = db_session.get(AsyncTaskRecord, task_id)
    assert result["exit_code"] != 0
    assert task.status == "failed" and "FACTOR_MEMORY_BUDGET_EXCEEDED" in task.errors_json
    resource = json.loads(task.result_json)["process_resources"]
    assert resource["peak_rss_bytes"] > resource["rss_limit_bytes"] == 64*1024*1024
    assert resource["requested_window_days"] == 250 and resource["requested_validation_days"] == 50
    assert resource["rss_scope"] == "process_tree"
    for pid in resource["observed_process_ids"]:
        assert_pid_stopped(pid)


def test_unavailable_rss_monitor_fails_closed_without_starting_child(db_session, tmp_path, monkeypatch):
    from app.services.factors import pipeline_process
    task_id, factory = task_for(db_session, "rss-monitor-unavailable")
    output = tmp_path / "must-not-run.txt"
    def unavailable(_pid):
        raise OSError("RSS monitor unavailable")
    monkeypatch.setattr(pipeline_process, "resident_memory_bytes", unavailable)
    with pytest.raises(OSError, match="RSS monitor unavailable"):
        pipeline_process.supervise_factor_pipeline(task_id, session_factory=factory, warehouse_path=tmp_path / "no-rss.duckdb",
            child_command=[sys.executable, "-c", "from pathlib import Path; Path(__import__('sys').argv[1]).touch()", str(output)])
    assert not output.exists()
    db_session.expire_all()
    assert db_session.get(AsyncTaskRecord, task_id).status == 'failed'


def test_abnormal_parent_exit_cleans_observed_training_descendants(db_session, tmp_path):
    from app.services.factors.pipeline_process import supervise_factor_pipeline
    from app.services.factors.process_memory import resident_memory_bytes, terminate_windows_process
    task_id, factory = task_for(db_session, "abnormal-parent-exit")
    actual_pid_file = tmp_path / "grandchild-pid.txt"
    nested = "import os,time; from pathlib import Path; Path(" + repr(str(actual_pid_file)) + ").write_text(str(os.getpid())); time.sleep(30)"
    code = "import subprocess,sys,time,os; subprocess.Popen([sys.executable,'-c'," + repr(nested) + "]); time.sleep(0.5); os._exit(37)"
    try:
        result = supervise_factor_pipeline(task_id, session_factory=factory, warehouse_path=tmp_path / "orphan.duckdb",
            child_command=[sys.executable, "-c", code], poll_seconds=0.02)
        assert result["exit_code"] == 37 and actual_pid_file.exists()
        assert_pid_stopped(int(actual_pid_file.read_text()))
    finally:
        # Failed red runs must not leave the deliberately spawned sleeper.
        if actual_pid_file.exists():
            pid = int(actual_pid_file.read_text())
            if sys.platform == "win32":
                from app.services.factors.process_memory import process_is_running
                if process_is_running(pid):
                    terminate_windows_process(pid)
            else:
                import signal
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_native_exit_rolls_back_uncommitted_model_and_preserves_forensic_file(db_session, tmp_path):
    from app.models.factor_model import FactorModelRun
    from app.services.factors.pipeline_process import supervise_factor_pipeline
    task_id, factory = task_for(db_session, 'native-model-transaction')
    previous = FactorModelRun(id='previous-validated-model', status='validated')
    db_session.add(previous)
    db_session.commit()
    evidence = tmp_path / 'uncommitted-input-snapshot.txt'
    code = ("import os; from pathlib import Path; from sqlalchemy import create_engine; "
            "from app.models.factor_model import FactorModelRun; "
            "e=create_engine(os.environ['FACTOR_PIPELINE_DATABASE_URL']); c=e.connect(); "
            "c.execute(FactorModelRun.__table__.insert().values(id='native-half-model',status='training')); "
            "Path(" + repr(str(evidence)) + ").write_text('model insert executed without commit'); os._exit(37)")
    result = supervise_factor_pipeline(task_id, session_factory=factory, warehouse_path=tmp_path / 'native-model.duckdb',
        child_command=[sys.executable, '-c', code], poll_seconds=0.05)
    assert result['exit_code'] == 37 and evidence.exists()
    db_session.expire_all()
    assert db_session.get(FactorModelRun, 'native-half-model') is None
    assert db_session.get(FactorModelRun, previous.id).status == 'validated'
    assert db_session.get(AsyncTaskRecord, task_id).status == 'failed'


def test_terminal_receipt_handoff_waits_without_overlapping_warehouse_owner(tmp_path):
    from app.services.factors.store import external_warehouse_worker, FactorWarehouseUnavailable
    path=tmp_path/'handoff.duckdb'
    attempted=threading.Event(); acquired=threading.Event()
    def next_owner():
        attempted.set()
        with external_warehouse_worker(path,wait_for_worker=True,timeout_seconds=2):
            acquired.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with external_warehouse_worker(path):
            future=pool.submit(next_owner)
            assert attempted.wait(1)
            assert not acquired.wait(.1)
            with pytest.raises(FactorWarehouseUnavailable):
                with external_warehouse_worker(path,wait_for_worker=True,timeout_seconds=.02):pass
            assert not acquired.is_set()
            # An unrelated warehouse remains usable during the handoff.
            with external_warehouse_worker(tmp_path/'unrelated.duckdb'):pass
        future.result(timeout=2)
    assert acquired.is_set()
    with external_warehouse_worker(path):pass
