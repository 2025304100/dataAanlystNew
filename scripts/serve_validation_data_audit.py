"""Serve current application code against gpfx_validation without startup writes/jobs.

Only this process is read-only. Controlled missing-row inserts run separately
from frozen manifests; normal application startup/configuration is untouched.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def build_app(expected_revision: str):
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    url = os.environ.get("DATABASE_URL", "")
    parsed = make_url(url)
    if (parsed.database != "gpfx_validation" or parsed.host not in {"127.0.0.1", "localhost"}
            or not parsed.drivername.startswith("mysql")):
        raise RuntimeError("Explicit local gpfx_validation DATABASE_URL is required")
    from app.services.inspection_acceptance_readonly import (
        enable_acceptance_read_only, install_acceptance_http_guard,
    )
    enable_acceptance_read_only()
    from app.main import app
    from app.db.manager import DatabaseManager

    tracked = ["app/services/task_outcomes.py", "app/services/source_field_integrity.py",
               "app/services/fundamental_data.py", "app/services/market_data.py",
               "app/services/universe_sync.py", "app/services/external_data_sync_task.py",
               "app/services/capital_flow_data.py", "app/services/capital_flow_history_provider.py"]
    tracked.append("app/services/market_history_contract.py")
    tracked.extend(["app/api/routes/external_data.py", "scripts/serve_validation_data_audit.py"])
    tracked.append("app/services/universe_health.py")
    status = {"mode": "inspection_acceptance", "purpose": "validation_data_audit", "database": "gpfx_validation",
              "read_only": True, "background_jobs_enabled": False,
              "migrations_enabled": False, "startup_repairs_enabled": False,
              "task_registration_allowed": False, "task_dispatch_allowed": False,
              "expected_revision": expected_revision,
              "loaded_files": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                               for name in tracked}}

    @asynccontextmanager
    async def safe_lifespan(_):
        manager = DatabaseManager.get()
        manager.initialize(url, db_type="mysql")
        with manager.engine.connect() as connection:
            database = connection.scalar(text("SELECT DATABASE()"))
            versions = list(connection.scalars(text("SELECT version_num FROM alembic_version")))
            running = connection.scalar(text("SELECT COUNT(*) FROM async_tasks WHERE status IN ('running','queued')"))
        if database != "gpfx_validation" or versions != [expected_revision] or running:
            manager.dispose()
            raise RuntimeError("Validation database/revision/idle preflight failed; no initialization was run")
        status["revision"] = versions[0]
        status["pid"] = os.getpid()
        try:
            yield
        finally:
            manager.dispose()

    app.router.lifespan_context = safe_lifespan
    install_acceptance_http_guard(app, status_provider=lambda: status)

    @app.get("/api/v1/validation-data-audit/status")
    def validation_status():
        return status

    # app.main already includes its SPA catch-all; put this explicit API first.
    app.router.routes.insert(0, app.router.routes.pop())

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(build_app(args.expected_revision), host="127.0.0.1", port=args.port)
