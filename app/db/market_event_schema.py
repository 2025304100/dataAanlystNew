"""Retain legacy event facts while permitting the current market-news writer."""
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def ensure_market_event_legacy_writes(connection):
    inspector = sa.inspect(connection)
    if not inspector.has_table("market_events"):
        return
    columns = {column["name"]: column for column in inspector.get_columns("market_events")}
    retired = [columns[name] for name in ("event_type", "event_date") if name in columns and not columns[name]["nullable"]]
    if not retired:
        return
    operations = Operations(MigrationContext.configure(connection))
    with operations.batch_alter_table("market_events", recreate="always" if connection.dialect.name == "sqlite" else "auto") as batch:
        for column in retired:
            batch.alter_column(column["name"], existing_type=column["type"], nullable=True,
                existing_server_default=sa.text(column["default"]) if column.get("default") is not None else None)
