"""R2/R3 candidate schema. Deliberately absent from Base and normal Alembic heads.

Only the explicit candidate migration installs these tables. JSON payloads store
canonical decimal strings; derived price/factor columns use the R1 SQL decimals.
Application writer validation (M05.3/4) is required in addition to SQL constraints.
"""
from sqlalchemy import (
    CHAR, JSON, BigInteger, Column, Date, DateTime, ForeignKeyConstraint,
    Integer, LargeBinary, MetaData, Numeric, String, Table, Text, UniqueConstraint,
)
from sqlalchemy.dialects import mysql

from app.db.governance_master_schema import metadata as master_metadata

metadata = MetaData()
for _table in master_metadata.tables.values():
    _table.to_metadata(metadata)

HASH = CHAR(64).with_variant(mysql.CHAR(64, charset='ascii', collation='ascii_bin'), 'mysql')
UTC = DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql')
BIGINT = BigInteger().with_variant(Integer(), 'sqlite')
DOCUMENT = Text().with_variant(mysql.LONGTEXT(), 'mysql')
BLOB = LargeBinary().with_variant(mysql.LONGBLOB(), 'mysql')


def table(name, *columns):
    return Table(name, metadata, *columns, mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_bin')


def col(name, kind, *, null=False, pk=False):
    return Column(name, kind, nullable=null, primary_key=pk, autoincrement=False)


def fk(columns, target, targets, *, name):
    return ForeignKeyConstraint(columns, [target + '.' + c for c in targets], name=name, ondelete='RESTRICT', onupdate='RESTRICT')


raw_evidence = table('raw_evidence',
    col('raw_hash', HASH, pk=True), col('raw_bytes', BLOB), col('source_uri', DOCUMENT), col('observed_at', UTC))
fact_keys = table('fact_keys',
    col('dataset', String(32), pk=True), col('logical_key', HASH, pk=True), col('key_json', DOCUMENT))
fact_versions = table('fact_versions',
    col('data_version', HASH, pk=True), col('dataset', String(32)), col('logical_key', HASH),
    col('key_json', DOCUMENT), col('payload_json', DOCUMENT), col('raw_hash', HASH),
    col('contract_version', HASH), col('definition_id', HASH), col('source_tag', String(128)), col('field_sources', JSON),
    col('event_date', Date), col('published_at', UTC, null=True), col('publication_date', Date, null=True),
    col('publication_precision', String(8)), col('public_available_at', UTC, null=True), col('observed_at', UTC),
    col('valid_from', Date), col('valid_to', Date, null=True), col('revision_kind', String(16)),
    col('supersedes_version', HASH, null=True), col('publication_evidence_hash', HASH, null=True),
    col('source_revision_token', String(256)), col('normalized_payload_hash', HASH),
    col('source_revision_order', JSON, null=True), col('source_revision_evidence_hash', HASH, null=True),
    UniqueConstraint('dataset', 'logical_key', 'data_version', name='uq_fact_version_binding'),
    fk(['dataset', 'logical_key'], 'fact_keys', ['dataset', 'logical_key'], name='fk_fact_key'),
    fk(['raw_hash'], 'raw_evidence', ['raw_hash'], name='fk_fact_raw'),
    fk(['dataset', 'logical_key', 'supersedes_version'], 'fact_versions', ['dataset', 'logical_key', 'data_version'], name='fk_fact_parent'))

source_decisions = table('source_decisions',
    col('decision_id', HASH, pk=True), col('dataset', String(32)), col('logical_key', HASH),
    col('candidate_version', HASH), col('previous_version', HASH, null=True),
    col('event', String(32)), col('from_state', String(16)), col('to_state', String(16)),
    col('actor', String(128)), col('evidence_hash', HASH), col('decided_at', UTC),
    col('expected_pointer_revision', BIGINT), col('result', String(32)),
    UniqueConstraint('decision_id', 'candidate_version', name='uq_decision_candidate'),
    UniqueConstraint('dataset', 'logical_key', 'decision_id', name='uq_decision_key'),
    fk(['dataset', 'logical_key', 'candidate_version'], 'fact_versions', ['dataset', 'logical_key', 'data_version'], name='fk_decision_candidate'),
    fk(['dataset', 'logical_key', 'previous_version'], 'fact_versions', ['dataset', 'logical_key', 'data_version'], name='fk_decision_previous'))
version_availability = table('version_availability',
    col('data_version', HASH, pk=True), col('available_at', UTC), col('decision_id', HASH),
    fk(['data_version'], 'fact_versions', ['data_version'], name='fk_availability_version'),
    fk(['decision_id', 'data_version'], 'source_decisions', ['decision_id', 'candidate_version'], name='fk_availability_decision'))
fact_current = table('fact_current',
    col('dataset', String(32), pk=True), col('logical_key', HASH, pk=True), col('data_version', HASH),
    col('pointer_revision', BIGINT), col('source_state', String(16)), col('quality_profile', String(64)),
    col('quality_status', String(16)), col('provenance_mode', String(16)), col('updated_at', UTC),
    fk(['dataset', 'logical_key', 'data_version'], 'fact_versions', ['dataset', 'logical_key', 'data_version'], name='fk_current_binding'),
    fk(['data_version'], 'version_availability', ['data_version'], name='fk_current_available'))
acceptance_history = table('acceptance_history',
    col('dataset', String(32), pk=True), col('logical_key', HASH, pk=True), col('pointer_revision', BIGINT, pk=True),
    col('data_version', HASH), col('source_state', String(16)), col('quality_status', String(16)),
    col('system_from', UTC), col('system_to', UTC, null=True), col('decision_id', HASH),
    fk(['dataset', 'logical_key', 'data_version'], 'fact_versions', ['dataset', 'logical_key', 'data_version'], name='fk_acceptance_version'),
    fk(['data_version'], 'version_availability', ['data_version'], name='fk_acceptance_available'),
    fk(['dataset', 'logical_key', 'decision_id'], 'source_decisions', ['dataset', 'logical_key', 'decision_id'], name='fk_acceptance_decision'))

write_receipts = table('write_receipts',
    col('idempotency_key', HASH, pk=True), col('write_token', HASH), col('dataset', String(32)), col('logical_key', HASH),
    col('data_version', HASH), col('operation_kind', String(32)), col('decision_id', HASH), col('created_at', UTC), col('result_json', JSON),
    UniqueConstraint('write_token', name='uq_write_token'),
    fk(['dataset', 'logical_key', 'data_version'], 'fact_versions', ['dataset', 'logical_key', 'data_version'], name='fk_receipt_version'),
    fk(['decision_id', 'data_version'], 'source_decisions', ['decision_id', 'candidate_version'], name='fk_receipt_decision'))
fact_observations = table('fact_observations',
    col('observation_id', HASH, pk=True), col('data_version', HASH), col('raw_hash', HASH),
    col('observed_at', UTC), col('batch_id', String(128)),
    fk(['data_version'], 'fact_versions', ['data_version'], name='fk_observation_version'),
    fk(['raw_hash'], 'raw_evidence', ['raw_hash'], name='fk_observation_raw'))
governance_outbox = table('governance_outbox',
    col('outbox_id', HASH, pk=True), col('write_token', HASH), col('version_ids', JSON),
    col('source_high_water', JSON), col('state', String(16)), col('created_at', UTC), col('delivered_at', UTC, null=True),
    fk(['write_token'], 'write_receipts', ['write_token'], name='fk_outbox_receipt'))
outbox_members = table('outbox_members',
    col('outbox_id', HASH, pk=True), col('data_version', HASH, pk=True),
    fk(['outbox_id'], 'governance_outbox', ['outbox_id'], name='fk_outbox_member_message'),
    fk(['data_version'], 'fact_versions', ['data_version'], name='fk_outbox_member_version'))

adjustment_sets = table('adjustment_sets',
    col('adj_factor_version', HASH, pk=True), col('instrument_id', BIGINT), col('base_date', Date), col('as_of', UTC),
    col('knowledge_mode', String(16)), col('algorithm_version', String(64)), col('event_version_ids', JSON), col('factor_rows_hash', HASH),
    UniqueConstraint('adj_factor_version', 'base_date', name='uq_adjustment_base'),
    fk(['instrument_id'], 'instrument_master', ['instrument_id'], name='fk_adjustment_instrument'))
adjustment_factor_rows = table('adjustment_factor_rows',
    col('adj_factor_version', HASH, pk=True), col('trade_date', Date, pk=True), col('factor', Numeric(28, 16)),
    fk(['adj_factor_version'], 'adjustment_sets', ['adj_factor_version'], name='fk_factor_set'))
adjusted_bars = table('adjusted_bars',
    col('raw_data_version', HASH, pk=True), col('adj_factor_version', HASH, pk=True),
    col('instrument_id', BIGINT), col('trade_date', Date), col('base_date', Date),
    *(col('adj_' + name, Numeric(24, 8)) for name in ('open', 'high', 'low', 'close')),
    fk(['raw_data_version'], 'fact_versions', ['data_version'], name='fk_adjusted_raw'),
    fk(['adj_factor_version', 'base_date'], 'adjustment_sets', ['adj_factor_version', 'base_date'], name='fk_adjusted_base'),
    fk(['instrument_id'], 'instrument_master', ['instrument_id'], name='fk_adjusted_instrument'))

source_policies = table('source_policies',
    col('policy_version', HASH, pk=True), col('dataset', String(32)), col('quality_profile', String(64)),
    col('asset_type', String(16)), col('instrument_family', String(64)), col('definition_id', HASH), col('members', JSON),
    col('comparison_policy', JSON), col('effective_from', UTC), col('effective_to', UTC, null=True),
    col('evidence_hash', HASH), col('schema_version', String(32)))
dataset_versions = table('dataset_versions',
    col('dataset_version', HASH, pk=True), col('dataset', String(32)), col('member_hash', HASH),
    col('acceptance_history_hash', HASH), col('contract_versions', JSON), col('source_policy_versions', JSON), col('created_at', UTC))
dataset_members = table('dataset_members',
    col('dataset_version', HASH, pk=True), col('data_version', HASH, pk=True),
    fk(['dataset_version'], 'dataset_versions', ['dataset_version'], name='fk_dataset_member_set'),
    fk(['data_version'], 'fact_versions', ['data_version'], name='fk_dataset_member_version'))
snapshot_manifest = table('snapshot_manifest',
    col('snapshot_id', HASH, pk=True), col('manifest_json', DOCUMENT), col('manifest_hash', HASH),
    col('state', String(16)), col('created_at', UTC), col('published_at', UTC, null=True))
snapshot_datasets = table('snapshot_datasets',
    col('snapshot_id', HASH, pk=True), col('dataset', String(32), pk=True), col('dataset_version', HASH),
    fk(['snapshot_id'], 'snapshot_manifest', ['snapshot_id'], name='fk_snapshot_dataset_manifest'),
    fk(['dataset_version'], 'dataset_versions', ['dataset_version'], name='fk_snapshot_dataset_version'))
snapshot_pointer = table('snapshot_pointer',
    col('channel', String(16), pk=True), col('snapshot_id', HASH), col('pointer_revision', BIGINT),
    fk(['snapshot_id'], 'snapshot_manifest', ['snapshot_id'], name='fk_snapshot_pointer'))

schema_migrations = table('governance_schema_migrations',
    col('revision', String(32), pk=True), col('schema_hash', HASH), col('state', String(16)),
    col('started_at', UTC), col('completed_at', UTC, null=True), col('error_code', String(128), null=True))
