"""补齐 34 个「ORM 已声明、库里未落实」的外键。

Revision ID: wps_0023_067_missing_foreign_keys
Revises: wps_0023_066_trade_calendar_table

背景
====
TD5 的《技术债-全库schema漂移核对报告》指出 18 张表缺 30 个外键，成因是早期
MyISAM 引擎静默吞掉 FK 定义（T01/T07 两轮修引擎时补了引擎、没补回约束）。

本次直接以 ORM 声明为准重新枚举（比报告更准），实际待补 **34 个 / 18 张表**：
CASCADE 19、SET NULL 11、RESTRICT 4。

前置条件（已满足）
==================
报告明确要求「加 FK 前必须先清孤儿数据，否则迁移必炸」。2026-10-01 已执行：
- 清理组合 2（2026-08 的测试组合，成员含 symbol_id = -1/-2/-3 假标的）残留
  **1275 行 / 7 张表**；
- 删父表后暴露的**二级孤儿** `scan_results` 6 行也已清（该表无 portfolio_id 列，
  只能按 scan_run_id 定位）。

清理后复核：**34 个待补外键列全部零孤儿**（`.workbuddy/mining/_orphan_audit.py`
输出 `✅ 所有待补外键列均无孤儿`）。

实现要点
========
- **SQLite 直接跳过**：SQLite 不支持 `ALTER TABLE ... ADD CONSTRAINT`，本仓迁移
  已有先例（0054/0056 同样打印「dialect=sqlite：跳过（预期行为）」）。测试库走
  迁移链时没有这些 FK，不影响用例（现有用例在无 FK 环境下已全绿）。
- **幂等**：先 `inspector.get_foreign_keys` 查该列是否已有 FK，有则跳过。
- **约束名显式给**（`fk_<table>_<column>`）：不依赖 MySQL 自动生成的
  `<table>_ibfk_<n>`；漂移工具按「本地列元组」比对、不看名字，所以命名自由，
  但显式命名让后续排查可读。
- 大表提醒：`daily_bars`（142 万行）加 FK 时 MySQL 需要锁表校验，耗时以秒计。
  （实测整批 34 个 14 秒完成。）

实测结果与一处预期内的残留漂移
==============================
执行后 `verify_schema_drift.py`：漂移 **51 → 43** 张表，`missing_fk` **18 → 0**。

但 `fk_mismatch` 由 12 涨到 15 —— **这不是本迁移的缺陷**，而是 MySQL 的固有行为：

    本迁移对 4 个 FK 写了 `ON DELETE RESTRICT`（idempotency_records.portfolio_id、
    portfolio_rules.portfolio_id、positions.portfolio_id、positions.symbol_id），
    但 MySQL 认为 RESTRICT 与"默认行为"等价，**在 SHOW CREATE TABLE 里直接把
    ON DELETE 子句省略掉**，information_schema.REFERENTIAL_CONSTRAINTS.DELETE_RULE
    于是返回 `NO ACTION`。

    即 `SHOW CREATE TABLE positions` 显示：
        CONSTRAINT `fk_positions_portfolio_id` FOREIGN KEY (`portfolio_id`)
          REFERENCES `portfolios` (`id`)          ← 没有 ON DELETE

    而 CASCADE / SET NULL 都如实写入（同一表里 `trade_setups` 的 4 个 FK 全部正确）。

这与 TD5 报告 §B3 的判断一致：「MySQL 中 RESTRICT 与 NO ACTION 语义等价（都是立即
拒绝，无延迟检查），属纯声明漂移」。**无法通过 DDL 让 MySQL 存成 RESTRICT**，
只能登记豁免或改 ORM 声明，属独立决策，不在本迁移范围内。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "wps_0023_067_missing_foreign_keys"
down_revision = "wps_0023_066_trade_calendar_table"
branch_labels = None
depends_on = None

# (表, 列, 目标表, 目标列, ondelete)
_FK_DEFS: list[tuple[str, str, str, str, str]] = [
    ("backtest_runs", "strategy_snapshot_id", "strategy_execution_snapshots", "id", 'SET NULL'),
    ("backtest_trades", "decision_evidence_id", "decision_evidence", "id", 'SET NULL'),
    ("backtest_trades", "exit_evidence_id", "decision_evidence", "id", 'SET NULL'),
    ("cash_ledger", "portfolio_id", "portfolios", "id", 'CASCADE'),
    ("custom_indicator_versions", "indicator_id", "custom_indicators", "id", 'CASCADE'),
    ("daily_bars", "symbol_id", "symbols", "id", 'CASCADE'),
    ("factor_values", "factor_id", "factors", "id", 'CASCADE'),
    ("factor_values", "symbol_id", "symbols", "id", 'CASCADE'),
    ("factors", "active_version_id", "factor_versions", "id", 'SET NULL'),
    ("factors", "shadow_version_id", "factor_versions", "id", 'SET NULL'),
    ("idempotency_records", "portfolio_id", "portfolios", "id", 'RESTRICT'),
    ("portfolio_rules", "portfolio_id", "portfolios", "id", 'RESTRICT'),
    ("positions", "portfolio_id", "portfolios", "id", 'RESTRICT'),
    ("positions", "symbol_id", "symbols", "id", 'RESTRICT'),
    ("scan_results", "scan_run_id", "scan_runs", "id", 'CASCADE'),
    ("scan_results", "symbol_id", "symbols", "id", 'CASCADE'),
    ("scan_runs", "portfolio_id", "portfolios", "id", 'SET NULL'),
    ("scan_runs", "portfolio_rule_id", "portfolio_rules", "id", 'SET NULL'),
    ("scan_runs", "preset_id", "scan_presets", "id", 'SET NULL'),
    ("scores", "symbol_id", "symbols", "id", 'CASCADE'),
    ("signal_rules", "portfolio_id", "portfolios", "id", 'CASCADE'),
    ("sim_orders", "member_id", "portfolio_members", "id", 'SET NULL'),
    ("sim_orders", "portfolio_id", "portfolios", "id", 'CASCADE'),
    ("sim_orders", "symbol_id", "symbols", "id", 'CASCADE'),
    ("sim_trades", "order_id", "sim_orders", "id", 'CASCADE'),
    ("sim_trades", "portfolio_id", "portfolios", "id", 'CASCADE'),
    ("sim_trades", "symbol_id", "symbols", "id", 'CASCADE'),
    ("trade_setups", "portfolio_id", "portfolios", "id", 'CASCADE'),
    ("trade_setups", "scan_run_id", "scan_runs", "id", 'SET NULL'),
    ("trade_setups", "score_id", "scores", "id", 'CASCADE'),
    ("trade_setups", "symbol_id", "symbols", "id", 'CASCADE'),
    ("watchlist_items", "symbol_id", "symbols", "id", 'CASCADE'),
    ("watchlist_items", "target_portfolio_id", "portfolios", "id", 'SET NULL'),
    ("watchlist_items", "watchlist_id", "watchlists", "id", 'CASCADE'),
]


def _existing_fk_columns(conn, table: str) -> set[str]:
    """该表已有哪些列已是外键（用于幂等跳过）。"""
    try:
        return {
            col
            for fk in sa.inspect(conn).get_foreign_keys(table)
            for col in (fk.get("constrained_columns") or [])
        }
    except Exception:
        return set()


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name
    if dialect != "mysql":
        print(
            f"[0067] dialect={dialect}：不支持 ALTER TABLE ADD CONSTRAINT，"
            "跳过补外键（预期行为；测试库无这批 FK 不影响现有用例）"
        )
        return

    created = 0
    skipped = 0
    for table, column, ref_table, ref_column, ondelete in _FK_DEFS:
        if column in _existing_fk_columns(conn, table):
            skipped += 1
            continue
        op.create_foreign_key(
            f"fk_{table}_{column}",
            table,
            ref_table,
            [column],
            [ref_column],
            ondelete=ondelete or None,
        )
        created += 1
    print(f"[0067] 外键补齐：新建 {created} 个，跳过已存在 {skipped} 个")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "mysql":
        return
    for table, column, _ref_table, _ref_column, _ondelete in _FK_DEFS:
        if column not in _existing_fk_columns(conn, table):
            continue
        try:
            op.drop_constraint(f"fk_{table}_{column}", table, type_="foreignkey")
        except Exception:
            pass
