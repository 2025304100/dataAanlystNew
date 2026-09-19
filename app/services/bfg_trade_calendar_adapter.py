"""交易日历 Adapter（fail-closed 严格）。

实现优先级：
1. 查询现有 trade_calendar 表（若存在，is_trading_day=1 且 date BETWEEN start AND end）。
   表不存在或 query 抛 Exception -> 走 2。
2. 查询现有 calendar_utils 模块（若存在同名 get_trading_days 函数则调用）。
   不存在或抛 Exception -> 抛 TradeCalendarUnavailableError。
3. 绝不使用自然日 5/7 粗估（严禁 usable = (end-start).days * 5 // 7）。
"""
from __future__ import annotations

import importlib
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class TradeCalendarUnavailableError(Exception):
    """交易日历服务不可用（fail-closed）：禁止用 5/7 自然日粗估兜底。"""


def _query_trade_calendar_table(session: "Session", start: date, end: date) -> list[date] | None:
    """Priority 1: 查询 trade_calendar 表（通过 raw SQL 动态探测，不依赖 ORM 模型）。

    返回 list[date] 表示成功；返回 None 表示表不存在或查询失败，继续下一 fallback。
    """
    try:
        # 探测表是否存在（MySQL 方言）；兼容 SQLite 用另一套语法会在捕获异常时返回 None。
        # 先尝试直接 SELECT —— 如果表不存在 SQLAlchemy 会抛 ProgrammingError/SQLAlchemyError。
        stmt = text(
            "SELECT date FROM trade_calendar "
            "WHERE is_trading_day = 1 AND date >= :s AND date <= :e "
            "ORDER BY date ASC"
        )
        params = {"s": start.isoformat(), "e": end.isoformat()}
        rows = session.execute(stmt, params).fetchall()
        # 兼容返回格式：date 列可能是 date 或 datetime
        result: list[date] = []
        for row in rows:
            val = row[0]
            if isinstance(val, date):
                result.append(val)
            elif hasattr(val, "date"):
                result.append(val.date())
            else:
                # 未知类型 -> 放弃该 fallback
                return None
        return result
    except SQLAlchemyError:
        return None
    except Exception:
        return None


def _query_calendar_utils_module(start: date, end: date) -> list[date] | None:
    """Priority 2: 动态 import calendar_utils，调用同名 get_trading_days。"""
    try:
        mod = importlib.import_module("calendar_utils")
    except Exception:
        return None

    fn = getattr(mod, "get_trading_days", None)
    if not callable(fn):
        return None
    try:
        result = fn(start, end)
    except Exception:
        return None

    if not isinstance(result, list):
        return None
    # 确保元素都是 date
    cleaned: list[date] = []
    for item in result:
        if isinstance(item, date):
            cleaned.append(item)
        elif hasattr(item, "date"):
            cleaned.append(item.date())
        else:
            return None
    return cleaned


def get_trading_days(
    start_date: date,
    end_date: date,
    session: "Session | None" = None,
) -> list[date]:
    """严格交易日历查询入口（fail-closed）。

    参数 session 可选：若缺省则直接跳 priority 1（因为没有 DB 连接可用）。

    Raises:
        TradeCalendarUnavailableError: 当且仅当两个数据源都不可用时抛出，
            不做自然日 5/7 粗估，调用方必须按 fail-closed 处理。
    """
    if start_date > end_date:
        return []

    # Priority 1: trade_calendar 表（需要 session）
    if session is not None:
        try:
            result = _query_trade_calendar_table(session, start_date, end_date)
            if result is not None:
                return result
        except Exception:
            # 任何异常都继续下一 fallback，不做兜底
            pass

    # Priority 2: calendar_utils.get_trading_days
    try:
        result = _query_calendar_utils_module(start_date, end_date)
        if result is not None:
            return result
    except Exception:
        pass

    # Priority 3: fail-closed —— 严禁 5/7 粗估
    raise TradeCalendarUnavailableError(
        "交易日历不可用：trade_calendar 表缺失且 calendar_utils.get_trading_days 未实现。"
        "禁止使用自然日 5/7 粗估作为 fallback。"
    )
