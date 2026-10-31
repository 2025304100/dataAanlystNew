"""Candidate ETF profiles and validated immutable cross-profile dependencies."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal,localcontext,ROUND_HALF_UP
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
import math
import json
from sqlalchemy import select
from app.db import governance_schema as s,governance_etf_schema as e
from app.db.governance_migration import migrate_candidate
from app.services.governance_fact_contracts import WriterContract,WriterError,canonical,digest,hash_object,day,instant
from app.services.governance_master_data import MasterDataService
from app.services.governance_price_gates import evaluate_price_gate

PRODUCTS={'nav':('eastmoney','fund_etf_fund_info_em'),'shares':('SSE','SSE:etf_shares'),
          'iopv':('eastmoney','fund_etf_spot_em'),'size':('eastmoney','GOV:etf_size'),
          'tracking':('eastmoney','GOV:etf_tracking')}


def install_etf_schema(engine):return migrate_candidate(engine,metadata=e.metadata,revision='DSG-ETF-001')


def etf_contract(product, *, evidence):
    if product not in PRODUCTS:raise WriterError('ETF_PRODUCT_UNREGISTERED')
    names={'nav':['nav_official','valuation_date','currency','premium_official_pct'],
        'shares':['total_shares','share_class','valuation_date'],
        'iopv':['iopv','valuation_date','observation_at','currency'],
        'size':['fund_size_cny','nav_data_version','shares_data_version'],
        'tracking':['tracking_error_pct','window','benchmark_id','nav_return_version','benchmark_return_version']}[product]
    fields={name:{'wire_field':name,'native_unit':'percentage_point' if name.endswith('_pct') else 'CNY',
        'normalized_unit':'percentage_point' if name.endswith('_pct') else 'share' if name=='total_shares' else 'CNY',
        'scale':'10000' if name=='total_shares' else '1','offset':'0'} for name in names}
    if product=='shares':fields['total_shares']['native_unit']='10000_fund_units'
    return WriterContract.profile(dataset='etf',profile='etf/'+product,provider=PRODUCTS[product][0],endpoint=PRODUCTS[product][1],
        asset_type='etf',instrument_family='cn_etf',definition={'version':1,'product':product,
            'basis':'official_NAV' if product in {'nav','size'} else 'IOPV' if product=='iopv' else 'total_return' if product=='tracking' else 'listed_units',
            'currency':'CNY','window':60 if product=='tracking' else None},fields=fields,evidence=evidence,publication_timezone='Asia/Shanghai')


def _decimal(value,scale=8):
    if isinstance(value,(float,bool)):raise WriterError('ETF_NUMERIC_INVALID')
    try:
        with localcontext() as ctx:
            ctx.prec=80;n=Decimal(str(value))
            if not n.is_finite():raise ValueError()
            return format(n.quantize(Decimal(1).scaleb(-scale),rounding=ROUND_HALF_UP),'f')
    except Exception as exc:
        if isinstance(exc,WriterError):raise
        raise WriterError('ETF_NUMERIC_INVALID') from None


def _source_wire(product,row):
    expected={'valuation_date','share_class','basis','value','unit'}|({'observation_at'} if product=='iopv' else set())
    if type(row) is not dict or set(row)!=expected:raise WriterError('ETF_SOURCE_FIELDS_INVALID')
    basis={'nav':'official_NAV','shares':'listed_units','iopv':'IOPV'}[product]
    unit='10000_fund_units' if product=='shares' else 'CNY'
    if row['basis']!=basis or row['unit']!=unit:raise WriterError('ETF_SOURCE_BASIS_OR_UNIT_MISMATCH')
    if not isinstance(row['share_class'],str) or not row['share_class'].strip():raise WriterError('ETF_SHARE_CLASS_REQUIRED')
    wire={'valuation_date':day(row['valuation_date']).isoformat()}
    if product=='nav':wire.update(nav_official=row['value'],currency='CNY',premium_official_pct=None)
    elif product=='shares':wire.update(total_shares=row['value'],share_class=row['share_class'])
    else:wire.update(iopv=row['value'],currency='CNY',observation_at=row['observation_at'])
    return wire


def _known(writer,version,as_of,profile=None,instrument_id=None):
    fact=writer.fact(version)
    if profile and (fact['dataset']!='etf' or fact['key'].get('profile')!=profile):raise WriterError('ETF_DEPENDENCY_PROFILE_MISMATCH')
    if instrument_id is not None and fact['key'].get('instrument_id')!=instrument_id:raise WriterError('ETF_DEPENDENCY_INSTRUMENT_MISMATCH')
    with writer.engine.connect() as db:
        rows=db.execute(select(s.acceptance_history).where(s.acceptance_history.c.data_version==version,
            s.acceptance_history.c.system_from<=as_of,
            (s.acceptance_history.c.system_to.is_(None)) | (s.acceptance_history.c.system_to>as_of))).mappings().all()
    if len(rows)!=1 or fact['available_at'] is None or fact['available_at']>as_of or fact['revision_kind']=='withdrawal':
        raise WriterError('ETF_DEPENDENCY_NOT_KNOWN_AS_OF')
    return fact


def _size(writer,proof,key):
    versions=[proof.get('nav_data_version'),proof.get('shares_data_version')]
    if any(v is None for v in versions):return {'fund_size_cny':None,'nav_data_version':versions[0],'shares_data_version':versions[1]},'insufficient',{}
    as_of=instant(proof['as_of']);identity=key['instrument_id']
    nav=_known(writer,versions[0],as_of,'etf/nav',identity);shares=_known(writer,versions[1],as_of,'etf/shares',identity)
    date=key['valuation_date'];cls=key['share_class']
    if any(f['key']['valuation_date']!=date or f['key']['share_class']!=cls for f in [nav,shares]):raise WriterError('ETF_DEPENDENCY_DATE_OR_CLASS_MISMATCH')
    inputs={'nav_official':nav['payload']['nav_official'],'total_shares':shares['payload']['total_shares'],
        'nav_date':date,'shares_date':date,'size_date':date,'nav_share_class':cls,'shares_share_class':cls,'size_share_class':cls}
    measured=evaluate_price_gate('P10',asset_type='etf',inputs=inputs)
    payload={'fund_size_cny':_decimal(measured['value']) if measured['status']=='pass' else None,
        'nav_data_version':versions[0],'shares_data_version':versions[1]}
    sources={'fund_size_cny':versions,'nav_data_version':versions[0],'shares_data_version':versions[1]}
    return payload,measured['status'],sources


def _series(writer,version):
    with writer.engine.connect() as db:row=db.execute(select(e.return_inputs).where(e.return_inputs.c.series_version==version)).mappings().one()
    doc=json.loads(row['document_json'])
    identity={'document':doc,'source_hash':digest(bytes(row['source_evidence'])),'review_hash':digest(bytes(row['review_evidence']))}
    if hash_object('etf-return-series-v1',identity)!=version:raise WriterError('ETF_RETURN_HASH_MISMATCH')
    return doc,row['available_at']


def _tracking(writer,proof,key,calendar_version):
    left,right=proof.get('nav_return_version'),proof.get('benchmark_return_version')
    payload={'tracking_error_pct':None,'window':60,'benchmark_id':proof['benchmark_id'],
        'nav_return_version':left,'benchmark_return_version':right}
    if not left or not right:return payload,'insufficient',{}
    nav,nav_at=_series(writer,left);bench,bench_at=_series(writer,right);as_of=instant(proof['as_of'])
    if nav_at>as_of or bench_at>as_of:raise WriterError('ETF_DEPENDENCY_NOT_KNOWN_AS_OF')
    if nav['kind']!='dividend_reinvested' or bench['kind']!='total_return':raise WriterError('ETF_TOTAL_RETURN_REQUIRED')
    if nav['instrument_id']!=key['instrument_id'] or nav['share_class']!=key['share_class'] or nav['benchmark_id']!=proof['benchmark_id'] or bench['benchmark_id']!=proof['benchmark_id']:
        raise WriterError('ETF_RETURN_IDENTITY_MISMATCH')
    dates=[p['date'] for p in nav['points']];bd=[p['date'] for p in bench['points']]
    if dates!=bd or len(dates)!=61:return payload,'insufficient',{}
    if dates[-1]!=key['valuation_date'] or nav['calendar_version']!=bench['calendar_version'] or nav['calendar_version']!=calendar_version or nav['exchange']!=bench['exchange']:
        raise WriterError('ETF_RETURN_DATE_MISMATCH')
    expected=MasterDataService(writer.engine).trading_days(nav['calendar_version'],nav['exchange'],dates[0],day(dates[-1])+timedelta(days=1))
    if [d.isoformat() for d in expected]!=dates:return payload,'insufficient',{}
    def returns(points):
        with localcontext() as ctx:
            ctx.prec=60
            return [str(Decimal(b['value'])/Decimal(a['value'])-1) for a,b in zip(points,points[1:])]
    measured=evaluate_price_gate('P11',asset_type='etf',inputs={'nav_total_returns':returns(nav['points']),
        'benchmark_total_returns':returns(bench['points']),'nav_return_kind':nav['kind'],'benchmark_return_kind':bench['kind'],
        'nav_dates':dates[1:],'benchmark_dates':bd[1:],'nav_point_dates':dates})
    payload['tracking_error_pct']=_decimal(measured['value'],10) if measured['status']=='pass' else None
    sources={name:([left,right] if name=='tracking_error_pct' else left if name=='nav_return_version' else right)
        for name in payload}
    return payload,measured['status'],sources


def validate_etf_request(writer,request,payload,doc):
    """Called by FactWriter too: caller-supplied gate pass cannot bypass inputs."""
    product=request['profile'].split('/',1)[-1]
    contract=writer.contracts[request['contract_version']]
    if etf_contract(product,evidence=contract.evidence).document!=doc:raise WriterError('ETF_CONTRACT_MISMATCH')
    key=request['key'];on=day(key['valuation_date']);master=MasterDataService(writer.engine)
    if key['profile']!=request['profile']:raise WriterError('ETF_PROFILE_KEY_MISMATCH')
    identity=master.validate_lifecycle(request['instrument_version'],key['instrument_id'],on)
    if product=='shares':
        uri=urlsplit(request['source_uri'])
        if identity['exchange']!='SSE' or uri.scheme!='https' or uri.username or uri.password or not uri.hostname or not (uri.hostname=='sse.com.cn' or uri.hostname.endswith('.sse.com.cn')):raise WriterError('ETF_SHARE_SOURCE_UNVERIFIED')
    trading=master.trading_days(request['calendar_version'],identity['exchange'],on,on+timedelta(days=1))
    if product in {'iopv','tracking'} and on not in trading:raise WriterError('NOT_TRADING_DAY')
    today=instant(writer.clock()).replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
    if on>today:raise WriterError('ETF_VALUATION_IN_FUTURE')
    try:
        evidence=request['etf_evidence'];proof=json.loads(evidence)
        if type(evidence) is not bytes or canonical(proof).encode()!=evidence:raise ValueError()
    except (KeyError,TypeError,ValueError,UnicodeError):raise WriterError('ETF_INPUT_EVIDENCE_REQUIRED') from None
    if product in {'nav','shares','iopv'}:
        if set(proof)!={'kind','row_index','raw_hash'} or proof['kind']!='source' or proof['raw_hash']!=digest(request['raw_response']) or type(proof['row_index']) is not int:raise WriterError('ETF_SOURCE_EVIDENCE_INVALID')
        raw=json.loads(request['raw_response'])
        if set(raw)!={'format','product','symbol','rows'} or raw['format']!='etf-records-v1' or raw['product']!=product or canonical(raw).encode()!=request['raw_response'] or not 0<=proof['row_index']<len(raw['rows']):raise WriterError('ETF_SOURCE_EVIDENCE_INVALID')
        row=raw['rows'][proof['row_index']]
        codes=[r['code'] for r in master.instrument_snapshot(request['instrument_version'])['codes'] if r['instrument_id']==key['instrument_id'] and day(r['valid_from'])<=on and (r['valid_to'] is None or on<day(r['valid_to']))]
        if len(codes)!=1 or raw['symbol']!={'SSE':'sh','SZSE':'sz'}[identity['exchange']]+codes[0]:raise WriterError('ETF_SYMBOL_MISMATCH')
        if row['valuation_date']!=key['valuation_date'] or row['share_class']!=key['share_class']:raise WriterError('ETF_SOURCE_KEY_MISMATCH')
        normalized=contract.normalize({**request,'wire_record':_source_wire(product,row)})
        if normalized!=payload:raise WriterError('ETF_SOURCE_PAYLOAD_MISMATCH')
        if product=='iopv':
            observed=instant(payload['observation_at'])
            if observed>instant(writer.clock()) or observed.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()!=on or instant(key['observation_at'])!=observed:raise WriterError('ETF_OBSERVATION_INVALID')
        return {},{}
    if request['raw_response']!=evidence or proof.get('kind')!=product:raise WriterError('ETF_DERIVED_EVIDENCE_INVALID')
    as_of=instant(proof['as_of'])
    if as_of>instant(writer.clock()):raise WriterError('ETF_ASOF_IN_FUTURE')
    expected,status,sources=_size(writer,proof,key) if product=='size' else _tracking(writer,proof,key,request['calendar_version'])
    if payload!=expected:raise WriterError('ETF_DERIVED_PAYLOAD_MISMATCH')
    return {('P10' if product=='size' else 'P11'):status},sources


class ETFService:
    def __init__(self,writer):self.writer=writer

    def source_requests(self,capture, *, product,contract_version,instrument_id,instrument_version,calendar_version,source_uri,batch_id,start,end_exclusive):
        if product not in {'nav','shares','iopv'}:raise WriterError('ETF_PRODUCT_UNREGISTERED')
        raw=json.loads(capture)
        if type(capture) is not bytes or canonical(raw).encode()!=capture or set(raw)!={'format','product','symbol','rows'} or raw['format']!='etf-records-v1' or raw['product']!=product or type(raw['rows']) is not list:raise WriterError('ETF_SOURCE_EVIDENCE_INVALID')
        first,end=day(start),day(end_exclusive)
        if end<=first:raise WriterError('ETF_SOURCE_RANGE_INVALID')
        requests=[];seen=set()
        for index,row in enumerate(raw['rows']):
            on=day(row['valuation_date']);key={'instrument_id':instrument_id,'valuation_date':on.isoformat(),'profile':'etf/'+product,'share_class':row['share_class']}
            if not first<=on<end:raise WriterError('ETF_SOURCE_RANGE_INVALID')
            if product=='iopv':key['observation_at']=row['observation_at']
            from app.services.governance_fact_contracts import logical_identity
            marker=logical_identity('etf',key)[0]
            if marker in seen:raise WriterError('DUPLICATE_BUSINESS_KEY')
            seen.add(marker)
            request=self._request(product,contract_version,key,_source_wire(product,row),instrument_version,calendar_version,source_uri,batch_id,capture,
                canonical({'kind':'source','row_index':index,'raw_hash':digest(capture)}).encode())
            self.writer.preview(request);requests.append(request)
        return requests

    def _request(self,product,contract,key,wire,instruments,calendar,uri,batch,raw,proof):
        on=day(key['valuation_date'])
        return {'dataset':'etf','key':key,'contract_version':contract,'profile':'etf/'+product,'instrument_version':instruments,
            'calendar_version':calendar,'wire_record':wire,'raw_response':raw,'source_uri':uri,'batch_id':batch,
            'time':{'event_date':on.isoformat(),'publication_precision':'unknown','published_at':None,'publication_date':None,
                'valid_from':on.isoformat(),'valid_to':(on+timedelta(days=1)).isoformat(),'publication_evidence':None},
            'revision':{'kind':'original','token':None,'order':None,'order_evidence':None,'supersedes_version':None},
            'flags':{},'gate_results':{},'field_sources':{},'etf_evidence':proof}

    def ingest(self,*args,**kwargs):
        requests=self.source_requests(*args,**kwargs)
        with self.writer.engine.begin() as db:return [self.writer.write(r,connection=db) for r in requests]

    def register_returns(self,document, *, source_evidence,review_evidence):
        doc=json.loads(canonical(document))
        if set(doc)!={'kind','instrument_id','instrument_version','share_class','benchmark_id','calendar_version','exchange','currency','unit','points'} or doc['kind'] not in {'dividend_reinvested','total_return'} or doc['currency']!='CNY' or doc['unit']!='total_return_index':raise WriterError('ETF_TOTAL_RETURN_REQUIRED')
        if type(source_evidence) is not bytes or not source_evidence or type(review_evidence) is not bytes or not review_evidence:raise WriterError('ETF_RETURN_EVIDENCE_REQUIRED')
        points=doc['points']
        if type(points) is not list or not 1<=len(points)<=61:raise WriterError('ETF_RETURN_POINTS_INVALID')
        dates=[]
        for p in points:
            if type(p) is not dict or set(p)!={'date','value'} or Decimal(_decimal(p['value'],16))<=0:raise WriterError('ETF_RETURN_POINTS_INVALID')
            dates.append(day(p['date']).isoformat())
        if dates!=sorted(set(dates)):raise WriterError('ETF_RETURN_POINTS_INVALID')
        today=instant(self.writer.clock()).replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
        if day(dates[-1])>today:raise WriterError('ETF_RETURN_POINTS_IN_FUTURE')
        master=MasterDataService(self.writer.engine)
        trading=master.trading_days(doc['calendar_version'],doc['exchange'],dates[0],day(dates[-1])+timedelta(days=1))
        if not set(dates)<={d.isoformat() for d in trading}:raise WriterError('ETF_RETURN_POINTS_INVALID')
        if doc['kind']=='dividend_reinvested':
            for on in dates:
                identity=master.validate_lifecycle(doc['instrument_version'],doc['instrument_id'],on)
                if identity['asset_type']!='etf' or identity['exchange']!=doc['exchange']:raise WriterError('ETF_RETURN_IDENTITY_MISMATCH')
            if type(doc['instrument_id']) is not int or not doc['share_class'] or not isinstance(doc['benchmark_id'],str) or not doc['benchmark_id'].strip():raise WriterError('ETF_RETURN_IDENTITY_MISMATCH')
        elif doc['instrument_id'] is not None or doc['instrument_version'] is not None or doc['share_class'] is not None or not doc['benchmark_id']:raise WriterError('ETF_RETURN_IDENTITY_MISMATCH')
        identity={'document':doc,'source_hash':digest(source_evidence),'review_hash':digest(review_evidence)}
        version=hash_object('etf-return-series-v1',identity)
        with self.writer.engine.begin() as db:
            values={'series_version':version,'document_json':canonical(doc),'source_evidence':source_evidence,'review_evidence':review_evidence,'available_at':instant(self.writer.clock())}
            if db.dialect.name=='mysql':
                from sqlalchemy.dialects.mysql import insert
                command=insert(e.return_inputs).values(**values).on_duplicate_key_update(series_version=version)
            else:
                from sqlalchemy.dialects.sqlite import insert
                command=insert(e.return_inputs).values(**values).on_conflict_do_nothing(index_elements=['series_version'])
            db.execute(command)
        return version

    def derive_request(self,product, *, contract_version,instrument_id,instrument_version,calendar_version,valuation_date,
                       share_class,as_of,nav_data_version=None,shares_data_version=None,nav_return_version=None,
                       benchmark_return_version=None,benchmark_id=None):
        key={'instrument_id':instrument_id,'valuation_date':day(valuation_date).isoformat(),'profile':'etf/'+product,'share_class':share_class}
        proof={'kind':product,'as_of':as_of}
        if product=='size':proof.update(nav_data_version=nav_data_version,shares_data_version=shares_data_version);payload,status,sources=_size(self.writer,proof,key)
        elif product=='tracking':proof.update(nav_return_version=nav_return_version,benchmark_return_version=benchmark_return_version,benchmark_id=benchmark_id);payload,status,sources=_tracking(self.writer,proof,key,calendar_version)
        else:raise WriterError('ETF_PRODUCT_UNREGISTERED')
        encoded=canonical(proof).encode()
        request=self._request(product,contract_version,key,payload,instrument_version,calendar_version,'candidate://etf/'+product,'etf-derived',encoded,encoded)
        request['field_sources']=sources
        self.writer.preview(request)
        return request

    def official_premium(self,nav_version,raw_version, *, as_of):
        when=instant(as_of)
        if when>instant(self.writer.clock()):raise WriterError('ETF_ASOF_IN_FUTURE')
        nav=_known(self.writer,nav_version,when,'etf/nav');raw=_known(self.writer,raw_version,when,instrument_id=nav['key']['instrument_id'])
        if raw['dataset']!='daily_bars':raise WriterError('ETF_DEPENDENCY_PROFILE_MISMATCH')
        gate=evaluate_price_gate('P09',asset_type='etf',inputs={'raw_close':raw['payload']['raw_close'],'nav_official':nav['payload']['nav_official'],
            'trade_date':raw['key']['trade_date'],'valuation_date':nav['key']['valuation_date'],'as_of':as_of,
            'nav_available_at':nav['available_at'].replace(tzinfo=timezone.utc).isoformat(),'currency':'CNY','nav_currency':nav['payload']['currency']})
        return {'status':gate['status'],'premium_official_pct':_decimal(gate['value'],10) if gate['status']=='pass' else None,
                'nav_data_version':nav_version,'raw_data_version':raw_version}


def capture_etf(db, *, product,symbol,start,end_exclusive,share_class):
    """Existing managed NAV/IOPV transports; explicit record archive only.

    Quote f38 shares are not relabelled as official dated share evidence. Shares
    require the separately reviewed SSE input; no new official HTTP route here.
    """
    if product not in {'nav','iopv'}:raise WriterError('ETF_SHARE_SOURCE_UNVERIFIED')
    if not isinstance(symbol,str) or len(symbol)!=8 or symbol[:2] not in {'sh','sz'} or not symbol[2:].isdigit():raise WriterError('ETF_SYMBOL_MISMATCH')
    first,end=day(start),day(end_exclusive)
    if end<=first:raise WriterError('ETF_SOURCE_RANGE_INVALID')
    rows=[]
    if product=='nav':
        import akshare as ak
        from app.services.akshare_utils import call_akshare_with_retry,quiet_akshare_output
        from app.services.market_data import _proxy_bypass
        with _proxy_bypass(),quiet_akshare_output():
            frame=call_akshare_with_retry(ak.fund_etf_fund_info_em,fund=symbol[2:],start_date=first.strftime('%Y%m%d'),
                end_date=(end-timedelta(days=1)).strftime('%Y%m%d'),api_key='fund_etf_fund_info_em',db=db,max_attempts=2)
        for record in frame.to_dict('records'):
            on=day(str(record['净值日期'])[:10])
            if not first<=on<end:raise WriterError('ETF_SOURCE_RANGE_INVALID')
            rows.append({'valuation_date':on.isoformat(),'share_class':share_class,'basis':'official_NAV','unit':'CNY','value':record['单位净值']})
    else:
        from app.services.etf_provider import fetch_etf_quotes
        quote=fetch_etf_quotes(db,[symbol[2:]]).get(symbol[2:])
        if quote:
            if quote.get('nav_basis')!='iopv':raise WriterError('ETF_SOURCE_BASIS_OR_UNIT_MISMATCH')
            on=day(quote['trade_date'])
            if not first<=on<end:raise WriterError('ETF_SOURCE_RANGE_INVALID')
            rows=[{'valuation_date':on.isoformat(),'share_class':share_class,'basis':'IOPV','unit':'CNY',
                'value':quote['nav'],'observation_at':quote['updated_at']}]
    for row in rows:
        if type(row['value']) is float:
            if not math.isfinite(row['value']):raise WriterError('ETF_NUMERIC_INVALID')
            row['value']=str(row['value'])
    return canonical({'format':'etf-records-v1','product':product,'symbol':symbol,'rows':rows}).encode()
