"""Pytest 公共 fixtures。

使用 SQLite 内存数据库做隔离测试，避免污染运行中的实例。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path，使 `import app.xxx` 可用
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.manager import DatabaseManager
from app.models import *  # noqa: F401,F403 - 确保所有模型被注册
from app import models  # noqa: F401
# 显式导入 __init__.py 未导出的模型，确保 Base.metadata 包含全部表
# （否则外键约束的目标表可能缺失，导致 create_all 报 NoReferencedTableError）
from app.models import (  # noqa: F401
    portfolio, symbol, watchlist, daily_bar, score, scan,
    signal_rule, trade_setup, journal_entry, news_event, alert,
    macro_data, factor, sim_account, discovery,
)


@pytest.fixture(scope="function")
def tmp_sqlite_url() -> str:
    """每个测试函数独立 SQLite 文件，测完自动清理。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_test_")
    os.close(fd)
    yield f"sqlite:///{path}"
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture(scope="function")
def db_session(tmp_sqlite_url):
    """初始化一个全新 DatabaseManager + 内存表，返回 Session。"""
    mgr = DatabaseManager.get()
    # 释放可能存在的旧实例
    try:
        mgr.dispose()
    except Exception:
        pass
    mgr.initialize(tmp_sqlite_url, db_type="sqlite")
    engine = mgr.engine
    Base.metadata.create_all(engine)
    SessionLocal = mgr.session_factory
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        try:
            mgr.dispose()
        except Exception:
            pass
