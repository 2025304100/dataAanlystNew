from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

from app.core.config import settings
from app.db.base import Base
from app.db.manager import DatabaseManager

from app.models import (
    alert, backtest, custom_indicator, daily_bar, discovery, discovery_plan, factor, journal_entry, macro_data,
    market_event, news_event, portfolio, scan, score, signal_rule,
    sim_account, symbol, trade_setup, watchlist,
)


def _ensure_sqlite_columns(engine, table_name: str, additions: dict[str, str]) -> None:
    """为 SQLite 数据库补充可能缺失的列（兼容旧库升级）。"""
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


def _ensure_sqlite_indicator_version_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "custom_indicator_versions",
        {
            "change_note": "TEXT DEFAULT ''",
        },
    )


def _ensure_sqlite_journal_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "journal_entries",
        {
            "review_tags_json": "TEXT",
        },
    )


def _ensure_mysql_indicator_version_columns(engine) -> None:
    """为 MySQL 中已存在的表补充新增列。"""
    import logging
    logger = logging.getLogger(__name__)
    with engine.begin() as conn:
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        if not db_name:
            return
        # custom_indicator_versions.change_note
        result = conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'custom_indicator_versions' AND COLUMN_NAME = 'change_note'"
        ), {"db": db_name})
        if result.first() is None:
            try:
                conn.execute(text(
                    "ALTER TABLE custom_indicator_versions ADD COLUMN change_note TEXT DEFAULT ''"
                ))
                logger.info("Added change_note column to custom_indicator_versions")
            except Exception as e:
                logger.warning("Failed to add change_note column: %s", e)
        # journal_entries.review_tags_json
        result = conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'journal_entries' AND COLUMN_NAME = 'review_tags_json'"
        ), {"db": db_name})
        if result.first() is None:
            try:
                conn.execute(text(
                    "ALTER TABLE journal_entries ADD COLUMN review_tags_json TEXT"
                ))
                logger.info("Added review_tags_json column to journal_entries")
            except Exception as e:
                logger.warning("Failed to add review_tags_json column: %s", e)


def _convert_myisam_to_innodb(engine) -> None:
    """将 MySQL 中已存在的 MyISAM 表转为 InnoDB，确保外键兼容。"""
    import logging
    logger = logging.getLogger(__name__)
    with engine.begin() as conn:
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        if not db_name:
            return
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
    """初始化数据库：创建表结构，按需执行必要的补丁。"""
    mgr = DatabaseManager.get()
    eng = mgr.engine

    if mgr.is_sqlite:
        db_path = settings.database_url.replace("sqlite:///", "", 1)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    if mgr.is_mysql:
        _convert_myisam_to_innodb(eng)

    Base.metadata.create_all(bind=eng)

    if mgr.is_sqlite:
        _ensure_sqlite_scan_result_columns(eng)
        _ensure_sqlite_score_columns(eng)
        _ensure_sqlite_trade_setup_columns(eng)
        _ensure_sqlite_indicator_version_columns(eng)
        _ensure_sqlite_journal_columns(eng)
        with eng.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            conn.execute(text("PRAGMA busy_timeout=30000;"))
    else:
        _ensure_mysql_indicator_version_columns(eng)
