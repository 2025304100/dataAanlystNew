"""`trade_calendar` 建表迁移。

Revision ID: wps_0023_066_trade_calendar_table
Revises: wps_0023_065_universe_bar_trade_date_index

为什么需要
==========
这张表在真实库里一直存在，但**翻遍 alembic/versions 找不到任何创建它的迁移** ——
它是一次性手工建出来的。后果：

1. 测试库（`tmp_alembic_db` 走完整迁移链）里没有这张表，任何依赖交易日历的
   单测都无法在真表上跑；
2. 新环境部署会缺表，`bfg_trade_calendar_adapter` 与
   `portfolio_resume_service._next_trade_date` 都会失效。

本迁移把结构补上。**只建表、不塞数据** —— 内容由
`scripts/sync_trade_calendar.py` 依据交易所休市安排维护（每年 11 月跑一次）。

表结构按线上实际形态固化：
- `date` DATE，主键（逐日一行，含休市日）
- `is_trading_day` TINYINT(1) NOT NULL DEFAULT 0
- 复合索引 `idx_td (is_trading_day, date)`：沿用线上既定索引名与列序
  （按开市日过滤时同时用上 date 排序，不需要再单独建 is_trading_day 单列索引）

实现要点
========
- inspector 判存在，幂等可重跑（真实库已有该表，不能被破坏）；
- 显式 `mysql_engine="InnoDB"`（本仓迁移约定）。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "wps_0023_066_trade_calendar_table"
down_revision = "wps_0023_065_universe_bar_trade_date_index"
branch_labels = None
depends_on = None

_TABLE = "trade_calendar"
_INDEX = "idx_td"


def _table_exists(conn) -> bool:
    return _TABLE in sa.inspect(conn).get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn):
        return
    op.create_table(
        _TABLE,
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "is_trading_day",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.PrimaryKeyConstraint("date"),
        sa.Index(_INDEX, "is_trading_day", "date"),
        mysql_engine="InnoDB",
    )


def downgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn):
        op.drop_table(_TABLE)
