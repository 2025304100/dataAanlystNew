"""Preserve Score publication and model input time precision on MySQL.

Expansion only; no fabricated historical microseconds. Stop writers and keep a
verified backup. All columns, defaults and legacy dates are checked before DDL.
MySQL DDL commits implicitly; re-entry is safe after a verified interruption.
"""
from datetime import datetime
import re
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.schema import CreateColumn

revision = "wps_0024_088_score_precision"
down_revision = "wps_0024_087_binding_precision"
branch_labels = depends_on = None


def upgrade():
    conn=op.get_bind()
    if conn.dialect.name != "mysql":
        return  # Other supported DateTime stores already retain microseconds.
    inspector=sa.inspect(conn)
    specification={
        "scores": ("created_at","published_at","first_published_at","revision_at","factor_data_cutoff_at"),
        "factor_model_runs": ("created_at","data_cutoff_at"),
    }
    definitions=[]
    for table,names in specification.items():
        if not inspector.has_table(table) or str(inspector.get_table_options(table).get("mysql_engine","")).lower() != "innodb":
            raise RuntimeError(f"Score/model precision requires existing InnoDB table: {table}")
        columns={c["name"]:c for c in inspector.get_columns(table)}
        for name in names:
            column=columns.get(name)
            if column is None or not isinstance(column["type"],DATETIME) or column.get("computed"):
                raise RuntimeError(f"Unproven Score/model timestamp: {table}.{name}")
            default=column.get("default")
            converted_default=None
            if default is not None:
                raw=str(default)
                if re.fullmatch(r"current_timestamp(?:\([0-6]\))?",raw,re.I):
                    converted_default="CURRENT_TIMESTAMP(6)"
                else:
                    try:
                        datetime.fromisoformat(raw.strip("'"))
                    except ValueError as error:
                        raise RuntimeError(f"Unproven Score/model timestamp default: {table}.{name}") from error
            # Never let a strict ALTER silently coerce unproven legacy dates.
            invalid=conn.scalar(sa.text(f"SELECT COUNT(*) FROM `{table}` WHERE `{name}` IS NOT NULL AND "
                f"(YEAR(`{name}`)=0 OR MONTH(`{name}`)=0 OR DAY(`{name}`)=0 OR LAST_DAY(`{name}`) IS NULL "
                f"OR DAY(`{name}`)>DAY(LAST_DAY(`{name}`)))"))
            if invalid:
                raise RuntimeError(f"Unproven Score/model timestamp values: {table}.{name}")
            definitions.append((table,name,column,converted_default))
    # One ALTER per table avoids rebuilding a large Score table five times.
    for table in specification:
        changes=[]
        for owner,name,column,converted_default in definitions:
            if owner != table or (getattr(column["type"],"fsp",None) or 0) >= 6:
                continue
            default=converted_default or column.get("default")
            declaration=sa.Column(name,DATETIME(fsp=6),nullable=column["nullable"],
                server_default=sa.text(default) if default is not None else None,
                comment=column.get("comment"))
            # Compile identifiers/defaults/comments with the connected dialect;
            # exec_driver_sql does not reinterpret :words inside old comments.
            changes.append("MODIFY COLUMN "+str(CreateColumn(declaration).compile(dialect=conn.dialect)))
        if changes:
            conn.exec_driver_sql(f"ALTER TABLE `{table}` "+", ".join(changes))


def downgrade():
    raise RuntimeError("Do not truncate Score/model disclosure microseconds; retain expanded schema or restore a verified new database")
