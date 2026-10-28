"""Repeat reviewed queue boundaries against fresh local MySQL 5.7 schemas."""
import inspect
import os
from pathlib import Path
import socket
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.base import Base
from tests.test_notification_worker_regressions import (
    test_stale_manual_retry_must_not_revive_another_workers_claim_or_success,
    test_success_persistence_exception_must_not_become_automatic_transport_retry,
    test_accepted_retry_payload_must_remain_immutable_for_its_idempotency_key,
    test_two_workers_claim_same_pending_row_only_once,
    test_two_independent_processes_poll_same_row_and_invoke_substitute_once,
    signals_client,
    test_late_success_cannot_complete_a_newer_attempt,
    test_duplicate_success_receipt_is_idempotent,
    test_late_failure_cannot_reset_a_newer_attempt,
    test_retry_rejects_an_aba_failure_generation,
    test_retry_http_reports_conflict_after_other_request_claims,
    test_error_after_success_commit_cannot_requeue_the_receipt,
    test_unknown_legacy_attempt_is_held_without_transport,
    test_pretransport_failure_remains_safely_retryable,
    test_mutated_frozen_content_is_held_without_resend,
    test_frozen_batch_with_cancelled_member_is_not_filtered_and_resent,
)

pytestmark = [pytest.mark.whitebox, pytest.mark.skipif(
    not os.environ.get('OFFLINE_WORKER_MYSQL_URL'), reason='Requires explicit isolated local MySQL 5.7 access')]


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    url = make_url(os.environ['OFFLINE_WORKER_MYSQL_URL'])
    original = socket.socket.connect
    def database_only(sock, address):
        caller = inspect.currentframe().f_back
        if caller.f_code.co_name == 'socketpair' and Path(caller.f_code.co_filename).name == 'socket.py':
            return original(sock, address)
        if address[0] in ('127.0.0.1', 'localhost') and address[1] == (url.port or 3306):
            return original(sock, address)
        raise AssertionError('Only the isolated local MySQL database may be contacted')
    monkeypatch.setattr(socket.socket, 'connect', database_only)
    monkeypatch.setattr(socket.socket, 'connect_ex', database_only)


@pytest.fixture
def db_session(record_property, request):
    url = make_url(os.environ['OFFLINE_WORKER_MYSQL_URL'])
    assert url.host in ('127.0.0.1', 'localhost') and not url.database
    schema = 'codex_worker_review_' + uuid4().hex[:16]
    admin = create_engine(url, hide_parameters=True)
    with admin.begin() as connection:
        version = connection.scalar(text('SELECT VERSION()'))
        assert version.startswith('5.7.')
        connection.exec_driver_sql(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    engine = create_engine(url.set(database=schema), hide_parameters=True)
    if 'signals_client' in request.fixturenames:
        Base.metadata.create_all(engine)
    else:
        from app.models.notification import NotificationChannel, NotificationOutbox, NotificationDelivery
        Base.metadata.create_all(engine, tables=[NotificationChannel.__table__, NotificationOutbox.__table__, NotificationDelivery.__table__])
    record_property('mysql_version', version)
    record_property('isolated_schema', schema)
    try:
        with Session(engine, autoflush=False) as db:
            yield db
    finally:
        engine.dispose()
        admin.dispose()  # Preserve schemas for audit; do not drop business data.
