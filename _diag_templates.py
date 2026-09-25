# -*- coding: utf-8 -*-
"""诊断：模板表实况（列表接口返回 0 的根因）。"""
import sys

sys.path.insert(0, ".")
from sqlalchemy import text  # noqa: E402

from app.main import initialize_runtime_database  # noqa: E402

initialize_runtime_database()
from app.db.session import get_session_local  # noqa: E402

db = get_session_local()()
n = db.execute(text("select count(*), scope from factor_mining_templates group by scope")).fetchall()
print("db rows by scope:", n)
rows = db.execute(text("select id,name,scope,enabled from factor_mining_templates limit 5")).fetchall()
for r in rows:
    print(r)
db.close()
