"""WP-S.5: async_tasks state machine columns

Revision ID: wps_0023_019_async_tasks_state_machine
Revises: wps_0023_018_discovery_score_snapshots
Create Date: 2026-07-23 00:19:00.000000

为 async_tasks 表补齐 WP-S.5 任务防卡死状态机扩展字段（11 列）：
heartbeat_at / stage_budget_seconds / stage_started_at / last_progress_at /
last_progress_percent / current_step_description / suggested_action /
batch_recovery_json / last_patrol_at / worker_thread_id / cancel_requested。
全部 nullable=True，向后兼容旧数据。
init_db.py SQLite 路径有补丁，MySQL 路径部分缺失，Alembic revision 缺失。
幂等：列已存在时跳过。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_019_async_tasks_state_machine"
down_revision: Union[str, None] = "wps_0023_018_discovery_score_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


# (column_name, column_type, nullable, server_default)
_NEW_COLUMNS = [
    ("heartbeat_at", sa.DateTime(), True, None),
    ("stage_budget_seconds", sa.Integer(), True, None),
    ("stage_started_at", sa.DateTime(), True, None),
    ("last_progress_at", sa.DateTime(), True, None),
    ("last_progress_percent", sa.Float(), True, None),
    ("current_step_description", sa.Text(), True, None),
    ("suggested_action", sa.Text(), True, None),
    ("batch_recovery_json", sa.Text(), True, None),
    ("last_patrol_at", sa.DateTime(), True, None),
    ("worker_thread_id", sa.String(length=32), True, None),
    ("cancel_requested", sa.Integer(), True, sa.text("0")),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "async_tasks"):
        return

    for col_name, col_type, nullable, server_default in _NEW_COLUMNS:
        if not _column_exists(inspector, "async_tasks", col_name):
            op.add_column(
                "async_tasks",
                sa.Column(col_name, col_type, nullable=nullable, server_default=server_default),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "async_tasks"):
        return

    for col_name, _, _, _ in reversed(_NEW_COLUMNS):
        if _column_exists(inspector, "async_tasks", col_name):
            op.drop_column("async_tasks", col_name)
