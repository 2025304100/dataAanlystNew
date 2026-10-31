"""Additive reviewed candidate total-return inputs; no production bootstrap."""
from sqlalchemy import MetaData,Table
from app.db.governance_schema import metadata as previous,HASH,DOCUMENT,BLOB,UTC,col
metadata=MetaData()
for t in previous.tables.values():t.to_metadata(metadata)
return_inputs=Table('etf_return_inputs',metadata,col('series_version',HASH,pk=True),col('document_json',DOCUMENT),
    col('source_evidence',BLOB),col('review_evidence',BLOB),col('available_at',UTC),
    mysql_engine='InnoDB',mysql_charset='utf8mb4',mysql_collate='utf8mb4_bin')
