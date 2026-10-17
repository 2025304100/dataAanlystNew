"""Reviewed receipt import into business inputs; never trains or publishes scores.

The maintenance caller must stop writers and retain the plan/preimages before
applying. SQL is one transaction. Warehouse refresh is a separate, retryable
phase; a failed refresh never claims that coverage has been refreshed.
"""
from __future__ import annotations
from datetime import date,datetime,timezone,timedelta
from dataclasses import asdict
import hashlib,json,math
from pathlib import Path
from sqlalchemy import select,inspect,text,func
from app.models.symbol import Symbol
from app.models.daily_bar import DailyBar
from app.models.universe import UniverseSymbol,UniverseDailyBar
from app.models.financial_report import StockFinancialReport
from app.models.capital_flow import CapitalFlow
from app.services.market_bar_correction import bar_snapshot,VerifiedBarCorrection,apply_verified_bar_corrections
from app.services.data_governance_audit import write_audit_event
from app.services.factors.input_replay import _load_inputs
from app.services.market_history_evidence import load_trusted_history_dataset

SOURCE='verified:fixed129-v1'
BAR_SOURCE=SOURCE+':qfq'
BAR_FIELDS=('open','high','low','close','volume','amount','turnover_rate')
FIN_FIELDS=('roe_ttm','net_profit','revenue','net_profit_yoy','revenue_yoy')
FLOW_FIELDS=('main_net_inflow','main_net_inflow_pct','small_net_inflow','medium_net_inflow','large_net_inflow','super_large_net_inflow')
PRECISION_FIELDS={'daily_bars':BAR_FIELDS,'universe_daily_bars':BAR_FIELDS,
                  'stock_financial_reports':FIN_FIELDS,'capital_flows':FLOW_FIELDS}
MODELS={m.__tablename__:m for m in (DailyBar,UniverseDailyBar,StockFinancialReport,CapitalFlow,UniverseSymbol)}

def encode(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,default=str,separators=(',',':'))
def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()
def snapshot(row):return {c.name:getattr(row,c.name) for c in row.__table__.columns}

def ensure_input_precision(engine,progress=lambda *args:None):
    """Widen only input numeric fields; preserve old values, constraints and IDs."""
    if engine.dialect.name!='mysql':return []
    changed=[]
    for table,fields in PRECISION_FIELDS.items():
        columns={c['name']:c for c in inspect(engine).get_columns(table)}
        clauses=[]
        for field in fields:
            c=columns[field]
            if str(c['type']).upper().startswith('DOUBLE'):continue
            if not str(c['type']).upper().startswith('FLOAT'):raise ValueError('verified_import_unexpected_numeric_schema')
            clauses.append(f"MODIFY COLUMN `{field}` DOUBLE {'NULL' if c['nullable'] else 'NOT NULL'}")
        if clauses:
            progress('widening_input_precision',table)
            with engine.begin()as conn:
                conn.execute(text(f"ALTER TABLE `{table}` "+', '.join(clauses)))
            changed.append(table)
    return changed

def load_verified_bundle(bundle_id,manifest_sha256):
    manifest,inputs=_load_inputs(bundle_id,manifest_sha256)
    dataset=load_trusted_history_dataset(manifest['dataset_id'])
    if dataset.manifest_sha256!=manifest['dataset_manifest_sha256']:raise ValueError('verified_import_dataset_changed')
    now=datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    if dataset.calendar[-1]>=now.date():raise ValueError('verified_import_requires_closed_prior_day')
    codes=set(dataset.symbols)
    for rows in inputs.values():
        if any(r['symbol']not in codes for r in rows):raise ValueError('verified_import_outside_cohort')
    return manifest,inputs,dataset

