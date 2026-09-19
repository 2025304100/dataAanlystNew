# -*- coding: utf-8 -*-
"""TD6 只读探针：strategy_execution_snapshots 的权威 DDL（列类型/索引/FK/引擎字符集）。"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "alembic"))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import build_mysql_url, load_db_config  # noqa: E402

cfg = load_db_config()
url = build_mysql_url(cfg)
eng = create_engine(url, pool_pre_ping=True)
with eng.connect() as conn:
    ddl = conn.execute(text("SHOW CREATE TABLE strategy_execution_snapshots")).fetchone()
print(ddl[1])
eng.dispose()
