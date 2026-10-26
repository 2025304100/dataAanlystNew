"""Local regression guards for failures discovered on real MySQL 5.7."""
import importlib.util
from pathlib import Path
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.whitebox


def test_0087_never_emits_mysql57_text_defaults():
    path = ROOT / "alembic/versions/2026_10_25_0087_market_event_collect_runs.py"
    spec = importlib.util.spec_from_file_location("ledger_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Collector:
        table = None
        def create_table(self, name, *columns):
            self.table = sa.Table(name, sa.MetaData(), *columns)
        def create_index(self, *args, **kwargs):
            pass
    collector = Collector()
    module.op = collector
    module.upgrade()
    ddl = str(CreateTable(collector.table).compile(dialect=mysql.dialect()))
    assert "TEXT NOT NULL DEFAULT" not in ddl
    assert collector.table.c.sources_json.server_default is None
    assert collector.table.c.errors_json.server_default is None


def test_retired_market_columns_keep_values_and_allow_current_writes():
    from app.db.market_event_schema import ensure_market_event_legacy_writes
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE market_events (id INTEGER PRIMARY KEY, event_type VARCHAR(32) NOT NULL, event_date VARCHAR(32) NOT NULL, title TEXT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO market_events VALUES (1,'legacy','2026-10-26','历史原文')")
        ensure_market_event_legacy_writes(connection)
        ensure_market_event_legacy_writes(connection)
        connection.exec_driver_sql("INSERT INTO market_events(id,title) VALUES (2,'当前写入')")
        assert connection.exec_driver_sql("SELECT * FROM market_events ORDER BY id").all() == [(1,'legacy','2026-10-26','历史原文'),(2,None,None,'当前写入')]
    engine.dispose()


@pytest.mark.parametrize("required", [(), ("event_type",), ("event_date",), ("event_type", "event_date")])
def test_0091_repairs_partial_or_complete_legacy_constraints_idempotently(required):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = ROOT / "alembic/versions/2026_10_26_0091_market_event_legacy_write_compat.py"
    spec = importlib.util.spec_from_file_location("market_compensation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        definitions = [f"{name} VARCHAR(32){' NOT NULL' if name in required else ''}"
                       for name in ("event_type", "event_date")]
        connection.exec_driver_sql("CREATE TABLE market_events (id INTEGER PRIMARY KEY, " +
            ", ".join(definitions) + ", title TEXT NOT NULL, sector_tag_source VARCHAR(16), affected_sectors TEXT)")
        connection.exec_driver_sql("CREATE INDEX ix_market_title_compat ON market_events(title)")
        connection.exec_driver_sql("INSERT INTO market_events VALUES (1,'legacy','2026-10-26','历史原文','manual','custom_id')")
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        module.upgrade()  # execute the migration function again, not just a revision no-op
        connection.exec_driver_sql("INSERT INTO market_events(id,title,sector_tag_source) VALUES (2,'当前写入','auto')")
        before_rollback = connection.exec_driver_sql("SELECT * FROM market_events ORDER BY id").all()
        module.downgrade()
        module.upgrade()
        assert connection.exec_driver_sql("SELECT * FROM market_events ORDER BY id").all() == before_rollback
        assert before_rollback[0] == (1,'legacy','2026-10-26','历史原文','manual','custom_id')
        assert before_rollback[1] == (2,None,None,'当前写入','auto',None)
        assert all(c["nullable"] for c in sa.inspect(connection).get_columns("market_events")
                   if c["name"] in ("event_type", "event_date"))
        assert "ix_market_title_compat" in {index["name"] for index in sa.inspect(connection).get_indexes("market_events")}
    engine.dispose()
