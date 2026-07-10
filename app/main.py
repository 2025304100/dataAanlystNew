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

    # P2：启动定时增量同步后台任务（每日 18:00 自动增量同步 universe K线）
    incremental_sync_task = asyncio.create_task(_periodic_universe_incremental_sync())

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

    incremental_sync_task.cancel()
    try:
        await incremental_sync_task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("incremental_sync_task shutdown failed")

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


# P2：定时增量同步配置
# 每日触发时间（小时，24h 制），A股收盘后数据更新
UNIVERSE_INCREMENTAL_SYNC_HOUR = 18
# 检查间隔（秒）：每 10 分钟检查一次是否到达触发时间
UNIVERSE_INCREMENTAL_CHECK_INTERVAL = 10 * 60


async def _periodic_universe_incremental_sync() -> None:
    """定时增量同步：每日 18:00 自动触发 universe K线增量同步。

    策略：
    - 每 10 分钟检查一次当前时间是否到达 18:00
    - 到达触发时间且当日未执行过增量同步则触发
    - 并发保护：已有运行中的同步任务则跳过
    - 非阻塞：增量同步在独立线程执行，不影响主事件循环
    """
    from app.services import universe_sync_task
    from app.models.async_task import AsyncTaskRecord
    from sqlalchemy import select, desc
    import datetime as _dt

    last_trigger_date: _dt.date | None = None
    while True:
        await asyncio.sleep(UNIVERSE_INCREMENTAL_CHECK_INTERVAL)
        try:
            now = _dt.datetime.now()
            # 检查是否到达触发时间（18:00 ± 检查间隔/2）
            if now.hour < UNIVERSE_INCREMENTAL_SYNC_HOUR:
                continue
            today = now.date()
            # 当日已触发过则跳过
            if last_trigger_date == today:
                continue

            # 检查当日是否已有完成的增量同步任务（避免重启后重复触发）
            SessionLocal = _get_session_local()
            db = SessionLocal()
            try:
                today_start = _dt.datetime.combine(today, _dt.time.min)
                existing = db.execute(
                    select(AsyncTaskRecord).where(
                        AsyncTaskRecord.task_type == "universe_incremental_sync",
                        AsyncTaskRecord.created_at >= today_start,
                    ).order_by(desc(AsyncTaskRecord.created_at)).limit(1)
                ).scalars().first()
                if existing is not None:
                    last_trigger_date = today
                    logger.info(
                        "Universe incremental sync already triggered today (%s, status=%s), skip",
                        existing.id, existing.status,
                    )
                    continue
            finally:
                db.close()

            # 触发增量同步
            logger.info("Triggering daily universe incremental sync at %s", now.isoformat())
            try:
                universe_sync_task.start_universe_incremental_sync(max_workers=5)
                last_trigger_date = today
            except Exception:
                logger.exception("Failed to start universe incremental sync")
        except Exception:
            logger.exception("Periodic universe incremental sync check failed")


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