def build_import_plan(db,*,bundle_id,manifest_sha256,loaded=None):
    manifest,inputs,dataset=loaded or load_verified_bundle(bundle_id,manifest_sha256)
    codes=tuple(dataset.symbols)
    business={s.symbol:s for s in db.scalars(select(Symbol).where(Symbol.symbol.in_(codes)))}
    universe={s.symbol:s for s in db.scalars(select(UniverseSymbol).where(UniverseSymbol.symbol.in_(codes)))}
    # Creating an active symbol would change a user's trading universe. Require
    # existing metadata; this importer never alters watchlists or symbol flags.
    if set(business)!=set(codes)or set(universe)!=set(codes):raise ValueError('verified_import_missing_symbol_metadata')
    if any(s.asset_type!='stock'or s.market not in ('cn','sh','sz','bj')for s in business.values()):raise ValueError('verified_import_business_asset_conflict')
    if any(s.asset_type!='stock'or s.region!='cn'for s in universe.values()):raise ValueError('verified_import_universe_asset_conflict')
    ids={s.id:c for c,s in business.items()};uids={s.id:c for c,s in universe.items()}
    first,last=dataset.calendar[0],dataset.calendar[-1]
    old={}
    for model,idmap,fk in ((DailyBar,ids,'symbol_id'),(UniverseDailyBar,uids,'universe_symbol_id')):
        old[model.__tablename__]={(idmap[getattr(r,fk)],r.trade_date):r for r in db.scalars(select(model).where(
            getattr(model,fk).in_(idmap),model.trade_date>=first,model.trade_date<=last))}
    old['stock_financial_reports']={(ids[r.symbol_id],r.report_period,r.announcement_date,r.report_type):r for r in db.scalars(
        select(StockFinancialReport).where(StockFinancialReport.symbol_id.in_(ids),StockFinancialReport.source==SOURCE))}
    old['capital_flows']={(ids[r.symbol_id],r.trade_date):r for r in db.scalars(select(CapitalFlow).where(
        CapitalFlow.symbol_id.in_(ids),CapitalFlow.trade_date>=first,CapitalFlow.trade_date<=last))}
    old['universe_symbols']={(code,):row for code,row in universe.items()}
    operations=[];seen=set()
    def add(table,key,values):
        identity=(table,key)
        if identity in seen:raise ValueError('verified_import_duplicate_identity')
        seen.add(identity)
        row=old[table].get(key)
        if row is not None and all(getattr(row,k)==v for k,v in values.items()):return
        operations.append({'table':table,'key':key,'before':snapshot(row)if row is not None else None,'values':values})
    for r in dataset.bars.to_dict('records'):
        day=r['trade_date'].date()if isinstance(r['trade_date'],datetime)else r['trade_date']
        values={k:float(r[k])if r[k]is not None else None for k in BAR_FIELDS}
        if any(v is not None and not math.isfinite(v)for v in values.values()):raise ValueError('verified_import_nonfinite')
        values['source']=BAR_SOURCE
        for table,fk,ident in (('daily_bars','symbol_id',business[r['symbol']].id),('universe_daily_bars','universe_symbol_id',universe[r['symbol']].id)):
            add(table,(r['symbol'],day),dict(values,**{fk:ident,'trade_date':day}))
    for kind,table,fields in (('financial','stock_financial_reports',FIN_FIELDS),('flow','capital_flows',FLOW_FIELDS)):
        for row in inputs[kind]:
            values={k:row[k]for k in fields}
            proof={'bundle_id':bundle_id,'bundle_manifest_sha256':manifest_sha256,'evidence':row['evidence'],
                'historical_asof_certified':False,'observed_import_only':True,
                'roe_semantics':'report_period_weighted_not_certified_ttm'if kind=='financial'else None}
            values.update(symbol_id=business[row['symbol']].id,source=SOURCE,raw_json=encode(proof))
            if kind=='financial':
                values.update(report_period=date.fromisoformat(row['report_period']),announcement_date=date.fromisoformat(row['announcement_date']),report_type=row['report_type'])
                key=(row['symbol'],values['report_period'],values['announcement_date'],values['report_type'])
            else:
                values.update(trade_date=date.fromisoformat(row['trade_date']),main_net_inflow_score=None)
                key=(row['symbol'],values['trade_date'])
            add(table,key,values)
    stats={sid:(count,latest)for sid,count,latest in db.execute(select(UniverseDailyBar.universe_symbol_id,
        func.count(UniverseDailyBar.id),func.max(UniverseDailyBar.trade_date)).where(
        UniverseDailyBar.universe_symbol_id.in_(uids)).group_by(UniverseDailyBar.universe_symbol_id))}
    for code,symbol in universe.items():
        count,latest=stats.get(symbol.id,(0,None))
        additions=sum(1 for item in operations if item['table']=='universe_daily_bars'and item['key'][0]==code and item['before']is None)
        newest=max([d for d in (latest,last)if d is not None])
        values=dict(symbol=code,bar_count=count+additions,last_bar_date=newest)
        if symbol.bar_count!=values['bar_count']or symbol.last_bar_date!=newest:
            values['last_synced_at']=datetime.now(timezone.utc).replace(tzinfo=None)
            add('universe_symbols',(code,),values)
    plan={'schema_version':1,'bundle_id':bundle_id,'manifest_sha256':manifest_sha256,
        'dataset_id':dataset.dataset_id,'dataset_manifest_sha256':dataset.manifest_sha256,
        'symbols':list(codes),'first_date':str(first),'last_date':str(last),'historical_asof_certified':False,
        'operations':operations,'input_counts':{'bars':len(dataset.bars),'financial':len(inputs['financial']),'flow':len(inputs['flow'])}}
    return plan

