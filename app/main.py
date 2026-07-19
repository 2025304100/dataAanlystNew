from contextlib import asynccontextmanager
import asyncio
import logging
import socket
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import settings, load_db_config, build_mysql_url
from app.db.init_db import init_db
from app.db.manager import DatabaseManager
from app.services.discovery_cleanup import cleanup_expired_discovery_results
from app.services.async_tasks import interrupt_orphaned_async_tasks
from app.services.scheduled_tasks import scheduler_loop
from app.services.symbol_cleanup import cleanup_stale_discovery_symbols

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

# 清理间隔（秒），默认 30 分钟
CLEANUP_INTERVAL_SECONDS = 30 * 60


def _get_session_local():
    """延迟获取 SessionLocal（此时 manager 已初始化）。"""
    return DatabaseManager.get().session_factory


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 从配置文件初始化数据库引擎
    cfg = load_db_config()
    mgr = DatabaseManager.get()

    if cfg.get("use_mysql") and cfg.get("mysql", {}).get("host"):
        url = build_mysql_url(cfg)
        logger.info("Initializing MySQL engine: %s:%s/%s",
                     cfg["mysql"]["host"], cfg["mysql"]["port"], cfg["mysql"]["database"])
        mgr.initialize(url, db_type="mysql")
    else:
        logger.info("Initializing SQLite engine: %s", settings.database_url)
        mgr.initialize(settings.database_url, db_type="sqlite")

    init_db()

    # 启动时清理：移除过期且未冻结的挖掘结果
    _run_startup_cleanup()

    # 启动定期清理后台任务
    cleanup_task = asyncio.create_task(_periodic_cleanup())

    # 跨平台持久化调度器：Linux / Windows 均由设置页统一管理。
    scheduled_task_loop = asyncio.create_task(scheduler_loop())

    # 基础数据隔离层：首次启动时检测 universe_symbols 为空 → 自动触发初始化（异步，不阻塞启动）
    _auto_start_universe_init()

    yield

    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass  # 正常取消路径
    except Exception:
        # 风控加固：不再静默吞没，记录日志便于定位 lifespan 关闭问题
        logger.exception("cleanup_task shutdown failed")

    scheduled_task_loop.cancel()
    try:
        await scheduled_task_loop
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("scheduled_task_loop shutdown failed")

    # 风控加固：关闭探测线程池，避免 uvicorn reload 时线程泄漏
    try:
        from app.api.routes.akshare_apis import _PROBE_EXECUTOR
        _PROBE_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        logger.info("Probe executor shutdown complete")
    except Exception:
        logger.exception("Probe executor shutdown failed")

    # 关闭数据库连接池
    mgr.dispose()


def _run_startup_cleanup() -> None:
    """启动时执行一次清理。"""
    try:
        SessionLocal = _get_session_local()
        db = SessionLocal()
        try:
            interrupted_ids = interrupt_orphaned_async_tasks(db)
            if interrupted_ids:
                logger.warning(
                    "Startup cleanup: marked %d orphaned async tasks as interrupted: %s",
                    len(interrupted_ids),
                    interrupted_ids,
                )
            result = cleanup_expired_discovery_results(db)
            if result["deleted"] > 0:
                logger.info(
                    "Startup cleanup completed: %d expired results removed",
                    result["deleted"],
                )
            else:
                logger.info("Startup cleanup: no expired results to remove")
            # 清理挖掘遗留的僵尸标的
            stale_result = cleanup_stale_discovery_symbols(db)
            if stale_result["total_cleaned"] > 0:
                logger.info(
                    "Startup cleanup: %d stale symbols deactivated (tasks: %s)",
                    stale_result["total_cleaned"],
                    stale_result["cleaned_task_ids"],
                )
        finally:
            db.close()
    except Exception:
        logger.exception("Startup cleanup failed")


async def _periodic_cleanup() -> None:
    """定期清理过期的挖掘结果。"""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            SessionLocal = _get_session_local()
            db = SessionLocal()
            try:
                result = cleanup_expired_discovery_results(db)
                if result["deleted"] > 0:
                    logger.info(
                        "Periodic cleanup: %d expired results removed",
                        result["deleted"],
                    )
                # 清理挖掘遗留的僵尸标的
                stale_result = cleanup_stale_discovery_symbols(db)
                if stale_result["total_cleaned"] > 0:
                    logger.info(
                        "Periodic cleanup: %d stale symbols deactivated",
                        stale_result["total_cleaned"],
                    )
            finally:
                db.close()
        except Exception:
            logger.exception("Periodic cleanup failed")


