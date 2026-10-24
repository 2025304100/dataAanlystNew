"""Serve the real local database for inspection acceptance without trading.

Run migrations separately first. This entry point deliberately skips ordinary
startup seeds, repair/cleanup loops and other schedule types. It keeps existing
trading settings intact and returns 423 for all non-inspection mutations.
"""
from __future__ import annotations

import asyncio
import argparse
from contextlib import asynccontextmanager, suppress
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.responses import JSONResponse
from sqlalchemy import text
from app.main import app, initialize_runtime_database
from app.db.manager import DatabaseManager
from app.services.scheduled_tasks import scheduler_loop

# These endpoints read evidence or persist checks/association requests. Final
# recovery, baseline acceptance/activation, binding and admission are excluded.
ALLOWED_WRITES = re.compile(
    r"^/api/v1/portfolios/1/(?:reconcile|inspection/reconciliation-job/recheck|"
    r"inspection/evidence-supplements(?:/batch|/[^/]+/review)?|"
    r"inspection/material-investigations|"
    r"inspection/repairs(?:/[a-f0-9]{32}/(?:retry|cancel))?|"
    r"inspection/coverage/preview)$"
)
BACKGROUND_JOBS_ENABLED = True


@app.middleware('http')
async def inspection_mutation_guard(request, call_next):
    if request.method == 'GET' and request.url.path == '/api/v1/inspection-acceptance/status':
        return JSONResponse(content=acceptance_status())
    if request.method not in {'GET', 'HEAD', 'OPTIONS'} and not ALLOWED_WRITES.fullmatch(request.url.path):
        return JSONResponse(status_code=423, content={
            'error_code': 'LOCAL_INSPECTION_REVIEW_ONLY',
            'message': '当前为本地验收模式：恢复交易、下单、准入及其他设置写入已拦截，请先审核验收结果。',
        })
    return await call_next(request)


@app.get('/api/v1/inspection-acceptance/status')
def acceptance_status():
    return {'mode': 'inspection_acceptance', 'portfolio_id': 1,
            'scheduled_task_types': ['portfolio_reconciliation'] if BACKGROUND_JOBS_ENABLED else [],
            'background_jobs_enabled': BACKGROUND_JOBS_ENABLED,
            'automatic_trading_enabled': False,
            'trading_mutations_allowed': False, 'final_reviews_allowed': False}


@asynccontextmanager
async def inspection_lifespan(_):
    initialize_runtime_database()
    with DatabaseManager.get().engine.connect() as connection:
        versions = connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all()
        if versions != ['wps_0023_076_inspection_workflow']:
            raise RuntimeError('Apply inspection migrations before starting acceptance service')
    loop = (asyncio.create_task(scheduler_loop(task_types=frozenset({'portfolio_reconciliation'})))
            if BACKGROUND_JOBS_ENABLED else None)
    try:
        yield
    finally:
        if loop is not None:
            loop.cancel()
            with suppress(asyncio.CancelledError):
                await loop


app.router.lifespan_context = inspection_lifespan

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-background-jobs', action='store_true',
                        help='Disable all scheduler/background jobs for a controlled manual acceptance run')
    args = parser.parse_args()
    BACKGROUND_JOBS_ENABLED = not args.no_background_jobs
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8000)
