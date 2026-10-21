"""The narrow schema upgrade preserves repeated same-day snapshot history."""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


pytestmark = pytest.mark.whitebox


def test_snapshot_version_upgrade_keeps_history_and_refuses_lossy_downgrade(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{(tmp_path / 'snapshot-versions.db').as_posix()}"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", url)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    previous = "wps_0023_072_verified_input_precision"
    command.upgrade(config, previous)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO discovery_score_snapshots "
                "(id,scope,trade_date,status,created_at,updated_at) VALUES "
                "(1,'cn_stock','2026-10-19','superseded','2026-10-19','2026-10-19'),"
                "(2,'cn_stock','2026-10-19','ready','2026-10-19','2026-10-19')"))
            connection.execute(text("INSERT INTO discovery_score_snapshot_items "
                "(snapshot_id,universe_symbol_id,created_at) VALUES (1,10,'2026-10-19')"))
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(text("UPDATE discovery_score_snapshots SET status='superseded' WHERE id=2"))
            connection.execute(text("INSERT INTO discovery_score_snapshots "
                "(id,scope,trade_date,status,created_at,updated_at) VALUES "
                "(3,'cn_stock','2026-10-19','ready','2026-10-19','2026-10-19')"))
            assert connection.scalar(text("SELECT COUNT(*) FROM discovery_score_snapshots")) == 3
            assert connection.scalar(text("SELECT COUNT(*) FROM discovery_score_snapshot_items WHERE snapshot_id=1")) == 1
        assert "ix_snapshot_scope_status_date" in {item["name"] for item in inspect(engine).get_indexes("discovery_score_snapshots")}
        with pytest.raises(RuntimeError, match="historical snapshots"):
            command.downgrade(config, previous)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM discovery_score_snapshots")) == 3
    finally:
        engine.dispose()
