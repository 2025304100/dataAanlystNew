"""wps_0023_007_scans.

Revision ID: wps_0023_007_scans
Revises: wps_0023_006_scoring_configs
Create Date: 2026-08-17 00:08:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_007_scans'
down_revision = 'wps_0023_006_scoring_configs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'factor_runtime_state',
        Column('id', Integer, primary_key=True, server_default='1'),
        Column('weight_mode', String(16), nullable=False, server_default='manual'),
        Column('active_model_run_id', String(64), ForeignKey('factor_model_runs.id', ondelete='SET NULL'), nullable=True),
        Column('updated_by', String(128), nullable=False, server_default='system'),
        Column('fallback_reason', Text, nullable=True),
        Column('version', Integer, nullable=False, server_default='1'),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_factor_runtime_state_weight_mode', 'factor_runtime_state', ['weight_mode'])
    op.create_index('ix_factor_runtime_state_active_model_run_id', 'factor_runtime_state', ['active_model_run_id'])

    op.create_table(
        'factor_system_config',
        Column('id', Integer, primary_key=True, server_default='1'),
        Column('feature_enabled', Integer, nullable=False, server_default='0'),
        Column('warehouse_path', String(1024), nullable=False),
        Column('updated_by', String(128), nullable=False, server_default='environment'),
        Column('updated_at', DateTime, nullable=False),
    )

    op.create_table(
        'factor_model_audit_logs',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('action', String(32), nullable=False),
        Column('model_run_id', String(64), ForeignKey('factor_model_runs.id', ondelete='SET NULL'), nullable=True),
        Column('previous_mode', String(16), nullable=False),
        Column('new_mode', String(16), nullable=False),
        Column('previous_model_run_id', String(64), nullable=True),
        Column('new_model_run_id', String(64), nullable=True),
        Column('actor', String(128), nullable=False, server_default='local_user'),
        Column('note', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_factor_model_audit_logs_action', 'factor_model_audit_logs', ['action'])
    op.create_index('ix_factor_model_audit_logs_model_run_id', 'factor_model_audit_logs', ['model_run_id'])
    op.create_index('ix_factor_model_audit_logs_created_at', 'factor_model_audit_logs', ['created_at'])

    op.create_table(
        'sim_accounts',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('name', String(128), nullable=False, unique=True),
        Column('portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='CASCADE'), nullable=False),
        Column('initial_capital', Float, nullable=False),
        Column('current_capital', Float, nullable=False),
        Column('cash', Float, nullable=False),
        Column('status', String(16), nullable=False, server_default='active'),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
    )
    op.create_index('ix_sim_accounts_portfolio_id', 'sim_accounts', ['portfolio_id'])
    op.create_index('ix_sim_accounts_status', 'sim_accounts', ['status'])

    op.create_table(
        'sim_orders',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('sim_account_id', Integer, ForeignKey('sim_accounts.id', ondelete='CASCADE'), nullable=False),
        Column('portfolio_id', Integer, nullable=False),
        Column('symbol_id', Integer, ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        Column('side', String(16), nullable=False),
        Column('order_type', String(16), nullable=False),
        Column('quantity', Float, nullable=False),
        Column('submitted_price', Float, nullable=False),
        Column('status', String(16), nullable=False),
        Column('filled_quantity', Float, nullable=False, server_default='0'),
        Column('filled_price', Float, nullable=False, server_default='0'),
        Column('filled_amount', Float, nullable=False, server_default='0'),
        Column('fee', Float, nullable=False, server_default='0'),
        Column('created_at', DateTime, nullable=False),
        Column('member_id', Integer, nullable=True),
        Column('source_type', String(32), nullable=True),
        Column('source_id', Integer, nullable=True),
        Column('signal_id', Integer, nullable=True),
        Column('signal_snapshot_json', Text, nullable=True),
        Column('rule_version_id', Integer, nullable=True),
        Column('execution_mode', String(16), nullable=True),
        Column('client_order_key', String(128), nullable=True),
        Column('decision_snapshot_json', Text, nullable=True),
        Column('rejection_code', String(64), nullable=True),
        Column('rejection_detail', Text, nullable=True),
    )
    # ``Base.metadata.create_all`` in an existing installation may have
    # created the current ORM shape first.  The historical sim-account
    # linkage is still required by this revision, so add it before creating
    # the legacy index instead of failing halfway through upgrade.
    inspector = inspect(op.get_bind())
    if 'sim_orders' in inspector.get_table_names():
        columns = {item['name'] for item in inspector.get_columns('sim_orders')}
        if 'sim_account_id' not in columns:
            with op.batch_alter_table('sim_orders') as batch_op:
                batch_op.add_column(Column('sim_account_id', Integer, nullable=True))
    op.create_index('ix_sim_orders_sim_account_id', 'sim_orders', ['sim_account_id'])
    op.create_index('ix_sim_orders_portfolio_id', 'sim_orders', ['portfolio_id'])
    op.create_index('ix_sim_orders_symbol_id', 'sim_orders', ['symbol_id'])
    op.create_index('ix_sim_orders_status', 'sim_orders', ['status'])
    op.create_index('ix_sim_orders_member_id', 'sim_orders', ['member_id'])
    op.create_index('ix_sim_orders_source_id', 'sim_orders', ['source_id'])


def downgrade() -> None:
    op.drop_index('ix_sim_orders_source_id', table_name='sim_orders')
    op.drop_index('ix_sim_orders_member_id', table_name='sim_orders')
    op.drop_index('ix_sim_orders_status', table_name='sim_orders')
    op.drop_index('ix_sim_orders_symbol_id', table_name='sim_orders')
    op.drop_index('ix_sim_orders_portfolio_id', table_name='sim_orders')
    op.drop_index('ix_sim_orders_sim_account_id', table_name='sim_orders')
    op.drop_table('sim_orders')

    op.drop_index('ix_sim_accounts_status', table_name='sim_accounts')
    op.drop_index('ix_sim_accounts_portfolio_id', table_name='sim_accounts')
    op.drop_table('sim_accounts')

    op.drop_index('ix_factor_model_audit_logs_created_at', table_name='factor_model_audit_logs')
    op.drop_index('ix_factor_model_audit_logs_model_run_id', table_name='factor_model_audit_logs')
    op.drop_index('ix_factor_model_audit_logs_action', table_name='factor_model_audit_logs')
    op.drop_table('factor_model_audit_logs')

    op.drop_table('factor_system_config')

    op.drop_index('ix_factor_runtime_state_active_model_run_id', table_name='factor_runtime_state')
    op.drop_index('ix_factor_runtime_state_weight_mode', table_name='factor_runtime_state')
    op.drop_table('factor_runtime_state')
