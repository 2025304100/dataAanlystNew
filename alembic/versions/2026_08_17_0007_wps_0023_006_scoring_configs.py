"""wps_0023_006_scoring_configs.

Revision ID: wps_0023_006_scoring_configs
Revises: wps_0023_005_trade_setups
Create Date: 2026-08-17 00:07:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_006_scoring_configs'
down_revision = 'wps_0023_005_trade_setups'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'factor_versions',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('factor_id', Integer, ForeignKey('factors.id', ondelete='CASCADE'), nullable=False),
        Column('version', Integer, nullable=False),
        Column('formula_expr', Text, nullable=False),
        Column('params_json', Text, nullable=False, server_default='{}'),
        Column('direction', String(32), nullable=False, server_default='higher_better'),
        Column('source_mapping_json', Text, nullable=False, server_default='{}'),
        Column('effective_from', DateTime, nullable=False),
        Column('change_note', Text, nullable=False, server_default=''),
        Column('is_latest', Integer, nullable=False, server_default='1'),
        Column('formula_ast_json', Text, nullable=True),
        Column('postprocess_json', Text, nullable=True),
        Column('parameter_schema_json', Text, nullable=True),
        Column('data_dependencies_json', Text, nullable=True),
        Column('compiler_version', String(32), nullable=True),
        Column('execution_plan_hash', String(64), nullable=True),
        Column('complexity_score', Float, nullable=True),
        Column('created_by', String(128), nullable=True),
        Column('created_via', String(32), nullable=True),
        Column('ai_provenance_json', Text, nullable=True),
        Column('validation_status', String(32), nullable=True),
        Column('validation_errors_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('factor_id', 'version', name='uq_factor_version'),
    )
    op.create_index('ix_factor_versions_factor_id', 'factor_versions', ['factor_id'])
    op.create_index('ix_factor_versions_is_latest', 'factor_versions', ['is_latest'])
    op.create_index('ix_factor_versions_execution_plan_hash', 'factor_versions', ['execution_plan_hash'])
    op.create_index('ix_factor_versions_validation_status', 'factor_versions', ['validation_status'])

    op.create_table(
        'factor_model_runs',
        Column('id', String(64), primary_key=True),
        Column('model_type', String(32), nullable=False, server_default='ridge'),
        Column('asset_type', String(16), nullable=False, server_default='stock'),
        Column('target_code', String(64), nullable=False, server_default='target_5d_return'),
        Column('train_start_date', String(32), nullable=True),
        Column('train_end_date', String(32), nullable=True),
        Column('validation_start_date', String(32), nullable=True),
        Column('validation_end_date', String(32), nullable=True),
        Column('data_cutoff_at', DateTime, nullable=True),
        Column('feature_versions_json', Text, nullable=False, server_default='{}'),
        Column('hyperparameters_json', Text, nullable=False, server_default='{}'),
        Column('metrics_json', Text, nullable=False, server_default='{}'),
        Column('sample_count', Integer, nullable=False, server_default='0'),
        Column('symbol_count', Integer, nullable=False, server_default='0'),
        Column('trade_date_count', Integer, nullable=False, server_default='0'),
        Column('status', String(24), nullable=False, server_default='training'),
        Column('rejection_reason', Text, nullable=True),
        Column('artifact_path', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('activated_at', DateTime, nullable=True),
    )
    op.create_index('ix_factor_model_runs_model_type', 'factor_model_runs', ['model_type'])
    op.create_index('ix_factor_model_runs_asset_type', 'factor_model_runs', ['asset_type'])
    op.create_index('ix_factor_model_runs_data_cutoff_at', 'factor_model_runs', ['data_cutoff_at'])
    op.create_index('ix_factor_model_runs_status', 'factor_model_runs', ['status'])
    op.create_index('ix_factor_model_runs_created_at', 'factor_model_runs', ['created_at'])

    op.create_table(
        'factor_weight_snapshots',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('model_run_id', String(64), ForeignKey('factor_model_runs.id', ondelete='CASCADE'), nullable=False),
        Column('factor_code', String(64), nullable=False),
        Column('factor_version', Integer, nullable=False),
        Column('coefficient', Float, nullable=False),
        Column('normalized_weight', Float, nullable=False),
        Column('train_ic', Float, nullable=True),
        Column('validation_ic', Float, nullable=True),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint('model_run_id', 'factor_code', 'factor_version', name='uq_factor_model_weight'),
    )
    op.create_index('ix_factor_weight_snapshots_model_run_id', 'factor_weight_snapshots', ['model_run_id'])
    op.create_index('ix_factor_weight_snapshots_factor_code', 'factor_weight_snapshots', ['factor_code'])


def downgrade() -> None:
    op.drop_index('ix_factor_weight_snapshots_factor_code', table_name='factor_weight_snapshots')
    op.drop_index('ix_factor_weight_snapshots_model_run_id', table_name='factor_weight_snapshots')
    op.drop_table('factor_weight_snapshots')

    op.drop_index('ix_factor_model_runs_created_at', table_name='factor_model_runs')
    op.drop_index('ix_factor_model_runs_status', table_name='factor_model_runs')
    op.drop_index('ix_factor_model_runs_data_cutoff_at', table_name='factor_model_runs')
    op.drop_index('ix_factor_model_runs_asset_type', table_name='factor_model_runs')
    op.drop_index('ix_factor_model_runs_model_type', table_name='factor_model_runs')
    op.drop_table('factor_model_runs')

    op.drop_index('ix_factor_versions_validation_status', table_name='factor_versions')
    op.drop_index('ix_factor_versions_execution_plan_hash', table_name='factor_versions')
    op.drop_index('ix_factor_versions_is_latest', table_name='factor_versions')
    op.drop_index('ix_factor_versions_factor_id', table_name='factor_versions')
    op.drop_table('factor_versions')
