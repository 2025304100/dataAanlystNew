"""Candidate index quotes and separately versioned official composition inputs.

Price points are never stock prices. Index identities, dated membership and
weights are distinct products; current lists cannot become historical universes.
"""
from datetime import datetime,timedelta,timezone
from decimal import Decimal,localcontext,ROUND_HALF_UP
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import json,re,base64,math
from sqlalchemy import select,insert,update
from app.db import governance_index_schema as x
from app.db.governance_migration import migrate_candidate
from app.services.governance_fact_contracts import WriterContract,WriterError,canonical,digest,hash_object,day,instant,time_fields,logical_identity
from app.services.governance_master_data import MasterDataService

SOURCES={'eastmoney':'index_zh_a_hist','sina':'index_zh_a_daily','tencent':'stock_zh_index_daily_tx'}
HOSTS={'CSI':'csindex.com.cn','CNI':'cnindex.com.cn'}

def install_index_schema(engine):return migrate_candidate(engine,metadata=x.metadata,revision='DSG-INDEX-001')


def index_contract(provider, *, evidence,volume_unit='unknown',series_kind='price'):
    if provider not in SOURCES or volume_unit not in {'share','lot','index_native','unknown'} or series_kind not in {'price','total_return'}:raise WriterError('INDEX_CONTRACT_INVALID')
    fields={n:{'wire_field':n,'native_unit':'point' if n.startswith('index_') and n!='index_id' else volume_unit if n=='volume' else 'CNY',
        'normalized_unit':'point' if n.startswith('index_') and n!='index_id' else volume_unit if n=='volume' else 'CNY',
        'scale':'1','offset':'0'} for n in ['index_open','index_high','index_low','index_close','index_id','volume','amount']}
    return WriterContract.profile(dataset='index',profile='index/core',provider=provider,endpoint=SOURCES[provider],asset_type='index',
        instrument_family='registered_index',definition={'version':1,'series_kind':series_kind,'basis':'unadjusted_index_points'},
        fields=fields,evidence=evidence,publication_timezone='Asia/Shanghai')


def _decimal(value):
    if value is None:return None
    if isinstance(value,(float,bool)):raise WriterError('INDEX_NUMERIC_INVALID')
    try:
        with localcontext() as ctx:
            ctx.prec=80;n=Decimal(str(value))
            if not n.is_finite():raise ValueError()
            return format(n.quantize(Decimal('0.0000000001'),rounding=ROUND_HALF_UP),'f')
    except Exception:raise WriterError('INDEX_NUMERIC_INVALID') from None


def _tencent(raw,symbol):
    try:
        body=json.loads(raw,parse_float=str)
        if body.get('code',0)!=0:raise ValueError()
        rows=body['data'][symbol]['day']
        if type(rows) is not list or len(rows)>640:raise ValueError()
        if any(type(r) is not list or len(r)!=6 for r in rows):raise ValueError()
        return [dict(zip(['date','open','close','high','low','quantity'],[str(v) if type(v) is int else v for v in r])) for r in rows]
    except (ValueError,KeyError,TypeError):raise WriterError('INDEX_RAW_MODE_UNVERIFIED') from None


def _eastmoney(raw,secid):
    try:
        body=json.loads(raw,parse_float=str);data=body['data']
        if body.get('rc')!=0 or str(data['market'])+'.'+data['code']!=secid or type(data['klines']) is not list:raise ValueError()
        names=['日期','开盘','收盘','最高','最低','成交量','成交额','振幅','涨跌幅','涨跌额','换手率']
        parts=[line.split(',') for line in data['klines']]
        if any(len(r)!=11 for r in parts):raise ValueError()
        return [dict(zip(names,r)) for r in parts]
    except (ValueError,KeyError,TypeError):raise WriterError('INDEX_RAW_RESPONSE_INVALID') from None


