"""`portfolio_candidates.symbol_id` 外键行为修正：CASCADE -> RESTRICT。

Revision ID: wps_0023_069_pc_symbol_fk_restrict
Revises: wps_0023_068_missing_check_constraints

背景（TD5 报告 §A2）
====================
`portfolio_candidates` 的两个外键在库里都是 **ON DELETE CASCADE**，而 ORM 声明的是
`RESTRICT` —— 是 §A2 认定的「唯一行为级差异（数据丢失风险）」：

    CONSTRAINT `portfolio_candidates_ibfk_2` FOREIGN KEY (`symbol_id`)
      REFERENCES `symbols` (`id`) ON DELETE CASCADE

`symbol_id` 用 CASCADE 的实际风险：**删掉任意一个 symbol（共享的基础数据），
会静默连带删掉所有引用它的组合候选记录**。`symbols` 是全市场标的表，
不是某个组合的从属数据，这种级联没有业务依据。

本迁移只改 `symbol_id` 这一个（明确该改、且零风险 —— 全仓 grep 确认
**没有任何删除 `symbols` 的代码路径**）。

**为什么本次不动 `portfolio_id`**：那一个存在真实的设计冲突 ——
`app/api/routes/portfolios.py::delete_portfolio` 的 docstring 明确写着
「级联清理（依赖 DB 外键 ondelete=CASCADE）」，即**接口设计本就依赖级联删除**；
而 ORM 声明是 RESTRICT。两边都是"作者意图"，需要产品口径拍板，
不该由迁移单方面决定。已记入 TD5 报告 §9.6 待裁决。

实现要点
========
- MySQL：`DROP FOREIGN KEY` + 重建（带 ON DELETE RESTRICT）。MySQL 认为 RESTRICT
  等价于默认行为、DDL 里会省略该子句 —— 这是预期内的（见 `wps_0023_067` 的说明）。
- **SQLite 跳过**：不支持 ALTER TABLE DROP/ADD CONSTRAINT（沿用既有先例）。
  测试库由 SQLAlchemy 按 ORM 声明建表，本来就是 RESTRICT，无需处理。
- 幂等：先读 `inspector.get_foreign_keys` 的 ondelete，已是 RESTRICT 就跳过。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "wps_0023_069_pc_symbol_fk_restrict"
down_revision = "wps_0023_068_missing_check_constraints"
branch_labels = None
depends_on = None

_TABLE = "portfolio_candidates"
_COLUMN = "symbol_id"
_REF_TABLE = "symbols"
_REF_COLUMN = "id"


def _current_ondelete(conn) -> tuple[str | None, str | None]:
    """返回 (约束名, ondelete)。找不到 FK 返回 (None, None)。"""
    try:
        for fk in sa.inspect(conn).get_foreign_keys(_TABLE):
            if (fk.get("constrained_columns") or []) == [_COLUMN]:
                opts = fk.get("options") or {}
                return fk.get("name"), str(opts.get("ondelete") or "").upper() or None
    except Exception:
        pass
    return None, None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "mysql":
        print(
            f"[0069] dialect={conn.dialect.name}：不支持 ALTER TABLE DROP/ADD "
            "CONSTRAINT，跳过（预期行为）"
        )
        return

    name, ondelete = _current_ondelete(conn)
    if name is None:
        print(f"[0069] {_TABLE}.{_COLUMN} 没有外键，改为直接新建（RESTRICT）")
        op.create_foreign_key(
            f"fk_{_TABLE}_{_COLUMN}", _TABLE, _REF_TABLE, [_COLUMN], [_REF_COLUMN],
            ondelete="RESTRICT",
        )
        return

    if ondelete == "RESTRICT":
        print(f"[0069] 已是 RESTRICT，跳过（{name}）")
        return

    op.drop_constraint(name, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        f"fk_{_TABLE}_{_COLUMN}", _TABLE, _REF_TABLE, [_COLUMN], [_REF_COLUMN],
        ondelete="RESTRICT",
    )
    print(f"[0069] {_TABLE}.{_COLUMN}: {ondelete} -> RESTRICT（原约束 {name}）")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "mysql":
        return
    name, ondelete = _current_ondelete(conn)
    if name is None or ondelete == "CASCADE":
        return
    op.drop_constraint(name, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        f"fk_{_TABLE}_{_COLUMN}", _TABLE, _REF_TABLE, [_COLUMN], [_REF_COLUMN],
        ondelete="CASCADE",
    )
