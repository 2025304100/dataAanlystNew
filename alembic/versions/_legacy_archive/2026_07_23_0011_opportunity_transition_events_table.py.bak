"""WP3.1: opportunity_transition_events table

Revision ID: wps_0023_011_opportunity_transitions
Revises: wps_0023_010_watchlist_items_extend
Create Date: 2026-07-23 00:11:00.000000

创建 opportunity_transition_events 表（机会状态流转审计事件）。
init_db.py SQLite/MySQL 路径都补了，Alembic 缺失。
幂等：表已存在时跳过。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_011_opportunity_transitions"
down_revision: Union[str, None] = "wps_0023_010_watchlist_items_extend"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _index_exists(inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return index_name in {i["name"] for i in inspector.get_indexes(table_name)}


_TABLE = "opportunity_transition_events"

_INDEXES = [
    ("idx_ote_symbol_event", ["symbol_id", "event_type"]),
    ("idx_ote_source", ["source_type", "source_id"]),
    ("idx_ote_target", ["target_type", "target_id"]),
    ("idx_ote_created", ["created_at"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _table_exists(inspector, _TABLE):
        # 表已存在，仅补缺失的索引（幂等）
        for idx_name, idx_cols in _INDEXES:
            if not _index_exists(inspector, _TABLE, idx_name):
                op.create_index(idx_name, _TABLE, idx_cols, unique=False)
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("reason_json", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_opportunity_transition_events_idempotency_key"),
    )
    # symbol_id / event_type / source_id / target_id / created_at 单列索引
    op.create_index("ix_opportunity_transition_events_symbol_id", _TABLE, ["symbol_id"], unique=False)
    op.create_index("ix_opportunity_transition_events_event_type", _TABLE, ["event_type"], unique=False)
    op.create_index("ix_opportunity_transition_events_source_id", _TABLE, ["source_id"], unique=False)
    op.create_index("ix_opportunity_transition_events_target_id", _TABLE, ["target_id"], unique=False)
    op.create_index("ix_opportunity_transition_events_created_at", _TABLE, ["created_at"], unique=False)
    op.create_index("ix_opportunity_transition_events_idempotency_key", _TABLE, ["idempotency_key"], unique=True)

    for idx_name, idx_cols in _INDEXES:
        op.create_index(idx_name, _TABLE, idx_cols, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, _TABLE):
        return

    for idx_name, _ in _INDEXES:
        if _index_exists(inspector, _TABLE, idx_name):
            op.drop_index(idx_name, table_name=_TABLE)

    for idx_name in [
        "ix_opportunity_transition_events_idempotency_key",
        "ix_opportunity_transition_events_created_at",
        "ix_opportunity_transition_events_target_id",
        "ix_opportunity_transition_events_source_id",
        "ix_opportunity_transition_events_event_type",
        "ix_opportunity_transition_events_symbol_id",
    ]:
        if _index_exists(inspector, _TABLE, idx_name):
            op.drop_index(idx_name, table_name=_TABLE)

    op.drop_table(_TABLE)
