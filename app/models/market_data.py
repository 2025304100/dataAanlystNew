"""市场基础数据 ORM 映射（`trade_calendar`）。

为什么有这么一个只装一张表的模块
================================
`trade_calendar` 表此前**只有裸 SQL 访问**（`app/services/bfg_trade_calendar_adapter.py`
里用 `text("SELECT date FROM trade_calendar ...")`），从来没有 ORM 映射。

而 `app/services/portfolio_resume_service.py` 从写下那天起就写着：

    from app.models.market_data import MarketCalendar
    stmt = select(MarketCalendar.trade_date).where(..., MarketCalendar.is_trading_day == 1)

本模块当时并不存在 → import 抛 `ModuleNotFoundError` → 被外层
`except Exception: pass` 静默吞掉 → **那两个函数从未真正查过数据库**，
一直走「周一~周五」的弱近似。后果是国庆这类长假会被当成交易日排进
「待补算交易日」，resume 时对着一堆没有数据的假期执行决策。

本模块把缺位的映射补上，让那条早已写好、却从未跑通的查询路径真正生效。
（模块名沿用原 import 的 `market_data`，不制造新的路径约定。）

维护
====
表内容由 `scripts/sync_trade_calendar.py` 依据交易所休市安排（经新浪财经收录）维护。
2026-10-01 校正过一次：修正 75 行「应休市却标开市」，
各年交易日数从 260/262/261/261 收敛到真实的 242/242/243/242。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MarketCalendar(Base):
    """`trade_calendar`：逐日的开市/休市标记。

    刻意不声明 `__table_args__`（索引）：该表在库中已存在且带索引，
    模型只负责列映射，避免 `create_all` 在既有库里重建同名索引。
    """

    __tablename__ = "trade_calendar"

    # 真实列名是 `date`，但既有调用方（portfolio_resume_service）按 `trade_date` 引用；
    # 用 mapped_column("date", ...) 把属性名映射到真实列名，避免改动调用方契约。
    trade_date: Mapped[date] = mapped_column("date", Date, primary_key=True)
    is_trading_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


__all__ = ["MarketCalendar"]
