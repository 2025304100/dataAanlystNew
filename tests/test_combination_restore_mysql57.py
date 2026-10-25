"""Real dump/restore and before-image-checked incremental accounting replay.

Both databases must be fresh codex_combination_* schemas. No existing database
is overwritten; baseline dump and delta evidence remain for inspection.
"""
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import pytest
from sqlalchemy import MetaData, and_, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.init_db import _refresh_model_metadata
from app.models.async_task import AsyncTaskRecord
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.services.sim_accounts import ensure_sim_account_seed, place_sim_order
from scripts.db_backup import backup_mysql, write_manifest
from scripts.db_restore import restore_mysql
from scripts.review_migration_integrity import canonical

pytestmark = pytest.mark.whitebox


def capture(engine):
    metadata = MetaData()
    metadata.reflect(engine)
    result = {}
    with engine.connect() as conn:
        for table in metadata.sorted_tables:
            keys = list(table.primary_key.columns)
            assert keys, f"No silent omission of tables without PK: {table.name}"
            rows = [dict(row) for row in conn.execute(select(table).order_by(*keys)).mappings()]
            result[table.name] = rows
    return metadata, result


def sha(value):
    def convert(item):
        if isinstance(item, dict):
            return {key: convert(val) for key, val in item.items()}
        if isinstance(item, list):
            return [convert(val) for val in item]
        return canonical(item)
    return hashlib.sha256(json.dumps(convert(value), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def replay_delta(engine, before, after):
    """Preserve IDs and exact values; never recreate transactions at new prices."""
    metadata = MetaData(); metadata.reflect(engine)
    applied = 0
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            names = [column.name for column in table.primary_key.columns]
            keyed = lambda rows: {tuple(row[name] for name in names): row for row in rows}
            old, new = keyed(before[table.name]), keyed(after[table.name])
            assert old.keys() <= new.keys(), "Historical deletions cannot be replayed by this recovery contract"
            for key, next_row in new.items():
                previous = old.get(key)
                if previous == next_row:
                    continue
                predicate = and_(*(table.c[name] == value for name, value in zip(names, key)))
                current = conn.execute(select(table).where(predicate).with_for_update()).mappings().one_or_none()
                current = dict(current) if current is not None else None
                if current == next_row:
                    continue  # Already restored/replayed; do not double-book.
                if current != previous:
                    raise ValueError(f"before_image_conflict: {table.name} {key}")
                values = {column.name: next_row[column.name] for column in table.columns if column.computed is None}
                conn.execute(table.insert().values(**values) if previous is None else table.update().where(predicate).values(**values))
                applied += 1
    return applied


def test_real_mysql57_backup_restore_preserves_old_rows_and_replays_new_accounting(tmp_path):
    raw, target = os.environ.get("MYSQL57_TEST_URL"), os.environ.get("MYSQL57_RESTORE_TEST_URL")
    if not raw or not target:
        pytest.skip("MYSQL57_TEST_URL and MYSQL57_RESTORE_TEST_URL required for fresh source/restore databases")
    urls = [make_url(raw), make_url(target)]
    assert urls[0].database != urls[1].database
    engines = []
    for url in urls:
        assert url.get_backend_name() == "mysql" and (url.database or "").startswith("codex_combination_")
        engine = create_engine(url, hide_parameters=True)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT DATABASE()")) == url.database
            assert conn.scalar(text("SELECT VERSION()")).startswith("5.7.")
            assert not inspect(conn).get_table_names(), "Fresh isolated databases are required; evidence is never cleared"
        engines.append(engine)
    source, restored = engines
    _refresh_model_metadata()
    names = {"symbols", "portfolios", "portfolio_members", "portfolio_rules", "positions", "cash_ledger",
             "sim_orders", "sim_trades", "daily_bars", "notification_policies", "async_tasks"}
    while True:
        parents = {fk.column.table.name for name in names for fk in Base.metadata.tables[name].foreign_keys}
        if parents <= names:
            break
        names |= parents
    metadata = MetaData()
    for name in names:
        table = Base.metadata.tables[name].to_metadata(metadata)
        table.dialect_options["mysql"]["engine"] = "InnoDB"
    metadata.create_all(source)
    with Session(source) as db:
        p = Portfolio(name="Recovery acceptance", account_type="simulated", total_capital=100000,
                      investable_ratio=.8, cash_reserve_ratio=.2)
        s = Symbol(symbol="600790", name="Recovery acceptance", asset_type="stock", market="SH")
        db.add_all([p, s]); db.flush()
        ensure_sim_account_seed(db, p)
        for days in (1, 2):
            db.add(DailyBar(symbol_id=s.id, trade_date=date.today()-timedelta(days=days), open=10,
                           high=10.2, low=9.8, close=10, volume=100000, amount=1000000, turnover_rate=.32))
        db.commit()
        # An actual service transaction is part of the pre-release history.
        place_sim_order(db, p, s, "buy", 100, price=10, apply_fees=False, enforce_rules=True,
                        note="历史成交完整性，禁止截断；保留原始字段")
        db.commit()
        pid, sid = p.id, s.id
    directory = Path(os.environ.get("COMBINATION_EVIDENCE_DIR", tmp_path)) / "restore"
    directory.mkdir(parents=True, exist_ok=True)
    dump_bin = os.environ.get("MYSQLDUMP_BIN") or shutil.which("mysqldump")
    mysql_bin = os.environ.get("MYSQL_BIN") or shutil.which("mysql")
    assert dump_bin and mysql_bin, "Real mysql/mysqldump binaries are required; no SQL-only mock acceptance"
    try:
        _, before = capture(source)
        started = time.perf_counter()
        artifact = backup_mysql(raw, directory, dump_bin)
        write_manifest(artifact)
        backup_seconds = time.perf_counter() - started
        manifest = json.loads(Path(artifact.manifest_file).read_text(encoding="utf-8"))
        # Simulate committed business and audit after the backup boundary.
        with Session(source) as db:
            place_sim_order(db, db.get(Portfolio, pid), db.get(Symbol, sid), "buy", 100, price=10,
                            apply_fees=False, enforce_rules=True)
            db.add(AsyncTaskRecord(id="post-backup-audit", task_type="recovery_acceptance", status="done",
                stage="done", payload_json='{"meaning":"备份后完整审计证据"}', result_json='{"orders":2}'))
            db.commit()
        _, after = capture(source)
        started = time.perf_counter()
        assert restore_mysql(manifest, directory/artifact.backup_file, target, mysql_bin,
                             dry_run=False, no_hash_check=False, force_rewrite_over_0026=False) == 0
        _, baseline = capture(restored)
        assert sha(baseline) == sha(before)
        # No insert/update is authorized by a count alone: store full row evidence.
        delta = {"before_sha256": sha(before), "after_sha256": sha(after), "before": before, "after": after}
        (directory/"incremental-evidence.json").write_text(json.dumps(delta, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
        applied = replay_delta(restored, before, after)
        assert applied > 0
        assert replay_delta(restored, before, after) == 0
        _, final = capture(restored)
        assert sha(final) == sha(after)
        # A second dump import would erase the post-backup accounting delta.
        # Even the legacy force flag must refuse this populated MySQL target.
        assert restore_mysql(manifest, directory/artifact.backup_file, target, mysql_bin,
                             dry_run=False, no_hash_check=False, force_rewrite_over_0026=True) == 4
        assert sha(capture(restored)[1]) == sha(final)
        assert len(final["sim_orders"]) == len(final["sim_trades"]) == 2
        assert len(final["cash_ledger"]) == 3 and final["cash_ledger"][-1]["balance_after"] == 98000
        assert len(final["async_tasks"]) == 1
        # Before-image conflict must roll back the replay; it cannot overwrite a
        # destination divergence just because the natural key matches.
        corrupted = {**before, "positions": [{**before["positions"][0], "quantity": -123}]}
        with restored.begin() as conn:
            conn.execute(text("UPDATE positions SET quantity=101 WHERE portfolio_id=:id"), {"id": pid})
        _, divergence = capture(restored)
        with pytest.raises(ValueError, match="before_image_conflict"):
            replay_delta(restored, corrupted, after)
        assert sha(capture(restored)[1]) == sha(divergence)
        # Restore only the specifically observed test divergence, retaining all rows.
        with restored.begin() as conn:
            conn.execute(text("UPDATE positions SET quantity=200 WHERE portfolio_id=:id AND quantity=101"), {"id": pid})
        assert sha(capture(restored)[1]) == sha(after)
        report = {"source_database": urls[0].database, "restore_database": urls[1].database,
                  "tables": len(before), "old_rows": sum(map(len, before.values())),
                  "new_rows": sum(map(len, after.values()))-sum(map(len, before.values())),
                  "backup_sha256": artifact.sha256, "all_old_rows_restored_sha256": sha(before),
                  "after_replay_sha256": sha(after), "delta_operations": applied,
                  "second_replay_operations": 0, "backup_seconds": backup_seconds,
                  "restore_and_replay_seconds": time.perf_counter()-started,
                  "rpo_rows_in_this_fixture": 0, "scope": "isolated controlled sample; not production RTO/RPO"}
        (directory/"restore-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    finally:
        for engine in engines:
            engine.dispose()
