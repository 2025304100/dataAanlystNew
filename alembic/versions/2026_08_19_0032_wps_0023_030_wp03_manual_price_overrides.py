# ===========================================================================
#  G1-WP0-3 补充迁移(2): manual_price_overrides 人工介入价表
# ===========================================================================
#  Purpose: T-A9 Q7.4 人工处理 DATA_BLOCKED；让 decision_engine.evaluate()
#  在决策前先查询未消费的 override 注入，避免数据阻断时强管。
#
#  可逆：downgrade() 会直接 DROP TABLE（先尝试删除外键/索引/约束）。
# ===========================================================================
"""wps_0023_030_wp03_manual_price_overrides.

Revision ID: wps_0023_030_wp03_manual_price_overrides
Revises: wps_0023_029_wp03_portfolios_status_dim_columns
Create Date: 2026-08-19 11:50:00.000000
"""
from alembic import op
from sqlalchemy import (
    BigInteger, Column, Date, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, inspect,
)


revision = 'wps_0023_030_wp03_manual_price_overrides'
down_revision = 'wps_0023_029_wp03_portfolios_status_dim_columns'
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("manual_price_overrides"):
        return

    op.create_table(
        "manual_price_overrides",
        Column("id", BigInteger().with_variant(Integer, "sqlite"),
               primary_key=True, autoincrement=True),
        Column("portfolio_id", Integer,
               ForeignKey("portfolios.id", ondelete="RESTRICT"),
               nullable=False, index=True),
        Column("strategy_snapshot_id", String(64),
               ForeignKey("strategy_execution_snapshots.id",
                          ondelete="SET NULL"),
               nullable=True, index=True),
        Column("symbol_id", Integer,
               ForeignKey("symbols.id", ondelete="RESTRICT"),
               nullable=True, index=True),
        Column("trade_date", Date, nullable=False, index=True),
        Column("resolved_mode", String(32), nullable=False, index=True),
        Column("manual_executable_price", Float, nullable=True),
        Column("source_note", Text, nullable=True),
        Column("previous_data_gap_days", Integer, nullable=True),
        Column("consumed_flag", Integer, nullable=False,
               server_default="0", index=True),
        Column("consumed_at", DateTime, nullable=True),
        Column("consumed_by_run_id", String(64),
               ForeignKey("decision_runs.id", ondelete="SET NULL"),
               nullable=True, index=True),
        Column("operator_id", String(128), nullable=False,
               server_default="system"),
        Column("correlation_id", String(64), nullable=True, index=True),
        Column("created_at", DateTime, nullable=False, index=True),
        Column("updated_at", DateTime, nullable=False),
    )
    # 唯一键 + 复合索引（注意：SQLite 下 create_check_constraint 在 env.py 中已
    # 捕获 NotImplementedError，此处无脑包 try/except 即可）。
    try:
        op.create_unique_constraint(
            "uq_manual_price_override_portfolio_date_symbol",
            "manual_price_overrides",
            ["portfolio_id", "trade_date", "symbol_id"],
        )
    except Exception:
        try:
            op.create_index(
                "uq_manual_price_override_portfolio_date_symbol",
                "manual_price_overrides",
                ["portfolio_id", "trade_date", "symbol_id"],
                unique=True,
            )
        except Exception:
            pass
    try:
        op.create_index(
            "ix_manual_price_overrides_portfolio_date_consumed",
            "manual_price_overrides",
            ["portfolio_id", "trade_date", "consumed_flag"],
            unique=False,
        )
    except Exception:
        pass
    try:
        op.create_check_constraint(
            "ck_manual_price_override_resolved_mode_3values",
            "manual_price_overrides",
            "resolved_mode IN ('confirm_manual_price','continue_forward','keep_paused')",
        )
    except Exception:
        pass


def downgrade() -> None:
    if not _table_exists("manual_price_overrides"):
        return
    # 先尝试删约束/索引
    for idx_name, is_unique in [
        ("ix_manual_price_overrides_portfolio_date_consumed", False),
    ]:
        try:
            op.drop_index(idx_name, table_name="manual_price_overrides")
        except Exception:
            pass
    # 唯一键两种形式
    for cname in ("uq_manual_price_override_portfolio_date_symbol",):
        try:
            op.drop_constraint(cname, "manual_price_overrides", type_="unique")
        except Exception:
            try:
                op.drop_index(cname, table_name="manual_price_overrides")
            except Exception:
                pass
    try:
        op.drop_constraint("ck_manual_price_override_resolved_mode_3values",
                           "manual_price_overrides", type_="check")
    except Exception:
        pass
    op.drop_table("manual_price_overrides")
