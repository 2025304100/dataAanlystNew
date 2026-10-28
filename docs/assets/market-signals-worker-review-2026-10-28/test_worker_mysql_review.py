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
from tests.test_worker_delivery_review import (
    test_stale_manual_retry_must_not_revive_another_workers_claim_or_success,
    test_success_persistence_exception_must_not_become_automatic_transport_retry,
    test_accepted_retry_payload_must_remain_immutable_for_its_idempotency_key,
    test_two_workers_claim_same_pending_row_only_once,
    test_two_independent_processes_poll_same_row_and_invoke_substitute_once,
    signals_client,
)

pytestmark = pytest.mark.whitebox


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
def db_session(record_property):
    url = make_url(os.environ['OFFLINE_WORKER_MYSQL_URL'])
    assert url.host in ('127.0.0.1', 'localhost') and not url.database
    schema = 'codex_worker_review_' + uuid4().hex[:16]
    admin = create_engine(url, hide_parameters=True)
    with admin.begin() as connection:
        version = connection.scalar(text('SELECT VERSION()'))
        assert version.startswith('5.7.')
        connection.exec_driver_sql(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    engine = create_engine(url.set(database=schema), hide_parameters=True)
    Base.metadata.create_all(engine)
    record_property('mysql_version', version)
    record_property('isolated_schema', schema)
    try:
        with Session(engine, autoflush=False) as db:
            yield db
    finally:
        engine.dispose()
        admin.dispose()  # Preserve schemas for audit; do not drop business data.
