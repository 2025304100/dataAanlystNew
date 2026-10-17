"""Receipt -> isolated warehouse -> real factor engine -> research inference.

This owns no production publication, model training or runtime-switch path.
Artifacts are immutable; external callers use the public scoring facade.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import math
import re
import shutil

import pandas as pd
from sqlalchemy import select

from app.models.factor_model import FactorModelRun
from app.models.factor_governance import FactorModelMember
from app.services.factor_model_contract import assess_factor_model_readiness
from app.services.market_history_evidence import load_trusted_history_dataset
from app.services.factors.factor_engine import calculate_stock_factors, resolve_frozen_factor_specs
from app.services.factors.scoring_bridge import _load_factor_rows, evaluate_linear_factor_model
from app.services.factors.trusted_history_training import build_training_warehouse
from app.services.factors.store import close_owned_warehouse_path
from app.services.factors.definitions import FACTOR_DEFINITIONS
from app.services.factors.fundamental import normalize_financial_analysis_frame

PROJECT = Path(__file__).resolve().parents[3]
BUNDLE_ROOT = PROJECT / 'tmp/verified_input_bundles'
REPORT_ROOT = PROJECT / 'tmp/factor_input_replays'
TASK_TYPE = 'factor_input_replay'
ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,79}$')

def _sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def _json(path):
    if path.stat().st_size > 32 * 1024 * 1024: raise ValueError('input_replay_file_too_large')
    return json.loads(path.read_text(encoding='utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('input_replay_nonfinite_json')))
def _inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()): raise ValueError('input_replay_path_escape')
    return path
def _bundle_dir(bundle_id):
    if not isinstance(bundle_id, str) or not ID.fullmatch(bundle_id): raise ValueError('input_replay_invalid_bundle')
    return _inside(BUNDLE_ROOT, bundle_id)

def register_staged_bundle(source: Path, *, bundle_id: str, dataset_id: str, dataset_sha256: str, label: str,
                           suggested_model_id: str | None = None):
    """Local registration only; HTTP never accepts arbitrary paths/uploads."""
    destination = _bundle_dir(bundle_id)
    if destination.exists(): raise ValueError('input_replay_bundle_immutable')
    original = _json(source / 'manifest.json')
    destination.mkdir(parents=True)
    copied = set()
    try:
        for kind, filename in (('financial', 'financial.json'), ('flow', 'flows.json')):
            assert _sha(source / filename) == original['files'][filename], 'input_replay_source_changed'
            records = _json(source / filename)
            for row in records:
                evidence = row['evidence']
                old = Path(evidence['receipt_path'])
                assert old.resolve().is_relative_to(PROJECT), 'input_replay_source_outside_project'
                assert _sha(old) == evidence['receipt_sha256']
                assert _sha(old.parent / 'response.bin') == evidence['response_sha256']
                relative = f'receipts/{row["symbol"]}/{kind}/receipt.json'
                target = _inside(destination, relative)
                if relative not in copied:
                    target.parent.mkdir(parents=True)
                    shutil.copyfile(old, target)
                    shutil.copyfile(old.parent / 'response.bin', target.parent / 'response.bin')
                    copied.add(relative)
                evidence['receipt_path'] = relative
            (destination / filename).write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        manifest = dict(schema_version=1, bundle_id=bundle_id, label=label,
            dataset_id=dataset_id, dataset_manifest_sha256=dataset_sha256,
            staged_manifest_sha256=_sha(source/'manifest.json'), suggested_model_id=suggested_model_id,
            evaluation_role='research_input_chain_only', historical_asof_certified=False,
            files={name: _sha(destination/name) for name in ('financial.json', 'flows.json')},
            financial_rows=original['financial_rows'], financial_symbols=original['financial_symbols'],
            flow_rows=original['flow_rows'], flow_symbols=original['flow_symbols'])
        (destination/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        # Retain failed registration evidence; no manifest means not selectable.
        raise
    return manifest

def list_input_bundles():
    rows=[]
    if not BUNDLE_ROOT.exists(): return rows
    for path in sorted(BUNDLE_ROOT.glob('*/manifest.json')):
        if not ID.fullmatch(path.parent.name): continue
        m = _json(path)
        if m.get('schema_version') != 1 or m.get('bundle_id') != path.parent.name: continue
        rows.append({k:m.get(k) for k in ('bundle_id','label','dataset_id','dataset_manifest_sha256',
            'suggested_model_id','financial_rows','financial_symbols','flow_rows','flow_symbols')} |
            {'manifest_sha256':_sha(path), 'historical_asof_certified':False})
    return rows

def _load_inputs(bundle_id, expected_sha):
    root = _bundle_dir(bundle_id); manifest = _json(root/'manifest.json')
    if _sha(root/'manifest.json') != expected_sha: raise ValueError('input_replay_reviewed_bundle_changed')
    if (manifest.get('schema_version') != 1 or manifest.get('bundle_id') != bundle_id
            or manifest.get('evaluation_role') != 'research_input_chain_only' or manifest.get('historical_asof_certified') is not False):
        raise ValueError('input_replay_invalid_role')
    records = {}
    checked, providers, currencies = {}, {}, {}
    for kind, filename in (('financial','financial.json'),('flow','flows.json')):
        if _sha(root/filename) != manifest['files'][filename]: raise ValueError('input_replay_file_checksum_mismatch')
        records[kind] = _json(root/filename)
        for row in records[kind]:
            proof = row['evidence']; path = _inside(root, proof['receipt_path'])
            if path not in checked:
                if _sha(path)!=proof['receipt_sha256'] or _sha(path.parent/'response.bin')!=proof['response_sha256']:
                    raise ValueError('input_replay_receipt_checksum_mismatch')
                meta=_json(path); raw=_json(path.parent/'response.bin')
                if meta.get('symbol') != row['symbol'] or meta.get('kind') != kind or meta.get('sha256') != proof['response_sha256']:
                    raise ValueError('input_replay_receipt_binding_mismatch')
                checked[path] = meta
                if kind=='financial':
                    source = raw['result']['data']
                    if any(str(r.get('SECURITY_CODE'))!=row['symbol'] for r in source):
                        raise ValueError('input_replay_financial_instrument_or_currency')
                    frame=normalize_financial_analysis_frame(pd.DataFrame(source),symbol=row['symbol'])
                    providers[path] = {(str(r['report_period']),str(r['announcement_date'])):r for r in frame.to_dict('records')}
                    currencies[path] = {(str(r.get('REPORT_DATE'))[:10],str(r.get('NOTICE_DATE'))[:10]):r.get('CURRENCY') for r in source}
                else:
                    if str(raw['data']['code'])!=row['symbol']:raise ValueError('input_replay_flow_instrument')
                    providers[path]={r.split(',')[0]:r.split(',') for r in raw['data']['klines']}
            # Every reference is bound, including repeated rows for the same receipt.
            if (checked[path]['sha256'] != proof['response_sha256'] or _sha(path)!=proof['receipt_sha256']
                    or checked[path].get('symbol') != row['symbol'] or checked[path].get('kind') != kind):
                raise ValueError('input_replay_receipt_binding_mismatch')
            if kind=='financial':
                if date.fromisoformat(row['report_period']) > date.fromisoformat(row['announcement_date']):
                    raise ValueError('input_replay_financial_date_order')
                source=providers[path].get((row['report_period'],row['announcement_date']))
                if currencies[path].get((row['report_period'],row['announcement_date']))!='CNY':
                    raise ValueError('input_replay_financial_instrument_or_currency')
                if source is None or row.get('financial_amount_unit')!='CNY' or row.get('growth_unit')!='percent':
                    raise ValueError('input_replay_financial_contract')
                fields=('roe_ttm','net_profit','revenue','net_profit_yoy','revenue_yoy')
                for key in fields:
                    value=source[key]
                    expected=None if pd.isna(value) else float(value)
                    if row.get(key)!=expected: raise ValueError('input_replay_normalized_value_mismatch')
            else:
                if row.get('amount_unit')!='CNY' or row.get('ratio_unit')!='percent': raise ValueError('input_replay_flow_unit')
                source=providers[path].get(row['trade_date'])
                if source is None or len(source)!=15:raise ValueError('input_replay_flow_schema')
                for key,index in {'main_net_inflow':1,'small_net_inflow':2,'medium_net_inflow':3,
                    'large_net_inflow':4,'super_large_net_inflow':5,'main_net_inflow_pct':6}.items():
                    if row[key]!=float(source[index]):raise ValueError('input_replay_normalized_value_mismatch')
    return manifest, records

def _put_inputs(warehouse, records, symbols, batch):
    now=datetime.now(timezone.utc).replace(tzinfo=None)
    for kind, table, keys in (
        ('financial','raw_financial_reports',('symbol','report_period','announcement_date','report_type',
            'roe_ttm','net_profit','revenue','net_profit_yoy','revenue_yoy')),
        ('flow','raw_fund_flows',('symbol','trade_date','main_net_inflow','main_net_inflow_pct',
            'small_net_inflow','medium_net_inflow','large_net_inflow','super_large_net_inflow'))):
        converted=[];seen=set()
        for row in records[kind]:
            if row['symbol'] not in symbols:raise ValueError('input_replay_symbol_outside_frozen_cohort')
            identity=tuple(row[k] for k in (('symbol','report_period','announcement_date','report_type') if kind=='financial' else ('symbol','trade_date')))
            if identity in seen: raise ValueError('input_replay_duplicate_observation')
            seen.add(identity)
            record={k:row[k]for k in keys}
            for key in ('report_period','announcement_date','trade_date'):
                if key in record:record[key]=date.fromisoformat(record[key])
            record.update(source='verified_receipt:'+row['evidence']['response_sha256'],ingested_at=now,batch_id=batch)
            if kind=='financial':record['source_hash']=row['evidence']['response_sha256']
            converted.append(record)
        warehouse.upsert_records(table,converted)

def load_model_contract(db, model_id):
    model=db.get(FactorModelRun,model_id)
    if model is None:raise ValueError('input_replay_model_not_found')
    hp=json.loads(model.hyperparameters_json or '{}')
    if model.asset_type!='stock' or model.status=='training' or not hp.get('trusted_history_dataset'):
        raise ValueError('input_replay_requires_saved_research_model')
    specs=resolve_frozen_factor_specs(db,(hp['factor_set_id'],))
    members=list(db.scalars(select(FactorModelMember).where(FactorModelMember.model_run_id==model_id)))
    versions={m.factor_code:int(m.factor_version)for m in members}
    if not members or versions!={s.code:s.version for s in specs} or versions!=json.loads(model.feature_versions_json):
        raise ValueError('input_replay_model_factor_version_mismatch')
    coefficient={m.factor_code:float(m.coefficient)for m in members}
    if not all(math.isfinite(v)for v in coefficient.values()):raise ValueError('input_replay_invalid_model_weights')
    gate=assess_factor_model_readiness(db,model_run_id=model_id)
    return specs, dict(id=model_id,status=model.status,label=hp.get('display_alias') or model_id,
        factor_set_id=hp['factor_set_id'],coefficients=coefficient,feature_versions=versions,
        intercept=float(hp.get('final_intercept') or 0),readiness_code=gate.code,
        production_eligible=False,fit_target=hp.get('fit_target_policy',{}).get('target','unknown'),
        training_manifest_sha256=hp['trusted_history_dataset'].get('manifest_sha256'),
        training_cohort_count=len(hp['trusted_history_dataset'].get('cohort_symbols',[])),
        model_input_fingerprint=hp.get('input_fingerprint'),factor_set_content_hash=hp.get('factor_set_content_hash'))

def run_input_replay(db, *, bundle_id, manifest_sha256, model_id, replay_id, progress=lambda *args:None):
    if not re.fullmatch('[a-f0-9]{32}',replay_id):raise ValueError('input_replay_invalid_request_id')
    progress('verifying_inputs',5)
    manifest,records=_load_inputs(bundle_id,manifest_sha256)
    dataset=load_trusted_history_dataset(manifest['dataset_id'])
    if dataset.manifest_sha256!=manifest['dataset_manifest_sha256']:raise ValueError('input_replay_dataset_changed')
    now=datetime.now(timezone.utc); local=now.astimezone(timezone(timedelta(hours=8)))
    last=dataset.calendar[-1]
    if last>local.date() or (last==local.date() and (local.hour,local.minute)<(15,15)):
        raise ValueError('input_replay_dataset_not_closed')
    with db.no_autoflush: specs,model=load_model_contract(db,model_id)
    db.rollback()  # No long SQL transaction while calculating in isolated DuckDB.
    directory=REPORT_ROOT/replay_id;directory.mkdir(parents=True,exist_ok=False)
    warehouse=None
    try:
        # Fresh, uniquely owned store; never open the default warehouse.
        progress('ingesting_inputs',35)
        warehouse=build_training_warehouse(dataset,'input-replay-'+replay_id)
        _put_inputs(warehouse,records,set(dataset.symbols),replay_id)
        progress('calculating_factors',55)
        dates=tuple(dataset.calendar[-5:])
        calculation=calculate_stock_factors(warehouse,start_date=dates[0],end_date=last,
            calc_batch_id='input-replay-'+replay_id,frozen_factor_specs=specs,trade_dates=dates)
        progress('research_inference',85)
        factor_rows,_=_load_factor_rows(warehouse,trade_date=last,factor_calc_batch_id=calculation.calc_batch_id,
            extra_factor_codes=tuple(model['coefficients']),expected_versions=model['feature_versions'])
        inference=evaluate_linear_factor_model(factor_rows,model['coefficients'],model['intercept'])
        with warehouse.connection(read_only=True)as conn:
            panel=conn.execute('SELECT symbol,trade_date,factor_code,factor_version,raw_value,normalized_value,eligible '
                'FROM factor_values WHERE calc_batch_id=? ORDER BY trade_date,symbol,factor_code,factor_version',
                [calculation.calc_batch_id]).df()
            counts={table:conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]for table in
                ('raw_daily_bars','raw_financial_reports','raw_fund_flows')}
        panel.to_parquet(directory/'factor_values.parquet',index=False)
        native={(d.code,d.version)for d in FACTOR_DEFINITIONS if d.model_enabled}
        coverage=[]
        for (code,version),group in panel[panel.trade_date.dt.date==last].groupby(['factor_code','factor_version']):
            if (code,int(version))not in native and model['feature_versions'].get(code)!=int(version):continue
            coverage.append(dict(code=code,version=int(version),eligible=int(group.eligible.sum()),
                denominator=len(dataset.symbols),model_feature=model['feature_versions'].get(code)==int(version)))
        rows=[]
        latest_panel=panel[panel.trade_date.dt.date==last]
        observations={symbol:[dict(code=r.factor_code,version=int(r.factor_version),
            raw_value=float(r.raw_value)if pd.notna(r.raw_value)else None,
            normalized_value=float(r.normalized_value)if pd.notna(r.normalized_value)else None,
            eligible=bool(r.eligible)) for r in group.itertuples()
            if (r.factor_code,int(r.factor_version))in native or model['feature_versions'].get(r.factor_code)==int(r.factor_version)]
            for symbol,group in latest_panel.groupby('symbol')}
        for symbol in dataset.symbols:
            values=factor_rows.get(symbol,{})
            rows.append(dict(symbol=symbol,score=inference['alpha_scores'].get(symbol),
                linear_signal=inference['alpha_raw'].get(symbol),features={code:{
                    'version':model['feature_versions'][code],
                    'raw_value':values.get(code,{}).get('raw_value'),
                    'normalized_value':values.get(code,{}).get('normalized_value'),
                    'coefficient':coef,'contribution':inference['contributions'].get(symbol,{}).get(code)}
                    for code,coef in model['coefficients'].items()},
                missing_features=[code for code in model['coefficients']if code not in values],
                factor_observations=observations.get(symbol,[])))
        rows.sort(key=lambda r:(r['score']is None,-(r['score']or 0),r['symbol']))
        result=dict(schema_version=1,replay_id=replay_id,published_at=now.isoformat(),research_only=True,
            production_scores_published=0,historical_asof_certified=False,training_performed=False,
            model_activated=False,bundle_id=bundle_id,bundle_manifest_sha256=manifest_sha256,
            dataset_id=dataset.dataset_id,dataset_manifest_sha256=dataset.manifest_sha256,
            first_date=str(dataset.calendar[0]),last_date=str(last),calendar_dates=len(dataset.calendar),
            frozen_symbol_count=len(dataset.symbols),scored_symbols=len(inference['alpha_scores']),
            score_semantics='fixed_cohort_percentile_not_expected_return',model=model,
            input_counts=counts,coverage=coverage,rows=rows,
            gaps=dict(no_financial_symbols=sorted(set(dataset.symbols)-{r['symbol']for r in records['financial']}),
                no_flow_symbols=sorted(set(dataset.symbols)-{r['symbol']for r in records['flow']}),
                valuation='NOT_INCLUDED_IN_THIS_RECEIPT_BUNDLE',etf='STOCK_CONTRACT_NOT_APPLICABLE'),
            input_lineage=dict(bundle_files=manifest['files'],staged_manifest_sha256=manifest['staged_manifest_sha256']),
            factor_values_sha256=_sha(directory/'factor_values.parquet'))
        (directory/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False,default=str),encoding='utf-8')
        (directory/'manifest.json').write_text(json.dumps({'report_sha256':_sha(directory/'report.json')}),encoding='utf-8')
        progress('done',100)
        return result
    finally:
        if warehouse is not None:close_owned_warehouse_path(warehouse.path)

def read_input_replay_report(replay_id):
    if not re.fullmatch('[a-f0-9]{32}',replay_id):raise ValueError('input_replay_invalid_request_id')
    directory=_inside(REPORT_ROOT,replay_id)
    manifest=_json(directory/'manifest.json')
    if _sha(directory/'report.json')!=manifest['report_sha256']:raise ValueError('input_replay_report_checksum_mismatch')
    report=_json(directory/'report.json')
    if _sha(directory/'factor_values.parquet')!=report['factor_values_sha256']:raise ValueError('input_replay_factor_checksum_mismatch')
    return report
