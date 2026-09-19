"""Migration contract for the Stage-2 data-quality governance ledger.

The runtime ORM has a newer shape than the historical data-quality snapshot
table.  These checks use the public Alembic boundary so a metadata-first
installation cannot hide a migration-only regression.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRE_0038_REVISION = "wps_0023_035_data_quality_quarantines"


def _alembic_config(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    db_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", db_url)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _assert_runtime_quality_contract(db_path: Path) -> None:
    from app.models.data_quality_snapshot import DataQualitySnapshot
    from app.services.data_governance_audit import (
        audit_source_failover,
        write_audit_event,
    )

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    try:
        columns = {item["name"] for item in inspect(engine).get_columns("data_quality_snapshots")}
        assert {
            "id",
            "dataset",
            "field",
            "readiness",
            "evaluation_mode",
            "row_count",
            "nonnull_rows",
            "distinct_symbols",
            "distinct_dates",
            "metrics_json",
            "quality_level",
            "quality_assessment_id",
            "missing_ratio",
            "max_consecutive_gap_days",
            "quarantine_required",
            "captured_at",
        }.issubset(columns)

        session = sessionmaker(bind=engine, future=True)()
        try:
            session.add(DataQualitySnapshot(
                id="dq-migration-proof",
                dataset="daily_bars",
                field="close",
                readiness="available",
                evaluation_mode="continuous",
                row_count=10,
                nonnull_rows=10,
                distinct_symbols=1,
                distinct_dates=10,
                quality_level="LIGHT",
                quality_assessment_id="assessment-migration-proof",
                missing_ratio=0.0,
                max_consecutive_gap_days=0,
                quarantine_required=False,
            ))
            audit_source_failover(
                session,
                interface_key="akshare.daily_bars",
                attempted_sources=["akshare", "baostock"],
                selected_source="baostock",
            )
            write_audit_event(
                session,
                "DATA_QUALITY_QUARANTINE",
                business_key="quarantine-migration-proof",
            )
            write_audit_event(
                session,
                "DATA_BLOCK_RESOLUTION",
                business_key="resolution-migration-proof",
            )
            session.commit()
        finally:
            session.close()
    finally:
        engine.dispose()


def test_fresh_upgrade_head_supports_quality_runtime_contract(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh_quality.db"
    command.upgrade(_alembic_config(db_path, monkeypatch), "head")

    _assert_runtime_quality_contract(db_path)


def test_historical_0037_upgrade_preserves_snapshot_rows_and_enables_new_actions(tmp_path, monkeypatch):
    db_path = tmp_path / "historical_quality.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, PRE_0038_REVISION)

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO data_quality_snapshots "
                "(id, snapshot_date, data_type, scope, total_count, valid_count, "
                "invalid_count, missing_count, coverage_pct, issues_json, created_at) "
                "VALUES (7, '2026-08-21', 'daily_bars', 'close', 10, 9, 1, 1, "
                "90.0, :issues_json, :created_at)"
            ), {
                "issues_json": '{\"legacy\":true}',
                "created_at": datetime(2026, 8, 21, 8, 0, 0),
            })
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT id, dataset, field, readiness, row_count, nonnull_rows "
                "FROM data_quality_snapshots WHERE id = '7'"
            )).mappings().one()
        assert row == {
            "id": "7",
            "dataset": "daily_bars",
            "field": "close",
            "readiness": "legacy",
            "row_count": 10,
            "nonnull_rows": 9,
        }
    finally:
        engine.dispose()

    _assert_runtime_quality_contract(db_path)


def test_metadata_first_current_models_can_be_stamped_and_upgraded(tmp_path, monkeypatch):
    from app.db.base import Base
    from app.models import data_governance_quarantine, data_quality_snapshot  # noqa: F401
    from app.services import data_governance_audit  # noqa: F401

    db_path = tmp_path / "metadata_first_quality.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    try:
        # Metadata-first is a supported legacy bootstrap path.  It creates the
        # current ORM shape before the release migration is stamped.
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    cfg = _alembic_config(db_path, monkeypatch)
    command.stamp(cfg, PRE_0038_REVISION)
    command.upgrade(cfg, "head")

    _assert_runtime_quality_contract(db_path)
