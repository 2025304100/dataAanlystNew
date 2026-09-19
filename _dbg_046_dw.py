import os, sys
from pathlib import Path
ROOT = Path(r"D:/ai_project/dataAanlystNew")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
db = ROOT / "_debug_046.db"
os.environ["ALEMBIC_DATABASE_URL"] = "sqlite:///" + db.resolve().as_posix()
from sqlalchemy import create_engine, inspect
from alembic.config import Config
from alembic import command
cfg = Config(str(ROOT / "alembic.ini"))
cfg.set_main_option("script_location", str(ROOT / "alembic"))
try:
    command.downgrade(cfg, "wps_0023_023_score_traceability")
    print("DOWNGRADE OK")
except Exception as e:
    print("DOWNGRADE FAIL:", type(e).__name__, e)
