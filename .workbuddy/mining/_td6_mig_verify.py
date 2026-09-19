# -*- coding: utf-8 -*-
"""TD6 迁移修订验证：全新 sqlite upgrade head -> 列/索引核对 -> downgrade -1 -> 回退核对。
只读探针（临时文件，跑完删除），不碰真实库。
"""
import sys, os, io, tempfile

sys.path.insert(0, r"D:\ai_project\dataAanlystNew")
os.chdir(r"D:\ai_project\dataAanlystNew")

OUT = r"D:\ai_project\dataAanlystNew\.workbuddy\mining\_td6_mig_out.txt"
buf = io.StringIO()

fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_mig_")
os.close(fd)
url = f"sqlite:///{path}"
os.environ["ALEMBIC_DATABASE_URL"] = url

try:
    from alembic.config import Config
    from alembic import command as ac

    cfg = Config(r"D:\ai_project\dataAanlystNew\alembic.ini")
    cfg.set_main_option("script_location", r"D:\ai_project\dataAanlystNew\alembic")

    ac.upgrade(cfg, "head")
    buf.write("UPGRADE head OK\n")

    import sqlalchemy as sa
    eng = sa.create_engine(url)
    insp = sa.inspect(eng)
    cols = [c["name"] for c in insp.get_columns("strategy_execution_snapshots")]
    idxs = sorted(i["name"] for i in insp.get_indexes("strategy_execution_snapshots"))
    missing_cols = [c for c in ("usage_binding_id", "snapshot_json", "content_hash") if c not in cols]
    want_idx = {
        "ix_strategy_execution_snapshots_snapshot_no",
        "ix_strategy_execution_snapshots_usage_binding_id",
        "ix_strategy_execution_snapshots_content_hash",
    }
    missing_idx = sorted(want_idx - set(idxs))
    cur = ac.ScriptDirectory.from_config(cfg).get_current_head()
    buf.write(f"head={cur}\n")
    buf.write(f"ncols={len(cols)} missing_cols={missing_cols}\n")
    buf.write(f"missing_idx={missing_idx}\n")
    buf.write(f"COLS={cols}\n")
    buf.write(f"IDX={idxs}\n")
    eng.dispose()

    ac.downgrade(cfg, "-1")
    buf.write("DOWNGRADE -1 OK\n")
    eng = sa.create_engine(url)
    insp = sa.inspect(eng)
    cols2 = [c["name"] for c in insp.get_columns("strategy_execution_snapshots")]
    idxs2 = sorted(i["name"] for i in insp.get_indexes("strategy_execution_snapshots"))
    buf.write(f"after -1: ncols={len(cols2)} reverted_cols_ok={not any(c in cols2 for c in ('usage_binding_id','snapshot_json','content_hash'))}\n")
    buf.write(f"after -1: missing_want_idx_ok={not (want_idx & set(idxs2))}\n")
    eng.dispose()
except Exception as exc:
    import traceback
    buf.write("FAIL: %r\n" % (exc,))
    buf.write(traceback.format_exc())
finally:
    try:
        os.remove(path)
    except OSError:
        pass

with open(OUT, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
print("WROTE", OUT)
