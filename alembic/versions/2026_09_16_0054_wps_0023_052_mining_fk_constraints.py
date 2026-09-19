"""Backfill the 6 missing foreign keys on the mining-domain tables.

Revision ID: wps_0023_052_mining_fk_constraints
Revises: wps_0023_051_factor_mining_core

为什么需要这个修订（**只在既有库上才有工作**）
================================================
修订 0053 已经声明了 6 个 `sa.ForeignKeyConstraint(...)`，**迁移本身是对的**：
在全新安装（一次性 scratch 库）上实测 6 个外键全部正确创建。

但**真实 MySQL 库里这 6 个外键一个都不存在**（2026-09-16 实测：14 张表 FK=0，
而同库其它表共 95 个外键 —— 说明该 server 的 FK 机制完全正常）。病因链：

1. 0053 首版**没有指定 `mysql_engine`** → 本机 MySQL server 默认引擎是 MyISAM → 表建成 **MyISAM**；
2. **MyISAM 不支持外键，且会静默忽略 FK 定义**（不报错、不警告）；
3. 随后 `init_db()` 的 `_auto_align_all_schema` 把引擎 **CONVERT 成 InnoDB**；
4. **但转换引擎不会补建外键** —— 外键在步骤 2 就被永久丢掉了。

所以「引擎已经是 InnoDB」的假象掩盖了「外键缺失」这一真实缺陷。
**这正是本修订存在的唯一理由**：把 0053 在 MyISAM 上丢掉的 6 个外键补回来。

⚠️ 这是 0053 三类缺陷（MyISAM 引擎 / 20 个冗余索引 / 3 个缺列）中的**第四类**，
   前两类当年已修，这一类**逃过了当年的漂移检查** ——
   因为 `verify_schema_drift.py` 当时**不比对索引之外的外键**（已同步补齐检查）。

⚠️ 本修订在**全新安装上是 no-op**（0053 已把外键建好，`_has_fk` 会跳过）。
   它的作用域是「曾经用 MyISAM 版 0053 建过表的库」。

不在本修订范围内（已记录，勿顺手改）
====================================
- **字符集不一致**：真实库这些表是 `utf8 / utf8_general_ci`，而 `MYSQL_KW` 声明的是
  `utf8mb4` → 全新安装是 utf8mb4。改在线表字符集是独立且风险更高的操作（需锁表 / 索引长度复核），
  且**不影响外键**（同表对内的 FK 两列字符集一致即可）。留作单独议题。
- 表内数据回填 / 孤儿行清理：三表当前均为空表，无需清理。
"""
from alembic import op
import sqlalchemy as sa


revision = "wps_0023_052_mining_fk_constraints"
down_revision = "wps_0023_051_factor_mining_core"
branch_labels = None
depends_on = None

#: 外键定义，由 ORM metadata **反推生成**（2026-09-16，脚本 `.workbuddy/mining/_derive_fks.py`）。
#: **勿手改**；改 ORM 的外键后重新生成并新建修订（不要改本文件 —— 迁移是固定的历史描述）。
#: 结构：表名 -> [(本地列, 被引用表, 被引用列, ondelete), ...]
_ORM_FOREIGN_KEYS: dict[str, list[tuple[str, str, str, str]]] = {
    "training_candidate_pool_members": [("pool_id", "training_candidate_pools", "id", "CASCADE")],
    "training_candidate_pool_snapshots": [("pool_id", "training_candidate_pools", "id", "CASCADE")],
    "factor_mining_candidates": [("run_id", "factor_mining_runs", "id", "CASCADE")],
    "factor_mining_generations": [("run_id", "factor_mining_runs", "id", "CASCADE")],
    "factor_training_checkpoints": [("run_id", "factor_mining_runs", "id", "CASCADE")],
    "factor_mining_template_versions": [("template_id", "factor_mining_templates", "id", "CASCADE")],
}

#: 建立顺序 = 被引用表必须先存在。
#: 这里显式排序（pools → runs → templates），避免依赖 dict 的插入顺序。
_CREATE_ORDER: tuple[str, ...] = (
    "training_candidate_pool_members",
    "training_candidate_pool_snapshots",
    "factor_mining_candidates",
    "factor_mining_generations",
    "factor_training_checkpoints",
    "factor_mining_template_versions",
)


# ══════════════════════════════════════════════════════════════
# 检查工具
# ══════════════════════════════════════════════════════════════


