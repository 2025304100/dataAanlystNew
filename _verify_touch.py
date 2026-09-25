# -*- coding: utf-8 -*-
"""验证 touch_run_progress 是否真实刷新 DB 的 updated_at（V2.3 补测）。"""
import sys
import time

sys.path.insert(0, ".")
from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import Settings  # noqa: E402

RUN = "179787eaa752460dbcbaf85ecde15573"
e = create_engine(str(Settings().database_url))


def q():
    with e.connect() as c:
        return c.execute(text(
            f"select status, updated_at from factor_mining_runs where id='{RUN}'"
        )).first()


r1 = q()
print("t0   :", r1)
time.sleep(50)
r2 = q()
print("t+50s:", r2)
if r1 and r2 and r1[1] and r2[1]:
    print("PASS V2.3 touch 生效（updated_at 前进）" if r2[1] > r1[1]
          else "FAIL V2.3 updated_at 未刷新 —— touch 未接线或评估卡死")
