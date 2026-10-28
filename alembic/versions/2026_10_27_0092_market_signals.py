"""Versioned market event subscriptions and immutable outcome evidence.

Create missing tables and widen legacy signal TEXT payloads. Downgrade retains
these tables, widened columns and their evidence:
deleting delivered predictions would destroy the audit trail. Re-upgrade is
idempotent. Physical cleanup, if ever required, is a separate explicit action.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "wps_0024_092_market_signals"
down_revision = "wps_0024_091_market_compat"
branch_labels = depends_on = None

_LONGTEXT_FIELDS = {
    "market_signal_evidence": ("text",),
    "market_signal_assessments": ("analysis_json", "input_json"),
    "market_signal_predictions": ("snapshot_json", "outcome_json"),
    "market_signal_outcomes": ("result_json",),
}


def _widen_existing_mysql_payloads():
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    inspector = sa.inspect(bind)
    for table, names in _LONGTEXT_FIELDS.items():
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        for name in names:
            column = columns[name]
            current = column["type"]
            if isinstance(current, mysql.LONGTEXT):
                continue
            if not isinstance(current, (sa.Text, mysql.TINYTEXT, mysql.MEDIUMTEXT)):
                raise RuntimeError(f"Refusing to convert non-text column {table}.{name}")
            op.alter_column(table, name,
                type_=mysql.LONGTEXT(charset=getattr(current, "charset", None),
                                    collation=getattr(current, "collation", None)),
                existing_type=current, existing_nullable=column["nullable"],
                existing_server_default=column.get("default"), existing_comment=column.get("comment"))


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table('market_signal_evidence'):
        op.create_table('market_signal_evidence',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('text', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('published_at', sa.DateTime(), nullable=True),
        sa.Column('time_quality', sa.String(length=24), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('metadata_json', sa.Text(), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('fingerprint'),
        mysql_engine='InnoDB'
        )
    if 'ix_market_signal_evidence_first_seen_at' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_evidence')}:
        op.create_index(op.f('ix_market_signal_evidence_first_seen_at'), 'market_signal_evidence', ['first_seen_at'], unique=False)
    if 'ix_market_signal_evidence_published_at' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_evidence')}:
        op.create_index(op.f('ix_market_signal_evidence_published_at'), 'market_signal_evidence', ['published_at'], unique=False)
    if not inspector.has_table('market_signal_rules'):
        op.create_table('market_signal_rules',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('target_type', sa.String(length=16), nullable=False),
        sa.Column('target_key', sa.String(length=64), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('config_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        mysql_engine='InnoDB'
        )
    if 'ix_market_signal_rules_target_key' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_rules')}:
        op.create_index(op.f('ix_market_signal_rules_target_key'), 'market_signal_rules', ['target_key'], unique=False)
    if 'ix_market_signal_rules_target_type' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_rules')}:
        op.create_index(op.f('ix_market_signal_rules_target_type'), 'market_signal_rules', ['target_type'], unique=False)
    if not inspector.has_table('market_signal_settings'):
        op.create_table('market_signal_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('config_json', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        mysql_engine='InnoDB'
        )
    if not inspector.has_table('market_signal_assessments'):
        op.create_table('market_signal_assessments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('evidence_id', sa.Integer(), nullable=False),
        sa.Column('rule_id', sa.Integer(), nullable=False),
        sa.Column('rule_revision', sa.Integer(), nullable=False),
        sa.Column('request_key', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('score', sa.Integer(), nullable=True),
        sa.Column('event_key', sa.String(length=64), nullable=True),
        sa.Column('fact_key', sa.String(length=64), nullable=True),
        sa.Column('analysis_json', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=False),
        sa.Column('input_json', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=False),
        sa.Column('model_name', sa.String(length=128), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=False),
        sa.Column('decision_reason', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['evidence_id'], ['market_signal_evidence.id'], ),
        sa.ForeignKeyConstraint(['rule_id'], ['market_signal_rules.id'], ),
        sa.PrimaryKeyConstraint('id'),
        mysql_engine='InnoDB'
        )
    if 'ix_market_signal_assessments_created_at' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_assessments')}:
        op.create_index(op.f('ix_market_signal_assessments_created_at'), 'market_signal_assessments', ['created_at'], unique=False)
    if 'ix_market_signal_assessments_event_key' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_assessments')}:
        op.create_index(op.f('ix_market_signal_assessments_event_key'), 'market_signal_assessments', ['event_key'], unique=False)
    if 'ix_market_signal_assessments_evidence_id' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_assessments')}:
        op.create_index(op.f('ix_market_signal_assessments_evidence_id'), 'market_signal_assessments', ['evidence_id'], unique=False)
    if 'ix_market_signal_assessments_request_key' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_assessments')}:
        op.create_index(op.f('ix_market_signal_assessments_request_key'), 'market_signal_assessments', ['request_key'], unique=False)
    if 'ix_market_signal_assessments_rule_id' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_assessments')}:
        op.create_index(op.f('ix_market_signal_assessments_rule_id'), 'market_signal_assessments', ['rule_id'], unique=False)
    if 'ix_market_signal_assessments_status' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_assessments')}:
        op.create_index(op.f('ix_market_signal_assessments_status'), 'market_signal_assessments', ['status'], unique=False)
    if not inspector.has_table('market_signal_rule_versions'):
        op.create_table('market_signal_rule_versions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('rule_id', sa.Integer(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('config_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['rule_id'], ['market_signal_rules.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('rule_id', 'revision', name='uq_ms_rule_version'),
        mysql_engine='InnoDB'
        )
    if 'ix_market_signal_rule_versions_rule_id' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_rule_versions')}:
        op.create_index(op.f('ix_market_signal_rule_versions_rule_id'), 'market_signal_rule_versions', ['rule_id'], unique=False)
    if not inspector.has_table('market_signal_predictions'):
        op.create_table('market_signal_predictions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('dedup_key', sa.String(length=64), nullable=False),
        sa.Column('event_key', sa.String(length=64), nullable=False),
        sa.Column('fact_key', sa.String(length=64), nullable=False),
        sa.Column('assessment_id', sa.Integer(), nullable=False),
        sa.Column('rule_id', sa.Integer(), nullable=False),
        sa.Column('rule_revision', sa.Integer(), nullable=False),
        sa.Column('target_type', sa.String(length=16), nullable=False),
        sa.Column('target_key', sa.String(length=64), nullable=False),
        sa.Column('delivery_status', sa.String(length=24), nullable=False),
        sa.Column('outbox_id', sa.Integer(), nullable=True),
        sa.Column('snapshot_json', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=False),
        sa.Column('outcome_status', sa.String(length=24), nullable=False),
        sa.Column('outcome_json', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('due_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['assessment_id'], ['market_signal_assessments.id'], ),
        sa.ForeignKeyConstraint(['rule_id'], ['market_signal_rules.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dedup_key'),
        mysql_engine='InnoDB'
        )
    if 'ix_market_signal_predictions_assessment_id' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_predictions')}:
        op.create_index(op.f('ix_market_signal_predictions_assessment_id'), 'market_signal_predictions', ['assessment_id'], unique=False)
    if 'ix_market_signal_predictions_created_at' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_predictions')}:
        op.create_index(op.f('ix_market_signal_predictions_created_at'), 'market_signal_predictions', ['created_at'], unique=False)
    if 'ix_market_signal_predictions_delivery_status' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_predictions')}:
        op.create_index(op.f('ix_market_signal_predictions_delivery_status'), 'market_signal_predictions', ['delivery_status'], unique=False)
    if 'ix_market_signal_predictions_due_at' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_predictions')}:
        op.create_index(op.f('ix_market_signal_predictions_due_at'), 'market_signal_predictions', ['due_at'], unique=False)
    if 'ix_market_signal_predictions_event_key' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_predictions')}:
        op.create_index(op.f('ix_market_signal_predictions_event_key'), 'market_signal_predictions', ['event_key'], unique=False)
    if 'ix_market_signal_predictions_outbox_id' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_predictions')}:
        op.create_index(op.f('ix_market_signal_predictions_outbox_id'), 'market_signal_predictions', ['outbox_id'], unique=False)
    if 'ix_market_signal_predictions_outcome_status' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_predictions')}:
        op.create_index(op.f('ix_market_signal_predictions_outcome_status'), 'market_signal_predictions', ['outcome_status'], unique=False)
    if 'ix_market_signal_predictions_rule_id' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_predictions')}:
        op.create_index(op.f('ix_market_signal_predictions_rule_id'), 'market_signal_predictions', ['rule_id'], unique=False)
    if 'ix_market_signal_predictions_target_key' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_predictions')}:
        op.create_index(op.f('ix_market_signal_predictions_target_key'), 'market_signal_predictions', ['target_key'], unique=False)
    if not inspector.has_table('market_signal_outcomes'):
        op.create_table('market_signal_outcomes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('prediction_id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('result_json', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['prediction_id'], ['market_signal_predictions.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('prediction_id', 'version', name='uq_ms_outcome_version'),
        mysql_engine='InnoDB'
        )
    if 'ix_market_signal_outcomes_prediction_id' not in {item['name'] for item in sa.inspect(op.get_bind()).get_indexes('market_signal_outcomes')}:
        op.create_index(op.f('ix_market_signal_outcomes_prediction_id'), 'market_signal_outcomes', ['prediction_id'], unique=False)

    # Older application starts may have created these tables from a previous
    # ORM version without advancing Alembic. Creation guards alone cannot fix it.
    _widen_existing_mysql_payloads()


def downgrade():
    # Retain new feature data and LONGTEXT capacity. Shrinking after users have
    # written large bodies would truncate data or make the rollback fail.
    pass
