"""Preserve binding timestamp precision and the complete save/apply audit action.

Expansion only. Stop writers and retain a verified backup: MySQL DDL commits
implicitly. Preflight every affected table before the first ALTER. Re-entry is
safe after an interruption; no historical values are fabricated or rewritten.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

revision = "wps_0024_087_binding_precision"
down_revision = "wps_0024_085_legacy_dates"
branch_labels = depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    specification = {
        "factor_model_audit_logs": ("action",),
        "portfolio_factor_usages": ("effective_from", "effective_to"),
        "strategy_execution_snapshots": ("effective_from",),
    }
    definitions = {}
    for table, names in specification.items():
        if not inspector.has_table(table):
            raise RuntimeError(f"Binding fidelity migration requires {table}")
        if conn.dialect.name == "mysql" and str(inspector.get_table_options(table).get("mysql_engine", "")).lower() != "innodb":
            raise RuntimeError(f"Binding fidelity migration requires InnoDB: {table}")
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        for name in names:
            column = columns.get(name)
            expected = sa.String if name == "action" else sa.DateTime
            if column is None or not isinstance(column["type"], expected):
                raise RuntimeError(f"Unproven binding fidelity column: {table}.{name}")
            if conn.dialect.name == "mysql" and name != "action" and not isinstance(column["type"], DATETIME):
                # TIMESTAMP inherits DateTime but carries session-zone conversion.
                # An unproven type drift must fail before any implicitly committed DDL.
                raise RuntimeError(f"Unproven binding fidelity column: {table}.{name}")
        definitions[table] = columns
    for table, names in specification.items():
        for name in names:
            column = definitions[table][name]
            old_type = column["type"]
            if name == "action":
                if old_type.length is None or old_type.length >= 64:
                    continue
                # Preserve per-column charset/collation and dialect options.
                # A plain String(64) would silently inherit table defaults.
                new_type = old_type.copy()
                new_type.length = 64
            elif conn.dialect.name == "mysql" and (getattr(old_type, "fsp", None) or 0) < 6:
                new_type = DATETIME(fsp=6)
            else:
                continue
            with op.batch_alter_table(table) as batch:
                batch.alter_column(name, existing_type=old_type, type_=new_type,
                    existing_nullable=column["nullable"],
                    existing_server_default=sa.text(column["default"]) if column.get("default") is not None else None,
                    existing_comment=column.get("comment"))


def downgrade():
    raise RuntimeError("Do not truncate full audit actions or binding microseconds; retain expanded schema or restore a verified new database")
