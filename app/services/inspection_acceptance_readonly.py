"""Process-local acceptance boundary. Normal application processes are unaffected.

Only the dedicated acceptance launcher enables this; there is no HTTP switch,
client header or environment fallback that can turn it off in that process.
"""
import re
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from fastapi.responses import JSONResponse

_enabled = False
_installed = False
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def acceptance_execution_status():
    if not _enabled:
        return {"mode": "application", "read_only": False}
    return {"mode": "inspection_acceptance", "portfolio_id": 1,
        "read_only": True, "mutation_requests_allowed": False,
        "task_registration_allowed": False, "task_dispatch_allowed": False,
        "scheduled_task_types": [], "background_jobs_enabled": False,
        "automatic_trading_enabled": False, "trading_mutations_allowed": False,
        "final_reviews_allowed": False}


class AcceptanceReadOnlyError(RuntimeError):
    pass


def require_task_writes_allowed():
    if _enabled:
        raise AcceptanceReadOnlyError("验收服务为只读模式，不能登记任务或启动后台执行。")


def _read_sql(statement):
    sql = re.sub(r"\A(?:\s+|/\*.*?\*/|--[^\n]*(?:\n|$)|\#[^\n]*(?:\n|$))*", "", statement, flags=re.S)
    sql = sql.rstrip().removesuffix(";")
    if ";" in sql or re.search(r"\b(?:FOR\s+UPDATE|LOCK\s+IN\s+SHARE|INTO\s+(?:OUTFILE|DUMPFILE)|GET_LOCK\s*\()", sql, re.I):
        return False
    if re.match(r"^(?:SELECT|SHOW|DESCRIBE|DESC|EXPLAIN)\b", sql, re.I):
        return True
    # SQLite inspection only; assignment and write PRAGMAs are intentionally excluded.
    return bool(re.fullmatch(r"PRAGMA\s+(?:(?:main|temp)\.)?(?:table_info|table_xinfo|index_list|index_info|index_xinfo|foreign_key_list|database_list|compile_options)(?:\([^;=]*\))?", sql, re.I))


def _before_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
    if _enabled and not _read_sql(statement):
        raise AcceptanceReadOnlyError("验收服务已阻止数据库写入；请使用只读查询。")


def _before_flush(session, _context, _instances):
    if _enabled and (session.new or session.dirty or session.deleted):
        raise AcceptanceReadOnlyError("验收服务已阻止保存或修改记录。")


def enable_acceptance_read_only():
    global _enabled, _installed
    if not _installed:
        # Engine class covers the main pool, control-plane pool and new engines.
        event.listen(Engine, "before_cursor_execute", _before_sql)
        event.listen(Session, "before_flush", _before_flush)
        _installed = True
    _enabled = True


def rejected():
    return JSONResponse(status_code=423, content={
        "error_code": "LOCAL_INSPECTION_READ_ONLY", "read_only": True,
        "message": "当前为只读验收服务：可以查看结果，不能登记、修改或派发任务。",
        "request_registered": False, "task_dispatched": False,
    })


def install_acceptance_http_guard(app, status_provider=None):
    @app.middleware("http")
    async def guard(request, call_next):
        if request.method not in READ_METHODS:
            return rejected()
        if request.method == "GET" and request.url.path == "/api/v1/inspection-acceptance/status" and status_provider is not None:
            return JSONResponse(content=status_provider())
        try:
            return await call_next(request)
        except AcceptanceReadOnlyError:
            return rejected()
