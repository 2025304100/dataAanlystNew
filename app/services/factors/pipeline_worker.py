"""Child-process entry point. Never infer a database from local configuration."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys


def require_transactional_factor_database(db):
    """Never claim native-crash rollback on a nontransactional training DB."""
    if db.get_bind().dialect.name != 'mysql':
        return
    from sqlalchemy import text
    tables = db.execute(text(
        "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"
    )).all()
    invalid = [str(name) for name, engine in tables if str(engine or '').lower() != 'innodb']
    if not tables or invalid:
        raise RuntimeError('Factor worker requires InnoDB for every base table in its bound database; no automatic conversion is allowed')


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    explicit_url = os.environ.get("FACTOR_PIPELINE_DATABASE_URL")
    expected_payload = os.environ.get("FACTOR_PIPELINE_PAYLOAD_SHA256")
    expected_warehouse = os.environ.get("FACTOR_PIPELINE_WAREHOUSE_PATH")
    if len(arguments) != 1 or not explicit_url or not expected_payload or not expected_warehouse:
        print("Factor worker requires explicit database, task, payload and warehouse identities", file=sys.stderr)
        return 2
    os.environ["DATABASE_URL"] = explicit_url
    os.environ["FACTOR_WAREHOUSE_PATH"] = expected_warehouse
    from sqlalchemy.engine import make_url
    url = make_url(explicit_url)
    if not url.database or url.get_backend_name() not in {"mysql", "sqlite"} or url.database == ":memory:":
        print("Factor worker requires a persistent explicit database", file=sys.stderr)
        return 2
    from app.db.manager import DatabaseManager
    manager = DatabaseManager.get()
    manager.initialize(explicit_url, db_type=url.get_backend_name())
    from app.models.async_task import AsyncTaskRecord
    from app.services.factors.config import get_factor_system_config
    with manager.session_factory() as db:
        require_transactional_factor_database(db)
        task = db.get(AsyncTaskRecord, arguments[0])
        if task is None or task.task_type != "factor_pipeline" or hashlib.sha256((task.payload_json or "{}").encode("utf-8")).hexdigest() != expected_payload:
            print("Factor worker task identity differs from dispatch", file=sys.stderr)
            return 2
        config = get_factor_system_config(db)
        if Path(config.warehouse_path).expanduser().resolve() != Path(expected_warehouse).resolve():
            print("Factor worker warehouse identity differs from dispatch", file=sys.stderr)
            return 2
    from app.services.factors.pipeline_task import _run_factor_pipeline
    _run_factor_pipeline(arguments[0])
    with manager.session_factory() as db:
        task = db.get(AsyncTaskRecord, arguments[0])
        return 0 if task and task.status in {"done", "partial", "cancelled"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
