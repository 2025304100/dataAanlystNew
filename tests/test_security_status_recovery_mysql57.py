"""Real 5.7 receipt round trips; MYSQL57_TEST_URL must name a dedicated clone.

No application database fallback is allowed. Tests preserve their evidence in
the dedicated database; supply a fresh clone name for each run.
"""
from datetime import date, datetime
import importlib.util
import json
import os
from pathlib import Path
import re

import pytest
from sqlalchemy import create_engine, inspect, select, text, MetaData
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.models.security_status import SecurityStatusDaily
from app.schemas.security_status import SecurityStatusFileRequest
from app.services.data_governance_audit import DataGovernanceAuditEvent
from app.services.security_status.local_import import preview_import, import_file, get_imported_execution_constraints
from app.services.security_status.timestamp_recovery import preview_status_timestamp_recovery, apply_status_timestamp_recovery

pytestmark = pytest.mark.whitebox


@pytest.fixture(scope="module")
def mysql57_engine():
    raw = os.environ.get("MYSQL57_TEST_URL")
    if not raw:
        pytest.skip("MYSQL57_TEST_URL is required for real MySQL 5.7 verification")
    url = make_url(raw)
    assert url.get_backend_name() == "mysql" and re.fullmatch(r"codex_combination_[a-z0-9][a-z0-9_]{0,44}", url.database or ""), "Only a dedicated codex_combination_ test database is allowed"
    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT DATABASE()")) == url.database
        assert connection.scalar(text("SELECT VERSION()")).startswith("5.7."), "This acceptance suite specifically requires MySQL 5.7"
        assert not inspect(connection).get_table_names(), "Use a fresh empty isolated test database; existing evidence must not be cleared"
    metadata = MetaData()
    for table in (Symbol.__table__, Portfolio.__table__, SecurityStatusDaily.__table__, DataGovernanceAuditEvent.__table__):
        copied = table.to_metadata(metadata)
        copied.dialect_options["mysql"]["engine"] = "InnoDB"
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def import_original(db, code, stamp):
    symbol = Symbol(symbol=code, name="precision acceptance", asset_type="stock", market="SH")
    db.add(symbol)
    db.commit()
    payload = SecurityStatusFileRequest(
        csv_text="symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date,price_limit_mode,upper_limit_price,lower_limit_price,tick_size\n"
                 f"{code},2025-09-01,0,0,0,1,1999-11-10,,LIMITED,11,9,0.01\n",
        source_name="provider_archive", source_reference=f"archive://daily/{code}",
        source_version="release-20250901", source_updated_at=stamp)
    preview = preview_import(db, payload)
    assert preview["can_import"]
    receipt = import_file(db, payload, preview_sha256=preview["content_sha256"])
    db.commit()
    return symbol.id, payload, receipt


def apply_recovery(db, payload, receipt):
    plan = preview_status_timestamp_recovery(db, payload, expected_content_sha256=receipt["content_sha256"])
    assert plan["can_apply"], plan
    result = apply_status_timestamp_recovery(db, payload, expected_content_sha256=receipt["content_sha256"],
        preview_sha256=plan["preview_sha256"], operator_id="mysql57-acceptance", confirmed=True)
    db.commit()
    return result


