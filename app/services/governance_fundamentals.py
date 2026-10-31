"""Frozen candidate valuation/report adapters; no production engine or promotion.

Captured records retain supplier fields. Unknown financial definitions remain
NULL rather than becoming aliases for the R1 core. Publication dates are date
precision; a same-day correction is never ordered by arrival or numeric value.
"""
from copy import deepcopy
from datetime import timedelta
import json
import re
import math
from sqlalchemy import select

from app.db import governance_schema as schema
from app.services.data_contract_profiles import assess_profile, GLOBAL_GATES
from app.services.governance_fact_contracts import WriterContract, WriterError, canonical, day, digest, instant, logical_identity
from app.services.governance_master_data import MasterDataService

VALUATION_DEFINITION={'version':1,'pe':'TTM_parent_net_profit','pb':'MRQ_parent_equity','price':'raw','currency':'CNY'}
FINANCIAL_DEFINITION={'version':1,'scope':'consolidated','profit':'parent','revenue':'operating',
                      'period':'year_to_date','roe':'weighted_period'}
PRODUCTS={
    'eastmoney_valuation':('eastmoney','stock_value_em','valuation'),
    'eastmoney_financial':('eastmoney','stock_financial_analysis_indicator_em','financial'),
    'baostock_valuation':('baostock','BAO:query_history_k_data_plus','valuation'),
}


def fundamental_contract(product, *, evidence, profile=None, sdk=None, sdk_evidence=None):
    if product=='baostock_financial':
        raise WriterError('BAOSTOCK_FINANCIAL_DEFINITION_UNVERIFIED')
    if product not in PRODUCTS:raise WriterError('FUNDAMENTAL_PRODUCT_UNREGISTERED')
    provider,endpoint,dataset=PRODUCTS[product]
    profile=profile or dataset+'/core'
    if profile not in ({'valuation/core','valuation/pe'} if dataset=='valuation' else {'financial/core','financial/net_profit'}):
        raise WriterError('PROFILE_DATASET_MISMATCH')
    if dataset=='valuation':
        names={'pe_ttm_x':'PE_TTM','pb_mrq_x':'PB_MRQ','total_market_cap_cny':'TOTAL_MARKET_CAP',
               'float_market_cap_cny':'NOTLIMITED_MARKETCAP_A','definition_id':'definition_id'} if provider=='eastmoney' else {
               'pe_ttm_x':'peTTM','pb_mrq_x':'pbMRQ','definition_id':'definition_id'}
    else:
        names={'net_profit_parent':'PARENTNETPROFIT','roe_weighted_period_pct':'ROEJQ',
               'net_profit_yoy_pct':'PARENTNETPROFITTZ','revenue_operating':'unavailable_operating_revenue',
               'revenue_yoy_pct':'unavailable_operating_revenue_yoy'}
    fields={name:{'wire_field':wire,'native_unit':'x' if name.endswith('_x') else 'percentage_point' if name.endswith('_pct') else 'CNY',
                  'normalized_unit':'x' if name.endswith('_x') else 'percentage_point' if name.endswith('_pct') else 'CNY',
                  'scale':'1','offset':'0'} for name,wire in names.items()}
    return WriterContract.profile(dataset=dataset,profile=profile,provider=provider,endpoint=endpoint,
        asset_type='stock',instrument_family='cn_stock',definition=VALUATION_DEFINITION if dataset=='valuation' else FINANCIAL_DEFINITION,
        fields=fields,evidence=evidence,publication_timezone='Asia/Shanghai',sdk=sdk,sdk_evidence=sdk_evidence)


