"""R4 additive candidate tables; base R2/R3 tables retain their exact shape."""
from sqlalchemy import MetaData,Table,String,JSON,UniqueConstraint
from app.db.governance_schema import metadata as base_metadata,HASH,UTC,BIGINT,DOCUMENT,col,fk
metadata=MetaData()
for t in base_metadata.tables.values():t.to_metadata(metadata)

def table(name,*columns):
    return Table(name,metadata,*columns,mysql_engine='InnoDB',mysql_charset='utf8mb4',mysql_collate='utf8mb4_bin')

policy_heads=table('source_policy_heads',col('scope_key',HASH,pk=True),col('policy_version',HASH),col('revision',BIGINT),
    fk(['policy_version'],'source_policies',['policy_version'],name='fk_source_policy_head'))
candidates=table('source_candidates',col('dataset',String(32),pk=True),col('logical_key',HASH,pk=True),col('data_version',HASH,pk=True),
    col('candidate_state',String(32)),col('policy_version',HASH),col('updated_at',UTC),
    fk(['dataset','logical_key','data_version'],'fact_versions',['dataset','logical_key','data_version'],name='fk_source_candidate_fact'),
    fk(['policy_version'],'source_policies',['policy_version'],name='fk_source_candidate_policy'))
transitions=table('source_transitions',col('transition_id',HASH,pk=True),col('dataset',String(32)),col('logical_key',HASH),
    col('sequence_no',BIGINT),UniqueConstraint('dataset','logical_key','sequence_no',name='uq_source_transition_sequence'),
    col('policy_version',HASH),col('event',String(32)),col('candidate_version',HASH,null=True),col('decision_id',HASH,null=True),
    col('previous_version',HASH,null=True),col('expected_revision',BIGINT),col('evidence_hash',HASH),col('happened_at',UTC),col('result_json',JSON),
    fk(['policy_version'],'source_policies',['policy_version'],name='fk_source_transition_policy'),
    fk(['dataset','logical_key','candidate_version'],'fact_versions',['dataset','logical_key','data_version'],name='fk_source_transition_fact'),
    fk(['decision_id'],'source_decisions',['decision_id'],name='fk_source_transition_decision'),
    fk(['evidence_hash'],'raw_evidence',['raw_hash'],name='fk_source_transition_evidence'))
recoveries=table('recovery_items',col('dataset',String(32),pk=True),col('logical_key',HASH,pk=True),col('backup_data_version',HASH,pk=True),
    col('policy_version',HASH),col('status',String(16)),col('attempts',BIGINT),col('next_attempt_at',UTC),col('last_error',DOCUMENT,null=True),
    col('candidate_version',HASH,null=True),col('lease_until',UTC,null=True),col('lease_owner',String(128),null=True),col('lease_token',HASH,null=True),
    col('completed_at',UTC,null=True),col('created_at',UTC),col('escalated_at',UTC,null=True),
    fk(['dataset','logical_key','backup_data_version'],'fact_versions',['dataset','logical_key','data_version'],name='fk_recovery_backup'),
    fk(['dataset','logical_key','candidate_version'],'fact_versions',['dataset','logical_key','data_version'],name='fk_recovery_candidate'),
    fk(['policy_version'],'source_policies',['policy_version'],name='fk_recovery_policy'))
legacy=table('legacy_candidates',col('migration_id',String(128),pk=True),col('legacy_key',String(256),pk=True),
    col('raw_hash',HASH),col('state',String(16)),col('claimed_version',HASH,null=True),col('mapping_evidence_hash',HASH,null=True),col('created_at',UTC),
    fk(['raw_hash'],'raw_evidence',['raw_hash'],name='fk_legacy_raw'),
    fk(['claimed_version'],'fact_versions',['data_version'],name='fk_legacy_claim'),
    fk(['mapping_evidence_hash'],'raw_evidence',['raw_hash'],name='fk_legacy_mapping'))
