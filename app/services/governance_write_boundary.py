"""Explicit candidate authority and read-only compatibility relations.

No default engine, live legacy table replacement, credentials or startup hooks.
"""
import json
from sqlalchemy import Column,Integer,JSON,String,Table,MetaData,select,insert,update,inspect
from app.db import governance_source_schema as source
from app.db.governance_migration import _candidate,migrate_candidate
from app.services.governance_source_state import migrate_source_state
from app.services.governance_fact_contracts import WriterError,hash_object
from app.services.governance_master_data import MasterDataService,in_range
from datetime import date

CANONICAL_ORIGIN='universe.fact_versions'
VIEWS=('governance_market_facts','governance_daily_bars_projection','governance_universe_daily_bars_projection')
metadata=MetaData()
for t in source.metadata.tables.values():t.to_metadata(metadata)
control=Table('governance_boundary_control',metadata,Column('id',Integer,primary_key=True),Column('state',String(16),nullable=False),
    Column('instrument_version',String(64),nullable=False),Column('view_hashes',JSON,nullable=False),mysql_engine='InnoDB',mysql_charset='utf8mb4')


def _view_hash(engine,name):
    definition=inspect(engine).get_view_definition(name)
    return hash_object('boundary-view-v1',{'name':name,'definition':definition})


def install_boundary(engine,*,instrument_version):
    _candidate(engine)
    MasterDataService(engine).instrument_snapshot(instrument_version)
    migrate_source_state(engine)
    migrate_candidate(engine,metadata=metadata,revision='DSG-WRITE-001')
    with engine.begin() as db:
        old=db.execute(select(control)).mappings().first()
        if old and old['instrument_version']!=instrument_version:raise WriterError('BOUNDARY_PIN_IMMUTABLE')
        if not old:db.execute(insert(control).values(id=1,state='building',instrument_version=instrument_version,view_hashes={}))
    hashes=dict(old['view_hashes']) if old else {}
    mysql=engine.dialect.name=='mysql'
    def field(document,key):
        expression=f"JSON_EXTRACT({document}, '$.{key}')"
        return f"NULLIF(JSON_UNQUOTE({expression}), 'null')" if mysql else expression
    columns=["c.dataset","c.logical_key","c.data_version","c.pointer_revision","c.source_state","c.quality_status","c.quality_profile",
        f"CAST({field('v.key_json','instrument_id')} AS {'UNSIGNED' if mysql else 'INTEGER'}) AS instrument_id",
        "v.event_date AS trade_date","v.source_tag","v.revision_kind","a.available_at",f"'{CANONICAL_ORIGIN}' AS source_origin"]
    columns += [field('v.payload_json',name)+' AS '+name for name in ('raw_open','raw_high','raw_low','raw_close','volume_shares','amount_cny','turnover_rate_pct')]
    core='SELECT '+', '.join(columns)+' FROM fact_current c JOIN fact_versions v ON c.dataset=v.dataset AND c.logical_key=v.logical_key AND c.data_version=v.data_version JOIN version_availability a ON a.data_version=v.data_version '+"WHERE c.dataset='daily_bars' AND c.source_state IN ('canonical','backup_temp')"
    for name in VIEWS:
        objects=inspect(engine)
        if name in objects.get_table_names():raise WriterError('BOUNDARY_OBJECT_COLLISION')
        if name in objects.get_view_names():
            if hashes.get(name)!=_view_hash(engine,name):raise WriterError('BOUNDARY_VIEW_DRIFT')
        else:
            if name in hashes:raise WriterError('BOUNDARY_VIEW_DRIFT')
            body=core if name==VIEWS[0] else 'SELECT * FROM '+VIEWS[0]
            with engine.begin() as db:db.exec_driver_sql(('CREATE SQL SECURITY DEFINER VIEW ' if mysql else 'CREATE VIEW ')+name+' AS '+body)
            hashes[name]=_view_hash(engine,name)
            with engine.begin() as db:db.execute(update(control).where(control.c.id==1).values(view_hashes=hashes))
    with engine.begin() as db:db.execute(update(control).where(control.c.id==1).values(state='ready'))
    return {'origin':CANONICAL_ORIGIN,'instrument_version':instrument_version,'views':list(VIEWS)}


class MarketWriteBoundary:
    def __init__(self,source_service,*,reader_engine=None):
        self.service=source_service;self.engine=reader_engine if reader_engine is not None else source_service.engine
        _candidate(self.engine);self._check()

    def _check(self):
        with self.engine.connect() as db:row=db.execute(select(control)).mappings().one()
        if row['state']!='ready' or set(row['view_hashes'])!=set(VIEWS):raise WriterError('BOUNDARY_NOT_READY')
        if any(_view_hash(self.engine,name)!=row['view_hashes'][name] for name in VIEWS):raise WriterError('BOUNDARY_VIEW_DRIFT')
        MasterDataService(self.engine).instrument_snapshot(row['instrument_version'])
        return row

    def submit(self,request,*,origin,**decision):
        if origin!=CANONICAL_ORIGIN:raise WriterError('NON_CANONICAL_ORIGIN')
        pin=self._check()
        if request.get('instrument_version')!=pin['instrument_version']:raise WriterError('BOUNDARY_PIN_MISMATCH')
        if request.get('dataset')!='daily_bars':raise WriterError('BOUNDARY_DATASET_MISMATCH')
        return self.service.submit(request,**decision)

    def projection(self,name):
        if name not in {'daily_bars','universe_daily_bars'}:raise WriterError('PROJECTION_UNKNOWN')
        pin=self._check();view=VIEWS[1] if name=='daily_bars' else VIEWS[2]
        with self.engine.connect() as db:
            rows=[dict(r) for r in db.exec_driver_sql('SELECT * FROM '+view+' ORDER BY instrument_id, trade_date').mappings()]
        missing=[{'instrument_id':r['instrument_id'],'trade_date':r['trade_date'],'data_version':r['data_version'],'reason':'RETRACTED'} for r in rows if r['revision_kind']=='withdrawal']
        snapshot=MasterDataService(self.engine).instrument_snapshot(pin['instrument_version'])
        namespace='symbols' if name=='daily_bars' else 'universe_symbols';aliases=[];unmapped=[]
        for row in rows:
            on=row['trade_date'] if type(row['trade_date']) is date else date.fromisoformat(row['trade_date'])
            matches=[a for a in snapshot['aliases'] if a['legacy_table']==namespace and a['instrument_id']==row['instrument_id'] and in_range(a,on)]
            identity={k:row[k] for k in ('instrument_id','trade_date','data_version','logical_key')}
            if matches:
                aliases.extend({**identity,'legacy_table':namespace,'legacy_id':a['legacy_id'],'code':a['code']} for a in matches)
            else:unmapped.append({**identity,'legacy_table':namespace,'reason':'LEGACY_ALIAS_UNMAPPED'})
        return {'origin':CANONICAL_ORIGIN,'instrument_version':pin['instrument_version'],'rows':[r for r in rows if r['revision_kind']!='withdrawal'],'missing':missing,'aliases':aliases,'missing_aliases':unmapped}
