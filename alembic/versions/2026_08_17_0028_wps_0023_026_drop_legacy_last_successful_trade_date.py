# ===========================================================================
# ⚠️  IRREVERSIBLE MIGRATION (G0 spec T2)                                   ⚠️
# ===========================================================================
# Revision ID : wps_0023_026_drop_legacy_last_successful_trade_date
# Operation   : DROP COLUMN portfolios.last_successful_trade_date
#                (single-field legacy success date that conflated the
#                 decision timestamp AND the reconciliation timestamp)
# Replacement : portfolios.last_decision_trade_date + last_reconciled_trade_date
#               (see PRD T7 / T8 for the split between 20:30 decision & 20:45
#                reconciliation timestamps).
#
# 👉 PRE-REQUISITE — backup REQUIRED before applying this revision:
#       python scripts/db_backup.py --label pre_0026_drop_last_successful_trade_date
#       # Outputs a timestamped .db + .manifest.json (MD5+SHA256).
#
# 👉 DOWNGRADE NOTE (the "reversible" column):
#       downgrade() recreates `last_successful_trade_date AS DATE NULLABLE`,
#       but it CANNOT recover any historical values — the column was dropped.
#       To restore previous data you MUST replay the backup taken above:
#           python scripts/db_restore.py --manifest ./backups/<the_manifest>.json
#
# 👉 REGRESSIVE RESTORE GUARD (see db_restore.py exit 4):
#       If the live DB is already at this irreversible revision but the backup
#       was taken BEFORE it, db_restore.py will refuse unless you explicitly
#       pass --force-rewrite-over-0026 (to acknowledge you accept losing the
#       DROPPED legacy data).
# ===========================================================================
"""wps_0023_026_drop_legacy_last_successful_trade_date.

Revision ID: wps_0023_026_drop_legacy_last_successful_trade_date
Revises: wps_0023_025_notifications_gap_fix
Create Date: 2026-08-17 00:28:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_026_drop_legacy_last_successful_trade_date'
down_revision = 'wps_0023_025_notifications_gap_fix'
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if _column_exists("portfolios", "last_successful_trade_date"):
        with op.batch_alter_table("portfolios") as batch_op:
            batch_op.drop_column("last_successful_trade_date")


def downgrade() -> None:
    if not _column_exists("portfolios", "last_successful_trade_date"):
        with op.batch_alter_table("portfolios") as batch_op:
            batch_op.add_column(Column('last_successful_trade_date', String(32), nullable=True))
