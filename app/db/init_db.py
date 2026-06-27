from pathlib import Path

from app.db.base import Base
from app.db.session import engine
from app.core.config import settings
from sqlalchemy import inspect, text

from app.models import daily_bar, discovery, factor, journal_entry, macro_data, market_event, news_event, portfolio, scan, score, signal_rule, sim_account, symbol, trade_setup, watchlist


def _ensure_sqlite_columns(table_name: str, additions: dict[str, str]) -> None:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    with engine.begin() as conn:
        for name, ddl in additions.items():
            if name not in columns:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}"))


def _ensure_sqlite_scan_result_columns() -> None:
    _ensure_sqlite_columns(
        "scan_results",
        {
            "warning_days": "INTEGER DEFAULT 3",
            "valid_days": "INTEGER DEFAULT 5",
            "is_frozen": "INTEGER DEFAULT 0",
        },
    )


def _ensure_sqlite_score_columns() -> None:
    _ensure_sqlite_columns(
        "scores",
        {
            "breakout_score": "REAL",
            "pullback_score": "REAL",
            "overheat_penalty": "REAL",
        },
    )


def init_db() -> None:
    if settings.database_url.startswith("sqlite:///"):
        db_path = Path(settings.database_url.replace("sqlite:///", "", 1))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        _ensure_sqlite_scan_result_columns()
        # 启用 WAL 模式提升并发写入性能
        with engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            conn.execute(text("PRAGMA busy_timeout=30000;"))  # 30秒等待锁释放
