import os, sys
from pathlib import Path
ROOT = Path(r"D:/ai_project/dataAanlystNew")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
db = ROOT / "_debug_046_4.db"
if db.exists(): db.unlink()
os.environ["ALEMBIC_DATABASE_URL"] = "sqlite:///" + db.resolve().as_posix()

from sqlalchemy import create_engine, inspect
from app.db.base import Base
from app.models import *  # noqa
engine = create_engine(os.environ["ALEMBIC_DATABASE_URL"])
Base.metadata.create_all(engine)

from alembic.config import Config
from alembic import command as ac
cfg = Config(str(ROOT / "alembic.ini"))
cfg.set_main_option("script_location", str(ROOT / "alembic"))
ac.upgrade(cfg, "wps_0023_024_decision_engine_contract")
print("rev 024 current:")
ac.current(cfg)
idxs = inspect(engine).get_indexes("decision_evidence")
print("024 decision_evidence indexes:")
for i in idxs: print(" ", i["name"], i["column_names"])

# 模拟 046 对 decision_evidence 做的补丁：额外建 10 条命名索引
from alembic.operations import Operations
from alembic.migration import MigrationContext
with engine.connect() as conn:
    ctx = MigrationContext.configure(conn)
    op = Operations(ctx)
    for idx_name, cols in [
        ("ix_decision_evidence_decision_run_id",       ["decision_run_id"]),
        ("ix_decision_evidence_strategy_snapshot_id",  ["strategy_snapshot_id"]),
        ("ix_decision_evidence_portfolio_id",          ["portfolio_id"]),
        ("ix_decision_evidence_symbol_id",             ["symbol_id"]),
        ("ix_decision_evidence_trade_date",            ["trade_date"]),
        ("ix_decision_evidence_action",                ["action"]),
        ("ix_decision_evidence_action_subtype",        ["action_subtype"]),
        ("ix_decision_evidence_rejection_reason",      ["rejection_reason"]),
        ("ix_decision_evidence_idempotency_key",       ["idempotency_key"]),
        ("ix_decision_evidence_created_at",            ["created_at"])]:
        try:
            op.create_index(idx_name, "decision_evidence", cols, if_not_exists=True)
        except Exception as e:
            print("FAIL create:", idx_name, type(e).__name__, str(e)[:100])

idxs = inspect(engine).get_indexes("decision_evidence")
print("\nafter manual create idem_key exists:",
      any(i["name"]=="ix_decision_evidence_idempotency_key" for i in idxs))

# 现在直接跑 wps_024 downgrade:
try:
    ac.downgrade(cfg, "wps_0023_023_score_traceability")
    print("DW 024->023 SUCCESS")
except Exception as e:
    print("DW FAIL:", type(e).__name__, str(e)[:300])
