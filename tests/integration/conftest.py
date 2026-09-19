"""集成测试基础设施：因子中心评价全面回归。

提供核心 fixture/工具：
- app: session scoped FastAPI 应用（确保 factor_evaluation 路由挂载）
- client: function scoped TestClient
- isolated_db_session: function scoped 隔离数据库会话（MySQL/SQLite fallback）
- wait_for_task_status: 纯函数，轮询等待任务状态
- seed_factor_close: function scoped，准备 close 因子定义 mock
"""
from __future__ import annotations

import json as _json
import os
import sys
import time
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import TypeDecorator, Text as _Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _stub_mod in ("akshare", "sklearn", "sklearn.linear_model", "sklearn.metrics"):
    try:
        __import__(_stub_mod)
    except ImportError:
        sys.modules[_stub_mod] = MagicMock(name=f"{_stub_mod}_stub")


class JSON(TypeDecorator):
    """SQLite 兼容的 JSON 类型装饰器（兜底用）。

    MySQL 专有 JSON 类型在 SQLite 中不被支持，这里统一用 TEXT + json.dumps/loads。
    """
    impl = _Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return _json.dumps(value, ensure_ascii=False) if value is not None else None

    def process_result_value(self, value, dialect):
        return _json.loads(value) if value is not None else None


def _ensure_json_compat(engine: Engine) -> None:
    """SQLite 下对 JSON 列做类型适配（若模型直接使用了 sqlalchemy.JSON）。

    通过 sqlite 方言的列类型替换事件实现兼容。
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _sqlite_connect(dbapi_conn, _rec):
        dbapi_conn.isolation_level = None
        # Background evaluation workers use independent connections while the
        # test keeps a savepoint open. WAL allows readers and writers to make
        # progress concurrently; busy_timeout absorbs short commit bursts.
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=60000")
        finally:
            cur.close()


# ══════════════════════════════════════════════════════════════════════════════
# 0) TD2 hardening: contain cross-test leaks of module-level patch targets
# ══════════════════════════════════════════════════════════════════════════════
# SyntheticMockBase（test_factor_evaluation_blockers）等用例用
# unittest.mock.patch 替换 app.services.factors.wp5_eval_task 的模块级名字
# （FactorWarehouse / FactorCompiler / ...），由各用例自己的 finally 负责
# 恢复。任何一次 stop 被吞 / 未执行都会让绑定停留在 MagicMock 上并污染
# 本 session 之后的所有用例——实测症状（run1 复盘）：e2e 的 worker 从模块
# 绑定解析 FactorWarehouse 时拿到 blockers 的 MagicMock，其 horizon 映射
# {5: "batch_t5_syn"} 使 horizon=6 直接 eval.data.target_horizon_unavailable。
# 防线：session 最早时刻（尚无任何 patcher 生效）快照 pristine 绑定；
# 每个用例结束后把漂移的绑定强制还原，把泄漏寿命限制在一个用例之内。
_WP5_BINDING_PRISTINE: list[tuple[Any, str, Any]] = []


def _build_wp5_pristine_bindings() -> list[tuple[Any, str, Any]]:
    import app.services.factors.wp5_eval_task as _wp5
    import app.services.factors.trade_calendar as _tc_mod
    import app.services.factors.factor_stress as _fs_mod
    import app.services.factors.factor_registry as _reg_mod

    targets: list[tuple[Any, str, Any]] = []
    for mod, names in (
        (_wp5, (
            "FactorWarehouse", "FactorCompiler", "FactorExecutor",
            "compile_formula", "run_stress_test", "latest_complete_trade_date",
            "get_factor_by_code", "get_latest_version",
        )),
        (_tc_mod, ("latest_complete_trade_date",)),
        (_fs_mod, ("run_stress_test",)),
        (_reg_mod, ("get_factor_by_code", "get_latest_version")),
    ):
        for _name in names:
            try:
                targets.append((mod, _name, getattr(mod, _name)))
            except Exception:
                continue
    return targets


@pytest.fixture(scope="session", autouse=True)
def _wp5_binding_pristine_snapshot():
    """session 最早时刻快照 pristine 绑定（此时还没有任何 patcher 生效）。"""
    global _WP5_BINDING_PRISTINE
    if not _WP5_BINDING_PRISTINE:
        try:
            _WP5_BINDING_PRISTINE = _build_wp5_pristine_bindings()
        except Exception:
            _WP5_BINDING_PRISTINE = []
    yield


@pytest.fixture(scope="function", autouse=True)
def _wp5_binding_leak_guard():
    """每个用例结束后，把被泄漏 patch 污染的模块级绑定强制还原（TD2 防线）。"""
    yield
    for _mod, _name, _pristine in _WP5_BINDING_PRISTINE:
        try:
            if getattr(_mod, _name, None) is not _pristine:
                setattr(_mod, _name, _pristine)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# 1) app fixture (session scoped)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def app():
    """构造或获取 FastAPI 应用，确保 factor_evaluation 路由已挂载。"""
    from app.main import app as _app
    from app.api.routes import factor_evaluation

    _all_paths = {getattr(r, "path", "") for r in _app.routes}

    has_preflight_v1 = any("/factor-evaluation/preflight" in p for p in _all_paths)
    has_preflight_raw = any(p.endswith("/factor-evaluation/preflight") for p in _all_paths)

    if not (has_preflight_v1 or has_preflight_raw):
        _app.include_router(factor_evaluation.router, prefix="")

    final_paths = [getattr(r, "path", "") for r in _app.routes]
    assert any(
        p.endswith("/factor-evaluation/preflight") for p in final_paths
    ), "factor_evaluation router not properly mounted (missing /factor-evaluation/preflight endpoint)"

    return _app


# ══════════════════════════════════════════════════════════════════════════════
# 2) client fixture (function scoped)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="function")
def client(app, isolated_db_session):
    """返回 TestClient 实例（function scoped）。"""
    from fastapi.testclient import TestClient
    with TestClient(app) as tc:
        yield tc


# ══════════════════════════════════════════════════════════════════════════════
# 3) isolated_db_session fixture (function scoped)
# ══════════════════════════════════════════════════════════════════════════════

def _build_mysql_test_url() -> str | None:
    """使用 test_ 前缀 MySQL 测试库：不存在则自动 CREATE；缺表时由 init_db 补齐。"""
    try:
        from app.core.config import load_db_config, build_mysql_url
        cfg = load_db_config()
        if not cfg.get("use_mysql") or not cfg.get("mysql", {}).get("host"):
            return None
        mysql_cfg = dict(cfg["mysql"])
        original_db = mysql_cfg.get("database", "")
        if not original_db.startswith("test_"):
            test_db_name = "test_" + original_db
        else:
            test_db_name = original_db
        host = mysql_cfg.get("host")
        port = int(mysql_cfg.get("port") or 3306)
        user = mysql_cfg.get("user")
        pwd = mysql_cfg.get("password") or ""
        try:
            import pymysql
            conn = pymysql.connect(
                host=host, port=port, user=user, password=pwd,
                connect_timeout=5, charset="utf8mb4",
            )
            cur = conn.cursor()
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{test_db_name}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            return None
        mysql_cfg["database"] = test_db_name
        cfg2 = dict(cfg)
        cfg2["mysql"] = mysql_cfg
        url = build_mysql_url(cfg2)
        if _test_mysql_connectable(url):
            # 确保 test_ 库有完整 schema
            try:
                from app.db.init_db import init_db as _run_init_db
                from app.db.manager import DatabaseManager
                mgr = DatabaseManager.get()
                saved = (mgr._engine, mgr._session_factory, mgr._db_type)
                try:
                    extra = dict(
                        pool_pre_ping=True, pool_recycle=60,
                        connect_args={
                            "connect_timeout": 10, "read_timeout": 60,
                            "write_timeout": 60, "charset": "utf8mb4",
                        },
                    )
                    mgr.initialize(url, db_type="mysql", **extra)
                    _run_init_db()
                finally:
                    try:
                        if mgr._engine is not None:
                            mgr._engine.dispose()
                    except Exception:
                        pass
                    mgr._engine, mgr._session_factory, mgr._db_type = saved
            except Exception:
                pass
            return url
        return None
    except Exception:
        return None


def _test_mysql_connectable(url: str) -> bool:
    """快速检测 MySQL URL 是否可连接（3s 超时）。"""
    try:
        eng = create_engine(url, connect_args={"connect_timeout": 3})
        with eng.connect():
            return True
    except Exception:
        return False
    finally:
        try:
            eng.dispose()
        except Exception:
            pass


@pytest.fixture(scope="function")
def isolated_db_session(request) -> Session:
    """隔离数据库会话：每个函数独立事务 → rollback。

    优先级：DATABASE_URL_TEST env > MySQL test_ 库 > SQLite 内存库。
    """
    from app.db.base import Base
    from app.models import async_task, factor_evaluation  # noqa: F401 - 注册模型
    from app.models import (  # noqa: F401 - 显式导入确保外键目标表存在
        portfolio, symbol, watchlist, daily_bar, score, scan,
        signal_rule, trade_setup, journal_entry, news_event, alert,
        macro_data, factor, sim_account, discovery,
    )

    db_url = os.environ.get("DATABASE_URL_TEST")
    db_type = None
    use_fallback_sqlite = False

    if db_url:
        if db_url.startswith(("mysql", "mariadb", "pymysql")):
            db_type = "mysql"
        else:
            db_type = "sqlite"
    else:
        # Real MySQL integration is explicit via DATABASE_URL_TEST only.
        # Generic test runs must never touch the developer's configured DB.
        fd, tmp_path = tempfile.mkstemp(
            suffix=".sqlite3", prefix="integration_", dir=str(ROOT)
        )
        os.close(fd)
        tmp_db = Path(tmp_path)
        db_url = f"sqlite:///{tmp_db.as_posix()}"
        db_type = "sqlite"
        use_fallback_sqlite = True

    engine_kwargs: dict[str, Any] = {}
    if db_type == "sqlite":
        engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 60}
    elif db_type == "mysql":
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_recycle"] = 60
        engine_kwargs["connect_args"] = {
            "connect_timeout": 10,
            "read_timeout": 60,
            "write_timeout": 60,
            "charset": "utf8mb4",
        }

    engine = create_engine(db_url, **engine_kwargs)
    _ensure_json_compat(engine)

    if db_type == "mysql":
        @event.listens_for(engine, "connect")
        def _mysql_init(dbapi_conn, _rec):
            try:
                cur = dbapi_conn.cursor()
                cur.execute("SET default_storage_engine=InnoDB")
                cur.execute("SET sql_mode='NO_ENGINE_SUBSTITUTION'")
                cur.execute("SET NAMES utf8mb4")
                cur.execute("SET SESSION innodb_strict_mode=OFF")
                cur.close()
            except Exception:
                pass

    if db_type != "mysql":
        Base.metadata.create_all(engine)

    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    from app.db.manager import DatabaseManager
    mgr = DatabaseManager.get()
    with mgr._lock:
        try:
            if mgr._engine is not None:
                mgr._engine.dispose()
        except Exception:
            pass
        mgr._engine = engine
        mgr._session_factory = SessionFactory
        mgr._db_type = db_type
        # TD2 (tests/integration order pollution): app.db.session caches the
        # control-plane factory in module globals on first use; without a
        # reset the cache keeps pointing at the FIRST test's temporary sqlite
        # database (whose file is unlinked during its teardown), so every
        # later test polls task status against a dead DB.  Drop the cache via
        # the official invalidation interface (TD2 收尾裁决新增) so each test
        # rebuilds it from the engine just bound above.
        try:
            import app.db.session as _app_db_session
            _app_db_session.reset_control_plane_cache()
        except Exception:
            pass

    # The FastAPI lifespan initializes DatabaseManager from DATABASE_URL.  Bind
    # it to this fixture's engine before TestClient starts, then restore it on
    # teardown so tests never silently connect to the operator's MySQL DB.
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url

    connection = engine.connect()
    outer_transaction = connection.begin()

    session = SessionFactory(bind=connection)

    inner_savepoint = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, trans):
        nonlocal inner_savepoint
        if trans.nested and not trans._parent.nested:
            inner_savepoint = connection.begin_nested()

    try:
        yield session
    finally:
        # Do not rotate DatabaseManager to the next test while a previous
        # evaluation worker still owns a SQLite transaction. This otherwise
        # leaks writes across temporary databases and produces intermittent
        # lock/PendingRollbackError failures in stress scenarios.
        try:
            from app.services.async_tasks import wait_workers_stopped
            wait_workers_stopped(10)
        except Exception:
            pass
        # TD2: drop this test's control-plane factory/engine BEFORE the tmp
        # database file is unlinked at the end of teardown.  The control-plane
        # engine was built from this test's engine URL and may still hold open
        # handles on the tmp file -- on Windows an open handle makes unlink()
        # silently fail (it is swallowed below), leaving stale files behind.
        # Disposing here releases those handles and guarantees the next test
        # starts with an empty control-plane cache.  (TD2 收尾裁决：改走
        # app/db/session.py 的公开失效接口 reset_control_plane_cache。)
        try:
            import app.db.session as _app_db_session
            _app_db_session.reset_control_plane_cache()
        except Exception:
            pass
        try:
            session.close()
        except Exception:
            pass
        try:
            if inner_savepoint.is_active:
                inner_savepoint.rollback()
        except Exception:
            pass
        try:
            if outer_transaction.is_active:
                outer_transaction.rollback()
        except Exception:
            pass
        try:
            connection.close()
        except Exception:
            pass
        try:
            engine.dispose()
        except Exception:
            pass
        # TD2: the FastAPI lifespan calls initialize_runtime_database(), which
        # re-initializes DatabaseManager from DATABASE_URL (this test's tmp
        # URL) and REPLACES the fixture engine with a second engine object
        # owned by the manager (manager.py: create_engine).  Its pooled
        # connections keep the tmp sqlite file open after this fixture's own
        # engine is disposed above, so tmp_db.unlink() below fails with
        # PermissionError(13) (swallowed silently) -- the historical
        # integration_* litter source, ~19 files per full-suite run.
        # Dispose the manager-owned engine before unlinking; the next test's
        # setup rebinds the manager under the same lock anyway.
        try:
            with mgr._lock:
                _mgr_engine = mgr._engine
            if _mgr_engine is not None and _mgr_engine is not engine:
                _mgr_engine.dispose()
        except Exception:
            pass
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        if use_fallback_sqlite:
            # TD2: last-resort sweep.  Any ORM Session/Connection leaked
            # anywhere in the process (app-side code paths are out of this
            # card's write scope) keeps a checked-out sqlite connection open
            # even after every engine was disposed above -- dispose() closes
            # POOLED connections but never checked-out ones.  That is what
            # made tmp_db.unlink() below fail with PermissionError(13) for
            # every TestClient-starting test (~19 per suite run) and silently
            # re-created the historical integration_* litter.  Close every
            # orphaned session/connection via gc, then retry the unlink a few
            # times before logging a diagnostic line.
            import gc as _gc
            try:
                from sqlalchemy.orm import Session as _OrmSession
                from sqlalchemy.engine import Connection as _SqlaConnection
                _gc.collect()
                for _obj in _gc.get_objects():
                    if isinstance(_obj, _OrmSession):
                        try:
                            _obj.close()
                        except Exception:
                            pass
                    elif isinstance(_obj, _SqlaConnection):
                        try:
                            _obj.close()
                        except Exception:
                            pass
                _gc.collect()
            except Exception:
                pass
            for _attempt in range(4):
                try:
                    if tmp_db.exists():
                        tmp_db.unlink()
                    break
                except Exception as _unlink_exc:
                    if _attempt == 3:
                        try:
                            _diag = ROOT / ".workbuddy" / "mining" / "_td2_unlink_failures.txt"
                            with _diag.open("a", encoding="utf-8") as _fh:
                                _fh.write(
                                    f"{request.node.nodeid}\t{repr(_unlink_exc)}\n"
                                )
                        except Exception:
                            pass
                    else:
                        time.sleep(0.3)


# ══════════════════════════════════════════════════════════════════════════════
# 4) wait_for_task_status 纯函数
# ══════════════════════════════════════════════════════════════════════════════

def wait_for_task_status(
    task_id: str,
    target_statuses: set[str],
    timeout: float = 60,
    poll_interval: float = 0.3,
) -> Any:
    """轮询等待任务达到目标状态集合。

    从 wp5_eval_task 导入 get_evaluation_task 并调用，
    若状态 ∈ target_statuses 立即返回该 task 对象；
    超时则抛 TimeoutError。
    """
    from app.services.factors.wp5_eval_task import get_evaluation_task

    deadline = time.monotonic() + timeout
    last_task = None
    while True:
        task = get_evaluation_task(task_id)
        last_task = task
        if task is not None:
            status = getattr(task, "status", None)
            if isinstance(task, dict):
                status = task.get("status")
            if status in target_statuses:
                return task
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_status = None
            if last_task is not None:
                if isinstance(last_task, dict):
                    last_status = last_task.get("status")
                else:
                    last_status = getattr(last_task, "status", None)
            raise TimeoutError(
                f"Task {task_id} did not reach {target_statuses} in {timeout}s, "
                f"last status={last_status}"
            )
        sleep_for = min(poll_interval, remaining)
        time.sleep(sleep_for)


# ══════════════════════════════════════════════════════════════════════════════
# 5) seed_factor_close fixture (function scoped, 可选)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="function")
def seed_factor_close(isolated_db_session):
    """准备 close 因子定义：优先从 DB 真实查找；否则独立事务 COMMIT fake records（跨连接可见）。"""
    from app.services.factors import factor_registry
    from app.models.factor import Factor
    from app.models.factor_model import FactorVersion
    from app.db.session import get_session_local

    db = isolated_db_session
    FID = 99999

    try:
        from sqlalchemy import select
        row = db.execute(
            select(Factor).where(Factor.code == "close")
        ).scalars().first()
        if row is not None:
            vrow = factor_registry.get_latest_version(db, row.id)
            if vrow is not None:
                yield {
                    "factor": row,
                    "version": vrow,
                    "patched": False,
                    "factor_id": row.id,
                    "version_id": vrow.id,
                }
                return
    except Exception:
        pass

    # 用独立 session COMMIT 插入（保证 worker 其他连接可见），最后 DELETE 清理
    SessionLocal_ = get_session_local()
    cleanup_session = SessionLocal_()
    inserted_factor = False
    inserted_version = False
    try:
        from sqlalchemy import text
        try:
            cleanup_session.execute(
                text(
                    "INSERT INTO factors (id, code, name, category, direction, status, description, formula_expr, "
                    "factor_kind, source_type, frequency, default_missing_policy, is_active, created_at, updated_at) "
                    "VALUES (:id, 'close', 'Close Price', 'price', 'higher_better', 'active', 'regression fixture', "
                    "'close', 'continuous', 'local_akshare', 'daily', 'exclude', 1, NOW(), NOW())"
                ),
                {"id": FID},
            )
            cleanup_session.commit()
            inserted_factor = True
        except Exception:
            cleanup_session.rollback()
        # SQLite enforces the factor_evaluation_runs -> factor_versions FK.
        # The legacy raw INSERT above predates newer non-null version columns;
        # fall back to the ORM so defaults are applied consistently.
        if not inserted_factor:
            try:
                cleanup_session.add(Factor(
                    id=FID, code="close", name="Close Price", category="price",
                    direction="higher_better", status="active",
                    description="regression fixture", formula_expr="close",
                    factor_kind="continuous", source_type="local_akshare",
                    frequency="daily", default_missing_policy="exclude", is_active=1,
                ))
                cleanup_session.commit()
                inserted_factor = True
            except Exception:
                cleanup_session.rollback()
        try:
            cleanup_session.execute(
                text(
                    "INSERT INTO factor_versions (id, factor_id, version, formula_expr, direction, is_latest, created_at) "
                    "VALUES (:id, :fid, 1, 'close', 'higher_better', 1, NOW())"
                ),
                {"id": FID, "fid": FID},
            )
            cleanup_session.commit()
            inserted_version = True
        except Exception:
            cleanup_session.rollback()
        if not inserted_version:
            try:
                cleanup_session.add(FactorVersion(
                    id=FID, factor_id=FID, version=1, formula_expr="close",
                    direction="higher_better", is_latest=1,
                ))
                cleanup_session.commit()
                inserted_version = True
            except Exception:
                cleanup_session.rollback()
    finally:
        cleanup_session.close()

    mock_factor = MagicMock()
    mock_factor.factor_code = "close"
    mock_factor.code = "close"
    mock_factor.kind = "continuous"
    mock_factor.factor_kind = "continuous"
    mock_factor.id = FID
    mock_factor.formula_expr = "close"
    mock_factor.direction = "higher_better"

    mock_version = MagicMock()
    mock_version.id = FID
    mock_version.formula = "close"
    mock_version.formula_expr = "close"
    mock_version.postprocess = {}
    mock_version.postprocess_json = None
    mock_version.params_json = None
    mock_version.direction = "higher_better"
    mock_version.factor_id = FID
    mock_version.is_latest = 1
    mock_version.version = 1

    from unittest.mock import patch

    patcher1 = patch.object(factor_registry, "get_factor_by_code", return_value=mock_factor)
    patcher2 = patch.object(factor_registry, "get_latest_version", return_value=mock_version)
    from app.services.factors import wp5_eval_task
    patcher3 = patch.object(wp5_eval_task, "get_factor_by_code", return_value=mock_factor)
    patcher4 = patch.object(wp5_eval_task, "get_latest_version", return_value=mock_version)

    patcher1.start()
    patcher2.start()
    patcher3.start()
    patcher4.start()
    try:
        yield {
            "factor": mock_factor,
            "version": mock_version,
            "patched": True,
            "factor_id": FID,
            "version_id": FID,
        }
    finally:
        try:
            patcher4.stop()
        except Exception:
            pass
        try:
            patcher3.stop()
        except Exception:
            pass
        try:
            patcher2.stop()
        except Exception:
            pass
        try:
            patcher1.stop()
        except Exception:
            pass
        # 清理 fake records（DELETE + COMMIT）
        cleanup_session = None
        try:
            from sqlalchemy import text
            cleanup_session = SessionLocal_()
            if inserted_version:
                try:
                    cleanup_session.execute(
                        text("DELETE FROM factor_versions WHERE id=:id"), {"id": FID}
                    )
                    cleanup_session.commit()
                except Exception:
                    cleanup_session.rollback()
            if inserted_factor:
                try:
                    cleanup_session.execute(
                        text("DELETE FROM factors WHERE id=:id"), {"id": FID}
                    )
                    cleanup_session.commit()
                except Exception:
                    cleanup_session.rollback()
        finally:
            if cleanup_session is not None:
                try:
                    cleanup_session.close()
                except Exception:
                    pass
