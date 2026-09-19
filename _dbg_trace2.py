import os, sys, logging, importlib.util
from pathlib import Path
ROOT = Path(r"D:/ai_project/dataAanlystNew")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
db = ROOT / "_debug_046_3.db"
if db.exists(): db.unlink()
os.environ["ALEMBIC_DATABASE_URL"] = "sqlite:///" + db.resolve().as_posix()

# Patch _drop_index via sitecustomize-like: dynamically patch before alembic runs by
# running the migrations via cli subprocess would be harder, so we use:
# import env.py's file as a module and inject a wrapper into Operations.drop_index.

from sqlalchemy import create_engine, inspect
from app.db.base import Base
from app.models import *  # noqa
engine = create_engine(os.environ["ALEMBIC_DATABASE_URL"], echo=False)
Base.metadata.create_all(engine)

from alembic.config import Config
from alembic import command as alembic_cmd
from alembic.operations import Operations
import functools
_orig_drop = Operations.drop_index
calls = []
def _wrap_drop(self, index_name, table_name=None, **kw):
    from sqlalchemy import inspect as si
    bind = self.get_bind()
    try:
        if bind is not None and table_name:
            insp = si(bind)
            has_t = insp.has_table(table_name)
            idxs = [i["name"] for i in (insp.get_indexes(table_name) if has_t else [])]
            has = index_name in idxs
        else:
            has_t = False; idxs = []; has = False
    except Exception as e:
        has_t=None; idxs=[]; has=False
    calls.append(("DROP", index_name, table_name, has))
    try:
        return _orig_drop(self, index_name, table_name=table_name, **kw)
    except Exception as e:
        calls.append(("RAISE", str(e)[:200]))
        raise
Operations.drop_index = _wrap_drop

cfg = Config(str(ROOT / "alembic.ini"))
cfg.set_main_option("script_location", str(ROOT / "alembic"))
alembic_cmd.upgrade(cfg, "head")

insp = inspect(engine)
ev_idx = insp.get_indexes("decision_evidence")
idem_de_idx = [i["name"] for i in ev_idx if "idempotency_key" in i["name"]]
print("before dw idem_de:", idem_de_idx)

# 只跑到 031 前，再单独观察到 024
try:
    alembic_cmd.downgrade(cfg, "wps_0023_031_wp03_decision_runs_evidence_full_cols")
    ev_idx = inspect(engine).get_indexes("decision_evidence")
    idem_de_idx = [i["name"] for i in ev_idx if "idempotency_key" in i["name"]]
    print("after dw to 031 idem_de:", idem_de_idx)

    alembic_cmd.downgrade(cfg, "wps_0023_024_decision_engine_contract")
    ev_idx = inspect(engine).get_indexes("decision_evidence")
    idem_de_idx = [i["name"] for i in ev_idx if "idempotency_key" in i["name"]]
    print("after dw to 024 idem_de:", idem_de_idx)

    alembic_cmd.downgrade(cfg, "wps_0023_023_score_traceability")
    print("after dw to 023 SUCCESS")
except Exception as e:
    print("FAIL DW:", type(e).__name__, str(e)[:300])

print("\n--- all drop_index calls mentioning idempotency_key or RAISE ---")
for c in calls:
    if "idempotency_key" in str(c) or c[0] == "RAISE":
        print(" ", c)
