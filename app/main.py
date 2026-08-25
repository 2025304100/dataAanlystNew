from contextlib import asynccontextmanager
import asyncio
import logging
import os
import socket
import time
import traceback
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware
from uuid import uuid4

from app.api.router import api_router
from app.core.config import settings, load_db_config, build_mysql_url
from app.db.init_db import init_db
from app.db.manager import DatabaseManager
from app.middleware.deprecation_log import (
    DEPRECATED_ENDPOINTS,
    _get_client_ip,
    _get_user_agent,
    _match_deprecated,
    apply_deprecation_headers,
    ensure_deprecation_log_table,
    record_deprecation_access,
)
from app.schemas.errors import (
    NextAction,
    TechnicalDetails,
    UnifiedErrorException,
    build_user_error,
)
from app.schemas.error_sanitizer import sanitize_message
from app.schemas.external_data import (
    CircuitBreakerOpenError,
    DataValidationError,
    StaleDataError,
)
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


def initialize_runtime_database() -> None:
    """Initialize the process database, honoring an explicit environment URL.

    ``config/db_config.json`` is an operator-managed default.  A process-level
    ``DATABASE_URL`` is intentionally higher priority so isolated test, preview,
    and container processes cannot silently connect to the shared MySQL instance.
    """
    explicit_url = os.environ.get("DATABASE_URL")
    mgr = DatabaseManager.get()

    if explicit_url:
        db_type = "mysql" if explicit_url.lower().startswith("mysql") else "sqlite"
        logger.info("Initializing database from explicit DATABASE_URL (%s)", db_type)
        mgr.initialize(explicit_url, db_type=db_type)
        return

    cfg = load_db_config()
    if cfg.get("use_mysql") and cfg.get("mysql", {}).get("host"):
        url = build_mysql_url(cfg)
        logger.info(
            "Initializing MySQL engine: %s:%s/%s",
            cfg["mysql"]["host"],
            cfg["mysql"]["port"],
            cfg["mysql"]["database"],
        )
        mgr.initialize(url, db_type="mysql")
    else:
        logger.info("Initializing SQLite engine: %s", settings.database_url)
        mgr.initialize(settings.database_url, db_type="sqlite")


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_runtime_database()
    mgr = DatabaseManager.get()

    init_db()

    # WP9.5：检测旧默认值迁移状态，记录日志便于运维定位
    _log_member_source_migration_status()

    # WP9.6：确保 api_deprecation_logs 表存在（防御性，init_db 已包含 create_all）
    try:
        ensure_deprecation_log_table(mgr.engine)
    except Exception:
        logger.exception("WP9.6: ensure_deprecation_log_table failed (non-fatal)")

    # 启动时清理：移除过期且未冻结的挖掘结果
    _run_startup_cleanup()

    # 启动定期清理后台任务
    cleanup_task = asyncio.create_task(_periodic_cleanup())

    # 跨平台持久化调度器：Linux / Windows 均由设置页统一管理。
    scheduled_task_loop = asyncio.create_task(scheduler_loop())

    # Unified notification Outbox dispatcher. Business services only enqueue;
    # this loop completes delivery for in-app and configured external channels.
    notification_dispatcher_task = asyncio.create_task(
        _notification_dispatcher_loop()
    )

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

    notification_dispatcher_task.cancel()
    try:
        await notification_dispatcher_task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("notification_dispatcher_task shutdown failed")

    # 风控加固：关闭探测线程池，避免 uvicorn reload 时线程泄漏
    try:
        from app.api.routes.akshare_apis import _PROBE_EXECUTOR
        _PROBE_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        logger.info("Probe executor shutdown complete")
    except Exception:
        logger.exception("Probe executor shutdown failed")

    # 关闭数据库连接池
    mgr.dispose()


