# ===========================================================================
#  G1-WP0-3 补充迁移：portfolios 四维度状态列 + 调度时间 + 生效日
# ===========================================================================
#  Purpose: 补充 ORM 中声明但 migration 0027/028 都没加入的 6 列：
#    - status_score / status_data / status_model / status_reconciliation
#       → 四维度独立状态（T-D1 / Q27.2 分表存储，避免单状态字段被覆盖）
#    - effective_start_date
#       → 组合生效日；NULL = 兼容老组合（回退到 created_at）
#    - auto_schedule_minute
#       → 正式 auto_simulation 调度分钟（0-59），与 auto_schedule_hour 组成完整调度时钟
#
#  SQLite: 使用 batch_alter_table 兼容 ALTER ADD COLUMN
# ===========================================================================
"""wps_0023_029_wp03_portfolios_status_dim_columns.

Revision ID: wps_0023_029_wp03_portfolios_status_dim_columns
Revises: wps_0023_028_g4_fr_p1_2_task_reliability_and_outbox
Create Date: 2026-08-19 11:40:00.000000
"""
from alembic import op
from sqlalchemy import (
    CheckConstraint, Column, Date, Integer, String, inspect,
)


# revision identifiers, used by Alembic.
revision = 'wps_0023_029_wp03_portfolios_status_dim_columns'
down_revision = 'wps_0023_028_g4_fr_p1_2_task_reliability_and_outbox'
branch_labels = None
depends_on = None


_PORTFOLIO_STATUS_VALUES = (
    "'READY','RUNNING','SCORE_STALE','DATA_INCOMPLETE_PAUSED',"
    "'MODEL_INACTIVE','RECONCILIATION_BLOCKED'"
)


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = {c["name"] for c in inspector.get_columns(table_name)}
    return column_name in columns


def upgrade() -> None:
    status_dim_cols = [
        ("status_score", String(32), "READY",
         f"status_score IN ({_PORTFOLIO_STATUS_VALUES})",
         "ck_portfolios_status_score_values"),
        ("status_data", String(32), "READY",
         f"status_data IN ({_PORTFOLIO_STATUS_VALUES})",
         "ck_portfolios_status_data_values"),
        ("status_model", String(32), "READY",
         f"status_model IN ({_PORTFOLIO_STATUS_VALUES})",
         "ck_portfolios_status_model_values"),
        ("status_reconciliation", String(32), "READY",
         f"status_reconciliation IN ({_PORTFOLIO_STATUS_VALUES})",
         "ck_portfolios_status_reconciliation_values"),
    ]
    # 1) 新增 4 个 status_* 维度列
    with op.batch_alter_table("portfolios") as batch_op:
        for col_name, col_type, srv_default, _check_expr, _ck_name in status_dim_cols:
            if not _column_exists("portfolios", col_name):
                batch_op.add_column(Column(
                    col_name, col_type, nullable=False,
                    server_default=srv_default,
                ))
        # 2) effective_start_date
        if not _column_exists("portfolios", "effective_start_date"):
            batch_op.add_column(Column("effective_start_date", Date, nullable=True))
        # 3) auto_schedule_minute
        if not _column_exists("portfolios", "auto_schedule_minute"):
            batch_op.add_column(Column(
                "auto_schedule_minute", Integer, nullable=False,
                server_default="30",
            ))

    # 4) 为 4 个 status_* 列追加 CHECK 约束（SQLite 统一在 env.py 中降级为 skip，
    #    PostgreSQL/MySQL 会真正执行，ORM 层同样有 __table_args__ CHECK）。
    for col_name, _col_type, _srv, check_expr, ck_name in status_dim_cols:
        try:
            with op.batch_alter_table("portfolios") as batch_op:
                try:
                    batch_op.create_check_constraint(ck_name, check_expr)
                except Exception:
                    pass
        except NotImplementedError:
            pass
        except Exception:
            pass

    # 5) 索引：4 个 status_* 维度列各自建单例索引
    for col_name, *_ in status_dim_cols:
        try:
            op.create_index(
                f"ix_portfolios_{col_name}",
                "portfolios", [col_name], unique=False,
            )
        except Exception:
            pass


def downgrade() -> None:
    # 1) 先删索引
    for col_name in ("status_score", "status_data", "status_model",
                     "status_reconciliation"):
        try:
            op.drop_index(f"ix_portfolios_{col_name}", table_name="portfolios")
        except Exception:
            pass
    # 2) CHECK 约束
    for col_name, ck_name in [
        ("status_score", "ck_portfolios_status_score_values"),
        ("status_data", "ck_portfolios_status_data_values"),
        ("status_model", "ck_portfolios_status_model_values"),
        ("status_reconciliation", "ck_portfolios_status_reconciliation_values"),
    ]:
        try:
            with op.batch_alter_table("portfolios") as batch_op:
                try:
                    batch_op.drop_constraint(ck_name, type_="check")
                except Exception:
                    pass
        except Exception:
            try:
                op.drop_constraint(ck_name, "portfolios", type_="check")
            except Exception:
                pass
    # 3) 列（反向删除 6 列）
    with op.batch_alter_table("portfolios") as batch_op:
        for col_name in ("auto_schedule_minute", "effective_start_date",
                         "status_reconciliation", "status_model",
                         "status_data", "status_score"):
            if _column_exists("portfolios", col_name):
                try:
                    batch_op.drop_column(col_name)
                except Exception:
                    pass