def apply_import_plan(db,plan,*,expected_plan_sha256,correlation_id):
    """All preimages checked before mutation. Caller owns SQL commit."""
    if digest(plan)!=expected_plan_sha256:raise ValueError('verified_import_plan_changed')
    locked=[]
    for item in plan['operations']:
        model=MODELS[item['table']];before=item['before'];values=item['values']
        keyfields=('symbol',)if model is UniverseSymbol else ('universe_symbol_id','trade_date')if model is UniverseDailyBar else (
            ('symbol_id','report_period','announcement_date','report_type','source')if model is StockFinancialReport else ('symbol_id','trade_date'))
        row=db.scalar(select(model).where(*(getattr(model,k)==values[k]for k in keyfields)).with_for_update().execution_options(populate_existing=True))
        if (row is None)!=(before is None)or(row is not None and encode(snapshot(row))!=encode(before)):
            raise ValueError('verified_import_preimage_changed')
        locked.append((item,row))
    evidence={'responses':[{'path':'tmp/verified_input_bundles/'+plan['bundle_id']+'/manifest.json','sha256':plan['manifest_sha256']},
        {'path':'tmp/trusted_history_datasets/'+plan['dataset_id']+'/manifest.json','sha256':plan['dataset_manifest_sha256']}],
        'volume_unit':'share','amount_unit':'CNY','turnover_unit':'percentage_point','historical_asof_certified':False,
        'plan_sha256':expected_plan_sha256,'import_correlation_id':correlation_id}
    corrections=[]
    for item,row in locked:
        values=item['values'];model=MODELS[item['table']]
        if row is not None and model in (DailyBar,UniverseDailyBar):
            corrections.append(VerifiedBarCorrection(item['table'],row.id,bar_snapshot(row),{k:values[k]for k in (*BAR_FIELDS,'source')}))
        elif row is None:db.add(model(**values))
        else:
            for key,value in values.items():setattr(row,key,value)
    audit_ids=[]
    for start in range(0,len(corrections),100):
        audit_ids+=apply_verified_bar_corrections(db,corrections[start:start+100],operator_id='user:verified-input-import',
            correlation_id=correlation_id,evidence=evidence)
    db.flush()
    # Compact source receipt, plus immutable full plan/preimages retained by caller.
    event=write_audit_event(db,'DATA_SOURCE_FAILOVER',operator_id='user:verified-input-import',
        business_key='verified-input-import:'+correlation_id,correlation_id=correlation_id,
        after={'input_counts':plan['input_counts'],'operations':len(locked),'correction_rows':len(corrections)},
        attributes=evidence,note='Explicit receipt import; no model activation or score publication.')
    audit_ids.append(event.event_id)
    return {'operations':len(locked),'correction_rows':len(corrections),'audit_ids':audit_ids}

def refresh_default_warehouse(db,warehouse,plan,*,batch_id,progress=lambda *args:None):
    """Mirror committed SQL values and calculate native factors; no inference."""
    from app.services.factors.bar_mirror import mirror_daily_bars
    from app.services.factors.data_sync import mirror_factor_inputs
    from app.services.factors.factor_engine import calculate_stock_factors
    from app.services.factors.health import get_factor_health
    first,last=date.fromisoformat(plan['first_date']),date.fromisoformat(plan['last_date'])
    progress('mirroring_business_bars',0)
    bars=mirror_daily_bars(db,warehouse=warehouse,start_date=first,end_date=last,include_universe=True,include_business=True)
    progress('mirroring_factor_inputs',0)
    inputs=mirror_factor_inputs(db,warehouse=warehouse,start_date=first,end_date=last,
        include_valuations=False,include_financial_reports=True,include_fund_flows=True,
        include_sentiment=False,include_tail_proxy=False,include_macro=False,include_etf_indicators=False)
    progress('calculating_native_factors',0)
    result=calculate_stock_factors(warehouse,start_date=last,end_date=last,calc_batch_id=batch_id)
    health=get_factor_health(warehouse,calc_batch_id=batch_id)
    with warehouse.connection(read_only=True)as conn:
        marks=','.join('?'for _ in plan['symbols'])
        fixed=[dict(code=c,eligible=n,denominator=len(plan['symbols']))for c,n in conn.execute(
            f'SELECT factor_code,COUNT(DISTINCT CASE WHEN eligible THEN symbol END) FROM factor_values WHERE calc_batch_id=? AND symbol IN ({marks}) GROUP BY factor_code ORDER BY factor_code',
            [batch_id,*plan['symbols']]).fetchall()]
    return {'bars':bars.to_dict(),'inputs':asdict(inputs),'calculation':asdict(result),'health':health.to_dict(),
        'fixed_pool_coverage':fixed,'production_scores_published':0,'model_activated':False}
