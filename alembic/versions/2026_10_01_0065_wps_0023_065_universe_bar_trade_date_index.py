"""universe_daily_bars 补 `trade_date` 前导索引（交易日解析性能）。

Revision ID: wps_0023_065_universe_bar_trade_date_index
Revises: wps_0023_064_app_settings

为什么需要
==========
「上一个已收盘的交易日」判定（`app/services/factors/trade_calendar.py`）要按
`trade_date >= cutoff` 过滤后再 `GROUP BY trade_date`。而 `universe_daily_bars`
（实测 1074 万行 / 数据 830MB / 索引 1066MB）现有的 4 个索引**全部以
`universe_symbol_id` 开头**：

  PRIMARY(id) / uq_universe_bar_symbol_date(universe_symbol_id, trade_date)
  / ix_universe_bar_symbol_date(universe_symbol_id, trade_date)
  / ix_universe_daily_bars_universe_symbol_id(universe_symbol_id)

没有一个能服务「按日期区间扫描」，MySQL 只能全表扫。

实测代价：单次 `previous_complete_trade_date()` 稳定 8.0~8.1s（连跑三次复现），
而它已经落在组合治理对账接口的每次调用路径上 —— 用户每打开一次「对账守恒」
面板就要等 8 秒。

本迁移只加一个 `(trade_date, universe_symbol_id)` 复合索引，覆盖
WHERE + GROUP BY + COUNT(DISTINCT universe_symbol_id)；不改数据、不改列。

顺带发现（**本次不处理**，留待独立评估）
========================================
`ix_universe_bar_symbol_date` 与唯一约束 `uq_universe_bar_symbol_date` 的列完全
相同（universe_symbol_id, trade_date），属重复索引，白占约 500MB 索引空间。
删除它要确认没有运维脚本按该索引名引用，属独立决策，不在本迁移范围内。

实现要点
========
- inspector 判存在，幂等可重跑（本仓迁移一律要求可重复执行）；
- SQLite（测试库）同样支持 CREATE INDEX，无需分方言。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "wps_0023_065_universe_bar_trade_date_index"
down_revision = "wps_0023_064_app_settings"
branch_labels = None
depends_on = None

_TABLE = "universe_daily_bars"
_INDEX = "ix_universe_bar_trade_date"


def _index_exists(conn) -> bool:
    insp = sa.inspect(conn)
    if _TABLE not in insp.get_table_names():
        return False
    return any(ix.get("name") == _INDEX for ix in insp.get_indexes(_TABLE))


def upgrade() -> None:
    conn = op.get_bind()
    if _index_exists(conn):
        return
    op.create_index(_INDEX, _TABLE, ["trade_date", "universe_symbol_id"], unique=False)


def downgrade() -> None:
    conn = op.get_bind()
    if not _index_exists(conn):
        return
    op.drop_index(_INDEX, table_name=_TABLE)
