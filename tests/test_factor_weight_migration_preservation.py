"""Upgrade and rollback preserve authentic historical coefficient rows."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.whitebox


def _migration(name, connection):
    path = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob(f"*{name}*.py"))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


def test_authentic_legacy_weights_survive_upgrade_roundtrip_without_guessing():
    engine = create_engine("sqlite://")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE factor_model_runs (id VARCHAR(64) PRIMARY KEY)"))
        db.execute(text("INSERT INTO factor_model_runs (id) VALUES ('old-model')"))
        db.execute(text("""CREATE TABLE factor_weight_snapshots (
            id INTEGER PRIMARY KEY, model_run_id VARCHAR(64), factor_code VARCHAR(64),
            factor_version INTEGER, coefficient FLOAT, normalized_weight FLOAT,
            train_ic FLOAT, validation_ic FLOAT, created_at DATETIME)"""))
        db.execute(text("""INSERT INTO factor_weight_snapshots VALUES
            (1,'old-model','ep_ttm',2,0.123,0.4,0.02,0.01,'2026-08-01'),
            (2,'old-model','turnover_z20',1,-0.42,-0.6,0.03,0.01,'2026-08-01'),
            (3,'old-model','ambiguous',1,0.1,0.2,NULL,NULL,'2026-08-01'),
            (4,'old-model','ambiguous',2,0.2,0.3,NULL,NULL,'2026-08-01')"""))
        snapshot = _migration("049_factor_weight_snapshots", db)
        member = _migration("050_factor_model_members", db)
        snapshot.upgrade()
        member.upgrade()
        assert db.scalar(text("SELECT COUNT(*) FROM factor_weight_snapshots_legacy")) == 4
        assert db.scalar(text("SELECT COUNT(*) FROM factor_weight_snapshots")) == 0
        rows = list(db.execute(text("SELECT factor_code, factor_version, coefficient FROM factor_model_members ORDER BY factor_code")))
        assert rows == [("ep_ttm", 2, 0.123), ("turnover_z20", 1, -0.42)]
        member.upgrade()  # replay never overwrites or duplicates authentic weights
        assert db.scalar(text("SELECT COUNT(*) FROM factor_model_members")) == 2
        member.downgrade()
        snapshot.downgrade()
        assert db.scalar(text("SELECT COUNT(*) FROM factor_weight_snapshots")) == 4
        snapshot.upgrade()
        member.upgrade()
        assert db.scalar(text("SELECT COUNT(*) FROM factor_model_members")) == 2
        assert db.scalar(text("SELECT COUNT(*) FROM factor_weight_snapshots_legacy")) == 4
