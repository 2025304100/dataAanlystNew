"""Additive input manifest for the existing R3 factor/adjusted-price tables."""
from sqlalchemy import MetaData,Table,Column,ForeignKeyConstraint
from app.db.governance_source_schema import metadata as previous
from app.db.governance_schema import HASH,DOCUMENT

metadata=MetaData()
for t in previous.tables.values():t.to_metadata(metadata)
inputs=Table('adjustment_set_inputs',metadata,
    Column('adj_factor_version',HASH,primary_key=True),Column('manifest_json',DOCUMENT,nullable=False),
    Column('evidence_hash',HASH,nullable=False),
    ForeignKeyConstraint(['adj_factor_version'],['adjustment_sets.adj_factor_version'],name='fk_price_input_set',ondelete='RESTRICT'),
    ForeignKeyConstraint(['evidence_hash'],['raw_evidence.raw_hash'],name='fk_price_input_evidence',ondelete='RESTRICT'),
    mysql_engine='InnoDB',mysql_charset='utf8mb4',mysql_collate='utf8mb4_bin')