def _auto_start_universe_init() -> None:
    """首次启动时检测 universe_symbols 表为空 → 自动触发初始化同步。

    异步执行，不阻塞启动。用户也可在前端手动触发。
    若已有运行中的同步任务则跳过。
    """
    try:
        from app.services import universe_sync, universe_sync_task
        SessionLocal = _get_session_local()
        db = SessionLocal()
        try:
            stats = universe_sync.get_universe_stats(db)
            if not stats["is_empty"]:
                logger.info(
                    "Universe data already exists (symbols=%d, synced=%d), skip auto init",
                    stats["total_symbols"], stats["synced_symbols"],
                )
                return
        finally:
            db.close()

        logger.info("Universe symbols table is empty, auto-starting initialization sync")
        universe_sync_task.start_universe_sync_init(max_workers=5, history_days=365)
    except Exception:
        logger.exception("Auto universe init failed (non-fatal, user can trigger manually)")


app = FastAPI(
    title="Personal Quant Workbench API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router)

FRONTEND_DEV_ORIGIN = "http://127.0.0.1:5173"
FRONTEND_DEV_HOST = "127.0.0.1"
FRONTEND_DEV_PORT = 5173
FRONTEND_DEV_PROBE_TTL_SECONDS = 1.0

web_root = Path(__file__).resolve().parent / "web"
dist_root = web_root / "dist"
_frontend_dev_probe: dict[str, float | bool] = {"checked_at": 0.0, "available": False}


def _frontend_dev_available(force: bool = False) -> bool:
    now = time.monotonic()
    if not force and now - float(_frontend_dev_probe["checked_at"]) < FRONTEND_DEV_PROBE_TTL_SECONDS:
        return bool(_frontend_dev_probe["available"])

    available = False
    try:
        with socket.create_connection((FRONTEND_DEV_HOST, FRONTEND_DEV_PORT), timeout=0.2):
            available = True
    except OSError:
        available = False

    _frontend_dev_probe["checked_at"] = now
    _frontend_dev_probe["available"] = available
    return available


def _frontend_dev_url(request: Request) -> str:
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{FRONTEND_DEV_ORIGIN}{request.url.path}{query}"


def _proxy_frontend_dev(request: Request) -> Response | None:
    if not _frontend_dev_available():
        return None

    upstream_request = UrlRequest(
        _frontend_dev_url(request),
        headers={"User-Agent": "personal-quant-workbench-dev-proxy"},
    )

    try:
        with urlopen(upstream_request, timeout=2.0) as upstream:
            body = upstream.read()
            headers: dict[str, str] = {}
            content_type = upstream.headers.get("Content-Type")
            cache_control = upstream.headers.get("Cache-Control")
            if content_type:
                headers["content-type"] = content_type
            if cache_control:
                headers["cache-control"] = cache_control
            return Response(content=body, status_code=upstream.status, headers=headers)
    except HTTPError as exc:
        body = exc.read()
        headers: dict[str, str] = {}
        content_type = exc.headers.get("Content-Type") if exc.headers else None
        if content_type:
            headers["content-type"] = content_type
        return Response(content=body, status_code=exc.code, headers=headers)
    except URLError:
        _frontend_dev_available(force=True)
        return None


app.mount("/static", StaticFiles(directory=web_root / "static"), name="static")


@app.get("/assets/{asset_path:path}", include_in_schema=False)
def frontend_assets(asset_path: str, request: Request) -> Response:
    proxied = _proxy_frontend_dev(request)
    if proxied is not None:
        return proxied

    asset_file = dist_root / "assets" / asset_path
    if asset_file.exists():
        return FileResponse(asset_file)
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
@app.get("/workbench", include_in_schema=False)
def workbench(request: Request) -> Response:
    proxied = _proxy_frontend_dev(request)
    if proxied is not None:
        return proxied

    index_path = dist_root / "index.html" if dist_root.exists() else web_root / "index.html"
    return FileResponse(index_path)


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str, request: Request) -> Response:
    if full_path.startswith(("api/", "static/", "health")):
        raise HTTPException(status_code=404, detail="Not found")

    proxied = _proxy_frontend_dev(request)
    if proxied is not None:
        return proxied

    index_path = dist_root / "index.html" if dist_root.exists() else web_root / "index.html"
    return FileResponse(index_path)
