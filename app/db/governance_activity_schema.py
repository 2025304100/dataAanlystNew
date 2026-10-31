"""Additive candidate receipt ledger; original fact schema remains unchanged."""
from sqlalchemy import MetaData,Table
from app.db.governance_schema import metadata as previous,HASH,DOCUMENT,BLOB,UTC,col

metadata=MetaData()
for t in previous.tables.values():t.to_metadata(metadata)
receipts=Table('activity_sync_receipts',metadata,
    col('receipt_id',HASH,pk=True),col('receipt_json',DOCUMENT),col('capture_bytes',BLOB),
    col('empty_proof',BLOB,null=True),col('contract_document',DOCUMENT),col('contract_evidence',BLOB),col('observed_at',UTC),
    mysql_engine='InnoDB',mysql_charset='utf8mb4',mysql_collate='utf8mb4_bin')