def _log_member_source_migration_status() -> None:
    """WP9.5：检测成员来源开关迁移状态并记录日志。

    WP9.5 将 AUTO_TRADE_MEMBER_SOURCE_ENABLED / PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED
    默认值由 False 改为 True。首次启动时检测：
    - 若开关为 True（新默认值）→ 记录 INFO 日志，确认已切换到成员来源
    - 若开关为 False（旧默认值，通过环境变量显式回退）→ 记录 WARNING 日志，
      提示运维该实例仍在使用旧的"持仓+最新扫描"来源

    本函数仅记录日志，不修改任何状态，不影响启动流程。
    """
    import os as _os

    auto_trade_flag = settings.AUTO_TRADE_MEMBER_SOURCE_ENABLED
    backtest_flag = settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED

    # 检测是否通过环境变量显式设置（用于判断是"新默认值"还是"显式回退"）
    auto_trade_env_set = _os.getenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED") is not None
    backtest_env_set = _os.getenv("PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED") is not None

    if auto_trade_flag:
        logger.info(
            "WP9.5: AUTO_TRADE_MEMBER_SOURCE_ENABLED=True（成员来源为默认）"
            "env_explicitly_set=%s",
            auto_trade_env_set,
        )
    else:
        logger.warning(
            "WP9.5: AUTO_TRADE_MEMBER_SOURCE_ENABLED=False（已回退到旧的持仓+最新扫描来源）。"
            "如无需回退，请移除环境变量 AUTO_TRADE_MEMBER_SOURCE_ENABLED=false 以使用新默认值。"
            "env_explicitly_set=%s",
            auto_trade_env_set,
        )

    if backtest_flag:
        logger.info(
            "WP9.5: PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=True（成员来源为默认）"
            "env_explicitly_set=%s",
            backtest_env_set,
        )
    else:
        logger.warning(
            "WP9.5: PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=False（已回退到旧的持仓+最新扫描来源）。"
            "如无需回退，请移除环境变量 PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=false 以使用新默认值。"
            "env_explicitly_set=%s",
            backtest_env_set,
        )


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
            # WPD-03: 恢复僵尸 factor_pipeline 任务，释放残留 warehouse 锁
            try:
                from app.services.factors.pipeline_task import (
                    recover_stale_pipeline_tasks,
                )
                recovered = recover_stale_pipeline_tasks(db)
                if recovered:
                    logger.warning(
                        "Startup cleanup: recovered %d stale factor_pipeline "
                        "tasks: %s",
                        len(recovered),
                        [r["task_id"] for r in recovered],
                    )
            except Exception:
                logger.exception(
                    "Startup cleanup: stale pipeline recovery failed (non-fatal)"
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


async def _notification_dispatcher_loop() -> None:
    """Continuously drain the persisted notification Outbox without blocking FastAPI."""
    from app.services.notifications.dispatcher import get_dispatcher

    dispatcher = get_dispatcher()
    while True:
        try:
            processed = await asyncio.to_thread(dispatcher.run_once)
            if processed:
                logger.info("Notification dispatcher processed %d item(s)", processed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Notification dispatcher cycle failed")
        await asyncio.sleep(dispatcher.poll_interval)


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


# ── WP9.6 API 废弃期访问日志中间件 ──────────────────────
#
# 对所有进入的请求匹配 DEPRECATED_ENDPOINTS 注册表：
# - 命中则添加 Deprecation/Sunset/Link 头，并异步记录访问日志
# - 不命中或日志写入失败均不阻断请求
# - 旧 API 在 Sunset 日期前仍可访问


class DeprecationLogMiddleware(BaseHTTPMiddleware):
    """WP9.6：对废弃端点添加 HTTP 头并记录访问日志。"""

    async def dispatch(self, request: Request, call_next):
        # 预先匹配，避免无关请求进入响应处理逻辑
        path = request.url.path
        method = request.method
        entry = _match_deprecated(path, method)

        response = await call_next(request)

        if entry is not None:
            # 1. 添加废弃相关 HTTP 头
            apply_deprecation_headers(response, entry)
            # 2. best-effort 记录访问日志（失败不阻断请求）
            try:
                client_ip = _get_client_ip(request)
                user_agent = _get_user_agent(request)
                SessionLocal = _get_session_local()
                db = SessionLocal()
                try:
                    record_deprecation_access(
                        db,
                        endpoint=path,
                        method=method,
                        client_ip=client_ip,
                        user_agent=user_agent,
                        successor_endpoint=entry.get("successor"),
                        sunset_date=entry.get("sunset"),
                        status_code=response.status_code,
                        context_json=entry.get("reason"),
                    )
                finally:
                    db.close()
            except Exception:
                logger.exception(
                    "WP9.6: deprecation access log failed path=%s", path,
                )

        return response


app.add_middleware(DeprecationLogMiddleware)  # WP9.6 deprecation middleware


# ── WP-S.6 全局异常处理器（统一用户错误协议） ──────────
#
# 所有异常统一转换为 UserError 响应：
# - 普通用户看到 user_message / impact / next_actions
# - technical_details 折叠（前端默认隐藏）
# - 敏感信息（密码/API Key/Webhook/SMTP）经 sanitize_message 脱敏后才能进入日志/响应


@app.exception_handler(UnifiedErrorException)
async def unified_error_handler(request: Request, exc: UnifiedErrorException):
    """处理业务方主动 raise 的 UnifiedErrorException。"""
    user_error = build_user_error(
        exc.error_code,
        correlation_id=exc.correlation_id,
        completed=exc.completed,
        technical_details=exc.technical_details,
        override_user_message=exc.override_user_message,
        override_impact=exc.override_impact,
        extra_next_actions=exc.extra_next_actions,
    )
    logger.warning(
        "UnifiedError [%s] correlation_id=%s path=%s status=%d",
        exc.error_code, exc.correlation_id, request.url.path, exc.status_code,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=user_error.model_dump(mode="json"),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """FastAPI 请求参数校验失败 → VALIDATION_ERROR。"""
    correlation_id = uuid4().hex
    errors_summary = "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
        for err in exc.errors()[:5]
    )
    # 记录到错误日志，方便排查：路径、方法、具体字段级错误
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "VALIDATION_ERROR correlation_id=%s path=%s method=%s errors=[%s]",
        correlation_id, request.url.path, request.method, errors_summary,
    )
    user_error = build_user_error(
        "VALIDATION_ERROR",
        correlation_id=correlation_id,
        technical_details=TechnicalDetails(
            exception_type="RequestValidationError",
            status_code=422,
            error_message=sanitize_message(errors_summary)[:500],
        ),
        extra_next_actions=[NextAction(
            label="修改后重试", action_type="retry",
            reason="请按提示修正输入参数",
        )],
    )
    return JSONResponse(status_code=422, content=user_error.model_dump(mode="json"))


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError):
    """SQLAlchemy 异常 → DB_CONNECTION_FAILED / DB_LOCK_TIMEOUT / DB_INTEGRITY_VIOLATION。"""
    correlation_id = uuid4().hex
    error_code = "DB_CONNECTION_FAILED"
    status_code = 503
    if isinstance(exc, OperationalError):
        msg = str(exc).lower()
        if "locked" in msg or "deadlock" in msg:
            error_code = "DB_LOCK_TIMEOUT"
        else:
            error_code = "DB_CONNECTION_FAILED"
    elif isinstance(exc, IntegrityError):
        error_code = "DB_INTEGRITY_VIOLATION"
        status_code = 409
    # 原始 DB 错误码（如 MySQL 1054 / SQLite UNIQUE constraint）
    orig = getattr(exc, "orig", None)
    db_error_code = None
    if orig is not None:
        # orig 可能是字符串或 DBAPI 异常对象
        orig_str = str(orig)
        if orig_str:
            db_error_code = orig_str[:100]
    user_error = build_user_error(
        error_code,
        correlation_id=correlation_id,
        technical_details=TechnicalDetails(
            exception_type=type(exc).__name__,
            status_code=status_code,
            error_message=sanitize_message(str(exc))[:500],
            db_error_code=db_error_code,
        ),
    )
    logger.exception(
        "DB error correlation_id=%s path=%s type=%s",
        correlation_id, request.url.path, type(exc).__name__,
    )
    return JSONResponse(status_code=status_code, content=user_error.model_dump(mode="json"))


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTPException 兼容包装为 UserError。"""
    correlation_id = uuid4().hex
    error_code = "UNKNOWN_ERROR"
    if exc.status_code == 404:
        error_code = "NOT_FOUND"
    elif exc.status_code == 401:
        error_code = "AUTH_MISSING"
    elif exc.status_code == 403:
        error_code = "FORBIDDEN"
    elif exc.status_code == 422:
        error_code = "VALIDATION_ERROR"
    elif exc.status_code == 409:
        # P0-AutoTrade：业务冲突/就绪检查未通过，统一 BUSINESS_BLOCKED
        error_code = "BUSINESS_BLOCKED"
    elif exc.status_code == 429:
        error_code = "RATE_LIMITED"
    elif exc.status_code == 503:
        # P1-05：503 统一映射为 CAPABILITY_BLOCKED，前端可据此展示中文 + next_actions
        error_code = "CAPABILITY_BLOCKED"

    detail = exc.detail
    extras: dict | None = None
    override_user_message: str | None = None
    override_impact: str | None = None
    extra_next_actions: list[dict] | None = None
    if isinstance(detail, dict):
        # G4：若业务方 raise HTTPException(status=4xx, detail={"error_code": "INVALID_FILTER",
        #   "user_message": "...", "impact": "...", "next_actions": [...]})
        #   则优先使用 detail 里的结构化错误码、文案、操作建议。
        inner_ec = detail.get("error_code")
        if isinstance(inner_ec, str) and inner_ec.strip():
            error_code = inner_ec.strip()
        extras = {}
        for k, v in detail.items():
            if k == "error_code":
                # 已优先映射为外层 error_code，不重复放 extras
                continue
            elif k == "user_message" and isinstance(v, str) and v.strip():
                override_user_message = v.strip()
            elif k == "message" and isinstance(v, str) and v.strip() and not override_user_message:
                override_user_message = v.strip()
            elif k == "impact" and isinstance(v, str) and v.strip():
                override_impact = v.strip()
            elif k == "next_actions" and isinstance(v, list):
                try:
                    extra_next_actions = [a for a in v if isinstance(a, dict)]
                except Exception:
                    extra_next_actions = None
            else:
                extras[k] = v
        if not extras:
            extras = None
    elif exc.status_code < 500 and isinstance(detail, str) and detail.strip():
        override_user_message = detail.strip()

    user_error = build_user_error(
        error_code,
        correlation_id=correlation_id,
        technical_details=TechnicalDetails(
            exception_type="HTTPException",
            status_code=exc.status_code,
            error_message=sanitize_message(str(exc.detail))[:500],
        ),
        override_user_message=override_user_message,
        override_impact=override_impact,
        extra_next_actions=extra_next_actions,
        extras=extras,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=user_error.model_dump(mode="json"),
    )


@app.exception_handler(CircuitBreakerOpenError)
async def circuit_breaker_error_handler(request: Request, exc: CircuitBreakerOpenError):
    """熔断器打开异常 → CIRCUIT_BREAKER_OPEN。"""
    return JSONResponse(
        status_code=503,
        content=exc.to_unified_error().model_dump(mode="json"),
    )


@app.exception_handler(DataValidationError)
async def data_validation_error_handler(request: Request, exc: DataValidationError):
    """数据校验异常 → DATA_VALIDATION_FAILED。"""
    return JSONResponse(
        status_code=422,
        content=exc.to_unified_error().model_dump(mode="json"),
    )


@app.exception_handler(StaleDataError)
async def stale_data_error_handler(request: Request, exc: StaleDataError):
    """数据过期异常 → STALE_DATA。"""
    return JSONResponse(
        status_code=503,
        content=exc.to_unified_error().model_dump(mode="json"),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底未捕获异常 → UNKNOWN_ERROR。

    所有未识别异常最终都返回 500 + UNKNOWN_ERROR，技术详情折叠堆栈（限 1000 字符）。
    """
    correlation_id = uuid4().hex
    tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    # 顶层 5 帧，限 1000 字符
    stack_summary = "".join(tb_lines[:5])[-1000:]
    user_error = build_user_error(
        "UNKNOWN_ERROR",
        correlation_id=correlation_id,
        technical_details=TechnicalDetails(
            exception_type=type(exc).__name__,
            status_code=500,
            error_message=sanitize_message(str(exc))[:500],
            stack_summary=stack_summary,
        ),
    )
    logger.exception(
        "Unhandled error correlation_id=%s path=%s type=%s msg=%s",
        correlation_id, request.url.path, type(exc).__name__,
        sanitize_message(str(exc))[:200],
    )
    return JSONResponse(status_code=500, content=user_error.model_dump(mode="json"))

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
