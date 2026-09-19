"""Prepare and validate the application database before service startup."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]
# When launched as ``python scripts/bootstrap_db.py``, Python puts only the
# scripts directory on sys.path. Register the project root before importing
# application modules so startup works from any current working directory.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REQUIRED_TABLES = {
    "factor_model_members",
    "factor_model_runs",
    "security_status_daily",
    "backtest_runs",
    "portfolio_members",
}


def main() -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
    )
    if result.returncode:
        print("Database migration failed; services will not start.", file=sys.stderr)
        return result.returncode

    from app.db.init_db import init_db
    from app.core.config import build_mysql_url, load_db_config, settings
    from app.main import initialize_runtime_database

    # init_db remains a compatibility/seed step. Migrations are the source of truth.
    initialize_runtime_database()
    init_db()
    cfg = load_db_config()
    url = os.environ.get("DATABASE_URL", settings.database_url)
    if not os.environ.get("DATABASE_URL") and cfg.get("use_mysql"):
        url = build_mysql_url(cfg)

    engine = create_engine(url, pool_pre_ping=True)
    missing = sorted(REQUIRED_TABLES - set(inspect(engine).get_table_names()))
    if missing:
        print("Database validation failed; missing tables: " + ", ".join(missing), file=sys.stderr)
        return 2
    print("Database ready: migration and required-table checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
