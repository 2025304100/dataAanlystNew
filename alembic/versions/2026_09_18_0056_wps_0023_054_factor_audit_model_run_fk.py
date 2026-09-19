"""Backfill the missing foreign key on `factor_audit_logs.model_run_id`.

Revision ID: wps_0023_054_factor_audit_model_run_fk
Revises: wps_0023_053_factor_audit_pool_id

为什么需要（TD1，2026-09-18 开工）
====================================
`factor_audit_logs` 建表迁移 0050（2026-09-02）**正确声明了**该外键
（`sa.ForeignKey("factor_model_runs.id", ondelete="SET NULL")`），但：

1. 本机 MySQL server 默认引擎是 **MyISAM**（实测 `@@default_storage_engine=MyISAM`，
   server 5.7.26），0050 的 create_table 没有声明 `mysql_engine`；
2. **MyISAM 不支持外键且静默忽略 FK 定义**（不报错、不警告）；
3. 随后 `init_db()` 的 `_auto_align_all_schema` 把引擎 CONVERT 成 InnoDB；
4. **转换引擎不会补建外键** —— FK 在步骤 2 就永久丢失（真实库实测 FK=0）。

与 T07 修复 0053 丢 FK（0054 修订）完全同构的病因链。
同指向 `factor_model_runs.id` 的另外两张表（`factor_runtime_state.active_model_run_id`、
`factor_model_audit_logs.model_run_id`）实测 **FK 存在**（由 auto-align 建 InnoDB 表时
从 ORM metadata 带出），**只有 `factor_audit_logs` 缺** —— 2026-09-18 开工探针确认。

双路径修复（「迁移是对的」必须两条路径都成立）
================================================
- **既有库**：本修订回填 FK（幂等：先按本地列名探测，存在即跳过）。
- **全新安装**：server 默认引擎至今仍是 MyISAM，0050 原样重跑还会吞 FK →
  0050 已**原地补** `mysql_engine="InnoDB"`（T01 修 0053 的同款先例；
  create_table 只在全新安装执行，对已应用库零影响）。

⚠️ 本修订在全新 MySQL 安装上是 no-op（0050 修复版已带 FK，`_fk_by_column` 会跳过）。
   它的作用域是「曾经用 MyISAM 版 0050 建过表的库」。

行为验证（information_schema 有定义 ≠ 被强制）
================================================
配套脚本 `.workbuddy/mining/_verify_audit_model_run_fk.py`：
插入孤儿行应被拒（MySQL **1452**）、删除父行应把子行 `model_run_id` 置 **NULL**（SET NULL）。

不在本修订范围内（已记录，勿顺手改）
====================================
- 表字符集统一（真实库 utf8_general_ci vs MYSQL_KW 声明 utf8mb4）：独立议题（TD5 汇总）。
- 其它 47 张漂移表（全量 drift 基线 48/127，含大量同类 MyISAM 吞 FK）：
  已立项 TD4（账务列）/TD5（全库核对），本卡不动。
"""
from alembic import op
import sqlalchemy as sa


revision = "wps_0023_054_factor_audit_model_run_fk"
down_revision = "wps_0023_053_factor_audit_pool_id"
branch_labels = None
depends_on = None

TABLE = "factor_audit_logs"
LOCAL_COL = "model_run_id"
REF_TABLE = "factor_model_runs"
REF_COL = "id"


def _fk_by_column(insp, table: str) -> dict[str, str]:
    """返回 `{本地列名: 约束名}`（按列名匹配，不按约束名 —— MySQL 约束名是自动生成的）。"""
    found: dict[str, str] = {}
    try:
        for fk in insp.get_foreign_keys(table):
            cols = fk.get("constrained_columns") or []
            name = fk.get("name")
            if len(cols) == 1 and name:
                found[cols[0]] = name
    except Exception:
        pass
    return found


def _used_fk_names(insp, table: str) -> set[str]:
    try:
        return {fk.get("name") for fk in insp.get_foreign_keys(table) if fk.get("name")}
    except Exception:
        return set()


def _next_ibfk_name(insp, table: str) -> str:
    """生成与 MySQL 自动命名同风格的约束名，并避开已占用的编号。"""
    used = _used_fk_names(insp, table)
    index = 1
    while f"{table}_ibfk_{index}" in used:
        index += 1
    return f"{table}_ibfk_{index}"


def _orphan_count(bind) -> int:
    """前置体检：有多少行的 model_run_id 在父表里找不到（MySQL 会以 1452 中断迁移）。"""
    sql = sa.text(
        f"SELECT COUNT(*) FROM {TABLE} c "
        f"LEFT JOIN {REF_TABLE} p ON c.{LOCAL_COL} = p.{REF_COL} "
        f"WHERE c.{LOCAL_COL} IS NOT NULL AND p.{REF_COL} IS NULL"
    )
    try:
        return int(bind.execute(sql).scalar() or 0)
    except Exception:
        return 0


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    insp = sa.inspect(bind)

    if dialect == "sqlite":
        # SQLite 不支持 `ALTER TABLE ... ADD CONSTRAINT`，也不需要：
        # 全新安装时 0050 的 create_table 已经把 FK 建好（文件头说明），本修订无工作可做。
        print("[0056] dialect=sqlite：不支持 ALTER ADD CONSTRAINT，且全新安装已由 0050 建好外键 → 跳过（预期行为）。")
        return

    if not insp.has_table(TABLE) or not insp.has_table(REF_TABLE):
        print(f"[0056] 跳过：{TABLE} 或 {REF_TABLE} 不存在（该库尚未执行 0050？）")
        return

    if LOCAL_COL in _fk_by_column(insp, TABLE):
        print(f"[0056] 已存在，跳过：{TABLE}.{LOCAL_COL} -> {REF_TABLE}.{REF_COL}（全新安装的正常结果）")
        return

    orphans = _orphan_count(bind)
    if orphans:
        raise RuntimeError(
            f"[0056] 中止：{TABLE}.{LOCAL_COL} 有 {orphans} 行在 {REF_TABLE}.{REF_COL} "
            f"中找不到父行，无法建立外键。请先清理孤儿数据（2026-09-18 开工探针实测为 0）。"
        )

    name = _next_ibfk_name(insp, TABLE)
    op.create_foreign_key(
        name, TABLE, REF_TABLE, [LOCAL_COL], [REF_COL], ondelete="SET NULL"
    )
    print(f"[0056] 已补建外键：{TABLE}.{LOCAL_COL} -> {REF_TABLE}.{REF_COL}  ondelete=SET NULL  ({name})")


def downgrade() -> None:
    """只回滚本修订补建的外键（按列定位，避免误删其它约束）。"""
    bind = op.get_bind()
    dialect = bind.dialect.name
    insp = sa.inspect(bind)

    if dialect == "sqlite":
        print("[0056] dialect=sqlite：无 ALTER 支持，且从未补建 → 跳过。")
        return

    name = _fk_by_column(insp, TABLE).get(LOCAL_COL)
    if not name:
        print("[0056] downgrade：外键不存在，无需回滚。")
        return
    try:
        op.drop_constraint(name, TABLE, type_="foreignkey")
        print(f"[0056] 已删除外键：{TABLE}.{LOCAL_COL} ({name})")
    except Exception as exc:  # pragma: no cover - 方言差异兜底
        print(f"[0056] 删除 {TABLE}.{name} 失败（忽略）：{exc}")