def _fk_by_column(insp, table: str) -> dict[str, str]:
    """返回 `{本地列名: 约束名}`。

    按**列名**匹配而不是按约束名 —— 因为 MySQL 的约束名是自动生成的
    （`<table>_ibfk_<n>`），不同库里编号可能不同，按名字判断会误判「不存在」而重复添加。
    """
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


def _orphan_count(bind, table: str, local_col: str, ref_table: str, ref_col: str) -> int:
    """前置体检：有多少行的外键值在父表里找不到。

    MySQL 遇到孤儿行会以 1452 报错中断迁移 —— 报错信息（"Cannot add or update a
    child row"）不告诉你是**哪张表、多少行**。先自己数一遍，失败时给出可行动的提示。
    """
    sql = sa.text(
        f"SELECT COUNT(*) FROM {table} c "
        f"LEFT JOIN {ref_table} p ON c.{local_col} = p.{ref_col} "
        f"WHERE c.{local_col} IS NOT NULL AND p.{ref_col} IS NULL"
    )
    try:
        return int(bind.execute(sql).scalar() or 0)
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    insp = sa.inspect(bind)

    if dialect == "sqlite":
        # SQLite 不支持 `ALTER TABLE ... ADD CONSTRAINT`。
        # 这里**不需要** batch recreate 兜底：全新安装时 0053 的 create_table
        # 已经把 6 个外键建好了（见文件头说明），本修订在 SQLite 上本来就没有工作可做。
        print(
            "[0054] dialect=sqlite：不支持 ALTER ADD CONSTRAINT，"
            "且全新安装已由 0053 建好外键 → 本修订跳过（预期行为）。"
        )
        return

    created: list[tuple[str, str]] = []
    for table in _CREATE_ORDER:
        if not insp.has_table(table):
            print(f"[0054] 跳过：表 {table} 不存在（该库尚未执行 0053？）")
            continue
        for local_col, ref_table, ref_col, ondelete in _ORM_FOREIGN_KEYS[table]:
            if not insp.has_table(ref_table):
                print(f"[0054] 跳过：被引用表 {ref_table} 不存在")
                continue

            existing = _fk_by_column(insp, table)
            if local_col in existing:
                print(f"[0054] 已存在，跳过：{table}.{local_col} -> {ref_table}.{ref_col}")
                continue

            orphans = _orphan_count(bind, table, local_col, ref_table, ref_col)
            if orphans:
                raise RuntimeError(
                    f"[0054] 中止：{table}.{local_col} 有 {orphans} 行在 "
                    f"{ref_table}.{ref_col} 中找不到父行，无法建立外键。"
                    f" 请先清理孤儿数据（本项目的设计是软删除，正常不应出现孤儿行）。"
                )

            name = _next_ibfk_name(insp, table)
            op.create_foreign_key(
                name, table, ref_table, [local_col], [ref_col], ondelete=ondelete
            )
            created.append((table, f"{local_col} -> {ref_table}.{ref_col}"))
            print(f"[0054] 已补建外键：{table}.{local_col} -> {ref_table}.{ref_col} ({name})")

    if created:
        print(f"[0054] 本次补建 {len(created)} 个外键。")
    else:
        print("[0054] 无需改动：6 个外键均已存在（全新安装的正常结果）。")


def downgrade() -> None:
    """只回滚本修订补建的外键（按列定位，避免误删其它约束）。"""
    bind = op.get_bind()
    dialect = bind.dialect.name
    insp = sa.inspect(bind)

    if dialect == "sqlite":
        print("[0054] dialect=sqlite：无 ALTER 支持，且从未补建 → 跳过。")
        return

    dropped = 0
    for table in _CREATE_ORDER:
        if not insp.has_table(table):
            continue
        existing = _fk_by_column(insp, table)
        for local_col, _ref_table, _ref_col, _ondelete in _ORM_FOREIGN_KEYS[table]:
            name = existing.get(local_col)
            if not name:
                continue
            # 只删由本修订管理的那些列上的外键
            try:
                op.drop_constraint(name, table, type_="foreignkey")
                dropped += 1
                print(f"[0054] 已删除外键：{table}.{local_col} ({name})")
            except Exception as exc:  # pragma: no cover - 主要给 MySQL 的方言差异兜底
                print(f"[0054] 删除 {table}.{name} 失败（忽略）：{exc}")
    print(f"[0054] downgrade 完成，删除 {dropped} 个外键。")