def capture_baostock_valuation(*, session_factory, symbol, start, end_exclusive):
    """Bounded SDK query in a caller-owned isolated session, under M04 controls.

    Session factory must own login/logout and isolation. Never reuse the global
    application's default session; no network session is constructed on import.
    """
    from app.services.data_source_execution import source_boundary
    start,end=day(start),day(end_exclusive)
    if end<=start or not re.fullmatch(r'(sh|sz)\.[0-9]{6}',symbol):raise WriterError('CAPTURE_SCOPE_INVALID')
    def acquire():
        with session_factory() as sdk:
            fields=['date','code','peTTM','pbMRQ']
            result=sdk.query_history_k_data_plus(symbol,','.join(fields),start_date=start.isoformat(),
                end_date=(end-timedelta(days=1)).isoformat(),frequency='d',adjustflag='3')
            if result.error_code!='0' or result.fields!=fields:raise WriterError('BAOSTOCK_RESPONSE_INVALID')
            rows=[]
            while result.next():
                if result.error_code!='0':raise WriterError('BAOSTOCK_RESPONSE_INVALID')
                values=result.get_row_data()
                if len(values)!=len(fields) or len(rows)>=(end-start).days:raise WriterError('BAOSTOCK_RESPONSE_INVALID')
                rows.append(dict(zip(fields,values)))
            if result.error_code!='0':raise WriterError('BAOSTOCK_RESPONSE_INVALID')
            return canonical({'format':'fundamental-records-v1','product':'baostock_valuation','rows':rows}).encode()
    return source_boundary('BAO:query_history_k_data_plus')(acquire)()


def capture_eastmoney_fundamentals(db, *, product, symbol, start, end_exclusive, should_cancel=None):
    """Archive interpreted provider records via the existing bounded transports.

    These are explicit record bytes, not supplier HTTP bytes. The valuation
    transport's projection is reversed by exact field names, never by magnitude.
    Financial total revenue is retained for audit only, not relabelled operating.
    """
    start,end=day(start),day(end_exclusive)
    if end<=start:raise WriterError('CAPTURE_SCOPE_INVALID')
    if product=='eastmoney_valuation':
        from app.services.valuation_provider import fetch_valuation_rows
        fetched=fetch_valuation_rows(db,[symbol],start,end-timedelta(days=1),should_cancel=should_cancel)
        rows=[{'SECURITY_CODE':r['symbol'],'TRADE_DATE':r['trade_date'].isoformat(),
               'PE_TTM':r['pe_ttm'],'PB_MRQ':r['pb'],'TOTAL_MARKET_CAP':r['total_market_cap'],
               'NOTLIMITED_MARKETCAP_A':r['circulating_market_cap']} for r in fetched]
    elif product=='eastmoney_financial':
        from app.services.financial_provider import fetch_financial_rows
        rows=fetch_financial_rows(db,symbol,start,end-timedelta(days=1),should_cancel=should_cancel)
    else:raise WriterError('FUNDAMENTAL_PRODUCT_UNREGISTERED')
    def encoded(value):
        if type(value) is float:
            if not math.isfinite(value):raise WriterError('INVALID_NUMERIC_VALUE')
            return str(value)
        if type(value) is dict:return {k:encoded(v) for k,v in value.items()}
        if type(value) is list:return [encoded(v) for v in value]
        return value
    return canonical({'format':'fundamental-records-v1','product':product,'rows':encoded(rows)}).encode()


