"""Candidate fund-flow, institution-seat and popularity facts with receipt evidence.

Only FactWriter mutates facts. The separate additive ledger records scope and
empty query evidence, without granting availability or production coverage.
"""
from datetime import datetime,timedelta,timezone
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP,localcontext
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import json
import math
from sqlalchemy import select
from app.db import governance_activity_schema as a
from app.db import governance_schema as s
from app.db.governance_migration import migrate_candidate
from app.services.governance_fact_contracts import WriterContract,WriterError,canonical,digest,hash_object,day,instant,logical_identity
from app.services.governance_master_data import MasterDataService

FLOW_DEFINITION='eastmoney-main-superlarge-plus-large-CNY-v1'
RANK_METHOD='eastmoney-stock-popularity-current-v1'
DEDUP='reason-seat-union-v1'
REASON_SCOPE='all_disclosed_reasons'
PRODUCTS={'flow_history':('capital_flow','stock_individual_fund_flow'),
          'lhb_institution':('lhb','stock_lhb_jgmmtj_em'),'hot_rank':('hot_rank','stock_hot_rank_em')}
FLOW_FIELDS={'main':'主力净流入-净额','super_large':'超大单净流入-净额','large':'大单净流入-净额',
             'medium':'中单净流入-净额','small':'小单净流入-净额'}


def install_activity_schema(engine):return migrate_candidate(engine,metadata=a.metadata,revision='DSG-ACTIVITY-001')


def activity_contract(product, *, evidence, profile=None):
    if product not in PRODUCTS:raise WriterError('ACTIVITY_PRODUCT_UNREGISTERED')
    dataset,endpoint=PRODUCTS[product];profile=profile or dataset+'/core'
    if profile not in ({'capital_flow/core','capital_flow/main'} if dataset=='capital_flow' else {dataset+'/core'}):
        raise WriterError('PROFILE_DATASET_MISMATCH')
    names=list(FLOW_FIELDS)+['flow_definition'] if dataset=='capital_flow' else (
        ['institution_buy','institution_sell','institution_net','reason_scope','dedup_rule_id'] if dataset=='lhb' else
        ['rank','ranking_method','observation_at','total','pct'])
    fields={n:{'wire_field':n,'native_unit':'percentage_point' if n=='pct' else 'CNY',
               'normalized_unit':'percentage_point' if n=='pct' else 'CNY','scale':'1','offset':'0'} for n in names}
    definition={'flow_definition':FLOW_DEFINITION,'main':'super_large+large','unit':'CNY','scale':8} if dataset=='capital_flow' else (
        {'reason_scope':REASON_SCOPE,'dedup_rule_id':DEDUP,'unit':'CNY','net':'buy-sell'} if dataset=='lhb' else
        {'ranking_method':RANK_METHOD,'pct':'100*(total-rank+1)/total','unknown_total':'NULL'})
    return WriterContract.profile(dataset=dataset,profile=profile,provider='eastmoney',endpoint=endpoint,
        asset_type='stock',instrument_family='cn_stock',definition=definition,fields=fields,evidence=evidence,publication_timezone='Asia/Shanghai')


def _amount(value):
    if value is None or value=='':return None
    if isinstance(value,(float,bool)):raise WriterError('INVALID_NUMERIC_VALUE')
    try:
        with localcontext() as ctx:
            ctx.prec=80
            result=Decimal(value).quantize(Decimal('0.00000001'),rounding=ROUND_HALF_UP)
            if not result.is_finite() or abs(result)>=Decimal(10)**20:raise ValueError()
            return format(abs(result) if result==0 else result,'f')
    except (ValueError,InvalidOperation,TypeError):raise WriterError('INVALID_NUMERIC_VALUE') from None


