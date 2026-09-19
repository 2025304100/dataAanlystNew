# -*- coding: utf-8 -*-
"""TD6 DoD3 探针：完整模拟 conftest.db_session fixture 流程，逐步打印
strategy_execution_snapshots 的列清单，定位 no such column / NOT NULL 两类报错根因。
只读探针（临时 sqlite 文件，跑完删除），不碰真实库。
"""
import sys, os, io, tempfile, logging, traceback

sys.path.insert(0, r"D:\ai_project\dataAanlystNew")
os.chdir(r"D:\ai_project\dataAanlystNew")

OUT = r"D:\ai_project\dataAanlystNew\.workbuddy\mining\_td6_probe2_out.txt"
buf = io.StringIO()


class Tee(logging.Handler):
    def emit(self, record):
        try:
            buf.write(self.format(record) + "\n")
        except Exception:
            pass


logging.basicConfig(level=logging.INFO, handlers=[Tee()])
logging.getLogger().setLevel(logging.INFO)

fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_probe_")
os.close(fd)
url = f"sqlite:///{path}"


def cols_of(engine):
    from sqlalchemy import inspect
    insp = inspect(engine)
    if "strategy_execution_snapshots" not in insp.get_table_names():
        return "<TABLE MISSING>"
    return [c["name"] for c in insp.get_columns("strategy_execution_snapshots")]


def dump(tag):
    line = f"== {tag} =="
    print(line)
    buf.write(line + "\n")


try:
    from app.db.manager import DatabaseManager
    mgr = DatabaseManager.get()
    try:
        mgr.dispose()
    except Exception:
        pass
    mgr.initialize(url, db_type="sqlite")
    dump("after mgr.initialize")
    buf.write(repr(cols_of(mgr.engine)) + "\n")

    from app.db.init_db import _auto_align_all_schema, _auto_repair_basic_data_integrity
    _auto_align_all_schema(mgr.engine)
    dump("after _auto_align_all_schema")
    buf.write(repr(cols_of(mgr.engine)) + "\n")

    _auto_repair_basic_data_integrity(mgr.engine)
    dump("after _auto_repair_basic_data_integrity")
    buf.write(repr(cols_of(mgr.engine)) + "\n")

    # 模拟旧式构造（不填 snapshot_json/content_hash）
    from app.models.decision_engine import StrategyExecutionSnapshot, PortfolioFactorUsage
    from app.models.portfolio import Portfolio
    from app.models.symbol import Symbol
    from datetime import datetime
    s = mgr.session_factory()
    try:
        p = Portfolio(name="probe", risk_level="medium")
        sym = Symbol(symbol="000001.SZ", name="probe")
        s.add_all([p, sym])
        s.flush()
        snap = StrategyExecutionSnapshot(
            portfolio_id=p.id,
            snapshot_no="PROBE-1",
            snapshot_type="save_and_apply",
            snapshot_hash="h" * 64,
            idempotency_key="k" * 64,
            created_at=datetime.now(),
        )
        s.add(snap)
        s.flush()
        buf.write("INSERT(old-style kwargs) OK, id=%r\n" % snap.id)
    except Exception as exc:
        buf.write("INSERT(old-style kwargs) FAIL: %r\n" % (exc,))
    finally:
        s.close()
    dump("after old-style INSERT probe")
except Exception:
    buf.write(traceback.format_exc() + "\n")
finally:
    try:
        mgr.dispose()
    except Exception:
        pass
    try:
        os.remove(path)
    except OSError:
        pass

with open(OUT, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
print("WROTE", OUT)