class FundamentalSyncService:
    def __init__(self,writer):
        self.writer=writer
        self.master=MasterDataService(writer.engine)

    def requests(self,capture, *, product, contract_version, instrument_id, instrument_version,
                 calendar_version, start, end_exclusive, source_uri, batch_id, revisions=None,
                 profile=None):
        """Pure preparation; rows cannot supply canonical field names or dates."""
        if product=='baostock_financial':raise WriterError('BAOSTOCK_FINANCIAL_DEFINITION_UNVERIFIED')
        contract=self.writer.contracts.get(contract_version)
        if contract is None:raise WriterError('UNIT_CONTRACT_UNREGISTERED')
        expected=fundamental_contract(product,evidence=contract.evidence,
            profile=contract.document['profile'],
            sdk={k:contract.document['sdk'][k] for k in ('name','version')} if contract.document['sdk'] else None,
            sdk_evidence=contract.sdk_evidence)
        if contract.document!=expected.document:raise WriterError('FUNDAMENTAL_CONTRACT_MISMATCH')
        if type(capture) is not bytes or not 0<len(capture)<=32*1024*1024:raise WriterError('CAPTURE_INVALID')
        try:
            doc=json.loads(capture)
            if set(doc)!={'format','product','rows'} or doc['format']!='fundamental-records-v1' or doc['product']!=product or type(doc['rows']) is not list or canonical(doc).encode()!=capture:raise ValueError()
        except (ValueError,TypeError,UnicodeError):raise WriterError('CAPTURE_INVALID') from None
        start,end=day(start),day(end_exclusive)
        if end<=start:raise WriterError('CAPTURE_SCOPE_INVALID')
        revisions=deepcopy(revisions or {})
        dataset=PRODUCTS[product][2];profile=profile or dataset+'/core'
        if profile not in ({'valuation/core','valuation/pe'} if dataset=='valuation' else {'financial/core','financial/net_profit'}):
            raise WriterError('PROFILE_DATASET_MISMATCH')
        # The writer contract has a frozen profile. A net-profit request uses a
        # corresponding independently pinned contract; no runtime relaxation.
        if profile!=contract.document['profile']:raise WriterError('PROFILE_CONTRACT_MISMATCH')
        result=[];seen=set()
        for row in doc['rows']:
            if type(row) is not dict:raise WriterError('CAPTURE_ROW_INVALID')
            if dataset=='financial':
                allowed={'SECUCODE','REPORT_DATE','NOTICE_DATE','ROEJQ','PARENTNETPROFIT','TOTALOPERATEREVE','PARENTNETPROFITTZ','TOTALOPERATEREVETZ'}
                period=day(str(row.get('REPORT_DATE',''))[:10]);publication=day(str(row.get('NOTICE_DATE',''))[:10])
                report_type={(3,31):'q1',(6,30):'half_year',(9,30):'q3',(12,31):'annual'}.get((period.month,period.day))
                if report_type is None or publication<period:raise WriterError('FINANCIAL_PERIOD_INVALID')
                on=period;scope=publication;symbol=row.get('SECUCODE')
                key={'instrument_id':instrument_id,'report_period':period.isoformat(),'report_type':report_type,
                     'definition_id':contract.document['definition_id']}
            else:
                bao=product=='baostock_valuation'
                allowed={'date','code','peTTM','pbMRQ'} if bao else {'SECURITY_CODE','TRADE_DATE','PE_TTM','PB_MRQ','TOTAL_MARKET_CAP','NOTLIMITED_MARKETCAP_A'}
                on=day(str(row.get('date' if bao else 'TRADE_DATE',''))[:10]);scope=on;publication=None
                symbol=row.get('code' if bao else 'SECURITY_CODE')
                key={'instrument_id':instrument_id,'trade_date':on.isoformat(),'definition_id':contract.document['definition_id']}
            if set(row)-allowed:raise WriterError('UNEXPECTED_WIRE_FIELD')
            if not start<=scope<end:raise WriterError('CAPTURE_OUTSIDE_SCOPE')
            identity=self.master.validate_lifecycle(instrument_version,instrument_id,on)
            if identity['asset_type']!='stock':raise WriterError('ASSET_TYPE_MISMATCH')
            code_rows=self.master.instrument_snapshot(instrument_version)['codes']
            codes=[r['code'] for r in code_rows if r['instrument_id']==instrument_id and day(r['valid_from'])<=on and (r['valid_to'] is None or on<day(r['valid_to']))]
            exchange={'SSE':'SH','SZSE':'SZ','BSE':'BJ'}[identity['exchange']]
            wanted=(codes[0]+'.'+exchange if dataset=='financial' else exchange.lower()+'.'+codes[0] if product=='baostock_valuation' else codes[0]) if len(codes)==1 else None
            if wanted is None or symbol!=wanted:raise WriterError('CAPTURE_SYMBOL_MISMATCH')
            if dataset=='valuation' and on not in self.master.trading_days(calendar_version,identity['exchange'],on,on+timedelta(days=1)):
                raise WriterError('NOT_TRADING_DAY')
            token=canonical(key)
            if token in seen:raise WriterError('DUPLICATE_BUSINESS_KEY_REQUIRES_SEPARATE_REVISION')
            seen.add(token)
            wire={rule['wire_field']:row.get(rule['wire_field']) for rule in contract.document['fields'].values()}
            if dataset=='valuation':wire['definition_id']=contract.document['definition_id']
            for name,value in wire.items():
                if value=='':wire[name]=None  # BaoStock uses empty string for unknown.
            rev=revisions.get(on.isoformat(),{'kind':'original','token':None,'order':None,'order_evidence':None,'supersedes_version':None})
            request={'dataset':dataset,'key':key,'contract_version':contract_version,'profile':profile,
                'instrument_version':instrument_version,'calendar_version':calendar_version if dataset=='valuation' else None,
                'wire_record':wire,'raw_response':capture,'source_uri':source_uri,
                'time':{'event_date':on.isoformat(),'publication_precision':'date' if publication else 'unknown',
                    'publication_date':publication.isoformat() if publication else None,'published_at':None,
                    'valid_from':on.isoformat(),'valid_to':(on+timedelta(days=1)).isoformat(),
                    'publication_evidence':capture if publication else None},'revision':rev,
                'flags':{},'gate_results':{},'field_sources':{},'batch_id':batch_id}
            if rev['kind']=='withdrawal':request['wire_record']={'retraction_reason':'supplier explicit withdrawal'}
            self.writer.preview(request);result.append(request)
        if set(revisions)-{r['time']['event_date'] for r in result}:raise WriterError('UNBOUND_REVISION')
        return result

    def ingest(self,capture,**kwargs):
        requests=self.requests(capture,**kwargs)
        with self.writer.engine.begin() as connection:
            results=[self.writer.write(r,connection=connection) for r in requests]
        return {'status':'staged' if results else 'empty','results':results,'coverage_complete':False,
                'production_admission':'not_certified','capture_hash':digest(capture)}

    def read_as_of(self, *, dataset, key, profile, as_of, knowledge_mode):
        """Candidate accepted history only. M08 adds published snapshot membership."""
        if dataset not in {'valuation','financial'} or not profile.startswith(dataset+'/') or knowledge_mode not in {'public_known','system_known'}:
            raise WriterError('ASOF_REQUEST_INVALID')
        cutoff=instant(as_of);logical,_=logical_identity(dataset,key)
        with self.writer.engine.connect() as db:
            rows=db.execute(select(schema.acceptance_history).where(schema.acceptance_history.c.dataset==dataset,
                schema.acceptance_history.c.logical_key==logical)).mappings().all()
        versions={r['data_version'] for r in rows if knowledge_mode=='public_known' or
            r['system_from']<=cutoff and (r['system_to'] is None or cutoff<r['system_to'])}
        facts=[]
        for version in versions:
            fact=self.writer.fact(version)
            known=fact['public_available_at'] if knowledge_mode=='public_known' else fact['available_at']
            if known is not None and known<=cutoff:
                if knowledge_mode=='public_known':self.writer.evidence(fact['publication_evidence_hash'])
                facts.append(fact)
        if not facts:return {'status':'missing','data_version':None}
        if knowledge_mode=='system_known' and len(facts)!=1:raise WriterError('ASOF_ACCEPTANCE_AMBIGUOUS')
        tips=[f for f in facts if f['data_version'] not in {g['supersedes_version'] for g in facts}]
        if len(tips)!=1:raise WriterError('ASOF_VERSION_AMBIGUOUS')
        fact=tips[0]
        if knowledge_mode=='public_known':
            by_id={f['data_version']:f for f in facts};visited=set();cursor=fact
            while cursor:
                if cursor['data_version'] in visited:raise WriterError('ASOF_VERSION_AMBIGUOUS')
                visited.add(cursor['data_version']);cursor=by_id.get(cursor['supersedes_version'])
            if visited!=set(by_id):raise WriterError('ASOF_VERSION_AMBIGUOUS')
        if fact['revision_kind']=='withdrawal':return {'status':'withdrawn','data_version':fact['data_version']}
        gates=dict.fromkeys(GLOBAL_GATES,'pass')
        requested=assess_profile(fact['payload'],profile,gate_results=gates)
        core=assess_profile(fact['payload'],dataset+'/core',gate_results=gates)
        return {'status':'pass' if requested.quality_status=='complete' else 'QUALITY_BLOCKED',
            'data_version':fact['data_version'],'payload':fact['payload'],'quality_status':core.quality_status,
            'request_quality_status':requested.quality_status,'missing_fields':list(requested.missing_fields),
            'definition_id':fact['definition_id'],'source_tag':fact['source_tag'],'available_at':fact['available_at']}
