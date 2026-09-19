"""wps_0023_025_notifications_gap_fix.

Revision ID: wps_0023_025_notifications_gap_fix
Revises: wps_0023_024_decision_engine_contract
Create Date: 2026-08-17 00:27:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_025_notifications_gap_fix'
down_revision = 'wps_0023_024_decision_engine_contract'
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    with op.batch_alter_table("notification_deliveries") as batch_op:
        if not _column_exists("notification_deliveries", "recipient_info_json"):
            batch_op.add_column(Column('recipient_info_json', Text, nullable=True))
        if not _column_exists("notification_deliveries", "provider_response_json"):
            batch_op.add_column(Column('provider_response_json', Text, nullable=True))
        if not _column_exists("notification_deliveries", "billing_units"):
            batch_op.add_column(Column('billing_units', Integer, nullable=True))
        if not _column_exists("notification_deliveries", "delivery_trace_id"):
            batch_op.add_column(Column('delivery_trace_id', String(128), nullable=True))
        if not _column_exists("notification_deliveries", "retry_of_delivery_id"):
            batch_op.add_column(Column('retry_of_delivery_id', Integer, nullable=True))

    with op.batch_alter_table("notification_templates") as batch_op:
        if not _column_exists("notification_templates", "language_code"):
            batch_op.add_column(Column('language_code', String(16), nullable=False, server_default='zh-CN'))
        if not _column_exists("notification_templates", "template_category"):
            batch_op.add_column(Column('template_category', String(32), nullable=True))
        if not _column_exists("notification_templates", "content_footer_template"):
            batch_op.add_column(Column('content_footer_template', Text, nullable=True))
        if not _column_exists("notification_templates", "jump_url_template"):
            batch_op.add_column(Column('jump_url_template', String(512), nullable=True))
        if not _column_exists("notification_templates", "icon_key"):
            batch_op.add_column(Column('icon_key', String(64), nullable=True))
        if not _column_exists("notification_templates", "default_channel_ids_json"):
            batch_op.add_column(Column('default_channel_ids_json', Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("notification_templates") as batch_op:
        if _column_exists("notification_templates", "default_channel_ids_json"):
            batch_op.drop_column("default_channel_ids_json")
        if _column_exists("notification_templates", "icon_key"):
            batch_op.drop_column("icon_key")
        if _column_exists("notification_templates", "jump_url_template"):
            batch_op.drop_column("jump_url_template")
        if _column_exists("notification_templates", "content_footer_template"):
            batch_op.drop_column("content_footer_template")
        if _column_exists("notification_templates", "template_category"):
            batch_op.drop_column("template_category")
        if _column_exists("notification_templates", "language_code"):
            batch_op.drop_column("language_code")

    with op.batch_alter_table("notification_deliveries") as batch_op:
        if _column_exists("notification_deliveries", "retry_of_delivery_id"):
            batch_op.drop_column("retry_of_delivery_id")
        if _column_exists("notification_deliveries", "delivery_trace_id"):
            batch_op.drop_column("delivery_trace_id")
        if _column_exists("notification_deliveries", "billing_units"):
            batch_op.drop_column("billing_units")
        if _column_exists("notification_deliveries", "provider_response_json"):
            batch_op.drop_column("provider_response_json")
        if _column_exists("notification_deliveries", "recipient_info_json"):
            batch_op.drop_column("recipient_info_json")
