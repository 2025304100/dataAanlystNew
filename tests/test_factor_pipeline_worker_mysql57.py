"""Independent MySQL5.7 worker engine guard; never use an application DB."""
import os
import re

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.services.factors.pipeline_worker import require_transactional_factor_database

pytestmark = pytest.mark.whitebox


def test_real_mysql57_worker_rejects_myisam_before_any_training_write():
    raw = os.environ.get('MYSQL57_FACTOR_TEST_URL')
    if not raw:
        pytest.skip('MYSQL57_FACTOR_TEST_URL must name a fresh isolated MySQL5.7 schema')
    url = make_url(raw)
    assert url.get_backend_name() == 'mysql' and re.fullmatch(r'codex_combination_[a-z0-9][a-z0-9_]{0,44}', url.database or '')
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text('SELECT DATABASE()')) == url.database
            assert connection.scalar(text('SELECT VERSION()')).startswith('5.7.')
            assert not inspect(connection).get_table_names(), 'A fresh schema is required; never clear prior evidence'
            connection.execute(text('CREATE TABLE factor_worker_atomic_evidence (id INT PRIMARY KEY) ENGINE=InnoDB'))
        with Session(engine) as db:
            require_transactional_factor_database(db)
            db.execute(text('INSERT INTO factor_worker_atomic_evidence VALUES (1)'))
            db.rollback()
            assert db.scalar(text('SELECT COUNT(*) FROM factor_worker_atomic_evidence')) == 0
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE unsafe_worker_evidence (id INT PRIMARY KEY) ENGINE=MyISAM'))
        with Session(engine) as db:
            with pytest.raises(RuntimeError, match='InnoDB'):
                require_transactional_factor_database(db)
            assert db.scalar(text('SELECT COUNT(*) FROM factor_worker_atomic_evidence')) == 0
            assert db.scalar(text('SELECT COUNT(*) FROM unsafe_worker_evidence')) == 0
    finally:
        engine.dispose()
