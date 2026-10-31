"""Additive candidate index identities and bitemporal composition inputs."""
from sqlalchemy import MetaData,Table,String,Date
from app.db.governance_schema import metadata as previous,HASH,DOCUMENT,BLOB,UTC,col,fk
metadata=MetaData()
for t in previous.tables.values():t.to_metadata(metadata)
def table(name,*columns):return Table(name,metadata,*columns,mysql_engine='InnoDB',mysql_charset='utf8mb4',mysql_collate='utf8mb4_bin')
indices=table('governance_index_versions',col('index_version',HASH,pk=True),col('index_id',String(64)),
    col('document_json',DOCUMENT),col('source_evidence',BLOB),col('review_evidence',BLOB),col('available_at',UTC))
compositions=table('index_composition_versions',col('composition_version',HASH,pk=True),col('scope_key',HASH),
    col('index_version',HASH),col('product',String(24)),col('document_json',DOCUMENT),col('source_evidence',BLOB),
    col('event_date',Date),col('valid_from',Date),col('valid_to',Date,null=True),col('published_at',UTC,null=True),
    col('public_available_at',UTC,null=True),col('observed_at',UTC),col('quality_status',String(16)),
    fk(['index_version'],'governance_index_versions',['index_version'],name='fk_index_composition_master'))
acceptance=table('index_composition_acceptance',col('composition_version',HASH,pk=True),col('scope_key',HASH),
    col('available_at',UTC),col('system_to',UTC,null=True),col('review_evidence',BLOB),
    fk(['composition_version'],'index_composition_versions',['composition_version'],name='fk_index_composition_acceptance'))
heads=table('index_composition_heads',col('scope_key',HASH,pk=True),col('composition_version',HASH),
    fk(['composition_version'],'index_composition_versions',['composition_version'],name='fk_index_composition_head'))
