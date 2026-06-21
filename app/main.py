from contextlib import asynccontextmanager
import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.api.router import api_router
from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.services.discovery_cleanup import cleanup_expired_discovery_results

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

# Cleanup interval in seconds (default 30 minutes)
CLEANUP_INTERVAL_SECONDS = 30 * 60


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()

    # Startup cleanup: remove expired non-frozen discovery results
    _run_startup_cleanup()

    # Start periodic cleanup background task
    cleanup_task = asyncio.create_task(_periodic_cleanup())

    yield

    cleanup_task.cancel()
    try:
        await cleanup_task
    except (asyncio.CancelledError, Exception):
        pass


def _run_startup_cleanup() -> None:
    """Run cleanup once at startup."""
    try:
        db = SessionLocal()
        try:
            result = cleanup_expired_discovery_results(db)
            if result["deleted"] > 0:
                logger.info(
                    "Startup cleanup completed: %d expired results removed",
                    result["deleted"],
                )
            else:
                logger.info("Startup cleanup: no expired results to remove")
        finally:
            db.close()
    except Exception:
        logger.exception("Startup cleanup failed")


async def _periodic_cleanup() -> None:
    """Periodically clean up expired discovery results."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            db = SessionLocal()
            try:
                result = cleanup_expired_discovery_results(db)
                if result["deleted"] > 0:
                    logger.info(
                        "Periodic cleanup: %d expired results removed",
                        result["deleted"],
                    )
            finally:
                db.close()
        except Exception:
            logger.exception("Periodic cleanup failed")


app = FastAPI(
    title="Personal Quant Workbench API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router)
web_root = Path(__file__).resolve().parent / "web"
dist_root = web_root / "dist"

# Serve React build assets (JS/CSS chunks)
if dist_root.exists():
    app.mount("/assets", StaticFiles(directory=dist_root / "assets"), name="assets")

# Keep legacy static mount for any remaining static files
app.mount("/static", StaticFiles(directory=web_root / "static"), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
@app.get("/workbench", include_in_schema=False)
def workbench() -> FileResponse:
    index_path = dist_root / "index.html" if dist_root.exists() else web_root / "index.html"
    return FileResponse(index_path)


# Catch-all for client-side routing (portfolio, discovery, settings tabs)
@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str) -> FileResponse:
    # Don't intercept API or static asset requests
    if full_path.startswith(("api/", "static/", "assets/", "health")):
        raise HTTPException(status_code=404, detail="Not found")
    index_path = dist_root / "index.html" if dist_root.exists() else web_root / "index.html"
    return FileResponse(index_path)