def _wire(provider,row,index_id):
    chinese=provider=='eastmoney'
    allowed={'日期','开盘','收盘','最高','最低','成交量','成交额','振幅','涨跌幅','涨跌额','换手率'} if chinese else (
        {'date','open','close','high','low','quantity'} if provider=='tencent' else {'date','open','high','low','close','volume'})
    if type(row) is not dict or set(row)-allowed:raise WriterError('UNEXPECTED_WIRE_FIELD')
    aliases={'open':'开盘','high':'最高','low':'最低','close':'收盘'} if chinese else {n:n for n in ['open','high','low','close']}
    return {**{'index_'+n:row.get(w) for n,w in aliases.items()},'index_id':index_id,
        'volume':row.get('成交量' if chinese else 'quantity' if provider=='tencent' else 'volume'),
        'amount':row.get('成交额') if chinese else None}


def validate_index_request(writer,request,payload,doc):
    contract=writer.contracts[request['contract_version']];provider=doc['adapter']['provider']
    expected=index_contract(provider,evidence=contract.evidence,volume_unit=doc['fields']['volume']['normalized_unit'],series_kind=doc['definition'].get('series_kind'))
    if expected.document!=doc:raise WriterError('INDEX_CONTRACT_INVALID')
    try:
        proof=json.loads(request['index_evidence']);raw=json.loads(request['raw_response'])
        if canonical(proof).encode()!=request['index_evidence'] or set(proof)!={'index_version','row_index','raw_hash'} or proof['raw_hash']!=digest(request['raw_response']):raise ValueError()
        if canonical(raw).encode()!=request['raw_response'] or set(raw)!={'format','provider','index_version','symbol','basis','rows','supplier_response_b64'} or raw['format']!='index-records-v1' or raw['provider']!=provider or raw['index_version']!=proof['index_version'] or raw['basis']!='unadjusted_index_points':raise ValueError()
        if type(proof['row_index']) is not int or not 0<=proof['row_index']<len(raw['rows']):raise ValueError()
    except (KeyError,ValueError,TypeError,UnicodeError):raise WriterError('INDEX_SOURCE_EVIDENCE_REQUIRED') from None
    service=IndexService(writer);master=service.index(proof['index_version']);on=day(request['key']['trade_date'])
    if master['index_id']!=request['key']['index_id'] or master['series_kind']!=doc['definition']['series_kind'] or raw['symbol']!=master['quote_market']+master['quote_symbol']:
        raise WriterError('INDEX_IDENTITY_MISMATCH')
    service.validate_index_date(master,on)
    today=instant(writer.clock()).replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
    if on>today:raise WriterError('INDEX_QUOTE_IN_FUTURE')
    if request['instrument_version'] is not None:raise WriterError('INDEX_NAMESPACE_MISMATCH')
    if on not in MasterDataService(writer.engine).trading_days(request['calendar_version'],master['calendar_exchange'],on,on+timedelta(days=1)):raise WriterError('NOT_TRADING_DAY')
    if provider in {'tencent','eastmoney'}:
        try:original=base64.b64decode(raw['supplier_response_b64'],validate=True)
        except Exception:raise WriterError('INDEX_RAW_MODE_UNVERIFIED') from None
        response_rows=_tencent(original,raw['symbol']) if provider=='tencent' else _eastmoney(original,master['eastmoney_secid'])
        if any(r not in response_rows for r in raw['rows']):raise WriterError('INDEX_RAW_RESPONSE_MISMATCH')
    elif raw['supplier_response_b64'] is not None:raise WriterError('INDEX_SOURCE_EVIDENCE_REQUIRED')
    row=raw['rows'][proof['row_index']]
    if day(row.get('日期' if provider=='eastmoney' else 'date')).isoformat()!=request['key']['trade_date']:raise WriterError('INDEX_DATE_MISMATCH')
    if contract.normalize({**request,'wire_record':_wire(provider,row,master['index_id'])})!=payload:raise WriterError('INDEX_PAYLOAD_MISMATCH')


