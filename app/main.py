from contextlib import asynccontextmanager
import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import settings, load_db_config, build_mysql_url
from app.db.init_db import init_db
from app.db.manager import DatabaseManager
from app.services.discovery_cleanup import cleanup_expired_discovery_results
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

    yield

    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass  # 正常取消路径
    except Exception:
        # 风控加固：不再静默吞没，记录日志便于定位 lifespan 关闭问题
        logger.exception("cleanup_task shutdown failed")

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


app = FastAPI(
    title="Personal Quant Workbench API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router)
web_root = Path(__file__).resolve().parent / "web"
dist_root = web_root / "dist"

# 提供 React 构建产物的静态服务（JS/CSS 分片）
if dist_root.exists():
    app.mount("/assets", StaticFiles(directory=dist_root / "assets"), name="assets")

# 保留旧版静态文件挂载
app.mount("/static", StaticFiles(directory=web_root / "static"), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
@app.get("/workbench", include_in_schema=False)
def workbench() -> FileResponse:
    index_path = dist_root / "index.html" if dist_root.exists() else web_root / "index.html"
    return FileResponse(index_path)


# 前端路由兜底（组合、挖掘、设置等标签页）
@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str) -> FileResponse:
    # 不拦截 API 或静态资源请求
    if full_path.startswith(("api/", "static/", "assets/", "health")):
        raise HTTPException(status_code=404, detail="Not found")
    index_path = dist_root / "index.html" if dist_root.exists() else web_root / "index.html"
    return FileResponse(index_path)
