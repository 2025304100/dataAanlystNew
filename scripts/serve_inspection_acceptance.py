"""Serve the real local database for inspection acceptance without trading.

Run migrations separately first. This entry point deliberately skips ordinary
startup seeds, repair/cleanup loops and all schedules. All mutations are denied;
database writes and task registration/dispatch are independently guarded.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.inspection_acceptance_readonly import enable_acceptance_read_only, install_acceptance_http_guard, acceptance_execution_status
enable_acceptance_read_only()

from sqlalchemy import text
from app.main import app, initialize_runtime_database
from app.db.manager import DatabaseManager

def acceptance_status():
    return acceptance_execution_status()


install_acceptance_http_guard(app, status_provider=acceptance_status)


@asynccontextmanager
async def inspection_lifespan(_):
    initialize_runtime_database()
    with DatabaseManager.get().engine.connect() as connection:
        versions = connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all()
        if versions != ['wps_0024_078_request_origin']:
            raise RuntimeError('Apply inspection migrations before starting acceptance service')
    yield


app.router.lifespan_context = inspection_lifespan

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-background-jobs', action='store_true',
                        help='Compatibility flag; all background jobs are always disabled in this read-only service')
    parser.parse_args()
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8000)
