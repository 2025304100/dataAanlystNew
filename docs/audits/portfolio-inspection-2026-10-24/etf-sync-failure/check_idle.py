"""Refuse a controlled backend restart while any task is active."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, text
from app.core.config import build_mysql_url, load_db_config
engine = create_engine(build_mysql_url(load_db_config()))
with engine.connect() as connection:
    active = connection.execute(text("SELECT id FROM async_tasks WHERE status IN ('queued','running')")).scalars().all()
    versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
engine.dispose()
if active:
    raise SystemExit("Active tasks exist; backend was not stopped")
if versions != ['wps_0023_076_inspection_workflow']:
    raise SystemExit("Migration version differs; backend was not stopped")
print("No active tasks; expected inspection schema is present.")
