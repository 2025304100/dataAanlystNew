"""
数据库会话管理

所有 engine / SessionLocal 的创建委托给 DatabaseManager 单例，
支持运行时热切换数据库（SQLite ↔ MySQL）。

对外仍导出：
- get_db()         : FastAPI 依赖注入用的 session 生成器
- get_engine()     : 获取当前 engine（供 init_db 等模块使用）
- get_session_local() : 获取 session factory（供 main.py / discovery_tasks 使用）
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy.orm import Session

from app.db.manager import DatabaseManager


def get_engine():
    """获取当前活动的 SQLAlchemy Engine。"""
    return DatabaseManager.get().engine


def get_session_local():
    """获取当前活动的 sessionmaker 工厂。"""
    return DatabaseManager.get().session_factory


# ── 向后兼容的属性代理 ────────────────────────────────────
# 部分模块直接 import engine / SessionLocal，
# 这里用延迟求值确保拿到的是当前活动的实例。

class _EngineProxy:
    """代理对象：访问任何属性时转发到当前 engine。"""
    def __getattr__(self, name):
        return getattr(DatabaseManager.get().engine, name)


class _SessionLocalProxy:
    """代理对象：调用时创建 session，使用当前 session factory。"""
    def __call__(self, **kwargs):
        return DatabaseManager.get().session_factory(**kwargs)

    def __getattr__(self, name):
        return getattr(DatabaseManager.get().session_factory, name)


engine = _EngineProxy()
SessionLocal = _SessionLocalProxy()


def get_db() -> Generator[Session, None, None]:
    """FastAPI Depends() 注入用的 session 生成器。"""
    db = DatabaseManager.get().get_session()
    try:
        yield db
    finally:
        db.close()