def _flow(row):
    allowed={'日期',*FLOW_FIELDS.values(),'主力净流入-净占比','小单净流入-净占比','中单净流入-净占比',
             '大单净流入-净占比','超大单净流入-净占比','收盘价','涨跌幅','reserved_1','reserved_2'}
    if set(row)-allowed:raise WriterError('UNEXPECTED_WIRE_FIELD')
    values={n:_amount(row.get(w)) for n,w in FLOW_FIELDS.items()}
    if all(values[n] is not None for n in ('main','super_large','large')):
        if Decimal(values['main'])!=Decimal(values['super_large'])+Decimal(values['large']):raise WriterError('FLOW_MAIN_IDENTITY_MISMATCH')
    return {**values,'flow_definition':FLOW_DEFINITION}


def _lhb(row,instrument_id,on):
    allowed={'代码','上榜日期','上榜原因','机构买入总额','机构卖出总额','机构买入净额','seat_evidence','reason_rows'}
    if set(row)-allowed:raise WriterError('UNEXPECTED_WIRE_FIELD')
    native={n:_amount(row.get(w)) for n,w in [('buy','机构买入总额'),('sell','机构卖出总额'),('net','机构买入净额')]}
    if any(native[n] is not None and Decimal(native[n])<0 for n in ('buy','sell')):raise WriterError('LHB_SEAT_AMOUNT_INVALID')
    if all(v is not None for v in native.values()) and Decimal(native['net'])!=Decimal(native['buy'])-Decimal(native['sell']):raise WriterError('LHB_NET_IDENTITY_MISMATCH')
    if 'reason_rows' in row:
        if type(row['reason_rows']) is not list:raise WriterError('LHB_SEAT_EVIDENCE_INVALID')
        for original in row['reason_rows']:
            if type(original) is not dict or original.get('代码')!=row['代码'] or original.get('上榜日期')!=on.isoformat():raise WriterError('LHB_SEAT_BINDING_MISMATCH')
            _lhb({k:v for k,v in original.items() if k in allowed and k!='reason_rows'},instrument_id,on)
    result={'institution_buy':None,'institution_sell':None,'institution_net':None,
            'reason_scope':REASON_SCOPE,'dedup_rule_id':DEDUP}
    proof=row.get('seat_evidence')
    if proof is None:return result  # Aggregates have no identity for a reason union.
    if type(proof) is not dict or set(proof)!={'source_uri','all_reasons','complete','facts'} or proof['complete'] is not True:
        raise WriterError('LHB_SEAT_EVIDENCE_INVALID')
    parsed=urlsplit(proof['source_uri'])
    if parsed.scheme!='https' or parsed.username or parsed.password or not parsed.hostname or not any(
        parsed.hostname==host or parsed.hostname.endswith('.'+host) for host in ('sse.com.cn','szse.cn','bse.cn')):
        raise WriterError('LHB_SEAT_AUTHORITY_UNVERIFIED')
    reasons=proof['all_reasons']
    if type(reasons) is not list or not reasons or any(not isinstance(r,str) or not r.strip() for r in reasons) or len(set(reasons))!=len(reasons) or type(proof['facts']) is not list:
        raise WriterError('LHB_SEAT_EVIDENCE_INVALID')
    known_reasons=[r.get('上榜原因') for r in row.get('reason_rows',[])]+[row.get('上榜原因')]
    if any(reason and reason not in reasons for reason in known_reasons):raise WriterError('LHB_REASON_COVERAGE_INCOMPLETE')
    seen={};pairs={};covered=set();sums={'buy':Decimal(0),'sell':Decimal(0)}
    for fact in proof['facts']:
        if type(fact) is not dict or set(fact)!={'fact_id','seat_id','side','amount_cny','reason_id','instrument_id','trade_date','institution'}:
            raise WriterError('LHB_SEAT_EVIDENCE_INVALID')
        if fact['instrument_id']!=instrument_id or fact['trade_date']!=on.isoformat() or fact['reason_id'] not in reasons or fact['institution'] is not True or fact['side'] not in sums:
            raise WriterError('LHB_SEAT_BINDING_MISMATCH')
        if any(not isinstance(fact[n],str) or not fact[n].strip() for n in ('fact_id','seat_id')):raise WriterError('LHB_SEAT_EVIDENCE_INVALID')
        amount=_amount(fact['amount_cny'])
        if amount is None or Decimal(amount)<0:raise WriterError('LHB_SEAT_AMOUNT_INVALID')
        identity=(fact['seat_id'],fact['side'],amount);pair=identity[:2]
        if fact['fact_id'] in seen and seen[fact['fact_id']]!=identity:raise WriterError('LHB_DUPLICATE_CONFLICT')
        if pair in pairs and pairs[pair]!=fact['fact_id']:raise WriterError('LHB_SEAT_IDENTITY_AMBIGUOUS')
        pairs[pair]=fact['fact_id'];covered.add(fact['reason_id'])
        if fact['fact_id'] not in seen:sums[fact['side']]+=Decimal(amount)
        seen[fact['fact_id']]=identity
    if covered!=set(reasons):raise WriterError('LHB_REASON_COVERAGE_INCOMPLETE')
    return {**result,'institution_buy':_amount(str(sums['buy'])),'institution_sell':_amount(str(sums['sell'])),
            'institution_net':_amount(str(sums['buy']-sums['sell']))}