def test_real_mysql57_rounding_migration_recovery_and_new_import_preserve_all_old_data(mysql57_engine):
    engine = mysql57_engine
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE security_status_daily MODIFY source_updated_at DATETIME NULL"))
    originals = []
    with Session(engine) as db:
        originals.append(import_original(db, "600001", "2025-09-02T10:00:00.126864+00:00"))
        originals.append(import_original(db, "600002", "2025-09-02T23:59:59.999999+00:00"))
        stored = db.scalars(select(SecurityStatusDaily).order_by(SecurityStatusDaily.id)).all()
        assert [row.source_updated_at for row in stored] == [datetime(2025, 9, 2, 10), datetime(2025, 9, 3)]
        before_rows = {row.id: {col.name: getattr(row, col.name) for col in row.__table__.columns} for row in stored}
        before_audits = list(db.execute(select(DataGovernanceAuditEvent.__table__).order_by(DataGovernanceAuditEvent.id)))
        for sid, payload, receipt in originals:
            assert get_imported_execution_constraints(db, sid, date(2025, 9, 1)) is None
            assert preview_import(db, payload)["can_import"] is False
    migration_path = Path(__file__).resolve().parents[1] / "alembic/versions/2026_10_25_0082_security_status_datetime6.py"
    spec = importlib.util.spec_from_file_location("mysql57_0082", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
    with Session(engine) as db:
        unchanged = {row.id: {col.name: getattr(row, col.name) for col in row.__table__.columns} for row in db.scalars(select(SecurityStatusDaily))}
        assert unchanged == before_rows
        for sid, payload, receipt in originals:
            assert apply_recovery(db, payload, receipt)["repaired"] == 1
            db.expire_all()
            assert get_imported_execution_constraints(db, sid, date(2025, 9, 1))["upper_limit_price"] == 11
        for row in db.scalars(select(SecurityStatusDaily)):
            assert {col.name: getattr(row, col.name) for col in row.__table__.columns if col.name != "source_updated_at"} == {
                name: value for name, value in before_rows[row.id].items() if name != "source_updated_at"}
        after_audits = list(db.execute(select(DataGovernanceAuditEvent.__table__).where(DataGovernanceAuditEvent.id <= before_audits[-1].id).order_by(DataGovernanceAuditEvent.id)))
        assert after_audits == before_audits
        sid, payload, receipt = import_original(db, "600003", "2025-09-04T10:00:00.999999+00:00")
    with Session(engine) as db:
        assert get_imported_execution_constraints(db, sid, date(2025, 9, 1))["source_updated_at"] == payload.source_updated_at.isoformat()
        row = db.scalar(select(SecurityStatusDaily).where(SecurityStatusDaily.symbol_id == sid))
        assert row.source_updated_at.microsecond == 999999
        assert apply_recovery(db, payload, receipt)["idempotent"] is True


def test_real_mysql57_audit_failure_preserves_legacy_row(mysql57_engine):
    engine = mysql57_engine
    with Session(engine) as db:
        sid, payload, receipt = import_original(db, "600004", "2025-09-05T10:00:00.126864+00:00")
        row = db.scalar(select(SecurityStatusDaily).where(SecurityStatusDaily.symbol_id == sid))
        row.source_updated_at = row.source_updated_at.replace(microsecond=0)
        db.commit()
    with engine.begin() as connection:
        connection.execute(text("CREATE TRIGGER reject_precision_audit BEFORE INSERT ON data_governance_audit_events FOR EACH ROW "
                                "BEGIN IF NEW.business_key LIKE 'status_precision:%' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='acceptance audit failure'; END IF; END"))
    try:
        with Session(engine) as db:
            with pytest.raises(Exception, match="acceptance audit failure"):
                apply_recovery(db, payload, receipt)
            db.rollback()
        with Session(engine) as db:
            row = db.scalar(select(SecurityStatusDaily).where(SecurityStatusDaily.symbol_id == sid))
            assert row.source_updated_at == datetime(2025, 9, 5, 10)
            assert get_imported_execution_constraints(db, sid, date(2025, 9, 1)) is None
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TRIGGER reject_precision_audit"))


def test_real_mysql57_myisam_refused_without_any_writes():
    raw = os.environ.get("MYSQL57_NONTRANSACTIONAL_TEST_URL")
    if not raw:
        pytest.skip("MYSQL57_NONTRANSACTIONAL_TEST_URL is required for real MyISAM refusal verification")
    url = make_url(raw)
    assert url.get_backend_name() == "mysql" and re.fullmatch(r"codex_combination_[a-z0-9][a-z0-9_]{0,44}", url.database or "")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT DATABASE()")) == url.database
            assert connection.scalar(text("SELECT VERSION()")).startswith("5.7.")
            empty = not inspect(connection).get_table_names()
        if empty:
            # CI can provision an empty schema. Do not depend on a previous
            # failed run, and never clear an existing evidence database.
            metadata = MetaData()
            for table in (Symbol.__table__, Portfolio.__table__, SecurityStatusDaily.__table__, DataGovernanceAuditEvent.__table__):
                copied = table.to_metadata(metadata)
                copied.dialect_options["mysql"]["engine"] = "MyISAM"
            metadata.create_all(engine)
            with Session(engine) as db:
                sid, _, _ = import_original(db, "600005", "2025-09-02T10:00:00.126864+00:00")
                row = db.scalar(select(SecurityStatusDaily).where(SecurityStatusDaily.symbol_id == sid))
                row.source_updated_at = row.source_updated_at.replace(microsecond=0)
                db.commit()
        with Session(engine) as db:
            assert db.scalar(text("SELECT DATABASE()")) == url.database
            engines = dict(db.execute(text("SELECT table_name,engine FROM information_schema.tables WHERE table_schema=DATABASE()")).all())
            assert engines["security_status_daily"] == "MyISAM"
            before = list(db.execute(select(SecurityStatusDaily.__table__)))
            before_audit = list(db.execute(select(DataGovernanceAuditEvent.__table__)))
            # Even an invalid input must be rejected at the transaction safety
            # boundary before recovery can touch nontransactional historical rows.
            payload = SecurityStatusFileRequest(csv_text="original input unavailable", source_name="provider_archive",
                source_reference="archive://daily/600001", source_version="release-20250901",
                source_updated_at="2025-09-02T10:00:00.126864+00:00")
            with pytest.raises(ValueError, match="InnoDB"):
                apply_status_timestamp_recovery(db, payload, expected_content_sha256="a" * 64,
                    preview_sha256="b" * 64, operator_id="mysql57-acceptance", confirmed=True)
            assert list(db.execute(select(SecurityStatusDaily.__table__))) == before
            assert list(db.execute(select(DataGovernanceAuditEvent.__table__))) == before_audit
    finally:
        engine.dispose()
