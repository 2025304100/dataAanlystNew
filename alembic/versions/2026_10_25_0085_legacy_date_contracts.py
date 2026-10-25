"""Keep exact legacy date text and restore native MySQL DATE contracts."""
from datetime import date
from alembic import op
import sqlalchemy as sa

revision="wps_0024_085_legacy_dates"
down_revision="wps_0024_084_http_auto_contracts"
branch_labels=depends_on=None

# Frozen inventory from the actual HTTP/automatic-chain schema comparison.
DATES={
    'scores':('trade_date',),
    'factor_model_runs':('train_start_date','train_end_date','validation_start_date','validation_end_date'),
    'portfolios':('last_decision_trade_date','last_reconciled_trade_date','effective_start_date'),
    'symbols':('listed_at',),
    'backtest_runs':('start_date','end_date'),
    'lhb_institution_trades':('trade_date',),
}

def upgrade():
    conn=op.get_bind()
    if conn.dialect.name!='mysql':
        return  # SQLite Date has its own result processor; no lossy CAST/rebuild.
    inspector=sa.inspect(conn);targets=[]
    for table,names in DATES.items():
        if not inspector.has_table(table):continue
        definitions={c['name']:c for c in inspector.get_columns(table)}
        for name in names:
            column=definitions.get(name)
            if column is None or isinstance(column['type'],sa.Date):continue
            if not isinstance(column['type'],sa.String):
                raise RuntimeError(f'Unapproved legacy date type: {table}.{name}; preserve and investigate')
            if str(inspector.get_table_options(table).get('mysql_engine','')).lower()!='innodb':
                raise RuntimeError(f'Legacy date migration requires InnoDB: {table}')
            shadow='legacy_'+name+'_v85'
            if shadow in definitions and not isinstance(definitions[shadow]['type'],sa.String):
                raise RuntimeError(f'Legacy evidence column has unexpected type: {table}.{shadow}')
            # Validate every column before the first MySQL implicit-commit DDL.
            fields=f'`{name}`'+(f',`{shadow}`' if shadow in definitions else '')
            rows=conn.execution_options(stream_results=True).exec_driver_sql(f'SELECT {fields} FROM `{table}`')
            try:
                for row in rows:
                    value=row[0]
                    if value is not None:
                        try:valid=isinstance(value,str) and date.fromisoformat(value).isoformat()==value
                        except ValueError:valid=False
                        if not valid:raise RuntimeError(f'Unproven legacy date in {table}.{name}; no values changed')
                    if len(row)>1 and row[1] is not None and row[1]!=value:
                        raise RuntimeError(f'Legacy date evidence conflict in {table}.{shadow}; no values changed')
            finally:rows.close()
            targets.append((table,name,column,shadow,shadow in definitions))
    for table,name,column,shadow,present in targets:
        if not present:
            op.add_column(table,sa.Column(shadow,column['type'],nullable=True,
                comment='Exact original text retained by 0085; NULL for later native-date rows'))
        conn.exec_driver_sql(f'UPDATE `{table}` SET `{shadow}`=`{name}` WHERE `{shadow}` IS NULL AND `{name}` IS NOT NULL')
        op.alter_column(table,name,existing_type=column['type'],type_=sa.Date(),
            existing_nullable=column['nullable'],server_default=None)

def downgrade():
    raise RuntimeError('Retain native date contracts and exact legacy text; use a verified new-database restore')