def _rank(row):
    if set(row)-{'sc','rk','observation_at','total','total_evidence'}:raise WriterError('UNEXPECTED_WIRE_FIELD')
    rank=row.get('rk');total=row.get('total')
    if type(rank) is not int or rank<=0:raise WriterError('RANK_INVALID')
    if total is not None:
        proof=row.get('total_evidence')
        if type(total) is not int or total<rank or type(proof) is not dict or set(proof)!={'ranking_method','observation_at','total','universe'} or proof!={
            'ranking_method':RANK_METHOD,'observation_at':row['observation_at'],'total':total,'universe':'all_ranked_stocks'}:
            raise WriterError('RANK_TOTAL_UNVERIFIED')
        with localcontext() as ctx:
            ctx.prec=80;pct=format((Decimal(100)*(total-rank+1)/total).quantize(Decimal('0.0000000001'),rounding=ROUND_HALF_UP),'f')
    else:
        if row.get('total_evidence') is not None:raise WriterError('RANK_TOTAL_UNVERIFIED')
        pct=None
    return {'rank':rank,'ranking_method':RANK_METHOD,'observation_at':row['observation_at'],'total':total,'pct':pct}


class ActivitySyncService:
    def __init__(self,writer):self.writer=writer;self.master=MasterDataService(writer.engine)

    def requests(self,capture, *, product,contract_version,instrument_id,instrument_version,calendar_version,
                 start,end_exclusive,source_uri,batch_id,revisions=None):
        contract=self.writer.contracts.get(contract_version)
        if contract is None:raise WriterError('UNIT_CONTRACT_UNREGISTERED')
        expected=activity_contract(product,evidence=contract.evidence,profile=contract.document['profile'])
        if contract.document!=expected.document:raise WriterError('ACTIVITY_CONTRACT_MISMATCH')
        if type(capture) is not bytes or not 0<len(capture)<=32*1024*1024:raise WriterError('CAPTURE_INVALID')
        try:
            doc=json.loads(capture)
            if type(doc) is not dict or set(doc)!={'format','product','symbol','rows'} or doc['format']!='activity-records-v1' or doc['product']!=product or type(doc['rows']) is not list or canonical(doc).encode()!=capture:raise ValueError()
        except (ValueError,TypeError,UnicodeError):raise WriterError('CAPTURE_INVALID') from None
        first,end=day(start),day(end_exclusive)
        if end<=first:raise WriterError('CAPTURE_SCOPE_INVALID')
        scope_identity=self.master.validate_lifecycle(instrument_version,instrument_id,first)
        if scope_identity['asset_type']!='stock':raise WriterError('ASSET_TYPE_MISMATCH')
        self.master.trading_days(calendar_version,scope_identity['exchange'],first,end)
        first_codes=[r['code'] for r in self.master.instrument_snapshot(instrument_version)['codes'] if r['instrument_id']==instrument_id and day(r['valid_from'])<=first and (r['valid_to'] is None or first<day(r['valid_to']))]
        if len(first_codes)!=1 or doc['symbol']!={'SSE':'sh','SZSE':'sz','BSE':'bj'}[scope_identity['exchange']]+first_codes[0]:raise WriterError('CAPTURE_SYMBOL_MISMATCH')
        dataset=PRODUCTS[product][0];revisions=revisions or {};requests=[];seen=set()
        for row in doc['rows']:
            if type(row) is not dict:raise WriterError('CAPTURE_ROW_INVALID')
            if dataset=='hot_rank':
                stamp=instant(row.get('observation_at'))
                local=stamp.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
                on=local.date()
                if stamp>instant(self.writer.clock()):raise WriterError('OBSERVATION_IN_FUTURE')
            else:on=day(row.get('日期' if dataset=='capital_flow' else '上榜日期'))
            if not first<=on<end:raise WriterError('CAPTURE_OUTSIDE_SCOPE')
            identity=self.master.validate_lifecycle(instrument_version,instrument_id,on)
            if identity['asset_type']!='stock':raise WriterError('ASSET_TYPE_MISMATCH')
            if on not in self.master.trading_days(calendar_version,identity['exchange'],on,on+timedelta(days=1)):raise WriterError('NOT_TRADING_DAY')
            codes=[r['code'] for r in self.master.instrument_snapshot(instrument_version)['codes'] if r['instrument_id']==instrument_id and day(r['valid_from'])<=on and (r['valid_to'] is None or on<day(r['valid_to']))]
            if len(codes)!=1:raise WriterError('CAPTURE_SYMBOL_MISMATCH')
            if doc['symbol']!={'SSE':'sh','SZSE':'sz','BSE':'bj'}[identity['exchange']]+codes[0]:raise WriterError('CAPTURE_SYMBOL_MISMATCH')
            if dataset=='lhb' and row.get('代码')!=codes[0]:raise WriterError('CAPTURE_SYMBOL_MISMATCH')
            if dataset=='hot_rank' and row.get('sc')!={'SSE':'SH','SZSE':'SZ','BSE':'BJ'}[identity['exchange']]+codes[0]:raise WriterError('CAPTURE_SYMBOL_MISMATCH')
            wire=_flow(row) if dataset=='capital_flow' else _lhb(row,instrument_id,on) if dataset=='lhb' else _rank(row)
            if dataset=='capital_flow':key={'instrument_id':instrument_id,'trade_date':on.isoformat(),'flow_definition':FLOW_DEFINITION}
            elif dataset=='lhb':key={'instrument_id':instrument_id,'trade_date':on.isoformat(),'reason_scope':REASON_SCOPE,'dedup_rule_id':DEDUP}
            else:key={'instrument_id':instrument_id,'ranking_method':RANK_METHOD,'observation_at':wire['observation_at']}
            marker=logical_identity(dataset,key)[0]
            if marker in seen:raise WriterError('DUPLICATE_BUSINESS_KEY')
            seen.add(marker)
            revision_key=wire['observation_at'] if dataset=='hot_rank' else on.isoformat()
            rev=revisions.get(revision_key,{'kind':'original','token':None,'order':None,'order_evidence':None,'supersedes_version':None})
            request={'dataset':dataset,'key':key,'contract_version':contract_version,'profile':contract.document['profile'],
                'instrument_version':instrument_version,'calendar_version':calendar_version,'wire_record':wire,
                'raw_response':capture,'source_uri':source_uri,'time':{'event_date':on.isoformat(),'publication_precision':'unknown',
                    'publication_date':None,'published_at':None,'valid_from':on.isoformat(),'valid_to':(on+timedelta(days=1)).isoformat(),'publication_evidence':None},
                'revision':rev,'flags':{},'gate_results':{},'field_sources':{},'batch_id':batch_id}
            if rev['kind']=='withdrawal':request['wire_record']={'retraction_reason':'explicit source withdrawal'}
            self.writer.preview(request);requests.append(request)
        if set(revisions)-{r['key'].get('observation_at',r['time']['event_date']) for r in requests}:raise WriterError('UNBOUND_REVISION')
        return requests

    def ingest(self,capture, *, empty_proof=None,**kwargs):
        requests=self.requests(capture,**kwargs)
        scope={n:kwargs[n] for n in ('product','instrument_id','instrument_version','calendar_version','start','end_exclusive','source_uri')}
        scope['start']=day(scope['start']).isoformat();scope['end_exclusive']=day(scope['end_exclusive']).isoformat()
        outcome='staged' if requests else 'empty_unverified'
        if empty_proof is not None:
            try:
                proof=json.loads(empty_proof)
                if type(empty_proof) is not bytes or canonical(proof).encode()!=empty_proof or set(proof)!={'scope','capture_hash','response'} or proof['scope']!=scope or proof['capture_hash']!=digest(capture):raise ValueError()
                response=proof['response']
                if requests or kwargs['product']!='lhb_institution' or canonical(response)!=canonical({'success':True,'result':{'count':0,'data':[]}}):raise ValueError()
            except (ValueError,TypeError,UnicodeError):raise WriterError('EMPTY_EVENT_EVIDENCE_INVALID') from None
            outcome='empty_verified'
        contract=self.writer.contracts[kwargs['contract_version']]
        with self.writer.engine.begin() as db:
            results=[self.writer.write(r,connection=db) for r in requests]
            document={'dataset':PRODUCTS[kwargs['product']][0],'scope':scope,'contract_version':contract.version,
                'capture_hash':digest(capture),'empty_proof_hash':digest(empty_proof) if empty_proof else None,
                'data_versions':[r['data_version'] for r in results],'outcome':outcome,'coverage_complete':False}
            receipt_id=hash_object('activity-receipt-v1',document)
            values={'receipt_id':receipt_id,'receipt_json':canonical(document),'capture_bytes':capture,'empty_proof':empty_proof,
                'contract_document':contract.document_json,'contract_evidence':contract.evidence,'observed_at':instant(self.writer.clock())}
            if db.dialect.name=='mysql':
                from sqlalchemy.dialects.mysql import insert
                command=insert(a.receipts).values(**values).on_duplicate_key_update(receipt_id=receipt_id)
            else:
                from sqlalchemy.dialects.sqlite import insert
                command=insert(a.receipts).values(**values).on_conflict_do_nothing(index_elements=['receipt_id'])
            db.execute(command)
        return {'receipt_id':receipt_id,'outcome':outcome,'results':results,'coverage_complete':False,'no_event_evidenced':outcome=='empty_verified'}

    def receipt(self,receipt_id):
        with self.writer.engine.connect() as db:
            row=db.execute(select(a.receipts).where(a.receipts.c.receipt_id==receipt_id)).mappings().one()
        doc=json.loads(row['receipt_json'])
        contract=WriterContract(row['contract_document'],bytes(row['contract_evidence']))
        if contract.version!=doc['contract_version']:raise WriterError('RECEIPT_HASH_MISMATCH')
        if hash_object('activity-receipt-v1',doc)!=receipt_id or digest(bytes(row['capture_bytes']))!=doc['capture_hash'] or (
            digest(bytes(row['empty_proof'])) if row['empty_proof'] is not None else None)!=doc['empty_proof_hash']:
            raise WriterError('RECEIPT_HASH_MISMATCH')
        return doc

    def latest_rank(self, *, instrument_id, cutoff):
        """Last accepted real snapshot before the system cutoff, never a top-N fill."""
        cutoff=instant(cutoff)
        with self.writer.engine.connect() as db:
            histories=db.execute(select(s.acceptance_history).where(s.acceptance_history.c.dataset=='hot_rank',
                s.acceptance_history.c.system_from<=cutoff,
                (s.acceptance_history.c.system_to.is_(None)) | (s.acceptance_history.c.system_to>cutoff))).mappings().all()
        facts=[]
        for history in histories:
            fact=self.writer.fact(history['data_version'])
            if fact['key']['instrument_id']==instrument_id and fact['key']['ranking_method']==RANK_METHOD and instant(fact['key']['observation_at'])<=cutoff and fact['available_at']<=cutoff:
                facts.append(fact)
        if not facts:return {'status':'missing','data_version':None}
        latest=max(instant(f['key']['observation_at']) for f in facts)
        selected=[f for f in facts if instant(f['key']['observation_at'])==latest]
        if len(selected)!=1:raise WriterError('RANK_SNAPSHOT_AMBIGUOUS')
        fact=selected[0]
        return {'status':'withdrawn' if fact['revision_kind']=='withdrawal' else 'pass','data_version':fact['data_version'],
                'payload':fact['payload'],'observation_at':fact['key']['observation_at']}


