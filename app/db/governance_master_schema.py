"""M05 master tables, intentionally separate from live Base auto-discovery.

Initialization is explicit. M05.2 owns installation/migration with the remaining
fact schema; importing the app or this module never creates these tables.
"""
from sqlalchemy import BigInteger, Column, Date, DateTime, ForeignKey, Integer, LargeBinary, MetaData, String, Table, Text, event, insert, inspect, select
from sqlalchemy.dialects.mysql import DATETIME, MEDIUMBLOB, LONGTEXT, VARCHAR

metadata=MetaData()
BIGINT=BigInteger().with_variant(Integer,'sqlite')
control=Table('governance_master_control',metadata,
    Column('id',Integer,primary_key=True),Column('next_instrument_id',BIGINT,nullable=False),
    Column('instrument_head',String(64)),mysql_engine='InnoDB',mysql_charset='utf8mb4')
instruments=Table('instrument_master',metadata,
    Column('instrument_id',BIGINT,primary_key=True,autoincrement=False),
    Column('identity_key',String(191).with_variant(VARCHAR(191,collation='utf8mb4_bin'),'mysql'),nullable=False,unique=True),
    Column('asset_type',String(8),nullable=False),Column('exchange',String(16),nullable=False),mysql_engine='InnoDB',mysql_charset='utf8mb4')
versions=Table('master_data_versions',metadata,
    Column('version',String(64),primary_key=True),Column('kind',String(16),nullable=False),
    Column('document_json',Text().with_variant(LONGTEXT(),'mysql'),nullable=False),
    Column('evidence',LargeBinary().with_variant(MEDIUMBLOB(),'mysql'),nullable=False),
    Column('observed_at',DateTime().with_variant(DATETIME(fsp=6),'mysql'),nullable=False),
    Column('parent_version',String(64),ForeignKey('master_data_versions.version',ondelete='RESTRICT')),mysql_engine='InnoDB',mysql_charset='utf8mb4')
members=Table('instrument_version_members',metadata,
    Column('instrument_version',String(64),ForeignKey('master_data_versions.version',ondelete='RESTRICT'),primary_key=True),
    Column('instrument_id',BIGINT,ForeignKey('instrument_master.instrument_id',ondelete='RESTRICT'),primary_key=True),mysql_engine='InnoDB',mysql_charset='utf8mb4')
aliases=Table('instrument_alias',metadata,
    Column('instrument_version',String(64),ForeignKey('master_data_versions.version',ondelete='RESTRICT'),primary_key=True),
    Column('legacy_table',String(32),primary_key=True),Column('legacy_id',BIGINT,primary_key=True),Column('valid_from',Date,primary_key=True),
    Column('valid_to',Date),Column('instrument_id',BIGINT,ForeignKey('instrument_master.instrument_id',ondelete='RESTRICT'),nullable=False),
    Column('exchange',String(16),nullable=False),Column('code',String(32),nullable=False),mysql_engine='InnoDB',mysql_charset='utf8mb4')


def initialize_master_schema(engine):
    """Explicit bootstrap for a new isolated candidate, not a live migration."""
    inspector=inspect(engine)
    existing=set(inspector.get_table_names())
    if inspector.get_view_names() or existing and existing!=set(metadata.tables):raise ValueError('MASTER_BOOTSTRAP_SCOPE')
    if existing and any({c['name'] for c in inspector.get_columns(name)}!=set(table.c.keys()) for name,table in metadata.tables.items()):
        raise ValueError('MASTER_BOOTSTRAP_SCHEMA_MISMATCH')
    if engine.dialect.name=='sqlite':
        def foreign_keys(connection,record):connection.execute('PRAGMA foreign_keys=ON')
        event.listen(engine,'connect',foreign_keys)
    metadata.create_all(engine)
    with engine.begin() as db:
        if engine.dialect.name=='sqlite':db.exec_driver_sql('PRAGMA foreign_keys=ON')
        if db.execute(select(control.c.id)).first() is None:
            db.execute(insert(control).values(id=1,next_instrument_id=1,instrument_head=None))
