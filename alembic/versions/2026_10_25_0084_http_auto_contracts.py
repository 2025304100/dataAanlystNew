"""Retain retired schedule evidence while permitting current HTTP writers."""
from alembic import op
import sqlalchemy as sa

revision = "wps_0024_084_http_auto_contracts"
down_revision = "wps_0024_083_combination_contracts"
branch_labels = depends_on = None

def upgrade():
    conn=op.get_bind()
    inspector=sa.inspect(conn)
    if not inspector.has_table('scheduled_tasks'):
        return
    definitions={column['name']:column for column in inspector.get_columns('scheduled_tasks')}
    columns=[definitions[name] for name in ('task_name','cron_expression')
        if name in definitions and (not definitions[name]['nullable'] or definitions[name].get('default') is not None)]
    if not columns:
        return
    if conn.dialect.name=='mysql' and str(inspector.get_table_options('scheduled_tasks').get('mysql_engine','')).lower()!='innodb':
        raise RuntimeError('Schedule migration requires InnoDB; no implicit engine conversion is allowed')
    if conn.dialect.name=='sqlite' and conn.exec_driver_sql('PRAGMA foreign_keys').scalar():
        for table in inspector.get_table_names():
            if any(fk['referred_table']=='scheduled_tasks' for fk in inspector.get_foreign_keys(table)):
                if conn.exec_driver_sql(f'SELECT 1 FROM "{table}" LIMIT 1').first():
                    raise RuntimeError('Stop before SQLite parent rebuild: preserve referenced schedule history with an explicit FK-safe migration connection')
    with op.batch_alter_table('scheduled_tasks',recreate='always' if conn.dialect.name=='sqlite' else 'auto') as batch:
        for column in columns:
            # Preserve original types, IDs, values, indexes and relationships.
            # New writers use name/frequency/time_of_day, never a fake old name.
            batch.alter_column(column['name'],existing_type=column['type'],nullable=True,server_default=None)

def downgrade():
    raise RuntimeError('Legacy names cannot be invented for new schedules; retain the expanded schema or restore a verified backup')
