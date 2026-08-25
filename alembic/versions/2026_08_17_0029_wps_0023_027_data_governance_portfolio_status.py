# ===========================================================================
# ⚠️  G3 数据治理持久化 schema 迁移 (可逆)                              ⚠️
# ===========================================================================
# Revision ID : wps_0023_027_g3_data_governance_and_portfolio_status
# Operation   : (a) 新建 data_governance_audit_events 表（7 类 action 审计）
#                   — WP0-2 要求：候选池 SCD2 变更 / 基准主备切换 / auto_sim
#                     成功失败 / 对账结果 / 非法状态跳转 / factor_usage / outbox
#               (b) 给 portfolios 加 portfolio_status VARCHAR(32) 列
#                   — 7 状态状态机持久化（见 portfolio_state_machine.py）
#
# 👉 SQLITE 兼容说明：
#    - 加列走 batch_alter_table（SQLite 标准流程）；多库方言全兼容
#    - data_governance_audit_events.attributes_json / before_json / after_json
#      都用 TEXT 存储 canonical JSON（序列化在 Python 层），避免跨 DB
#      JSON 类型差异
# ===========================================================================
"""wps_0023_027_g3_data_governance_and_portfolio_status.

Revision ID: wps_0023_027_g3_data_governance_and_portfolio_status
Revises: wps_0023_026_drop_legacy_last_successful_trade_date
Create Date: 2026-08-17 06:00:00.000000

"""
from alembic import op
from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text, inspect,
)

# revision identifiers, used by Alembic.
revision = 'wps_0023_027_g3_data_governance_and_portfolio_status'
down_revision = 'wps_0023_026_drop_legacy_last_successful_trade_date'
branch_labels = None
depends_on = None


ALLOWED_ACTIONS = (
    "'PORTFOLIO_CANDIDATE_SCD2_CHANGE',"
    "'BENCHMARK_SOURCE_FAILOVER',"
    "'AUTO_SIMULATION_RESULT',"
    "'RECONCILIATION_RESULT',"
    "'ILLEGAL_STATE_TRANSITION',"
    "'FACTOR_USAGE_APPLIED',"
    "'OUTBOX_EVENT_DISPATCHED',"
    "'UNKNOWN_AUDIT_ACTION'"
)


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # ── (a) data_governance_audit_events ──────────────────────────────────
    if not _table_exists("data_governance_audit_events"):
        op.create_table(
            "data_governance_audit_events",
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("action", String(64), nullable=False, index=True),
            Column("portfolio_id", Integer,
                   ForeignKey("portfolios.id", ondelete="SET NULL"), nullable=True, index=True),
            Column("symbol_id", Integer, nullable=True),
            Column("business_key", String(128), nullable=True, index=True),
            Column("occurred_at", DateTime, nullable=False, index=True),
            Column("operator_id", String(128), nullable=False,
                   server_default="system"),
            Column("correlation_id", String(128), nullable=True, index=True),
            Column("before_json", Text, nullable=True),
            Column("after_json", Text, nullable=True),
            Column("attributes_json", Text, nullable=True),
            Column("note", Text, nullable=True),
            CheckConstraint(
                f"action IN ({ALLOWED_ACTIONS})",
                name="ck_dg_audit_action_values",
            ),
            # 显式命名，防止不同 DB 下默认索引名不一致
        )
        op.create_index(
            "ix_dg_audit_portfolio_action_time",
            "data_governance_audit_events",
            ["portfolio_id", "action", "occurred_at"],
            unique=False,
        )
    # ── (b) portfolios.portfolio_status ───────────────────────────────────
    if not _column_exists("portfolios", "portfolio_status"):
        with op.batch_alter_table("portfolios") as batch_op:
            batch_op.add_column(
                Column("portfolio_status", String(32), nullable=True,
                       comment=(
                           "7 状态：PENDING_INITIAL_REVIEW / READY / "
                           "RUNNING_AUTO_SIMULATION / RUNNING_BACKTEST / "
                           "RECONCILIATION_BLOCKED / INTERRUPTED / ADMIN_PAUSED"
                       ))
            )


def downgrade() -> None:
    # ── (b) 还原：删 portfolio_status 列 ──────────────────────────────────
    if _column_exists("portfolios", "portfolio_status"):
        with op.batch_alter_table("portfolios") as batch_op:
            batch_op.drop_column("portfolio_status")
    # ── (a) 还原：删审计事件表 ─────────────────────────────────────────────
    if _table_exists("data_governance_audit_events"):
        # 注意：索引在删表时会被 SQLite / PG 等自动连带清除；这里显式保证
        try:
            op.drop_index("ix_dg_audit_portfolio_action_time",
                          table_name="data_governance_audit_events")
        except Exception:
            pass
        op.drop_table("data_governance_audit_events")
