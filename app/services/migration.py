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
    "failed_tables": [],
    "fk_checks_restored": True,
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
            "failed_tables": [],
            "fk_checks_restored": True,
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
        failed_tables: list[str] = []
        for i, table_name in enumerate(table_order):
            _migration_state["current_table"] = table_name
            table = Base.metadata.tables[table_name]

            try:
                # 从 SQLite 读取全部数据
                # 设计说明：迁移为一次性运维操作，源库表数据量可控；
                # 后续逻辑需要 len(rows) 与切片分批，故使用 fetchall 一次性读取而非流式。
                with sqlite_engine.connect() as src_conn:
                    rows = src_conn.execute(table.select()).fetchall()

                # 合并 delete + insert 到同一事务，避免中途失败导致目标表被清空
                with mysql_engine.begin() as mysql_conn:
                    mysql_conn.execute(table.delete())
                    if rows:
                        cols = [c.name for c in table.columns]
                        for batch_start in range(0, len(rows), BATCH_SIZE):
                            batch = rows[batch_start:batch_start + BATCH_SIZE]
                            data = [dict(zip(cols, row)) for row in batch]
                            mysql_conn.execute(table.insert(), data)

                if rows:
                    total_rows += len(rows)
                    logger.info("Migrated %s: %d rows", table_name, len(rows))
                else:
                    logger.info("Migrated %s: 0 rows (empty)", table_name)

            except Exception as table_err:
                # 风控加固：单表失败用 exception 记录完整堆栈（不是 warning 仅一行）
                # 并将失败表名加入 failed_tables，最终状态根据是否有失败区分
                logger.exception("Table %s migration failed, skipping to next table", table_name)
                failed_tables.append(table_name)

            _migration_state["tables_done"] = i + 1
            _migration_state["rows_migrated"] = total_rows
            _migration_state["failed_tables"] = list(failed_tables)

        # 恢复 MySQL 外键检查
        fk_restored = True
        try:
            with mysql_engine.begin() as conn:
                conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        except Exception as fk_err:
            # 风控加固：FK 检查恢复失败是最危险的情况——MySQL 将永久关闭 FK 检查
            # 后续业务写入都不会校验外键，可能产生大量孤儿行，数据完整性彻底失守
            # 必须 critical 级别日志 + 状态记录，前端展示醒目警告
            logger.critical(
                "CRITICAL: Failed to restore FOREIGN_KEY_CHECKS=1 after migration. "
                "MySQL will remain FK-disabled, data integrity at risk! Error: %s",
                fk_err,
            )
            fk_restored = False
            _migration_state["error"] = (
                f"FK checks restore failed: {fk_err}. "
                "MySQL remains FK-disabled, manual intervention required."
            )

        _migration_state["fk_checks_restored"] = fk_restored
        # 风控加固：有表失败时状态为 partial 而非 completed，前端明确展示
        if failed_tables:
            _migration_state["status"] = "partial"
            logger.warning(
                "Migration completed with errors: %d/%d tables failed: %s",
                len(failed_tables), len(table_order), failed_tables,
            )
        else:
            _migration_state["status"] = "completed"
        _migration_state["current_table"] = None
        logger.info("Migration finished: %d tables, %d rows, status=%s",
                    len(table_order), total_rows, _migration_state["status"])

    except Exception as e:
        _migration_state["status"] = "failed"
        _migration_state["error"] = str(e)
        logger.exception("Migration failed")

        # 即使失败也要恢复 FK 检查
        try:
            with mysql_engine.begin() as conn:
                conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
            _migration_state["fk_checks_restored"] = True
        except Exception as fk_err:
            # 风控加固：失败路径下 FK 恢复失败同样 critical
            logger.critical(
                "CRITICAL: Failed to restore FOREIGN_KEY_CHECKS=1 after migration failure. "
                "MySQL remains FK-disabled! Error: %s",
                fk_err,
            )
            _migration_state["fk_checks_restored"] = False

    finally:
        if sqlite_engine is not None:
            sqlite_engine.dispose()

    return get_migration_status()
