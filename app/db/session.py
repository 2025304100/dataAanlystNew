"""
数据库会话管理

所有 engine / SessionLocal 的创建委托给 DatabaseManager 单例，
支持运行时热切换数据库（SQLite ↔ MySQL）。

对外仍导出：
- get_db()         : FastAPI 依赖注入用的 session 生成器
- get_engine()     : 获取当前 engine（供 init_db 等模块使用）
- get_session_local() : 获取 session factory（供 main.py / discovery_tasks 使用）
- get_control_session_local() : 获取「控制平面」专用 session factory（NullPool，
  不与数据面重任务共用连接池，避免任务多时提交/心跳/status 查排队 100s+
  触发前端 axios 90s 超时）。
"""
from __future__ import annotations

import threading
from collections.abc import Generator

from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import Session, sessionmaker

from app.db.manager import DatabaseManager


# ── 控制平面 NullPool 引擎 ─────────────────────────────────────
# 专门给指数同步提交 / 心跳查询 / 状态列表等「轻量但对延迟敏感」的接口使用。
# 数据面重任务（universe_init / smart_sync / index worker）会占用主连接池
# 30 个连接长达数分钟，此时如果 submit/status/heartbeat 也去抢同一池，会在
# QueuePool 队列里排 100s+，前端 axios 90s 超时 → 用户截图中的
# 『提交同步任务失败: 请求超时』。
# NullPool 模式：每次请求都新开、用完关，完全不参与池排队；新建一条 MySQL
# TCP 连接约 5~15ms，对提交 33ms / 心跳 25ms 级的接口完全可接受。
_cp_lock = threading.Lock()
_cp_engine = None
_cp_factory = None


def _make_control_plane_factory():
    """基于 DatabaseManager 中当前生效的 DB URL / connect_args
    重新造一个独立的 NullPool engine + session factory。"""
    from sqlalchemy.pool import NullPool

    global _cp_engine, _cp_factory
    dm = DatabaseManager.get()
    main_engine = dm.engine
    url = main_engine.url
    db_type = dm.db_type
    kwargs: dict = {"poolclass": NullPool}

    if db_type == "mysql":
        # 复用主引擎同款 connect_args（timeout、charset），但关闭任何 pool。
        main_connect_args = getattr(main_engine, "pool", None)
        # 实际 connect_args 在 Dialect 的 connect_args 上；也可以直接从
        # main_engine.url.query / 初始化时保留的 connect_args 副本推导，
        # 但最稳是调用 engine.dialect.create_connect_args() 解析出参数。
        try:
            cargs, cparams = main_engine.dialect.create_connect_args(main_engine.url)
            # cparams 是 pymysql.connect(**params) 那套，已经含 read/write/connect timeout
            kwargs["connect_args"] = dict(cparams)
        except Exception:
            # Fallback: 显式写保守 connect_args
            kwargs["connect_args"] = {
                "connect_timeout": 10,
                "read_timeout": 20,
                "write_timeout": 20,
                "charset": "utf8mb4",
            }

    _cp_engine = create_engine(url, **kwargs)

    if db_type == "mysql":
        @sa_event.listens_for(_cp_engine, "connect")
        def _set_mysql_mode(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            try:
                cur.execute("SET sql_mode='NO_ENGINE_SUBSTITUTION'")
                cur.execute("SET NAMES utf8mb4")
                cur.execute("SET default_storage_engine=InnoDB")
            finally:
                cur.close()
    elif db_type == "sqlite":
        @sa_event.listens_for(_cp_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA foreign_keys=ON")
            finally:
                cur.close()

    _cp_factory = sessionmaker(
        bind=_cp_engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )


def get_control_session_local():
    """获取控制平面 SessionLocal 工厂（NullPool，每次新开连接）。"""
    global _cp_factory
    if _cp_factory is None:
        with _cp_lock:
            if _cp_factory is None:
                _make_control_plane_factory()
    return _cp_factory


# ── 原有对外 API ────────────────────────────────────────────────

def get_engine():
    """获取当前活动的 SQLAlchemy Engine。"""
    return DatabaseManager.get().engine


def get_session_local():
    """获取当前活动的 sessionmaker 工厂。"""
    return DatabaseManager.get().session_factory


# ── 向后兼容的属性代理 ────────────────────────────────────
# 部分模块直接 import engine / SessionLocal，
# 这里用延迟求值确保拿到的是当前活动的实例。

class _EngineProxy:
    """代理对象：访问任何属性时转发到当前 engine。"""
    def __getattr__(self, name):
        return getattr(DatabaseManager.get().engine, name)


class _SessionLocalProxy:
    """代理对象：调用时创建 session，使用当前 session factory。"""
    def __call__(self, **kwargs):
        return DatabaseManager.get().session_factory(**kwargs)

    def __getattr__(self, name):
        return getattr(DatabaseManager.get().session_factory, name)


engine = _EngineProxy()
SessionLocal = _SessionLocalProxy()


def get_db() -> Generator[Session, None, None]:
    """FastAPI Depends() 注入用的 session 生成器。"""
    db = DatabaseManager.get().get_session()
    try:
        yield db
    finally:
        db.close()
