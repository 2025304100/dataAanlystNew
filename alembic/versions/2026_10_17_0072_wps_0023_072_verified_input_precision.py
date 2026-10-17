"""Preserve verified price/quantity/fundamental input precision.

Revision ID: wps_0023_072_verified_input_precision
Revises: wps_0023_071_market_bar_correction_audit
"""
from alembic import op
import sqlalchemy as sa
revision='wps_0023_072_verified_input_precision'
down_revision='wps_0023_071_market_bar_correction_audit'
branch_labels=None
depends_on=None
FIELDS={
    'daily_bars':('open','high','low','close','volume','amount','turnover_rate'),
    'universe_daily_bars':('open','high','low','close','volume','amount','turnover_rate'),
    'stock_financial_reports':('roe_ttm','net_profit','revenue','net_profit_yoy','revenue_yoy'),
    'capital_flows':('main_net_inflow','main_net_inflow_pct','small_net_inflow','medium_net_inflow','large_net_inflow','super_large_net_inflow'),
}
def upgrade():
    conn=op.get_bind()
    if conn.dialect.name!='mysql':return  # SQLite REAL already stores binary64.
    for table,fields in FIELDS.items():
        columns={c['name']:c for c in sa.inspect(conn).get_columns(table)}
        clauses=[]
        for field in fields:
            c=columns[field];name=str(c['type']).upper()
            if name.startswith('DOUBLE'):continue
            if not name.startswith('FLOAT'):raise RuntimeError('Unexpected input numeric type')
            clauses.append(f"MODIFY COLUMN `{field}` DOUBLE {'NULL' if c['nullable'] else 'NOT NULL'}")
        if clauses:conn.execute(sa.text(f"ALTER TABLE `{table}` "+', '.join(clauses)))
def downgrade():
    # Narrowing would irreversibly round accepted observations; retain precision.
    pass
