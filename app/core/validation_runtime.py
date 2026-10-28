"""Temporary acceptance request policy and a fail-closed ORM write boundary."""
from contextvars import ContextVar
import re

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.orm import Session

read_only_request = ContextVar("validation_read_only_request", default=False)

_READ_PATHS = {
    "/market-signals/settings", "/market-signals/rules", "/market-signals/metadata",
    "/market-signals/evidence", "/market-signals/assessments", "/market-signals/predictions",
    "/market-signals/dashboard", "/market-events", "/market-events/scopes",
    "/market-events/sector-concepts", "/market-events/collect-runs", "/market-events/hotspots",
    "/portfolios", "/dashboard/workbench", "/dashboard/symbol-detail", "/symbols",
    "/signal-rules/presets", "/watchlists", "/system/capabilities", "/news/latest",
    "/notifications/inbox", "/notifications/channels", "/notifications/capabilities",
    "/discovery/tasks", "/market-data/sync-tasks", "/inspection-acceptance/status",
}
_READ_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"/market-signals/rules/[0-9]+/versions", r"/market-signals/predictions/[0-9]+/outcomes",
    r"/market-events/[0-9]+", r"/portfolios/[0-9]+/signal-rule", r"/watchlists/[0-9]+/items",
    r"/discovery/scopes/[a-z0-9-]+/stats",
))


def read_allowed(path, api_prefix):
    if not path.startswith(api_prefix+"/"):
        return True  # Health and static production/Vite assets never open DB sessions.
    relative = path[len(api_prefix):]
    return relative in _READ_PATHS or any(pattern.fullmatch(relative) for pattern in _READ_PATTERNS)


def _reject_write():
    raise HTTPException(409, "验收模式的查询请求禁止修改业务数据")


def _before_flush(session, _context, _instances):
    if read_only_request.get() and (session.new or session.dirty or session.deleted):
        _reject_write()


def _before_execute(state):
    if not read_only_request.get():
        return
    if state.is_insert or state.is_update or state.is_delete:
        _reject_write()
    if not state.is_select:
        statement = str(state.statement).lstrip().upper()
        if not re.match(r"^(SELECT|SHOW|EXPLAIN|DESCRIBE)\b", statement):
            _reject_write()


def install_write_guard():
    for name, handler in (("before_flush", _before_flush), ("do_orm_execute", _before_execute)):
        if not event.contains(Session, name, handler):
            event.listen(Session, name, handler)
