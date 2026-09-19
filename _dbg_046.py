import os, sys
from pathlib import Path
ROOT = Path(r"D:/ai_project/dataAanlystNew")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
db = ROOT / "_debug_046.db"
if db.exists(): db.unlink()
os.environ["ALEMBIC_DATABASE_URL"] = "sqlite:///" + db.resolve().as_posix()
from sqlalchemy import create_engine, inspect
from alembic.config import Config
from alembic import command
from app.db.base import Base
from app.models import *  # noqa

engine = create_engine(os.environ["ALEMBIC_DATABASE_URL"])
Base.metadata.create_all(engine)

cfg = Config(str(ROOT / "alembic.ini"))
cfg.set_main_option("script_location", str(ROOT / "alembic"))
command.upgrade(cfg, "wps_0023_024_decision_engine_contract")
# 再跑到 046
command.upgrade(cfg, "head")

insp = inspect(engine)
idxs = insp.get_indexes("decision_evidence")
print("decision_evidence INDEXES:")
for i in idxs:
    print(" ", i["name"], i["column_names"])
print()
print("idempotency_records INDEXES:")
for i in insp.get_indexes("idempotency_records"):
    print(" ", i["name"], i["column_names"])
print()
print("current rev: ", end="")
command.current(cfg)
