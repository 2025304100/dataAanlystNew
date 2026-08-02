"""
DatabaseManager — 数据库引擎热切换管理器

单例模式，支持在运行时替换 SQLAlchemy engine 和 session factory，
实现 SQLite ↔ MySQL 的无缝切换。
"""
from __future__ import annotations

import os
import threading
from typing import ClassVar

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class DatabaseManager:
    """线程安全的数据库引擎管理器（单例）。"""

    _instance: ClassVar[DatabaseManager | None] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._engine: Engine | None = None
        self._session_factory: sessionmaker | None = None
        self._db_type: str = "sqlite"  # "sqlite" | "mysql"

    @classmethod
    def get(cls) -> DatabaseManager:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── 核心方法 ──────────────────────────────────────────

    def initialize(self, url: str, db_type: str = "sqlite", **extra_kwargs) -> None:
        """创建新的 engine + session factory，dispose 旧连接。

        Parameters
        ----------
        url : str
            SQLAlchemy 数据库 URL。
        db_type : str
            "sqlite" 或 "mysql"。
        **extra_kwargs
            透传给 create_engine 的额外参数。
        """
        with self._lock:
            # 关闭旧连接池
            if self._engine is not None:
                try:
                    self._engine.dispose()
                except Exception:
                    pass

            engine_kwargs: dict = dict(extra_kwargs)

            if db_type == "sqlite":
                engine_kwargs.setdefault("connect_args", {"check_same_thread": False, "timeout": 30})
            elif db_type == "mysql":
                engine_kwargs.setdefault("pool_pre_ping", True)
                # Recycle sockets before a low MySQL wait_timeout closes them.
                # LIFO favors the most recently verified pooled connection.
                engine_kwargs.setdefault(
                    "pool_recycle", int(os.environ.get("DB_POOL_RECYCLE", "60"))
                )
                engine_kwargs.setdefault("pool_use_lifo", True)
                # 连接池大小：支持并发挖掘（3 worker + 主线程 + watchdog + HTTP 请求）
                # 可通过环境变量 DB_POOL_SIZE / DB_MAX_OVERFLOW 覆盖
                engine_kwargs.setdefault("pool_size", int(os.environ.get("DB_POOL_SIZE", "10")))
                engine_kwargs.setdefault("max_overflow", int(os.environ.get("DB_MAX_OVERFLOW", "20")))
                # pymysql 连接超时：防止 DB 操作永久卡住（Lost connection / 连接被 MySQL 关闭）
                # Defaults: connect_timeout=10s, read/write timeout=60s.
                # WPD-05: charset=utf8mb4 确保 pymysql 连接级字符集正确，
                # 配合下方 SET NAMES utf8mb4 事件监听双重保障中文不乱码。
                engine_kwargs.setdefault("connect_args", {
                    "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "10")),
                    "read_timeout": int(os.environ.get("DB_READ_TIMEOUT", "60")),
                    "write_timeout": int(os.environ.get("DB_WRITE_TIMEOUT", "60")),
                    "charset": "utf8mb4",
                })

            self._engine = create_engine(url, **engine_kwargs)

            # MySQL: 连接后自动设置 sql_mode 和 charset
            if db_type == "mysql":
                @event.listens_for(self._engine, "connect")
                def _set_mysql_mode(dbapi_conn, _connection_record):
                    cursor = dbapi_conn.cursor()
                    cursor.execute("SET sql_mode='NO_ENGINE_SUBSTITUTION'")
                    cursor.execute("SET NAMES utf8mb4")
                    cursor.execute("SET default_storage_engine=InnoDB")
                    cursor.close()

            self._session_factory = sessionmaker(
                bind=self._engine,
                autoflush=False,
                autocommit=False,
                future=True,
            )
            self._db_type = db_type

    # ── 属性 ──────────────────────────────────────────────

    @property
    def engine(self) -> Engine:
        with self._lock:
            if self._engine is None:
                raise RuntimeError("DatabaseManager not initialized. Call initialize() first.")
            return self._engine

    @property
    def session_factory(self) -> sessionmaker:
        with self._lock:
            if self._session_factory is None:
                raise RuntimeError("DatabaseManager not initialized. Call initialize() first.")
            return self._session_factory

    @property
    def db_type(self) -> str:
        return self._db_type

    @property
    def is_mysql(self) -> bool:
        return self._db_type == "mysql"

    @property
    def is_sqlite(self) -> bool:
        return self._db_type == "sqlite"

    # ── 会话工厂方法 ──────────────────────────────────────

    def get_session(self) -> Session:
        """创建一个新的数据库会话。"""
        return self.session_factory()

    def dispose(self) -> None:
        """关闭连接池，释放资源。"""
        with self._lock:
            if self._engine is not None:
                try:
                    self._engine.dispose()
                except Exception:
                    pass
