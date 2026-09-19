import os, sys
from pathlib import Path
ROOT = Path(r"D:/ai_project/dataAanlystNew")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
db = ROOT / "_debug_046_5.db"
if db.exists(): db.unlink()
os.environ["ALEMBIC_DATABASE_URL"] = "sqlite:///" + db.resolve().as_posix()

from sqlalchemy import create_engine, inspect
from app.db.base import Base
from app.models import *  # noqa
engine = create_engine(os.environ["ALEMBIC_DATABASE_URL"])
Base.metadata.create_all(engine)

# Patch env.py before running alembic via monkey into the patch function itself:
# Use a process-level rewrite: intercept env.py file before import via import hook.
# Simpler: import alembic.env directly and patch its Operations method AFTER
# _install_idempotent_operations_patch has already installed its wrapper,
# additionally wrap the actual drop result with try/except OperationalError.

import importlib.util
spec = importlib.util.spec_from_file_location("myenv", str(ROOT/"alembic"/"env.py"))
myenv = importlib.util.module_from_spec(spec)
# Fake `context` - tricky because alembic env.py uses module-level alembic.context.
# Instead we just run via CLI but add an import-level wrapper.
import alembic.operations
from alembic.operations import Operations
import functools
_orig = Operations.drop_index
installed = [0]
def _outer(self, index_name, table_name=None, **kw):
    installed[0] += 1
    try:
        return _orig(self, index_name, table_name=table_name, **kw)
    except Exception as e:
        msg = str(e)
        if "no such index" in msg.lower() or "does not exist" in msg.lower():
            from sqlalchemy import inspect as si
            return None
        raise
Operations.drop_index = _outer

from alembic.config import Config
from alembic import command as ac
cfg = Config(str(ROOT / "alembic.ini"))
cfg.set_main_option("script_location", str(ROOT / "alembic"))
ac.upgrade(cfg, "head")
try:
    ac.downgrade(cfg, "wps_0023_023_score_traceability")
    print("DW ALL OK to 023, calls:", installed[0])
except Exception as e:
    print("DW FAIL:", type(e).__name__, str(e)[:300])
