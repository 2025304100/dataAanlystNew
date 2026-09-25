# -*- coding: utf-8 -*-
"""查 V4a run 的 worker 任务 result_json（finalize failures 详情）。走与后端相同的 DB 初始化。"""
import json
import sys

sys.path.insert(0, ".")
from sqlalchemy import text  # noqa: E402

from app.main import initialize_runtime_database  # noqa: E402

initialize_runtime_database()
from app.db.session import get_session_local  # noqa: E402

RUN = "dc72436a675a47ee87c78eb6adc9a5f2"
db = get_session_local()()
rows = db.execute(text(
    "select id,status,stage,message,result_json from async_tasks "
    "where task_type like '%mining%' order by created_at desc limit 10")).fetchall()
db.close()
for r in rows:
    if RUN not in (r[4] or "") and "final" not in (r[4] or ""):
        pass
    print("task:", r[0], "|", r[1], r[2], "|", str(r[3])[:90])
    try:
        d = json.loads(r[4] or "{}")
        fin = d.get("final") or {}
        print("  finalize:", {k: fin.get(k) for k in ("evaluated", "failed")})
        for f in (fin.get("failures") or [])[:3]:
            print("   fail:", str(f)[:300])
    except Exception as ex:  # noqa: BLE001
        print("  parse err", ex, str(r[4])[:200])
