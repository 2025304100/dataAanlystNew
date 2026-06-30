from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

from app.core.config import settings
from app.db.base import Base
from app.db.manager import DatabaseManager

from app.models import (
    backtest, daily_bar, discovery, factor, journal_entry, macro_data,
    market_event, news_event, portfolio, scan, score, signal_rule,
    sim_account, symbol, trade_setup, watchlist,
)


def _ensure_sqlite_columns(engine, table_name: str, additions: dict[str, str]) -> None:
    """为 SQLite 数据库补全可能缺失的列（兼容旧库升级）。"""
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    with engine.begin() as conn:
        for name, ddl in additions.items():
            if name not in columns:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}"))


def _ensure_sqlite_scan_result_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "scan_results",
        {
            "warning_days": "INTEGER DEFAULT 3",
            "valid_days": "INTEGER DEFAULT 5",
            "is_frozen": "INTEGER DEFAULT 0",
        },
    )


def _ensure_sqlite_score_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "scores",
        {
            "breakout_score": "REAL",
            "pullback_score": "REAL",
            "overheat_penalty": "REAL",
        },
    )


def _ensure_sqlite_trade_setup_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "trade_setups",
        {
            "manual_overrides_json": "TEXT",
            "field_sources_json": "TEXT",
            "manual_tranche_plan_json": "TEXT",
        },
    )


def _convert_myisam_to_innodb(engine) -> None:
    """将 MySQL 中已存在的 MyISAM 表转为 InnoDB，确保外键兼容。"""
    import logging
    logger = logging.getLogger(__name__)
    with engine.begin() as conn:
        # 获取当前数据库名
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        if not db_name:
            return
        # 查找所有 MyISAM 表
        result = conn.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = :db AND ENGINE = 'MyISAM'"
        ), {"db": db_name})
        tables = [row[0] for row in result]
        for tbl in tables:
            try:
                conn.execute(text(f"ALTER TABLE `{tbl}` ENGINE=InnoDB"))
                logger.info("Converted table '%s' from MyISAM to InnoDB", tbl)
            except Exception as e:
                logger.warning("Failed to convert table '%s' to InnoDB: %s", tbl, e)


def init_db() -> None:
    """初始化数据库：创建表结构，按方言执行必要的补丁。"""
    mgr = DatabaseManager.get()
    eng = mgr.engine

    # SQLite: 确保数据库文件目录存在
    if mgr.is_sqlite:
        db_path = settings.database_url.replace("sqlite:///", "", 1)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # MySQL: 将所有已存在的 MyISAM 表转为 InnoDB，避免跨引擎外键错误
    if mgr.is_mysql:
        _convert_myisam_to_innodb(eng)

    # 创建所有 ORM 模型对应的表
    Base.metadata.create_all(bind=eng)

    # SQLite 专属补丁
    if mgr.is_sqlite:
        _ensure_sqlite_scan_result_columns(eng)
        _ensure_sqlite_score_columns(eng)
        _ensure_sqlite_trade_setup_columns(eng)
        # 启用 WAL 模式提升并发写入性能
        with eng.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            conn.execute(text("PRAGMA busy_timeout=30000;"))
