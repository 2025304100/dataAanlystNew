"""Frozen revision operations tested without importing mutable live schemas."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_0092_is_replayable_and_rollback_preserves_evidence(tmp_path):
    path = Path(__file__).resolve().parents[1]/"alembic/versions/2026_10_27_0092_market_signals.py"
    spec = importlib.util.spec_from_file_location("market_signals_0092", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///"+str(tmp_path/"signals.db"))
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert len(inspect(connection).get_table_names()) == 7
        connection.execute(text("INSERT INTO market_signal_evidence "
            "(fingerprint,title,text,source,time_quality,kind,metadata_json,first_seen_at) "
            "VALUES ('fixture','已送达证据','原文不丢失','manual','unknown','news','{}','2026-10-27 09:00:00')"))
        migration.downgrade()
        migration.upgrade()
        migration.upgrade()
        assert connection.scalar(text("SELECT text FROM market_signal_evidence")) == "原文不丢失"
        assert len(inspect(connection).get_table_names()) == 7
    engine.dispose()
