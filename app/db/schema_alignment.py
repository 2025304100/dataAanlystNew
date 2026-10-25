"""Safe column addition used by application bootstrap.

Missing required data must stop initialization; an implicit zero/empty value is
not a replacement for an explicit business default or historical evidence.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.schema import CreateColumn


def scalar_default(column):
    default = column.default
    if default is not None and default.is_scalar:
        value = default.arg
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
    return None


def column_definition(column, dialect, *, nullable=None, include_default=True):
    copied = column._copy()
    if nullable is not None:
        copied.nullable = nullable
    if not include_default:
        copied.server_default = None
    elif copied.computed is None and copied.server_default is None:
        default = scalar_default(column)
        if default is not None:
            literal = sa.literal(default, type_=column.type).compile(
                dialect=dialect, compile_kwargs={"literal_binds": True})
            copied.server_default = sa.DefaultClause(sa.text(str(literal)))
    # MySQL 5.7 forbids TEXT/BLOB defaults. The caller backfills the explicit
    # Python value before enforcing NOT NULL; do not emit illegal DDL.
    if dialect.name == "mysql" and isinstance(copied.type, (sa.Text, sa.LargeBinary)):
        copied.server_default = None
    sa.Table("_column_compile", sa.MetaData(), copied)
    return str(CreateColumn(copied).compile(dialect=dialect)).strip()


def type_definition(column, is_mysql):
    dialect = mysql.dialect() if is_mysql else sqlite.dialect()
    full = column_definition(column, dialect)
    quoted = dialect.identifier_preparer.quote(column.name)
    return full[len(quoted):].lstrip()


def add_column(conn, table_name, column):
    q = conn.dialect.identifier_preparer.quote_identifier
    count = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {q(table_name)}").scalar_one()
    definition = column_definition(column, conn.dialect)
    default = scalar_default(column)
    if count and not column.nullable and column.computed is None:
        has_server_default = " DEFAULT " in f" {definition} "
        if not has_server_default and default is None:
            raise RuntimeError(f"{table_name}.{column.name}: required historical values need an explicit backfill")
        if not has_server_default:
            # SQLite supports ADD NOT NULL with a scalar TEXT default. Only the
            # MySQL 5.7 no-TEXT-default case needs staged nullable/backfill DDL.
            conn.exec_driver_sql(f"ALTER TABLE {q(table_name)} ADD COLUMN " +
                                 column_definition(column, conn.dialect, nullable=True, include_default=False))
            conn.execute(sa.text(f"UPDATE {q(table_name)} SET {q(column.name)}=:value "
                                 f"WHERE {q(column.name)} IS NULL"), {"value": default})
            conn.exec_driver_sql(f"ALTER TABLE {q(table_name)} MODIFY COLUMN " + definition)
            return
    conn.exec_driver_sql(f"ALTER TABLE {q(table_name)} ADD COLUMN {definition}")
