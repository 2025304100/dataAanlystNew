"""Keep microseconds on security_status_daily.source_updated_at (MySQL).

BUG-17: CSV status imports store a full-microsecond ``source_updated_at`` in
the batch receipt (and _status_fingerprint hashes ``timespec="microseconds"``),
but the MySQL column was plain DATETIME, which loses fractional seconds
(including rounding into the next second on MySQL 5.7).
The row↔receipt consistency check (local_import._same_source) then always
failed on MySQL, so imported execution constraints (per-day price limits)
could never attach — and the backtest precheck failed with
PRICE_LIMIT_DATA_UNAVAILABLE for every imported row. SQLite keeps the full
string, which is why tests never caught it. Widen the MySQL column to
DATETIME(6); other dialects are unchanged.
"""
from alembic import op

revision = "wps_0024_082_status_dt6"
down_revision = "wps_0024_081_idempotency_pk_contract"
branch_labels = depends_on = None

_TABLE = "security_status_daily"
_COLUMN = "source_updated_at"


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    from sqlalchemy import text

    bind.execute(text(f"ALTER TABLE `{_TABLE}` MODIFY `{_COLUMN}` DATETIME(6) NULL"))


def downgrade():
    raise RuntimeError(
        "Preserve source timestamp precision and historical receipt fingerprints; "
        "0082 cannot be downgraded. Use an audited restore plan for application rollback."
    )
