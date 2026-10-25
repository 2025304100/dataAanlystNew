from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import subprocess
import sys

import pytest

from scripts import repair_validation_etf_cache as repair

import pytest

pytestmark = pytest.mark.whitebox



def sample_rows():
    result = []
    for symbol_id, (code, count, end) in repair.TARGETS.items():
        result.append({"id": symbol_id, "symbol": code, "region": "cn", "asset_type": "etf", "market": "sh",
                       "bar_count": {6549: 236, 6604: 196, 7098: 1277}[symbol_id],
                       "last_bar_date": date.fromisoformat("2026-10-20" if symbol_id == 7098 else end),
                       "updated_at": datetime(2026, 10, 21), "listed_at": None})
    return result


def sample_bars(symbol_id):
    _, count, end = repair.TARGETS[symbol_id]
    return [{"id": symbol_id * 10000 + n, "universe_symbol_id": symbol_id,
             "trade_date": date.fromisoformat(end) - timedelta(days=count - n - 1),
             "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "source": "unchanged", "volume": None}
            for n in range(count)]


def fixture_state():
    rows = sample_rows() + [{**sample_rows()[0], "id": 9999, "symbol": "other"}]
    columns = list(rows[0])
    baseline = {"database": "gpfx_validation", "all_tables": True, "tables": {
        "universe_symbols": {"existing": repair.digest(rows, columns), "total": len(rows)},
        "universe_daily_bars": {"existing": {"sha256": "immutable-bars"}, "total": 3359}}}
    targets = {r["id"]: repair.checked_cache(r, sample_bars(r["id"])) for r in rows[:3]}
    return rows, columns, baseline, targets


def test_projected_state_changes_only_four_reviewed_cells_and_preserves_original():
    rows, columns, baseline, targets = fixture_state()
    frozen = deepcopy((rows, baseline))
    expected, changes = repair.projected_state(baseline, rows, columns, targets)
    assert (rows, baseline) == frozen
    assert len(changes) == 4
    assert {c["field"] for c in changes} == repair.CACHE_FIELDS
    assert expected["tables"]["universe_daily_bars"] == baseline["tables"]["universe_daily_bars"]
    new_rows = [{**r, **targets.get(r["id"], {})} for r in rows]
    assert expected["tables"]["universe_symbols"]["existing"] == repair.digest(new_rows, columns)
    assert expected["tables"]["universe_symbols"]["existing"]["nulls"] == baseline["tables"]["universe_symbols"]["existing"]["nulls"]


@pytest.mark.parametrize("field", ["updated_at", "listed_at", "symbol", "is_synced"])
def test_projection_rejects_unapproved_columns(field):
    rows, columns, baseline, targets = fixture_state()
    targets[6549][field] = 1
    with pytest.raises(AssertionError, match="Only reviewed cache fields"):
        repair.projected_state(baseline, rows, columns, targets)


def test_projection_rejects_registry_drift_and_wrong_scope():
    rows, columns, baseline, targets = fixture_state()
    rows[-1]["updated_at"] = datetime(2026, 10, 25)
    with pytest.raises(AssertionError, match="Registry drift"):
        repair.projected_state(baseline, rows, columns, targets)
    with pytest.raises(AssertionError, match="scope"):
        repair.projected_state(baseline, rows, columns, {6549: targets[6549]})


@pytest.mark.parametrize("fault", ["wrong_identity", "duplicate_date", "missing_ohlc", "count_changed", "latest_changed"])
def test_source_validation_rejects_unverified_changes(fault):
    row = sample_rows()[0]; bars = sample_bars(row["id"])
    if fault == "wrong_identity": row["symbol"] = "600000"
    elif fault == "duplicate_date": bars[-1]["trade_date"] = bars[0]["trade_date"]
    elif fault == "missing_ohlc": bars[0]["close"] = None
    elif fault == "count_changed": bars.pop()
    elif fault == "latest_changed": bars[-1]["trade_date"] += timedelta(days=1)
    with pytest.raises(AssertionError): repair.checked_cache(row, bars)


class FakeDB:
    def __init__(self, *, changed_second=False, side_effect=False):
        self.rows = {r["id"]: r for r in sample_rows()}
        if changed_second: self.rows[6604]["updated_at"] += timedelta(days=1)
        self.original = deepcopy(self.rows)
        self.side_effect = side_effect
        self.committed = False
        self.rolled_back = False
        self.queries = []

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def cursor(self): return FakeCursor(self)
    def commit(self): self.committed = True
    def rollback(self): self.rows = deepcopy(self.original); self.rolled_back = True


class FakeCursor:
    def __init__(self, db): self.db = db; self.result = []; self.rowcount = 0
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def fetchone(self): return self.result[0]
    def fetchall(self): return self.result
    def execute(self, query, args=None):
        self.db.queries.append(query)
        if query.startswith("SELECT DATABASE"): self.result = [{"name": "gpfx_validation"}]
        elif query.startswith("SELECT version_num"): self.result = [{"version_num": repair.REVISION}]
        elif query.startswith("SELECT COUNT"): self.result = [{"n": 0}]
        elif query.startswith("SELECT TRIGGER_NAME"): self.result = []
        elif query.startswith("SHOW FULL COLUMNS"): self.result = [{"Field": f, "Extra": ""} for f in sample_rows()[0]]
        elif query.startswith("SELECT * FROM universe_symbols WHERE id IN"):
            self.result = [deepcopy(r) for r in self.db.rows.values()]
        elif query.startswith("SELECT * FROM universe_daily_bars"):
            self.result = sample_bars(args[0])
        elif query.startswith("UPDATE universe_symbols"):
            assert query == "UPDATE universe_symbols SET bar_count=%s,last_bar_date=%s WHERE id=%s AND bar_count <=> %s AND last_bar_date <=> %s"
            count, day, symbol_id, _, _ = args
            self.db.rows[symbol_id].update(bar_count=count, last_bar_date=day)
            if self.db.side_effect: self.db.rows[symbol_id]["updated_at"] += timedelta(days=1)
            self.rowcount = 1
        elif query.startswith("SELECT * FROM universe_symbols WHERE id="):
            self.result = [deepcopy(self.db.rows[args[0]])]
        elif query.startswith(("SET TRANSACTION", "START TRANSACTION")): self.result = []
        else: raise AssertionError("Unreviewed SQL: " + query)


def make_backup(tmp_path):
    rows = sample_rows()
    expected_path = tmp_path / "expected-after-all.json"
    repair.save_new(expected_path, {"test": "immutable expected state"})
    backup = {"database": "gpfx_validation", "revision": repair.REVISION,
              "allowed_fields": sorted(repair.CACHE_FIELDS), "columns": list(rows[0]),
              "bar_columns": list(sample_bars(6549)[0]), "expected_state_sha256": repair.sha(expected_path), "rows": []}
    for row in rows:
        bars = sample_bars(row["id"])
        backup["rows"].append({"before_row": row, "verified_cache": repair.checked_cache(row, bars),
                               "bars_digest": repair.digest(bars, backup["bar_columns"])})
    path = tmp_path / "cache-backup.json"
    repair.save_new(path, backup)
    return path


def test_apply_uses_only_two_columns_and_keeps_every_other_field(tmp_path, monkeypatch):
    path = make_backup(tmp_path); db = FakeDB()
    monkeypatch.setattr(repair, "connect", lambda: db)
    result = repair.apply_backup(path, repair.sha(path))
    assert result == {"committed": True, "rows_updated": 3, "cells_changed": 4}
    assert db.committed and not db.rolled_back
    assert sum(q.startswith("UPDATE") for q in db.queries) == 3
    assert all("FOR UPDATE" in q for q in db.queries if q.startswith("SELECT * FROM universe_daily_bars"))
    for symbol_id, row in db.rows.items():
        assert {k:v for k,v in row.items() if k not in repair.CACHE_FIELDS} == {
            k:v for k,v in db.original[symbol_id].items() if k not in repair.CACHE_FIELDS}
    assert json.loads((tmp_path / "apply-receipt.json").read_text("utf-8"))["rows_updated"] == 3


@pytest.mark.parametrize("fault", ["changed_second", "side_effect"])
def test_apply_rolls_back_all_rows_on_drift_or_unapproved_side_effect(tmp_path, monkeypatch, fault):
    path = make_backup(tmp_path); db = FakeDB(**{fault: True})
    monkeypatch.setattr(repair, "connect", lambda: db)
    with pytest.raises(AssertionError): repair.apply_backup(path, repair.sha(path))
    assert db.rolled_back and not db.committed and db.rows == db.original
    assert not (tmp_path / "apply-receipt.json").exists()


def test_tampered_backup_stops_before_database_access(tmp_path, monkeypatch):
    path = make_backup(tmp_path)
    monkeypatch.setattr(repair, "connect", lambda: pytest.fail("Must not connect"))
    with pytest.raises(AssertionError, match="checksum"):
        repair.apply_backup(path, "wrong")


def test_backup_cannot_overwrite_existing_evidence(tmp_path):
    path = tmp_path / "backup.json"
    repair.save_new(path, {"original": True})
    with pytest.raises(FileExistsError): repair.save_new(path, {"original": False})


@pytest.mark.parametrize("fault", ["database", "revision", "active_tasks", "trigger", "automatic_column"])
def test_preflight_rejects_unsafe_runtime_before_any_update(tmp_path, monkeypatch, fault):
    path = make_backup(tmp_path); db = FakeDB()
    class GuardCursor(FakeCursor):
        def execute(self, query, args=None):
            super().execute(query, args)
            if fault == "database" and query.startswith("SELECT DATABASE"): self.result = [{"name": "production"}]
            elif fault == "revision" and query.startswith("SELECT version_num"): self.result = [{"version_num": "other"}]
            elif fault == "active_tasks" and query.startswith("SELECT COUNT"): self.result = [{"n": 1}]
            elif fault == "trigger" and query.startswith("SELECT TRIGGER_NAME"): self.result = [{"TRIGGER_NAME": "side_effect"}]
            elif fault == "automatic_column" and query.startswith("SHOW FULL COLUMNS"):
                self.result[-1]["Extra"] = "on update CURRENT_TIMESTAMP"
    db.cursor = lambda: GuardCursor(db)
    monkeypatch.setattr(repair, "connect", lambda: db)
    with pytest.raises(AssertionError): repair.apply_backup(path, repair.sha(path))
    assert db.rolled_back and not db.committed and not any(q.startswith("UPDATE") for q in db.queries)


def test_changed_projected_state_stops_before_connect(tmp_path, monkeypatch):
    path = make_backup(tmp_path)
    (tmp_path / "expected-after-all.json").write_text('{}', encoding='utf-8')
    monkeypatch.setattr(repair, "connect", lambda: pytest.fail("Must not connect"))
    with pytest.raises(AssertionError): repair.apply_backup(path, repair.sha(path))


def test_source_change_between_backup_and_apply_rolls_back(tmp_path, monkeypatch):
    path = make_backup(tmp_path); db = FakeDB()
    class ChangedBarCursor(FakeCursor):
        def execute(self, query, args=None):
            super().execute(query, args)
            if query.startswith("SELECT * FROM universe_daily_bars") and args == (6604,):
                self.result[0]["volume"] = 100
    db.cursor = lambda: ChangedBarCursor(db)
    monkeypatch.setattr(repair, "connect", lambda: db)
    with pytest.raises(AssertionError, match="Source bars changed"):
        repair.apply_backup(path, repair.sha(path))
    assert db.rolled_back and not db.committed and db.rows == db.original


def test_optimized_python_cannot_disable_safety_guards():
    result = subprocess.run([sys.executable, "-O", str(repair.ROOT / "scripts/repair_validation_etf_cache.py"), "--help"],
                            capture_output=True, text=True)
    assert result.returncode != 0 and "disables safety assertions" in result.stderr
