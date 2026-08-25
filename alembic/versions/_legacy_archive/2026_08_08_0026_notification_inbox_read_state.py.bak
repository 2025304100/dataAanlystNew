"""Persist read state for the real in-app notification inbox.

Revision ID: wps_0026_026_notification_inbox_read_state
Revises: wps_0025_025_portfolio_candidate_admission_evidence
"""

from alembic import op
import sqlalchemy as sa


revision = "wps_0026_026_notification_inbox_read_state"
down_revision = "wps_0025_025_portfolio_candidate_admission_evidence"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "notification_outbox" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("notification_outbox")}
    if "read_at" not in columns:
        op.add_column(
            "notification_outbox",
            sa.Column("read_at", sa.DateTime(), nullable=True),
        )
    indexes = {index["name"] for index in inspector.get_indexes("notification_outbox")}
    if "ix_notification_outbox_read_at" not in indexes:
        op.create_index(
            "ix_notification_outbox_read_at",
            "notification_outbox",
            ["read_at"],
            unique=False,
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "notification_outbox" not in inspector.get_table_names():
        return
    indexes = {index["name"] for index in inspector.get_indexes("notification_outbox")}
    if "ix_notification_outbox_read_at" in indexes:
        op.drop_index("ix_notification_outbox_read_at", table_name="notification_outbox")
    columns = {column["name"] for column in inspector.get_columns("notification_outbox")}
    if "read_at" in columns:
        op.drop_column("notification_outbox", "read_at")
