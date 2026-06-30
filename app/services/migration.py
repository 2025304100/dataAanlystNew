"""SQLite → MySQL 数据迁移引擎

支持一键将本地 SQLite 数据库的全部数据迁移到远程 MySQL，
包含拓扑排序（外键安全顺序）、分批插入、进度上报、幂等重试。
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from app.core.config import settings
from app.db.base import Base
from app.db.manager import DatabaseManager

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000

# ── 全局迁移状态 ──────────────────────────────────────────

_migration_state: dict = {
    "status": "idle",
    "current_table": None,
    "tables_done": 0,
    "tables_total": 0,
    "rows_migrated": 0,
    "error": None,
}
_migration_lock = threading.Lock()


def get_migration_status() -> dict:
    """返回当前迁移进度的快照。"""
    return dict(_migration_state)


def is_migration_running() -> bool:
    return _migration_state["status"] == "running"


# ── 拓扑排序 ─────────────────────────────────────────────

def _topological_sort_tables(metadata) -> list[str]:
    """按外键依赖对表名进行拓扑排序，返回安全的插入顺序。"""
    all_tables = set(metadata.tables.keys())
    in_degree: dict[str, int] = {t: 0 for t in all_tables}
    graph: dict[str, list[str]] = defaultdict(list)

    for table_name, table in metadata.tables.items():
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent != table_name and parent in all_tables:
                graph[parent].append(table_name)
                in_degree[table_name] = in_degree.get(table_name, 0) + 1

    queue = deque(t for t in all_tables if in_degree.get(t, 0) == 0)
    result: list[str] = []

    while queue:
        node = queue.popleft()
        result.append(node)
        for child in graph.get(node, []):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    # 如果有循环依赖，把剩余的表追加到末尾（迁移时会禁用 FK 检查）
    remaining = [t for t in all_tables if t not in result]
    if remaining:
        logger.warning("Circular FK dependency detected among: %s — appending with FK checks disabled", remaining)
        result.extend(sorted(remaining))

    return result


# ── 迁移主逻辑 ───────────────────────────────────────────

def run_migration() -> dict:
    """执行 SQLite → MySQL 的数据迁移。

    Returns
    -------
    dict
        最终迁移状态。
    """
    global _migration_state

    with _migration_lock:
        if _migration_state["status"] == "running":
            return {"status": "error", "error": "迁移任务正在进行中"}
        _migration_state = {
            "status": "running",
            "current_table": None,
            "tables_done": 0,
            "tables_total": 0,
            "rows_migrated": 0,
            "error": None,
        }

    mgr = DatabaseManager.get()

    if not mgr.is_mysql:
        _migration_state["status"] = "failed"
        _migration_state["error"] = "当前数据库不是 MySQL，迁移仅支持 SQLite → MySQL"
        return get_migration_status()

    sqlite_engine: Engine | None = None
    mysql_engine = mgr.engine

    try:
        # 创建独立的 SQLite engine（始终读本地默认库）
        sqlite_engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
        )

        # 验证 SQLite 连接
        with sqlite_engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        # 获取排序后的表列表
        table_order = _topological_sort_tables(Base.metadata)
        _migration_state["tables_total"] = len(table_order)

        # 确保 MySQL 上所有表已创建
        Base.metadata.create_all(bind=mysql_engine)

        # 禁用 MySQL 外键检查（加速 + 避免顺序问题）
        with mysql_engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))

        # 逐表迁移
        total_rows = 0
        for i, table_name in enumerate(table_order):
            _migration_state["current_table"] = table_name
            table = Base.metadata.tables[table_name]

            try:
                # 从 SQLite 读取全部数据
                with sqlite_engine.connect() as src_conn:
                    rows = src_conn.execute(table.select()).fetchall()

                # 先清空 MySQL 目标表（幂等设计）
                with mysql_engine.begin() as mysql_conn:
                    mysql_conn.execute(table.delete())

                if rows:
                    cols = [c.name for c in table.columns]
                    # 分批插入
                    for batch_start in range(0, len(rows), BATCH_SIZE):
                        batch = rows[batch_start:batch_start + BATCH_SIZE]
                        data = [dict(zip(cols, row)) for row in batch]
                        with mysql_engine.begin() as mysql_conn:
                            mysql_conn.execute(table.insert(), data)

                    total_rows += len(rows)
                    logger.info("Migrated %s: %d rows", table_name, len(rows))
                else:
                    logger.info("Migrated %s: 0 rows (empty)", table_name)

            except Exception as table_err:
                logger.warning("Table %s migration warning: %s", table_name, table_err)
                # 继续迁移其他表

            _migration_state["tables_done"] = i + 1
            _migration_state["rows_migrated"] = total_rows

        # 恢复 MySQL 外键检查
        with mysql_engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

        _migration_state["status"] = "completed"
        _migration_state["current_table"] = None
        logger.info("Migration completed: %d tables, %d rows", len(table_order), total_rows)

    except Exception as e:
        _migration_state["status"] = "failed"
        _migration_state["error"] = str(e)
        logger.exception("Migration failed")

        # 即使失败也要恢复 FK 检查
        try:
            with mysql_engine.begin() as conn:
                conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        except Exception:
            pass

    finally:
        if sqlite_engine is not None:
            sqlite_engine.dispose()

    return get_migration_status()
