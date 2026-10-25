"""Real MySQL 5.7 date DDL must preserve original strings and every row."""
import importlib.util
import os
from pathlib import Path
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from alembic.migration import MigrationContext
from alembic.operations import Operations

pytestmark=pytest.mark.whitebox

@pytest.mark.parametrize("mode",["valid","invalid","shadow_conflict"])
def test_native_dates_preserve_text_or_refuse_before_first_ddl(mode):
    raw=os.environ.get("MYSQL57_TEST_URL")
    if not raw:pytest.skip("Explicit isolated MySQL 5.7 URL required")
    url=make_url(raw)
    assert url.get_backend_name()=="mysql" and url.database.startswith("codex_combination_")
    name=url.database+"_"+mode
    assert len(name)<=64
    admin=sa.create_engine(url._replace(database=None),hide_parameters=True)
    with admin.begin() as conn:
        assert conn.scalar(sa.text("SELECT VERSION()"))=="5.7.26"
        assert not conn.scalar(sa.text("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=:n"),{"n":name})
        conn.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
    admin.dispose()
    engine=sa.create_engine(url.set(database=name),hide_parameters=True)
    file=Path(__file__).resolve().parents[1]/"alembic/versions/2026_10_25_0085_legacy_date_contracts.py"
    spec=importlib.util.spec_from_file_location("date85",file);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE scores (id BIGINT PRIMARY KEY,trade_date VARCHAR(32) NOT NULL,payload TEXT NOT NULL) ENGINE=InnoDB")
        conn.exec_driver_sql("CREATE TABLE factor_model_runs (id BIGINT PRIMARY KEY,train_start_date VARCHAR(32) NULL,payload TEXT NOT NULL) ENGINE=InnoDB")
        conn.exec_driver_sql("INSERT INTO scores VALUES (1,'2026-10-20','all-key-fields'),(2,'2000-02-29','keep-history')")
        value="2026-02-30" if mode=="invalid" else "2025-01-01"
        conn.execute(sa.text("INSERT INTO factor_model_runs VALUES (1,:value,'model-metadata'),(2,NULL,'null-is-preserved')"),{"value":value})
        if mode=="shadow_conflict":
            conn.exec_driver_sql("ALTER TABLE factor_model_runs ADD legacy_train_start_date_v85 VARCHAR(32) NULL")
            conn.exec_driver_sql("UPDATE factor_model_runs SET legacy_train_start_date_v85='1999-01-01' WHERE id=1")
        names=("scores","factor_model_runs")
        before={name:[dict(r) for r in conn.execute(sa.text(f"SELECT * FROM {name} ORDER BY id")).mappings()] for name in names}
        with Operations.context(MigrationContext.configure(conn)):
            if mode!="valid":
                with pytest.raises(RuntimeError,match="Unproven legacy date|evidence conflict"):module.upgrade()
            else:module.upgrade();module.upgrade()
        for table in names:
            after=[dict(r) for r in conn.execute(sa.text(f"SELECT * FROM {table} ORDER BY id")).mappings()]
            if mode!="valid":
                assert after==before[table]
                assert isinstance(sa.inspect(conn).get_columns(table)[1]["type"],sa.String)
                if table=="scores":assert "legacy_trade_date_v85" not in {c["name"] for c in sa.inspect(conn).get_columns(table)}
            else:
                column="trade_date" if table=="scores" else "train_start_date"
                assert isinstance(sa.inspect(conn).get_columns(table)[1]["type"],sa.Date)
                assert len(after)==len(before[table])
                for old,new in zip(before[table],after):
                    assert new["legacy_"+column+"_v85"]==old[column]
                    projected={k:new[k] for k in old};projected[column]=new["legacy_"+column+"_v85"]
                    assert projected==old
                    assert new[column] is None or new[column].isoformat()==old[column]
    engine.dispose()
