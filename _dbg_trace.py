import os, sys
from pathlib import Path
ROOT = Path(r"D:/ai_project/dataAanlystNew")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
db = ROOT / "_debug_046_2.db"
if db.exists(): db.unlink()
os.environ["ALEMBIC_DATABASE_URL"] = "sqlite:///" + db.resolve().as_posix()

# Monkey-patch env.py's _drop_index with debug
import alembic.env as ae
orig_install = ae._install_idempotent_operations_patch
call_stack = []
def traced_install():
    orig_install()
    from alembic.operations import Operations
    orig = Operations.drop_index
    def traced(self, index_name, table_name=None, **kw):
        from sqlalchemy import inspect as si
        bind = self.get_bind()
        if bind is not None and table_name:
            insp = si(bind)
            has_t = insp.has_table(table_name)
            idxs = [i["name"] for i in insp.get_indexes(table_name)] if has_t else []
            has = index_name in idxs
        else:
            has_t = False; idxs=[]; has=False
        call_stack.append(("drop_index", index_name, table_name, has_t, idxs))
        return orig(self, index_name, table_name=table_name, **kw)
    Operations.drop_index = traced

ae._install_idempotent_operations_patch = traced_install

from sqlalchemy import create_engine, inspect
from alembic.config import Config
from alembic import command
from app.db.base import Base
from app.models import *  # noqa

engine = create_engine(os.environ["ALEMBIC_DATABASE_URL"])
Base.metadata.create_all(engine)
cfg = Config(str(ROOT / "alembic.ini"))
cfg.set_main_option("script_location", str(ROOT / "alembic"))
command.upgrade(cfg, "head")

# verify idx
insp = inspect(engine)
print("before dw, idem key idx:", [i["name"] for i in insp.get_indexes("decision_evidence") if "idem" in i["name"]])

try:
    command.downgrade(cfg, "wps_0023_023_score_traceability")
    print("DOWNGRADE OK")
except Exception as e:
    print("DOWNGRADE FAIL:", type(e).__name__, str(e)[:400])

# Print last 10 calls about idempotency_key
print("\n--- last drop_index calls with idempotency_key ---")
for c in call_stack:
    if "idempotency_key" in str(c[1]):
        print(c[:4])