def capture_activity(db, *, product,symbol,start,end_exclusive,clock=lambda:datetime.now(timezone.utc)):
    """Explicit record archives through existing controlled supplier transports.

    Interpreted SDK records are archived, not advertised as original HTTP bytes.
    LHB raw reason rows remain separate evidence; legacy summed output is unused.
    Current popularity snapshots use acquisition time, never a requested past day.
    """
    from datetime import date
    if product not in PRODUCTS:raise WriterError('ACTIVITY_PRODUCT_UNREGISTERED')
    if not isinstance(symbol,str) or len(symbol)!=8 or symbol[:2] not in {'sh','sz','bj'} or not symbol[2:].isdigit():raise WriterError('CAPTURE_SYMBOL_MISMATCH')
    start,end=day(start),day(end_exclusive)
    if end<=start:raise WriterError('CAPTURE_SCOPE_INVALID')
    if product=='flow_history':
        from app.services.capital_flow_history_provider import fetch_capital_flow_history
        frame=fetch_capital_flow_history(stock=symbol[2:],market=symbol[:2])
        rows=frame.to_dict('records')
        rows=[r for r in rows if start<=day(str(r['日期'])[:10])<end]
    elif product=='lhb_institution':
        import akshare as ak
        from app.services.akshare_utils import call_akshare_with_retry,quiet_akshare_output
        from app.services.market_data import _proxy_bypass
        if (end-start).days>31:raise WriterError('CAPTURE_SCOPE_INVALID')
        with _proxy_bypass(),quiet_akshare_output():
            frame=call_akshare_with_retry(ak.stock_lhb_jgmmtj_em,start_date=start.strftime('%Y%m%d'),
                end_date=(end-timedelta(days=1)).strftime('%Y%m%d'),api_key='stock_lhb_jgmmtj_em',db=db,max_attempts=2)
        grouped={}
        for r in frame.to_dict('records'):
            if str(r.get('代码'))!=symbol[2:]:continue
            on=day(str(r['上榜日期'])[:10])
            if not start<=on<end:raise WriterError('CAPTURE_OUTSIDE_SCOPE')
            grouped.setdefault(on.isoformat(),[]).append(r)
        rows=[{'代码':symbol[2:],'上榜日期':on,'reason_rows':raw} for on,raw in sorted(grouped.items())]
    else:
        now=clock()
        local=instant(now).replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
        if not start<=local<end:raise WriterError('CURRENT_RANK_ONLY')
        from app.services.hot_rank_data import _fetch_hot_rank
        _,raw=_fetch_hot_rank(db,local)
        original=raw.get(symbol[2:])
        rows=[] if original is None else [json.loads(original)]
        rows=[{'sc':r['sc'],'rk':r['rk'],'observation_at':now.isoformat()} for r in rows]
    def encode(value):
        if type(value) is float:
            if math.isnan(value):return None
            if not math.isfinite(value):raise WriterError('INVALID_NUMERIC_VALUE')
            return str(value)
        if isinstance(value,(date,datetime)):return value.isoformat()
        if type(value) is dict:return {k:encode(v) for k,v in value.items()}
        if type(value) is list:return [encode(v) for v in value]
        return value
    return canonical({'format':'activity-records-v1','product':product,'symbol':symbol,'rows':encode(rows)}).encode()