class IndexService:
    def __init__(self,writer):self.writer=writer;self.master=MasterDataService(writer.engine)

    def register_index(self,document, *, source_evidence,review_evidence):
        doc=json.loads(canonical(document))
        if set(doc)!={'index_id','authority','quote_symbol','quote_market','eastmoney_secid','calendar_exchange','series_kind','valid_from','valid_to','expected_members'}:raise WriterError('INDEX_MASTER_INVALID')
        if doc['authority'] not in HOSTS or not re.fullmatch(doc['authority']+r':[0-9]{6}',doc['index_id']) or not re.fullmatch(r'[0-9]{6}',doc['quote_symbol']) or (doc['quote_market'],doc['calendar_exchange']) not in {('sh','SSE'),('sz','SZSE')} or doc['series_kind'] not in {'price','total_return'}:raise WriterError('INDEX_MASTER_INVALID')
        if not isinstance(doc['eastmoney_secid'],str) or not re.fullmatch(r'[012]\.[0-9]{6}',doc['eastmoney_secid']) or doc['eastmoney_secid'][2:]!=doc['quote_symbol']:raise WriterError('INDEX_MASTER_INVALID')
        start=day(doc['valid_from']);end=day(doc['valid_to']) if doc['valid_to'] else None
        if end and end<=start or doc['expected_members'] is not None and (type(doc['expected_members']) is not int or doc['expected_members']<=0):raise WriterError('INDEX_MASTER_INVALID')
        if type(source_evidence) is not bytes or not source_evidence or type(review_evidence) is not bytes or not review_evidence:raise WriterError('INDEX_REVIEW_REQUIRED')
        version=hash_object('index-master-v1',{'document':doc,'source_hash':digest(source_evidence),'review_hash':digest(review_evidence)})
        with self.writer.engine.begin() as db:
            self._insert_once(db,x.indices,{'index_version':version,'index_id':doc['index_id'],'document_json':canonical(doc),
                'source_evidence':source_evidence,'review_evidence':review_evidence,'available_at':instant(self.writer.clock())},'index_version')
        return version

    @staticmethod
    def _insert_once(db,table,values,key):
        if db.dialect.name=='mysql':
            from sqlalchemy.dialects.mysql import insert as native
            command=native(table).values(**values).on_duplicate_key_update(**{key:values[key]})
        else:
            from sqlalchemy.dialects.sqlite import insert as native
            command=native(table).values(**values).on_conflict_do_nothing(index_elements=[key])
        db.execute(command)

    def index(self,version):
        with self.writer.engine.connect() as db:row=db.execute(select(x.indices).where(x.indices.c.index_version==version)).mappings().one()
        doc=json.loads(row['document_json'])
        if hash_object('index-master-v1',{'document':doc,'source_hash':digest(bytes(row['source_evidence'])),'review_hash':digest(bytes(row['review_evidence']))})!=version:raise WriterError('INDEX_MASTER_HASH_MISMATCH')
        return doc

    @staticmethod
    def validate_index_date(doc,on):
        if on<day(doc['valid_from']) or doc['valid_to'] is not None and on>=day(doc['valid_to']):raise WriterError('INDEX_OUTSIDE_LIFECYCLE')

    def quote_requests(self,capture, *, contract_version,index_version,calendar_version,start,end_exclusive,source_uri,batch_id,revisions=None):
        raw=json.loads(capture);provider=raw['provider'];first,end=day(start),day(end_exclusive);master=self.index(index_version)
        if end<=first or raw['index_version']!=index_version:raise WriterError('INDEX_SCOPE_INVALID')
        requests=[];seen=set();revisions=revisions or {}
        for index,row in enumerate(raw['rows']):
            on=day(row.get('日期' if provider=='eastmoney' else 'date'))
            if not first<=on<end:raise WriterError('INDEX_SCOPE_INVALID')
            if on in seen:raise WriterError('DUPLICATE_BUSINESS_KEY')
            seen.add(on)
            contract=self.writer.contracts[contract_version]
            request={'dataset':'index','key':{'index_id':master['index_id'],'trade_date':on.isoformat(),'definition_id':contract.document['definition_id']},
                'contract_version':contract_version,'profile':'index/core','instrument_version':None,'calendar_version':calendar_version,
                'wire_record':_wire(provider,row,master['index_id']),'raw_response':capture,'source_uri':source_uri,'batch_id':batch_id,
                'time':{'event_date':on.isoformat(),'publication_precision':'unknown','published_at':None,'publication_date':None,
                    'valid_from':on.isoformat(),'valid_to':(on+timedelta(days=1)).isoformat(),'publication_evidence':None},
                'revision':revisions.get(on.isoformat(),{'kind':'original','token':None,'order':None,'order_evidence':None,'supersedes_version':None}),
                'flags':{},'gate_results':{},'field_sources':{},'index_evidence':canonical({'index_version':index_version,'row_index':index,'raw_hash':digest(capture)}).encode()}
            if request['revision']['kind']=='withdrawal':request['wire_record']={'retraction_reason':'explicit index source withdrawal'}
            self.writer.preview(request);requests.append(request)
        if set(revisions)-{r['time']['event_date'] for r in requests}:raise WriterError('UNBOUND_REVISION')
        return requests

    def ingest_quotes(self,*args,**kwargs):
        requests=self.quote_requests(*args,**kwargs)
        with self.writer.engine.begin() as db:return [self.writer.write(r,connection=db) for r in requests]

    def publish_composition(self,document, *, source_evidence,review_evidence=None):
        doc=json.loads(canonical(document))
        keys={'index_version','instrument_version','product','valid_from','valid_to','publication_precision','published_at',
              'publication_date','source_uri','complete','weight_unit','constituents_version','members','revision_kind','supersedes_version'}
        if set(doc)!=keys or doc['product'] not in {'constituents','weights','current_constituents','current_weights'} or type(doc['complete']) is not bool:raise WriterError('COMPOSITION_FIELDS_INVALID')
        if type(source_evidence) is not bytes or not source_evidence:raise WriterError('COMPOSITION_SOURCE_REQUIRED')
        master=self.index(doc['index_version']);first=day(doc['valid_from']);end=day(doc['valid_to']) if doc['valid_to'] else None
        self.validate_index_date(master,first)
        uri=urlsplit(doc['source_uri']);host=HOSTS[master['authority']]
        if uri.scheme!='https' or uri.username or uri.password or not uri.hostname or not (uri.hostname==host or uri.hostname.endswith('.'+host)):raise WriterError('COMPOSITION_AUTHORITY_MISMATCH')
        now=instant(self.writer.clock());today=now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
        if doc['product'].startswith('current_') and (first!=today or end!=first+timedelta(days=1)):raise WriterError('CURRENT_COMPOSITION_NOT_HISTORICAL')
        times=time_fields({'event_date':first.isoformat(),'valid_from':first.isoformat(),'valid_to':doc['valid_to'],
            'publication_precision':doc['publication_precision'],'published_at':doc['published_at'],'publication_date':doc['publication_date'],
            'publication_evidence':source_evidence if doc['publication_precision']!='unknown' else None},'Asia/Shanghai')
        if doc['revision_kind'] not in {'original','correction','withdrawal'} or (doc['revision_kind']=='original')!=(doc['supersedes_version'] is None):raise WriterError('COMPOSITION_REVISION_INVALID')
        if type(doc['members']) is not list:raise WriterError('COMPOSITION_MEMBERS_INVALID')
        weights=doc['product'].endswith('weights');members=[];seen=set()
        for row in doc['members']:
            if type(row) is not dict or set(row)!=({'exchange','code','weight'} if weights else {'exchange','code'}):raise WriterError('COMPOSITION_MEMBERS_INVALID')
            identity=self.master.resolve_code(doc['instrument_version'],row['exchange'],row['code'],first)
            if identity['asset_type']!='stock' or identity['instrument_id'] in seen:raise WriterError('COMPOSITION_MEMBER_IDENTITY_INVALID')
            seen.add(identity['instrument_id']);weight=None
            if weights:
                native=_decimal(row['weight'])
                if native is not None and Decimal(native)<0:raise WriterError('COMPOSITION_WEIGHT_INVALID')
                if doc['weight_unit'] in {'percent','ratio'} and native is not None:weight=_decimal(str(Decimal(native)*(100 if doc['weight_unit']=='ratio' else 1)))
                if weight is not None and Decimal(weight)>100:raise WriterError('COMPOSITION_WEIGHT_INVALID')
            members.append({'instrument_id':identity['instrument_id'],'exchange':row['exchange'],'code':row['code'],'weight_pct':weight})
        if weights and doc['weight_unit'] not in {'percent','ratio','unknown'} or not weights and doc['weight_unit'] is not None:raise WriterError('COMPOSITION_WEIGHT_UNIT_INVALID')
        quality='complete' if doc['complete'] and members else 'partial'
        if master['expected_members'] is not None and len(members)!=master['expected_members']:quality='partial'
        if weights:
            if any(m['weight_pct'] is None for m in members):quality='partial'
            if quality=='complete' and abs(sum(Decimal(m['weight_pct']) for m in members)-100)>Decimal('0.05'):raise WriterError('COMPOSITION_WEIGHT_SUM_INVALID')
            parent=self.composition(doc['constituents_version']) if doc['constituents_version'] else None
            if parent is None:quality='partial'
            elif parent['document']['product']!=('current_constituents' if doc['product'].startswith('current_') else 'constituents') or parent['document']['index_version']!=doc['index_version'] or parent['document']['valid_from']!=doc['valid_from'] or parent['document']['valid_to']!=doc['valid_to'] or {m['instrument_id'] for m in parent['members']}!=seen or parent['available_at'] is None:raise WriterError('COMPOSITION_CONSTITUENT_BINDING_MISMATCH')
            elif parent['quality_status']!='complete':quality='partial'
        elif doc['constituents_version'] is not None:raise WriterError('COMPOSITION_CONSTITUENT_BINDING_MISMATCH')
        if doc['revision_kind']=='withdrawal':
            if members:raise WriterError('COMPOSITION_WITHDRAWAL_MEMBERS_INVALID')
            quality='partial'
        version=hash_object('index-composition-v1',{'document':doc,'members':members,'quality_status':quality,'source_hash':digest(source_evidence)})
        scope=hash_object('index-composition-key-v1',{'index_version':doc['index_version'],'product':doc['product'],'valid_from':doc['valid_from']})
        stored_doc={'source':doc,'members':members}
        with self.writer.engine.begin() as db:
            self._insert_once(db,x.compositions,{'composition_version':version,'scope_key':scope,'index_version':doc['index_version'],'product':doc['product'],
                'document_json':canonical(stored_doc),'source_evidence':source_evidence,'event_date':first,'valid_from':first,'valid_to':end,
                'published_at':times['published_at'],'public_available_at':times['public_available_at'],'observed_at':now,'quality_status':quality},'composition_version')
            if review_evidence is not None:
                if type(review_evidence) is not bytes or not review_evidence:raise WriterError('INDEX_REVIEW_REQUIRED')
                if times['public_available_at'] is not None and times['public_available_at']>now:raise WriterError('COMPOSITION_NOT_YET_PUBLIC')
                head=db.execute(select(x.heads).where(x.heads.c.scope_key==scope).with_for_update()).mappings().first()
                if head and head['composition_version']==version:return version
                if doc['supersedes_version']!=(head['composition_version'] if head else None):raise WriterError('COMPOSITION_PARENT_NOT_CURRENT')
                if head:
                    old=db.execute(select(x.acceptance).where(x.acceptance.c.composition_version==head['composition_version']).with_for_update()).mappings().one()
                    previous=db.execute(select(x.compositions).where(x.compositions.c.composition_version==head['composition_version'])).mappings().one()
                    if previous['public_available_at'] is not None and times['public_available_at'] is not None and times['public_available_at']<previous['public_available_at']:raise WriterError('COMPOSITION_PUBLICATION_REVERSED')
                    if now<=old['available_at']:raise WriterError('CLOCK_NOT_MONOTONIC')
                    db.execute(update(x.acceptance).where(x.acceptance.c.composition_version==head['composition_version']).values(system_to=now))
                    db.execute(update(x.heads).where(x.heads.c.scope_key==scope).values(composition_version=version))
                else:db.execute(insert(x.heads).values(scope_key=scope,composition_version=version))
                db.execute(insert(x.acceptance).values(composition_version=version,scope_key=scope,available_at=now,system_to=None,review_evidence=review_evidence))
        return version

    def composition(self,version):
        with self.writer.engine.connect() as db:
            row=db.execute(select(x.compositions).where(x.compositions.c.composition_version==version)).mappings().one()
            acceptance=db.execute(select(x.acceptance).where(x.acceptance.c.composition_version==version)).mappings().first()
        stored=json.loads(row['document_json']);doc=stored['source']
        if hash_object('index-composition-v1',{'document':doc,'members':stored['members'],'quality_status':row['quality_status'],'source_hash':digest(bytes(row['source_evidence']))})!=version:raise WriterError('COMPOSITION_HASH_MISMATCH')
        if row['valid_from']!=day(doc['valid_from']) or row['valid_to']!=(day(doc['valid_to']) if doc['valid_to'] else None) or row['product']!=doc['product'] or row['index_version']!=doc['index_version']:raise WriterError('COMPOSITION_HASH_MISMATCH')
        return {'document':doc,'members':stored['members'],'quality_status':row['quality_status'],
            'available_at':acceptance['available_at'] if acceptance else None,'row':dict(row)}

    def composition_as_of(self, *, index_version,product,effective_date,as_of,knowledge_mode):
        master=self.index(index_version);on=day(effective_date);self.validate_index_date(master,on);cutoff=instant(as_of)
        if cutoff>instant(self.writer.clock()):raise WriterError('INDEX_ASOF_IN_FUTURE')
        if product not in {'constituents','weights','current_constituents','current_weights'} or knowledge_mode not in {'system_known','public_known'}:raise WriterError('COMPOSITION_ASOF_INVALID')
        with self.writer.engine.connect() as db:
            rows=db.execute(select(x.compositions,x.acceptance.c.available_at,x.acceptance.c.system_to).join(x.acceptance,
                x.compositions.c.composition_version==x.acceptance.c.composition_version).where(x.compositions.c.index_version==index_version,
                x.compositions.c.product==product,x.compositions.c.valid_from<=on,
                (x.compositions.c.valid_to.is_(None)) | (x.compositions.c.valid_to>on))).mappings().all()
        eligible=[]
        for row in rows:
            if knowledge_mode=='system_known':known=row['available_at']<=cutoff and (row['system_to'] is None or cutoff<row['system_to'])
            else:known=row['public_available_at'] is not None and row['public_available_at']<=cutoff and (not product.startswith('current_') or row['available_at']<=cutoff)
            if known:eligible.append(row)
        if knowledge_mode=='public_known':
            unknown=[r for r in rows if r['public_available_at'] is None and r['available_at']<=cutoff and (r['system_to'] is None or cutoff<r['system_to'])]
            if unknown and (not eligible or max(r['valid_from'] for r in unknown)>=max(r['valid_from'] for r in eligible)):
                return {'status':'PUBLICATION_UNKNOWN','composition_version':None}
        if not eligible:return {'status':'missing','composition_version':None}
        latest=max(r['valid_from'] for r in eligible);eligible=[r for r in eligible if r['valid_from']==latest]
        facts=[(r,self.composition(r['composition_version'])) for r in eligible]
        parents={c['document']['supersedes_version'] for _,c in facts}
        tips=[(r,c) for r,c in facts if r['composition_version'] not in parents]
        if len(tips)!=1:raise WriterError('COMPOSITION_VERSION_AMBIGUOUS')
        row,result=tips[0]
        status='withdrawn' if result['document']['revision_kind']=='withdrawal' else 'pass' if result['quality_status']=='complete' else 'QUALITY_BLOCKED'
        if product.endswith('weights') and status=='pass':
            counterpart=self.composition_as_of(index_version=index_version,product='current_constituents' if product.startswith('current_') else 'constituents',
                effective_date=effective_date,as_of=as_of,knowledge_mode=knowledge_mode)
            if counterpart['status']!='pass' or counterpart['composition_version']!=result['document']['constituents_version']:status='DEPENDENCY_BLOCKED'
        return {'status':status,'composition_version':row['composition_version'],'members':result['members'],
            'quality_status':result['quality_status'],'valid_from':row['valid_from'].isoformat(),'available_at':row['available_at']}


