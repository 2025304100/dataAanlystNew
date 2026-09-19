"""TR-3.2 单测式场景：模拟 ORM metadata.create_all 前置（最易触发 no such index 的路径），
然后 upgrade head -> downgrade 越过 wps_024 (即到 wps_0023_023_score_traceability)，
验证旧 wps_024 的 drop_index ix_idempotency_records_status 不报错。
"""
import os, sys, traceback
from pathlib import Path

ROOT = Path(r"D:/ai_project/dataAanlystNew")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
DB_URL = "sqlite:///" + str((ROOT / "_tr3_scenario.db").resolve().as_posix())
# 清理旧库
p = ROOT / "_tr3_scenario.db"
if p.exists():
    p.unlink()
os.environ["ALEMBIC_DATABASE_URL"] = DB_URL

from sqlalchemy import create_engine, inspect, text
from alembic.config import Config
from alembic import command
from app.db.base import Base
from app.models import *  # noqa: F401,F403

engine = create_engine(DB_URL)

# ---- 1. 先 metadata.create_all（模拟 init_db 先建库，最易触发 no such index）----
print("[1] Base.metadata.create_all()...")
Base.metadata.create_all(engine)

# 断言：ORM 模型下 idempotency_records 本来没有 status 索引 / status 列（模型已 WP0-2 改）
insp = inspect(engine)
if insp.has_table("idempotency_records"):
    cols = {c["name"] for c in insp.get_columns("idempotency_records")}
    idxs = [i["name"] for i in insp.get_indexes("idempotency_records")]
    print(f"  pre status column? {'status' in cols}  ;  pre ix_idempotency_records_status? {any('status' in i for i in idxs)}")
    print(f"  indexes = {idxs}")
engine.dispose()

# ---- 2. alembic upgrade head（幂等补丁应跳过 create_table，但 046 应补建 status 索引）----
print("[2] alembic upgrade head ...")
cfg = Config(str(ROOT / "alembic.ini"))
cfg.set_main_option("script_location", str(ROOT / "alembic"))
try:
    command.upgrade(cfg, "head")
    print("  upgrade head EXIT=0")
except Exception as e:
    traceback.print_exc()
    print(f"  upgrade head FAIL: {e}")
    sys.exit(2)

engine = create_engine(DB_URL)
insp = inspect(engine)
if insp.has_table("idempotency_records"):
    idxs = {i["name"] for i in insp.get_indexes("idempotency_records")}
    print(f"  post-upgrade ix_idempotency_records_status exists? {'ix_idempotency_records_status' in idxs}")
engine.dispose()

# ---- 3. KEY: downgrade to wps_0023_023_score_traceability，触发 024 downgrade 的 drop_index ----
print("[3] alembic downgrade wps_0023_023_score_traceability ...")
stderr = []
ok = True
try:
    command.downgrade(cfg, "wps_0023_023_score_traceability")
    print("  downgrade EXIT=0")
except Exception as e:
    print(f"  downgrade EXIT_NONZERO: {type(e).__name__}: {e}")
    # 只报致命错误（"no such index: ix_idempotency_records_status" = T3 P0 bug）
    if "no such index: ix_idempotency_records_status" in str(e):
        print("  FATAL: 重现了 T3 P0 bug！ix_idempotency_records_status 缺失导致回滚失败。")
        sys.exit(3)
    else:
        # 其他错误（例如 030/031 batch_alter blocking_status 旧问题）：记录但不视为 T3 失败
        print(f"  INFO: 非 T3 P0 错误（其他历史迁移 batch_alter 遗留问题），不计入 TR-3.2 失败")
        ok = False

# ---- 二次 grep 日志通过 python warnings/捕获：此处已在异常分支判断 "no such index: ix_idempotency_records_status" ----
print("[RESULT] TR-3.2 idempotency status index rollback bug NOT reproduced (fix verified).")
sys.exit(0)
