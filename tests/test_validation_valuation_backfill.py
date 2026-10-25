"""Exercise real transactional INSERT/unique/rollback semantics in isolation."""
import hashlib
import json
import sqlite3

import pytest

from scripts import backfill_validation_valuations as backfill

import pytest

pytestmark = pytest.mark.whitebox



def test_insert_only_backfill_is_atomic_and_never_replaces_existing_rows(tmp_path, monkeypatch):
    database = tmp_path / "isolated.sqlite"
    sql = sqlite3.connect(database)
    sql.executescript("""
        CREATE TABLE async_tasks(status TEXT);
        CREATE TABLE stock_valuations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol_id INTEGER, trade_date TEXT,
            pe_ttm REAL,pb REAL,total_market_cap REAL,circulating_market_cap REAL,
            source TEXT,raw_json TEXT,created_at TEXT,UNIQUE(symbol_id,trade_date));
        INSERT INTO stock_valuations VALUES(1,3276,'2026-10-13',17.9,6.34,100,80,'original','original evidence','2026-10-13');
    """)
    sql.commit()
    original = sql.execute("SELECT * FROM stock_valuations").fetchall()

    class Cursor:
        def __init__(self, db):
            self.cursor = db.cursor()
            self.synthetic = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.cursor.close()
        def execute(self, statement, args=()):
            self.synthetic = ("information_schema.TRIGGERS" in statement or "information_schema.TABLES" in statement)
            self.engine_query = "information_schema.TABLES" in statement
            if not self.synthetic:
                self.cursor.execute(statement.replace("%s", "?"), args)
        def executemany(self, statement, args):
            self.cursor.executemany(statement.replace("%s", "?"), args)
        def fetchone(self):
            if self.synthetic:
                return {"ENGINE": "InnoDB"} if self.engine_query else {"n": 0}
            return dict(self.cursor.fetchone())
        @property
        def rowcount(self):
            return self.cursor.rowcount

    class Connection:
        def __init__(self):
            self.db = sqlite3.connect(database)
            self.db.row_factory = sqlite3.Row
        def cursor(self):
            return Cursor(self.db)
        def commit(self):
            self.db.commit()
        def rollback(self):
            self.db.rollback()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.db.close()

    monkeypatch.setattr(backfill, "connect", Connection)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"database": "gpfx_validation", "tables": {
        "stock_valuations": {"total": 1, "max_id": 1}}}), encoding="utf-8")
    staged = tmp_path / "stage.json"
    new = dict(symbol_id=3276, symbol="600519", trade_date="2026-10-12", pe_ttm=20,
               pb=6, total_market_cap=100, circulating_market_cap=80, raw={"dated": True})
    evidence = {"database": "gpfx_validation", "dataset": "stock_valuations", "start": "2026-10-12",
        "end": "2026-10-19", "sessions": ["2026-10-12", "2026-10-13"], "symbols": {"600519": 3276},
        "records": [new, {**new, "trade_date": "2026-10-13"}]}
    staged.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(sqlite3.IntegrityError):
        backfill.apply(staged, hashlib.sha256(staged.read_bytes()).hexdigest(), baseline)
    assert sql.execute("SELECT * FROM stock_valuations").fetchall() == original
    evidence["records"] = [new]
    staged.write_text(json.dumps(evidence), encoding="utf-8")
    backfill.apply(staged, hashlib.sha256(staged.read_bytes()).hexdigest(), baseline)
    assert sql.execute("SELECT * FROM stock_valuations WHERE id=1").fetchall() == original
    assert sql.execute("SELECT COUNT(*) FROM stock_valuations").fetchone()[0] == 2
    # Replaying against an old baseline fails instead of writing again.
    with pytest.raises(ValueError, match="baseline changed"):
        backfill.apply(staged, hashlib.sha256(staged.read_bytes()).hexdigest(), baseline)
    sql.close()
