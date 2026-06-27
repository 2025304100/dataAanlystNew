"""方言感知的 SQL 工具函数。

根据当前数据库类型（SQLite / MySQL）生成正确的 SQL 表达式，
避免在业务代码中硬编码方言特定的函数。
"""
from __future__ import annotations

from sqlalchemy import func

from app.db.manager import DatabaseManager


def days_since(date_column):
    """返回 date_column 距今的天数表达式。

    SQLite: julianday('now') - julianday(column)
    MySQL:  DATEDIFF(CURDATE(), column)
    """
    mgr = DatabaseManager.get()
    if mgr.is_mysql:
        return func.datediff(func.curdate(), date_column)
    return func.julianday(func.current_timestamp()) - func.julianday(date_column)
