"""Explicit candidate sync journal; no Base/startup/production migration hook."""
from sqlalchemy import MetaData, Table, String, Integer, ForeignKeyConstraint
from app.db.governance_schema import metadata as previous, HASH, DOCUMENT, UTC, col

metadata = MetaData()
for t in previous.tables.values(): t.to_metadata(metadata)
jobs = Table('governance_sync_jobs', metadata,
    col('job_id', HASH, pk=True), col('spec_json', DOCUMENT), col('spec_hash', HASH),
    col('status', String(16)), col('attempts', Integer), col('cancel_requested', Integer),
    col('lease_token', HASH, null=True), col('lease_until', UTC, null=True),
    col('result_json', DOCUMENT, null=True), col('created_at', UTC), col('updated_at', UTC),
    mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_bin')
attempts = Table('governance_sync_attempts', metadata,
    col('job_id', HASH, pk=True), col('attempt_no', Integer, pk=True), col('lease_token', HASH),
    col('status', String(16)), col('started_at', UTC), col('lease_until', UTC), col('finished_at', UTC, null=True),
    col('result_json', DOCUMENT, null=True), ForeignKeyConstraint(['job_id'], ['governance_sync_jobs.job_id']),
    mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_bin')
