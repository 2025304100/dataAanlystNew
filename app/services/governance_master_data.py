"""Version-pinned master identities; no implicit legacy merge or network I/O."""
from datetime import date,datetime,timedelta,timezone
import hashlib,json,re,unicodedata
from sqlalchemy import insert,select,update
from app.db import governance_master_schema as schema


class MasterDataError(ValueError):
    pass


def canonical(value):
    def normalize(v):
        if isinstance(v,str):return unicodedata.normalize('NFC',v)
        if isinstance(v,list):return [normalize(x) for x in v]
        if isinstance(v,dict):return {normalize(k):normalize(x) for k,x in v.items()}
        if v is None or type(v) in (int,bool):return v
        raise MasterDataError('NON_CANONICAL_VALUE')
    return json.dumps(normalize(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def version_hash(kind,document):
    return hashlib.sha256(canonical({'domain':kind+'-v1','value':document}).encode('utf-8')).hexdigest()


def day(value):
    if type(value) is date:return value
    if isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        try:return date.fromisoformat(value)
        except ValueError:pass
    raise MasterDataError('DATE_INVALID')


def fields(value,keys):
    if not isinstance(value,dict) or set(value)!=set(keys.split()):raise MasterDataError('MASTER_FIELDS_INVALID')


def positive(value):
    if type(value) is not int or not 0<value<2**63:raise MasterDataError('ID_INVALID')
    return value


def interval(row):
    first=day(row['valid_from']);end=day(row['valid_to']) if row['valid_to'] is not None else None
    if end is not None and end<=first:raise MasterDataError('INTERVAL_INVALID')
    return first,end


def in_range(row,on):
    return day(row['valid_from'])<=on and (row['valid_to'] is None or on<day(row['valid_to']))


def assert_nonoverlap(rows,key):
    groups={}
    for row in rows:groups.setdefault(key(row),[]).append(row)
    for group in groups.values():
        ordered=sorted(group,key=lambda r:r['valid_from'])
        for left,right in zip(ordered,ordered[1:]):
            if left['valid_to'] is None or left['valid_to']>right['valid_from']:raise MasterDataError('IDENTITY_INTERVAL_OVERLAP')


def inspect_legacy_candidates(db):
    """Read both current tables without claiming identity/lifecycle evidence.

    Equal codes or old integer IDs are only candidates for explicit review.
    Inactive/current flags never supply historical last-trading dates.
    """
    from app.models.symbol import Symbol
    from app.models.universe import UniverseSymbol
    exchange_names={'SH':'SSE','SZ':'SZSE','BJ':'BSE','SSE':'SSE','SZSE':'SZSE','BSE':'BSE','NYSE':'NYSE','NASDAQ':'NASDAQ'}
    result=[]
    for table,model in (('symbols',Symbol),('universe_symbols',UniverseSymbol)):
        for row in db.scalars(select(model).order_by(model.id)):
            result.append({'legacy_table':table,'legacy_id':row.id,'code':row.symbol,
                'exchange':exchange_names.get(str(row.market).upper()),'asset_type':row.asset_type,
                'listed_at_candidate':row.listed_at.isoformat() if row.listed_at else None,
                'instrument_id':None,'mapping_status':'unreviewed','lifecycle_status':'unknown',
                'evidence_status':'legacy_current_fields_only'})
    return result


class MasterDataService:
    def __init__(self,engine):
        self.engine=engine

    @staticmethod
    def _lock(db):
        db.execute(update(schema.control).where(schema.control.c.id==1).values(
            next_instrument_id=schema.control.c.next_instrument_id))
        row=db.execute(select(schema.control).where(schema.control.c.id==1)).mappings().first()
        if row is None:raise MasterDataError('MASTER_SCHEMA_UNINITIALIZED')
        return row

    def register_identity(self,identity_key,*,asset_type,exchange):
        if not isinstance(identity_key,str) or not identity_key.strip() or len(identity_key)>191:
            raise MasterDataError('IDENTITY_KEY_REQUIRED')
        identity_key=unicodedata.normalize('NFC',identity_key)
        if asset_type not in {'stock','etf'} or exchange not in {'SSE','SZSE','BSE','NYSE','NASDAQ'}:
            raise MasterDataError('IDENTITY_DOMAIN_INVALID')
        with self.engine.begin() as db:
            state=self._lock(db)
            old=db.execute(select(schema.instruments).where(schema.instruments.c.identity_key==identity_key)).mappings().first()
            if old:
                if (old['asset_type'],old['exchange'])!=(asset_type,exchange):raise MasterDataError('IDENTITY_CONFLICT')
                return old['instrument_id']
            value=state['next_instrument_id']
            if value>=2**63-1:raise MasterDataError('INSTRUMENT_ID_EXHAUSTED')
            db.execute(insert(schema.instruments).values(instrument_id=value,identity_key=identity_key,asset_type=asset_type,exchange=exchange))
            db.execute(update(schema.control).where(schema.control.c.id==1).values(next_instrument_id=value+1))
            return value

    def identity(self,instrument_id):
        if type(instrument_id) is not int or instrument_id<=0:raise MasterDataError('INSTRUMENT_ID_INVALID')
        with self.engine.connect() as db:
            row=db.execute(select(schema.instruments).where(schema.instruments.c.instrument_id==instrument_id)).mappings().first()
            if row is None:raise MasterDataError('INSTRUMENT_UNKNOWN')
            return dict(row)

    @staticmethod
    def _store(db,kind,document,evidence,*,parent=None):
        if not isinstance(evidence,bytes) or not 0<len(evidence)<=4*1024*1024:raise MasterDataError('EVIDENCE_REQUIRED')
        version=version_hash(kind,document)
        old=db.execute(select(schema.versions).where(schema.versions.c.version==version)).mappings().first()
        text=canonical(document)
        if old:
            if old['kind']!=kind or old['document_json']!=text or bytes(old['evidence'])!=evidence:raise MasterDataError('HASH_COLLISION')
        else:
            db.execute(insert(schema.versions).values(version=version,kind=kind,document_json=text,evidence=evidence,
                observed_at=datetime.now(timezone.utc).replace(tzinfo=None),parent_version=parent))
        return version,old is None

    def _version(self,version,kind):
        if not isinstance(version,str) or not re.fullmatch('[0-9a-f]{64}',version):raise MasterDataError('VERSION_REQUIRED')
        with self.engine.connect() as db:
            row=db.execute(select(schema.versions).where(schema.versions.c.version==version,schema.versions.c.kind==kind)).mappings().first()
            if row is None:raise MasterDataError('VERSION_UNKNOWN')
            try:
                document=json.loads(row['document_json'])
                if version_hash(kind,document)!=version or hashlib.sha256(bytes(row['evidence'])).hexdigest()!=document['evidence_hash']:
                    raise MasterDataError('VERSION_INTEGRITY')
            except (ValueError,KeyError,TypeError) as exc:
                raise MasterDataError('VERSION_INTEGRITY') from exc
            return document

    def instrument_snapshot(self,version):
        return self._version(version,'instrument')

    def publish_instruments(self,*,members,codes,aliases,evidence,expected_head):
        if not isinstance(evidence,bytes) or not evidence:raise MasterDataError('EVIDENCE_REQUIRED')
        if not all(isinstance(value,list) for value in (members,codes,aliases)) or not members:raise MasterDataError('MASTER_MEMBERS_REQUIRED')
        # Normalize only representation; no identity, lifecycle or code is inferred.
        members=json.loads(canonical(members));codes=json.loads(canonical(codes));aliases=json.loads(canonical(aliases))
        with self.engine.begin() as db:
            state=self._lock(db)
            identities={r['instrument_id']:dict(r) for r in db.execute(select(schema.instruments)).mappings()}
            lifecycle={}
            for row in members:
                fields(row,'instrument_id list_date last_trade_date lifecycle_status')
                identity=positive(row['instrument_id'])
                if identity not in identities:raise MasterDataError('INSTRUMENT_UNKNOWN')
                if identity in lifecycle:raise MasterDataError('MASTER_MEMBER_DUPLICATE')
                if row['list_date'] is None:raise MasterDataError('LIFECYCLE_UNKNOWN')
                first=day(row['list_date'])
                last=day(row['last_trade_date']) if row['last_trade_date'] is not None else None
                if row['lifecycle_status'] not in {'listed','delisted'} or (row['lifecycle_status']=='delisted')!=(last is not None):raise MasterDataError('LIFECYCLE_UNKNOWN')
                if last is not None and (last<first or last==date.max):raise MasterDataError('LIFECYCLE_INVALID')
                lifecycle[identity]=(first,last)
            for row in codes:fields(row,'instrument_id exchange code valid_from valid_to')
            for row in aliases:fields(row,'instrument_id exchange code valid_from valid_to legacy_table legacy_id')
            for row in codes+aliases:
                identity=positive(row['instrument_id'])
                if identity not in lifecycle:raise MasterDataError('ALIAS_MEMBER_UNKNOWN')
                if row['exchange']!=identities[identity]['exchange']:raise MasterDataError('EXCHANGE_MISMATCH')
                pattern=r'[0-9]{6}' if row['exchange'] in {'SSE','SZSE','BSE'} else r'[A-Z][A-Z0-9.-]{0,14}'
                if not isinstance(row['code'],str) or not re.fullmatch(pattern,row['code']):raise MasterDataError('CODE_INVALID')
                first,end=interval(row);listing,last=lifecycle[identity]
                if first<listing or (last is not None and (end is None or end>last+timedelta(days=1))):raise MasterDataError('OUTSIDE_LIFECYCLE')
                if 'legacy_table' in row:
                    if row['legacy_table'] not in {'symbols','universe_symbols'}:raise MasterDataError('LEGACY_TABLE_INVALID')
                    positive(row['legacy_id'])
            assert_nonoverlap(codes,lambda r:(r['exchange'],r['code']))
            assert_nonoverlap(aliases,lambda r:(r['legacy_table'],r['legacy_id']))
            code_groups={}
            for row in codes:code_groups.setdefault((row['instrument_id'],row['exchange'],row['code']),[]).append(row)
            for row in aliases:
                if not any(c['valid_from']<=row['valid_from']
                    and (c['valid_to'] is None or row['valid_to'] is not None and row['valid_to']<=c['valid_to'])
                    for c in code_groups.get((row['instrument_id'],row['exchange'],row['code']),())):raise MasterDataError('ALIAS_CODE_UNBOUND')
            if set(lifecycle)-{c['instrument_id'] for c in codes}:raise MasterDataError('CODE_UNKNOWN')
            document={'schema_version':'governance-1.4','members':sorted(members,key=lambda r:r['instrument_id']),
                'codes':sorted(codes,key=canonical),'aliases':sorted(aliases,key=canonical),
                'identities':[identities[i] for i in sorted(lifecycle)],'evidence_hash':hashlib.sha256(evidence).hexdigest()}
            version=version_hash('instrument',document)
            if state['instrument_head']!=expected_head and state['instrument_head']!=version:raise MasterDataError('MASTER_VERSION_CONFLICT')
            version,fresh=self._store(db,'instrument',document,evidence,parent=state['instrument_head'])
            if not fresh and state['instrument_head']!=version:raise MasterDataError('MASTER_VERSION_REWIND')
            if fresh:
                db.execute(insert(schema.members),[{'instrument_version':version,'instrument_id':i} for i in lifecycle])
                if aliases:
                    db.execute(insert(schema.aliases),[{**r,'instrument_version':version,'valid_from':day(r['valid_from']),
                        'valid_to':day(r['valid_to']) if r['valid_to'] is not None else None} for r in aliases])
            db.execute(update(schema.control).where(schema.control.c.id==1).values(instrument_head=version))
            return version

    def validate_lifecycle(self,version,instrument_id,event_date):
        on=day(event_date);positive(instrument_id)
        document=self.instrument_snapshot(version)
        row=next((r for r in document['members'] if r['instrument_id']==instrument_id),None)
        if row is None:raise MasterDataError('INSTRUMENT_UNMAPPED')
        if on<day(row['list_date']) or row['last_trade_date'] is not None and on>day(row['last_trade_date']):raise MasterDataError('OUTSIDE_LIFECYCLE')
        identity=next(r for r in document['identities'] if r['instrument_id']==instrument_id)
        return {**identity,**row,'instrument_version':version}

    def resolve_code(self,version,exchange,code,on_date):
        on=day(on_date);document=self.instrument_snapshot(version)
        found=[r for r in document['codes'] if r['exchange']==exchange and r['code']==code and in_range(r,on)]
        if len(found)!=1:raise MasterDataError('CODE_UNMAPPED')
        return self.validate_lifecycle(version,found[0]['instrument_id'],on)

    def resolve_legacy(self,version,legacy_table,legacy_id,on_date):
        positive(legacy_id);on=day(on_date);document=self.instrument_snapshot(version)
        found=[r for r in document['aliases'] if r['legacy_table']==legacy_table and r['legacy_id']==legacy_id and in_range(r,on)]
        if len(found)!=1:raise MasterDataError('LEGACY_UNMAPPED')
        return self.validate_lifecycle(version,found[0]['instrument_id'],on)

    def publish_calendar(self,*,exchange,source_uri,evidence):
        from app.services.trade_calendar_sync import SOURCE_URL,parse_exchange_calendar
        # Do not relabel SSE evidence as another exchange's official calendar.
        if exchange!='SSE' or source_uri!=SOURCE_URL:raise MasterDataError('CALENDAR_SOURCE_UNSUPPORTED')
        if not isinstance(evidence,bytes) or not 0<len(evidence)<=4*1024*1024:raise MasterDataError('EVIDENCE_REQUIRED')
        try:
            year,days=parse_exchange_calendar(evidence.decode('utf-8'))
            start,end=date(year,1,1),date(year+1,1,1)
        except (UnicodeError,ValueError,OverflowError) as exc:
            raise MasterDataError('CALENDAR_DOCUMENT_INVALID') from exc
        document={'schema_version':'governance-1.4','exchange':exchange,'source_uri':source_uri,
            'parser_version':'sse-annual-closures-v1','start':start.isoformat(),'end_exclusive':end.isoformat(),
            'days':{d.isoformat():value for d,value in sorted(days.items())},'evidence_hash':hashlib.sha256(evidence).hexdigest(),
            'publication_precision':'unknown','public_available_at':None}
        with self.engine.begin() as db:
            self._lock(db)
            version,_=self._store(db,'calendar',document,evidence)
            return version

    def calendar_snapshot(self,version):
        return self._version(version,'calendar')

    def trading_days(self,version,exchange,start,end_exclusive):
        start,end=day(start),day(end_exclusive)
        if end<start:raise MasterDataError('INTERVAL_INVALID')
        document=self.calendar_snapshot(version)
        if document['exchange']!=exchange:raise MasterDataError('CALENDAR_EXCHANGE_MISMATCH')
        if start<day(document['start']) or end>day(document['end_exclusive']):raise MasterDataError('CALENDAR_RANGE_UNKNOWN')
        result=[]
        while start<end:
            value=document['days'].get(start.isoformat())
            if type(value) is not bool:raise MasterDataError('CALENDAR_RANGE_UNKNOWN')
            if value:result.append(start)
            start+=timedelta(days=1)
        return result

    def version_info(self,version):
        with self.engine.connect() as db:
            row=db.execute(select(schema.versions).where(schema.versions.c.version==version)).mappings().first()
            if row is None:raise MasterDataError('VERSION_UNKNOWN')
            self._version(version,row['kind'])
            return {'version':version,'kind':row['kind'],'observed_at':row['observed_at'].replace(tzinfo=timezone.utc).isoformat(timespec='microseconds'),
                    'parent_version':row['parent_version'],'evidence_hash':hashlib.sha256(bytes(row['evidence'])).hexdigest()}