def capture_index(service,db, *, provider,index_version,start,end_exclusive):
    """Managed existing sources, with bounded raw Tencent instead of SDK qfq."""
    master=service.index(index_version);first,end=day(start),day(end_exclusive)
    if end<=first or provider not in SOURCES:raise WriterError('INDEX_SCOPE_INVALID')
    from app.services.akshare_utils import call_akshare_with_retry,quiet_akshare_output
    from app.services.market_data import _proxy_bypass
    symbol=master['quote_market']+master['quote_symbol'];original=None
    if provider in {'tencent','eastmoney'}:
        if provider=='tencent' and (end-first).days>640:raise WriterError('INDEX_TENCENT_RANGE_LIMIT')
        import requests
        from app.services.data_source_execution import source_boundary
        def request():
            with _proxy_bypass():
                if provider=='tencent':url='https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get';params={'param':f'{symbol},day,{first},{end-timedelta(days=1)},640,'}
                else:
                    url='https://push2his.eastmoney.com/api/qt/stock/kline/get'
                    params={'secid':master['eastmoney_secid'],'klt':'101','fqt':'0','beg':first.strftime('%Y%m%d'),
                        'end':(end-timedelta(days=1)).strftime('%Y%m%d'),'fields1':'f1,f2,f3,f4,f5,f6','fields2':','.join('f'+str(i) for i in range(51,62))}
                response=requests.get(url,params=params,timeout=(5,15),allow_redirects=False)
                response.raise_for_status()
                if response.status_code!=200 or not 0<len(response.content)<=16*1024*1024:raise WriterError('INDEX_RAW_RESPONSE_INVALID')
                return response.content
        original=source_boundary(SOURCES[provider])(request)()
        rows=_tencent(original,symbol) if provider=='tencent' else _eastmoney(original,master['eastmoney_secid'])
    else:
        import akshare as ak
        with _proxy_bypass(),quiet_akshare_output():
            frame=call_akshare_with_retry(ak.stock_zh_index_daily,symbol=symbol,api_key=SOURCES[provider],db=db,max_attempts=2)
        rows=frame.to_dict('records')
    selected=[]
    for row in rows:
        key='日期' if provider=='eastmoney' else 'date';on=day(str(row[key])[:10])
        if not first<=on<end:
            if provider=='eastmoney':raise WriterError('INDEX_SCOPE_INVALID')
            continue
        normal={}
        for k,v in row.items():
            if type(v) is float:
                if not math.isfinite(v):raise WriterError('INDEX_NUMERIC_INVALID')
                v=str(v)
            normal[k]=on.isoformat() if k==key else v
        selected.append(normal)
    return canonical({'format':'index-records-v1','provider':provider,'index_version':index_version,'symbol':symbol,
        'basis':'unadjusted_index_points','rows':selected,'supplier_response_b64':base64.b64encode(original).decode() if original is not None else None}).encode()
