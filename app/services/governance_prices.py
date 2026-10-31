"""Explicit pinned factor inputs and immutable derived prices, never live cutover.

Membership is supplied explicitly. Event acquisition and complete as-of/window
selection belong to M06.2/M06.4; this service never infers absent events or raw.
"""
from contextlib import contextmanager
from datetime import datetime,date,timedelta,timezone
from decimal import Decimal,localcontext,ROUND_HALF_UP,InvalidOperation
import json,re
from sqlalchemy import select,insert
from sqlalchemy.exc import NoResultFound
from app.db import governance_schema as s
from app.db import governance_price_schema as p
from app.db.governance_migration import _candidate,migrate_candidate
from app.services.governance_source_state import migrate_source_state
from app.services.governance_master_data import MasterDataService
from app.services.governance_fact_contracts import WriterError,canonical,hash_object,digest,day,instant

ALGORITHM='r1-qfq-v1'


def version_id(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def install_price_schema(engine):
    _candidate(engine)
    migrate_source_state(engine)
    return migrate_candidate(engine,metadata=p.metadata,revision='DSG-PRICE-001')


def decimal_text(value,precision,scale):
    if isinstance(value,(bool,float)):raise WriterError('ADJUSTMENT_UNAVAILABLE:INVALID_DECIMAL')
    try:
        with localcontext() as context:
            context.prec=80
            number=Decimal(str(value))
            if not number.is_finite() or number<=0:raise ValueError()
            number=number.quantize(Decimal(1).scaleb(-scale),rounding=ROUND_HALF_UP)
            if number<=0 or number>=Decimal(10)**(precision-scale):raise ValueError()
            return format(number,'f')
    except (InvalidOperation,ValueError):raise WriterError('ADJUSTMENT_UNAVAILABLE:INVALID_DECIMAL') from None


class PriceService:
    def __init__(self,writer):
        _candidate(writer.engine)
        self.writer=writer;self.engine=writer.engine

    @contextmanager
    def _transaction(self,instrument):
        name='dsg-price:'+str(instrument)
        with self.engine.connect() as db:
            locked=False
            try:
                if db.dialect.name=='mysql':
                    locked=db.exec_driver_sql('SELECT GET_LOCK(%s,15)',(name,)).scalar()==1
                    db.commit()
                    if not locked:raise WriterError('PRICE_WRITER_BUSY')
                with db.begin():
                    if db.dialect.name=='sqlite':db.exec_driver_sql('BEGIN IMMEDIATE')
                    yield db
            finally:
                if locked:
                    db.exec_driver_sql('SELECT RELEASE_LOCK(%s)',(name,));db.commit()

    def raw(self,version):
        fact=self.writer.fact(version)
        if fact['dataset']!='daily_bars' or fact['revision_kind']=='withdrawal':raise WriterError('ADJUSTMENT_UNAVAILABLE:RAW_REQUIRED')
        captures=self.writer.price_evidence(version)
        return {**fact,'price_captures':captures}

    def _dependency(self,version,spec,dataset):
        try:fact=self.raw(version) if dataset=='daily_bars' else self.writer.fact(version)
        except NoResultFound:raise WriterError('ADJUSTMENT_UNAVAILABLE:DEPENDENCY_MISSING') from None
        if fact['dataset']!=dataset or fact['key'].get('instrument_id')!=spec['instrument_id'] or fact['revision_kind']=='withdrawal':
            raise WriterError('ADJUSTMENT_UNAVAILABLE:DEPENDENCY_MISMATCH')
        time=fact['available_at'] if spec['knowledge_mode']=='system_known' else fact['public_available_at']
        if dataset=='daily_bars' and spec['knowledge_mode']=='system_known' and time is not None:
            time=max(time,min(c['observed_at'] for c in fact['price_captures']))
        if fact['available_at'] is None or time is None or time>instant(spec['as_of']):raise WriterError('ADJUSTMENT_UNAVAILABLE:DEPENDENCY_NOT_KNOWN')
        self.writer.evidence(fact['raw_hash'])
        if fact['publication_evidence_hash']:self.writer.evidence(fact['publication_evidence_hash'])
        return fact

    def _prepare(self,spec,evidence_hash):
        wanted={'instrument_id','instrument_version','calendar_version','base_date','as_of','knowledge_mode','algorithm_version','raw_data_versions','event_inputs'}
        if type(spec) is not dict or set(spec)!=wanted or spec['algorithm_version']!=ALGORITHM or spec['knowledge_mode'] not in {'system_known','public_known'}:
            raise WriterError('FACTOR_INPUT_INVALID')
        if type(spec['instrument_id']) is not int or spec['instrument_id']<=0:raise WriterError('FACTOR_INPUT_INVALID')
        raw_ids=spec['raw_data_versions'];events=spec['event_inputs']
        if type(raw_ids) is not list or not raw_ids or any(not version_id(v) for v in raw_ids) or len(raw_ids)!=len(set(raw_ids)) or type(events) is not list:raise WriterError('FACTOR_INPUT_INVALID')
        base=day(spec['base_date']);as_of=instant(spec['as_of'])
        if as_of>instant(self.writer.clock()) or base>as_of.date():raise WriterError('ADJUSTMENT_UNAVAILABLE:FUTURE_BASE')
        spec=json.loads(canonical({**spec,'base_date':base,'as_of':as_of.replace(tzinfo=timezone.utc),'raw_data_versions':sorted(raw_ids)}))
        events=spec['event_inputs']  # Freeze nested caller inputs before any I/O.
        master=MasterDataService(self.engine);entity=master.validate_lifecycle(spec['instrument_version'],spec['instrument_id'],base)
        if entity['asset_type'] not in {'stock','etf'}:raise WriterError('ADJUSTMENT_UNAVAILABLE:ASSET_TYPE')
        if master.trading_days(spec['calendar_version'],entity['exchange'],base,base+timedelta(days=1))!=[base]:raise WriterError('ADJUSTMENT_UNAVAILABLE:BASE_NOT_TRADING')
        raws=[self._dependency(v,spec,'daily_bars') for v in spec['raw_data_versions']]
        if any(f['event_date']>base for f in raws):raise WriterError('ADJUSTMENT_UNAVAILABLE:RAW_AFTER_BASE')
        normalized=[];ratios=[];event_days=set();event_ids=set()
        for item in events:
            if type(item) is not dict or set(item)!={'data_version','previous_raw_data_version'} or any(not version_id(v) for v in item.values()) or item['data_version'] in event_ids:
                raise WriterError('ADJUSTMENT_UNAVAILABLE:EVENT_INPUT_INVALID')
            event=self._dependency(item['data_version'],spec,'corporate_action')
            previous=self._dependency(item['previous_raw_data_version'],spec,'daily_bars')
            on=event['event_date'];payload=event['payload']
            if on>base or on in event_days or previous['event_date']>=on or event['valid_from']!=on:
                raise WriterError('ADJUSTMENT_UNAVAILABLE:EVENT_ORDER')
            if master.trading_days(spec['calendar_version'],entity['exchange'],previous['event_date'],on)!=[previous['event_date']]:
                raise WriterError('ADJUSTMENT_UNAVAILABLE:PREVIOUS_CLOSE_GAP')
            if payload.get('event_type') not in {'cash_dividend','stock_dividend','rights_issue','split','consolidation','combined'}:
                raise WriterError('ADJUSTMENT_UNAVAILABLE:EVENT_TYPE')
            try:
                names=('cash_dividend','stock_ratio','rights_ratio')
                if any(payload.get(k) is None or isinstance(payload[k],(bool,float)) for k in names):raise ValueError()
                with localcontext() as context:
                    context.prec=80
                    cash,stock,rights=(Decimal(str(payload[k])) for k in names)
                    price=payload.get('rights_price')
                    if rights!=0 and (price is None or isinstance(price,(bool,float))):raise ValueError()
                    rights_price=Decimal(str(price)) if price is not None else Decimal(0)
                    prev=Decimal(previous['payload']['raw_close'])
                    if not all(v.is_finite() for v in (cash,stock,rights,rights_price,prev)) or cash<0 or stock<=-1 or rights<0 or rights_price<0 or prev<=0:raise ValueError()
                    reference=(prev-cash+rights_price*rights)/(1+stock+rights)
                    if reference<=0:raise ValueError()
                    ratios.append((on,reference/prev))
            except (ValueError,InvalidOperation,ZeroDivisionError):raise WriterError('ADJUSTMENT_UNAVAILABLE:EVENT_PARAMETERS') from None
            normalized.append(item);event_ids.add(item['data_version']);event_days.add(on)
        spec['event_inputs']=sorted(normalized,key=lambda item:item['data_version'])
        rows=[]
        for d in sorted({base,*[f['event_date'] for f in raws]}):
            with localcontext() as context:
                context.prec=80;factor=Decimal(1)
                for on,ratio in sorted(ratios):
                    if d<on<=base:factor*=ratio
                rows.append({'trade_date':d.isoformat(),'factor':decimal_text(factor,28,16)})
        manifest={'spec':spec,'evidence_hash':evidence_hash}
        header={k:spec[k] for k in ('instrument_id','base_date','as_of','knowledge_mode','algorithm_version')}
        header.update(event_version_ids=sorted(event_ids),factor_rows_hash=hash_object('factor-rows-v1',rows))
        version=hash_object('adjustment-v1',{**header,'input_manifest_hash':digest(canonical(manifest).encode())})
        return version,header,rows,manifest

    def preview_factors(self,spec,*,evidence):
        if type(evidence) is not bytes or not 0<len(evidence)<=32*1024*1024:raise WriterError('FACTOR_INPUT_EVIDENCE_REQUIRED')
        version,header,rows,manifest=self._prepare(spec,digest(evidence))
        return {'adj_factor_version':version,**header,'rows':rows,'input_manifest':manifest,'selection_scope':'explicit_dependencies_only'}

    def freeze_factors(self,spec,*,evidence):
        planned=self.preview_factors(spec,evidence=evidence)
        version=planned['adj_factor_version'];manifest=planned['input_manifest'];rows=planned['rows']
        header={k:planned[k] for k in ('instrument_id','base_date','as_of','knowledge_mode','algorithm_version','event_version_ids','factor_rows_hash')}
        with self._transaction(header['instrument_id']) as db:
            existing=db.execute(select(p.inputs.c.manifest_json).where(p.inputs.c.adj_factor_version==version)).scalar_one_or_none()
            if existing is not None:
                if existing!=canonical(manifest):raise WriterError('HASH_COLLISION')
            else:
                self.writer._evidence(db,evidence,'price://explicit-input-membership',instant(self.writer.clock()))
                db.execute(insert(s.adjustment_sets).values(adj_factor_version=version,**{**header,'base_date':day(header['base_date']),'as_of':instant(header['as_of'])}))
                db.execute(insert(s.adjustment_factor_rows),[{'adj_factor_version':version,'trade_date':day(r['trade_date']),'factor':Decimal(r['factor'])} for r in rows])
                db.execute(insert(p.inputs).values(adj_factor_version=version,manifest_json=canonical(manifest),evidence_hash=digest(evidence)))
                persisted=[{'trade_date':r.trade_date.isoformat(),'factor':format(r.factor,'.16f')} for r in db.execute(select(s.adjustment_factor_rows).where(s.adjustment_factor_rows.c.adj_factor_version==version).order_by(s.adjustment_factor_rows.c.trade_date))]
                if persisted!=rows:raise WriterError('ADJUSTMENT_UNAVAILABLE:STORAGE_PRECISION')
        self.factor_set(version)
        return version

    def factor_set(self,version):
        with self.engine.connect() as db:
            header=db.execute(select(s.adjustment_sets).where(s.adjustment_sets.c.adj_factor_version==version)).mappings().one_or_none()
            link=db.execute(select(p.inputs).where(p.inputs.c.adj_factor_version==version)).mappings().one_or_none()
            rows=[{'trade_date':r.trade_date.isoformat(),'factor':format(r.factor,'.16f')} for r in db.execute(select(s.adjustment_factor_rows).where(s.adjustment_factor_rows.c.adj_factor_version==version).order_by(s.adjustment_factor_rows.c.trade_date))]
        if header is None or link is None:raise WriterError('ADJUSTMENT_UNAVAILABLE:FACTOR_SET_MISSING')
        try:
            manifest=json.loads(link['manifest_json']);self.writer.evidence(link['evidence_hash'])
            expected,fields,computed,normalized=self._prepare(manifest['spec'],link['evidence_hash'])
            actual={k:header[k] for k in fields};actual['base_date']=header['base_date'].isoformat();actual['as_of']=canonical(header['as_of'].replace(tzinfo=timezone.utc))[1:-1]
            if expected!=version or canonical(manifest)!=link['manifest_json'] or normalized!=manifest or actual!=fields or rows!=computed:raise ValueError()
        except (ValueError,KeyError,TypeError) as exc:raise WriterError('FACTOR_SET_INTEGRITY_FAILED') from exc
        return {'adj_factor_version':version,**fields,'rows':rows,'input_manifest':manifest,'selection_scope':'explicit_dependencies_only'}

    def _expected(self,raw_version,factor_version):
        factors=self.factor_set(factor_version);spec=factors['input_manifest']['spec']
        if raw_version not in spec['raw_data_versions']:raise WriterError('ADJUSTMENT_UNAVAILABLE:RAW_NOT_MEMBER')
        raw=self._dependency(raw_version,spec,'daily_bars');on=raw['event_date'].isoformat()
        coefficients={r['trade_date']:r['factor'] for r in factors['rows']}
        if on not in coefficients:raise WriterError('ADJUSTMENT_UNAVAILABLE:FACTOR_DATE_MISSING')
        with localcontext() as context:
            context.prec=80
            values={'adj_'+name:decimal_text(Decimal(raw['payload']['raw_'+name])*Decimal(coefficients[on]),24,8) for name in ('open','high','low','close')}
        return {'raw_data_version':raw_version,'adj_factor_version':factor_version,'instrument_id':spec['instrument_id'],
            'trade_date':on,'base_date':spec['base_date'],**values},raw['payload']

    def derive(self,raw_version,factor_version):
        expected,_=self._expected(raw_version,factor_version)
        with self._transaction(expected['instrument_id']) as db:
            old=db.execute(select(s.adjusted_bars).where(s.adjusted_bars.c.raw_data_version==raw_version,s.adjusted_bars.c.adj_factor_version==factor_version)).first()
            if old is None:
                db.execute(insert(s.adjusted_bars).values(**{**expected,'trade_date':day(expected['trade_date']),'base_date':day(expected['base_date'])}))
                row=db.execute(select(s.adjusted_bars).where(s.adjusted_bars.c.raw_data_version==raw_version,s.adjusted_bars.c.adj_factor_version==factor_version)).mappings().one()
                if self._bar_row(row)!=expected:raise WriterError('ADJUSTMENT_UNAVAILABLE:STORAGE_PRECISION')
        return self.adjusted(raw_version,factor_version)

    def adjusted(self,raw_version,factor_version):
        expected,raw=self._expected(raw_version,factor_version)
        with self.engine.connect() as db:
            row=db.execute(select(s.adjusted_bars).where(s.adjusted_bars.c.raw_data_version==raw_version,s.adjusted_bars.c.adj_factor_version==factor_version)).mappings().one_or_none()
        if row is None:raise WriterError('ADJUSTMENT_UNAVAILABLE:DERIVED_MISSING')
        actual=self._bar_row(row)
        if actual!=expected:raise WriterError('DERIVED_PRICE_INTEGRITY_FAILED')
        return {**expected,**{k:raw.get(k) for k in ('volume_shares','amount_cny','turnover_rate_pct','is_suspended','synthetic_bar')}}

    @staticmethod
    def _bar_row(row):
        return {k:(format(v,'.8f') if k.startswith('adj_') and k!='adj_factor_version' else v.isoformat() if type(v) is date else v) for k,v in row.items()}
