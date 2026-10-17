"""Production preparation for immutable research candidates.

This is an evidence check and an immutable prospective protocol, not a release
override. A frozen plan is never a certificate, trained model or activation.
"""
from __future__ import annotations
from datetime import date,datetime,timezone,timedelta
from pathlib import Path
import hashlib,json,math,re,os,tempfile,threading
import pandas as pd
from app.services.factors.input_replay import PROJECT,_bundle_dir,_json,_sha,load_model_contract
from app.services.factors.training_input_snapshot import load_training_input_snapshot
from app.services.factors.config import get_factor_system_config
from app.services.factors.store import FactorWarehouse
from app.services.factor_model_contract import assess_factor_model_readiness
from app.models.factor_model import FactorModelRun,FactorVersion
from app.models.factor_evaluation import FactorSet

PLAN_ROOT=PROJECT/'tmp/factor_production_admission_plans'
SCHEMA=1
HEX=re.compile(r'^[a-f0-9]{64}$')
_FREEZE_LOCK=threading.RLock()
def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'),default=str)
def fingerprint(value):return hashlib.sha256(canonical(value).encode()).hexdigest()

def _parent_context(db,model_id):
    model=db.get(FactorModelRun,model_id)
    if model is None:raise ValueError('production_admission_model_not_found')
    hp=json.loads(model.hyperparameters_json or '{}')
    if not isinstance(hp.get('trusted_history_dataset'),dict):raise ValueError('production_admission_requires_research_parent')
    specs,contract=load_model_contract(db,model_id)
    if contract['feature_versions']!={'turnover_z20':3} or model.target_code!='target_5d_return':
        raise ValueError('production_admission_requires_turnover_z20_v3_protocol')
    frozen_set=db.get(FactorSet,contract['factor_set_id'])
    if not frozen_set or not frozen_set.content_hash or frozen_set.content_hash!=contract['factor_set_content_hash']:
        raise ValueError('production_admission_factor_set_hash_mismatch')
    versions=[]
    for spec in specs:
        version=db.get(FactorVersion,spec.version_id)
        versions.append({name:getattr(version,name) for name in ('id','factor_id','version','formula_expr',
            'params_json','source_mapping_json','postprocess_json','direction')})
    path=Path(model.artifact_path or '').resolve()
    if not path.is_relative_to((PROJECT/'tmp').resolve()):raise ValueError('production_admission_snapshot_outside_project')
    _,snapshot=load_training_input_snapshot(path)
    if (snapshot['model_run_id']!=model_id or snapshot['input_fingerprint']!=hp.get('input_fingerprint')
            or snapshot['feature_versions']!=contract['feature_versions']):
        raise ValueError('production_admission_snapshot_binding_mismatch')
    metrics=json.loads(model.metrics_json or '{}')
    observed=[date.fromisoformat(hp['data_cutoff_date']),model.validation_end_date]
    availability=model.data_cutoff_at
    if availability is not None:
        if availability.tzinfo is None:availability=availability.replace(tzinfo=timezone.utc)
        observed.append(availability.astimezone(timezone(timedelta(hours=8))).date())
    if model.created_at:
        created=model.created_at
        if created.tzinfo is None:created=created.replace(tzinfo=timezone.utc)
        observed.append(created.astimezone(timezone(timedelta(hours=8))).date())
    context={'model_id':model_id,'factor_set_id':contract['factor_set_id'],
        'factor_set_content_hash':contract['factor_set_content_hash'],'feature_versions':contract['feature_versions'],
        'coefficients':contract['coefficients'],'intercept':contract['intercept'],'factor_definitions':versions,
        'input_fingerprint':hp['input_fingerprint'],'snapshot_manifest_sha256':_sha(path),
        'snapshot_samples_sha256':snapshot['sha256'],'training_cohort':hp['trusted_history_dataset']['cohort_symbols'],
        'fit_policy':hp.get('fit_policy'),'alphas':hp.get('alphas'),'model_gate':hp.get('model_gate'),
        'governance_gate_config':hp.get('governance_gate_config'),
        'research_metric_gate':metrics.get('research_model_gate_status')or hp.get('research_model_gate_status'),
        'research_validation_ic':metrics.get('validation_ic'),'observed_through':str(max(d for d in observed if d is not None))}
    if (context['fit_policy']!='cross_sectional_rank_v1'or not context['alphas']
            or not isinstance(context['model_gate'],dict) or not context['model_gate']
            or not isinstance(context['governance_gate_config'],dict) or not context['governance_gate_config']
            or any(isinstance(a,bool)or not math.isfinite(float(a))or float(a)<=0 for a in context['alphas'])):
        raise ValueError('production_admission_original_fit_policy_missing')
    return context,assess_factor_model_readiness(db,model_run_id=model_id).to_dict()

def _bundle_context(bundle_id):
    from app.services.market_history_evidence import TRUSTED_HISTORY_DATASET_ROOT
    root=_bundle_dir(bundle_id);bundle=_json(root/'manifest.json')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}',bundle['dataset_id']):raise ValueError('production_admission_dataset_id_invalid')
    dataset_root=TRUSTED_HISTORY_DATASET_ROOT.resolve();directory=(dataset_root/bundle['dataset_id']).resolve()
    if not directory.is_relative_to(dataset_root):raise ValueError('production_admission_dataset_path_escape')
    manifest=_json(directory/'manifest.json')
    if _sha(directory/'manifest.json')!=bundle['dataset_manifest_sha256']:raise ValueError('production_admission_dataset_changed')
    if manifest.get('units')!={'volume':'share','amount':'CNY','turnover_rate':'percentage_point'}or manifest.get('adjust')!='qfq':
        raise ValueError('production_admission_dataset_units_invalid')
    if _sha(directory/'bars.parquet')!=manifest['bars_sha256']or _sha(directory/'quality.json')!=manifest['quality_sha256']:
        raise ValueError('production_admission_dataset_content_changed')
    for filename in ('financial.json','flows.json'):
        if _sha(root/filename)!=bundle['files'][filename]:raise ValueError('production_admission_bundle_changed')
    bars=pd.read_parquet(directory/'bars.parquet')
    symbols=manifest['cohort_symbols'];days=tuple(date.fromisoformat(d)for d in manifest['calendar'])
    if (not symbols or symbols!=sorted(set(symbols))or not days or list(days)!=sorted(set(days))
            or bars.duplicated(['symbol','trade_date']).any()or not set(bars.symbol).issubset(symbols)):
        raise ValueError('production_admission_invalid_sample_scope')
    context={'bundle_id':bundle_id,'bundle_manifest_sha256':_sha(root/'manifest.json'),
        'dataset_id':bundle['dataset_id'],'dataset_manifest_sha256':_sha(directory/'manifest.json'),
        'bars_sha256':manifest['bars_sha256'],'symbols':symbols,'first_date':str(days[0]),'last_date':str(days[-1]),
        'raw_trade_dates':len(days),'bar_rows':len(bars),'source_historical_asof_certified':False}
    return context,bars,days

def _read_plan(path):
    try:
        envelope=_json(path)
        plan=envelope['plan']
        if (envelope.get('sha256')!=fingerprint(plan)or plan.get('schema_version')!=SCHEMA
                or plan['plan_id']!=fingerprint(plan['identity']) or path.stem!=plan['plan_id']):
            raise ValueError('production_admission_frozen_plan_corrupt')
    except (KeyError,TypeError,OSError,json.JSONDecodeError) as exc:
        raise ValueError('production_admission_frozen_plan_corrupt') from exc
    return plan,envelope['sha256']

def _existing_plan(model_id):
    # No mkdir on GET. Never move the original boundary when a user refreshes.
    if not PLAN_ROOT.exists():return None
    found=[]
    for path in PLAN_ROOT.glob('*.json'):
        if not HEX.fullmatch(path.stem):continue
        plan,sha=_read_plan(path)
        if plan['identity']['parent_model_id']==model_id:
            found.append((plan,sha))
    if len(found)>1:raise ValueError('production_admission_multiple_frozen_plans')
    return found[0]if found else None

def _timely_dates(bundle,bars,days,candidates):
    """Derive availability from checksummed receipt timestamps, never a flag.

    No source work when there are no unseen, potentially mature dates. Historical
    snapshots captured after a signal's next open cannot certify that signal.
    """
    if not candidates:return []
    from app.services.market_history_evidence import TRUSTED_HISTORY_DATASET_ROOT,load_history_evidence
    directory=(TRUSTED_HISTORY_DATASET_ROOT/bundle['dataset_id']).resolve()
    manifest=_json(directory/'manifest.json')
    if manifest.get('schema_version')==2:
        from app.services.market_history_evidence import load_trusted_history_dataset
        dataset=load_trusted_history_dataset(bundle['dataset_id'])
        if dataset.manifest_sha256!=bundle['dataset_manifest_sha256']:
            raise ValueError('production_admission_dataset_changed')
        quality=_json(directory/'quality.json');available={}
        for ref in manifest['composition']['parents']:
            parent_dir=TRUSTED_HISTORY_DATASET_ROOT/ref['dataset_id']
            parent=_json(parent_dir/'manifest.json')
            parent_times=_receipt_availability(parent_dir,parent)
            for item in quality['selection']:
                if item['dataset_id']==ref['dataset_id']:
                    for day in item['dates']:
                        key=item['symbol'],date.fromisoformat(day)
                        if key not in parent_times:
                            raise ValueError('production_admission_source_availability_missing')
                        available[key]=parent_times[key]
    else:
        available=_receipt_availability(directory,manifest)
    return _dates_available_before_entry(bundle,bars,days,candidates,available)


def _receipt_availability(directory,manifest):
    from app.services.market_history_evidence import load_history_evidence
    receipts={}
    for ref in manifest['evidence']['receipts']:
        path=(directory/Path(ref['path'])).resolve()
        if not path.is_relative_to(directory):raise ValueError('production_admission_receipt_path_escape')
        _,receipt=load_history_evidence({**ref,'path':str(path)})
        captured=datetime.fromisoformat(receipt['captured_at'].replace('Z','+00:00'))
        if captured.tzinfo is None:raise ValueError('production_admission_source_time_without_zone')
        receipts[ref['sha256']]=captured
    available={}
    for item in manifest['evidence']['field_lineage']:
        refs=[item[key]for key in ('receipt_sha256','verified_against_receipt_sha256','verified_against_raw_receipt_sha256')if item.get(key)]
        captured=max(receipts[r]for r in refs)
        dates=item.get('dates')or manifest['calendar']
        for day in dates:
            key=(item['symbol'],date.fromisoformat(day))
            available[key]=max(available.get(key,captured),captured)
    return available


def _dates_available_before_entry(bundle,bars,days,candidates,available):
    accepted=[];tz=timezone(timedelta(hours=8));by_symbol={s:g.sort_values('trade_date')for s,g in bars.groupby('symbol')}
    for signal in candidates:
        index=days.index(signal)
        if index+5>=len(days):continue
        deadline=datetime.combine(days[index+1],datetime.min.time(),tzinfo=tz).replace(hour=9,minute=30)
        good=0
        for symbol in bundle['symbols']:
            g=by_symbol.get(symbol)
            if g is None:continue
            window=g[pd.to_datetime(g.trade_date).dt.date<=signal].tail(20)
            used=[d.date()for d in pd.to_datetime(window.trade_date)]
            if (len(used)==20 and used[-1]==signal and all((symbol,d)in available and available[(symbol,d)]<=deadline for d in used)):
                good+=1
        if good/len(bundle['symbols'])>=.90:accepted.append(signal)
    return accepted

def inspect_production_admission(db,model_id,*,bundle_id):
    with db.no_autoflush:
        parent,release=_parent_context(db,model_id)
        cfg=get_factor_system_config(db)
    bundle,bars,days=_bundle_context(bundle_id)
    parent_sha=fingerprint(parent)
    existing=_existing_plan(model_id)
    if not Path(cfg.warehouse_path).is_file():raise ValueError('production_admission_warehouse_missing')
    with FactorWarehouse(cfg.warehouse_path).connection(read_only=True)as conn:
        latest=conn.execute('SELECT MAX(trade_date) FROM raw_daily_bars').fetchone()[0]
    today=datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
    observed=max(date.fromisoformat(parent['observed_through']),days[-1],latest or days[-1],today)
    identity={'schema_version':SCHEMA,'parent_model_id':model_id,'parent_fingerprint':parent_sha,
        'bundle_id':bundle_id,'bundle_manifest_sha256':bundle['bundle_manifest_sha256'],
        'dataset_manifest_sha256':bundle['dataset_manifest_sha256'],'symbols':bundle['symbols'],
        'factor_set_id':parent['factor_set_id'],'factor_set_content_hash':parent['factor_set_content_hash'],
        'feature_versions':parent['feature_versions'],'fit_policy':parent['fit_policy'],'alphas':parent['alphas'],
        'model_gate':parent['model_gate'],'governance_gate_config':parent['governance_gate_config'],
        'observed_through':str(observed),'window_days':250,'validation_days':50,
        'minimum_raw_trade_dates':274,'target_entry_offset':1,'target_exit_offset':5,
        'minimum_independent_signal_dates':50,'full_cohort_denominator':len(bundle['symbols']),
        'minimum_daily_bar_coverage':.90,'no_performance_based_date_selection':True,
        'historical_availability_proof_required':True,'net_cost_evaluation_required':True,
        'new_production_model_required':True,'manual_activation_confirmation_required':True}
    frozen,plan_sha=(existing if existing else (None,None))
    if frozen:
        # Source, factor versions and policy remain bound, never silently update.
        if frozen['identity']['parent_fingerprint']!=parent_sha:
            raise ValueError('production_admission_frozen_parent_changed')
        if frozen['identity']['symbols']!=bundle['symbols']:
            raise ValueError('production_admission_frozen_cohort_changed')
        # New immutable bundles may extend evidence under the original protocol.
        if (frozen['identity']['bundle_id']==bundle_id and
                frozen['identity']['bundle_manifest_sha256']!=bundle['bundle_manifest_sha256']):
            raise ValueError('production_admission_frozen_source_changed')
        if frozen['identity']['bundle_id']!=bundle_id:
            original,_,_=_bundle_context(frozen['identity']['bundle_id'])
            if original['bundle_manifest_sha256']!=frozen['identity']['bundle_manifest_sha256']:
                raise ValueError('production_admission_frozen_source_changed')
        identity=frozen['identity'];observed=date.fromisoformat(identity['observed_through'])
    day_counts=bars.groupby('trade_date').symbol.nunique()
    closed=[d for d in days if d<today
        and day_counts.get(pd.Timestamp(d),0)/len(bundle['symbols'])>=.9]
    # An upper bound only: every actual training label still needs the engine's
    # tradeability, finite features, purge and receipt-availability verification.
    # Gaps in cross-section quality must not compress the exchange calendar.
    calendar_index={day:index for index,day in enumerate(days)}
    possible=[d for d in closed if calendar_index[d]>=19 and calendar_index[d]+5<len(days)]
    independent=[d for d in possible if d>observed]
    timely=_timely_dates(bundle,bars,days,independent)
    metric=parent['research_metric_gate']=='validated'
    task=None
    if frozen:
        from app.services.factors.production_candidate_task import get_candidate_task
        task=get_candidate_task(db,frozen['plan_id'])
    gates=[
        dict(code='FROZEN_FACTOR_AND_SAMPLE_EVIDENCE',stage='prepare',passed=True,
            message='冻结因子版本、保存的系数及输入快照指纹一致。'),
        dict(code='RESEARCH_STATISTICAL_RESULT',stage='prepare',passed=metric,
            message='原研究数理门禁通过；该结论不授予生产资格。'if metric else '原研究数理门禁未通过，应先检查原验证结果。'),
        dict(code='PRODUCTION_SAMPLE_SPAN',stage='training',passed=len(closed)>=274,
            actual=len(closed),required=274,message='当前已登记核验样本的交易日数；250为总建模窗口，其中验证50日，另需窗口热身和标签成熟。'),
        dict(code='UNSEEN_MATURE_HOLDOUT',stage='validation',passed=len(independent)>=50,
            actual=len(independent),required=50,message='独立信号日必须晚于冻结的已观察边界，且T+5标签成熟；现有日期只是上限计数，还须逐样本复验。'),
        dict(code='POINT_IN_TIME_SOURCE_PROOF',stage='validation',passed=len(timely)>=50,
            actual=len(timely),required=50,message='按原始回执时间检查二十日特征能否在下次开盘前获得；回溯采集、入库时间或单一布尔标志不构成历史可用性证明。'),
        dict(code='INDEPENDENT_NET_COST_EVIDENCE',stage='release',passed=None,
            message='尚无绑定此冻结方案、模型版本及独立区间的成本后评估证据。'),
        dict(code='NEW_PRODUCTION_CANDIDATE',stage='release',passed=None,
            message='原研究模型保持原状态；候选训练任务已提交，需另行完成独立验证。'if task else
                '原研究模型保持原状态；可信训练历史足够后可另行提交候选，不自动启用。'),
    ]
    return {'schema_version':SCHEMA,'model_id':model_id,'model_label':parent['model_id'],
        'parent_fingerprint':parent_sha,'bundle':bundle,'parent':parent,'current_release_readiness':release,
        'identity':identity,'identity_sha256':fingerprint(identity),'frozen_plan':frozen,'frozen_plan_sha256':plan_sha,
        'can_freeze':bool(metric),'sample_precheck_ready':bool(frozen and metric and len(closed)>=274 and len(independent)>=50 and len(timely)>=50),
        'production_eligible':release['ready'],'planning_only':True,
        'training_not_submitted':task is None,'model_not_activated':True,
        'candidate_task':task.model_dump(mode='json')if task is not None else None,
        'gates':gates,'potential_mature_signal_days_upper_bound':len(possible),
        'independent_signal_days_upper_bound':len(independent),'independent_after':str(observed),
        'cohort_change':{'parent_count':len(parent['training_cohort']),'planned_count':len(bundle['symbols']),
            'added':sorted(set(bundle['symbols'])-set(parent['training_cohort'])),
            'removed':sorted(set(parent['training_cohort'])-set(bundle['symbols']))},
        'next_action':'COLLECT_AND_CERTIFY_UNSEEN_SAMPLES','read_only':True}

def freeze_production_admission(db,model_id,*,bundle_id,expected_identity_sha256):
    with _FREEZE_LOCK:
        return _freeze_production_admission(db,model_id,bundle_id=bundle_id,
            expected_identity_sha256=expected_identity_sha256)


def _freeze_production_admission(db,model_id,*,bundle_id,expected_identity_sha256):
    if not HEX.fullmatch(expected_identity_sha256):raise ValueError('production_admission_invalid_fingerprint')
    check=inspect_production_admission(db,model_id,bundle_id=bundle_id)
    if check['identity_sha256']!=expected_identity_sha256:raise ValueError('production_admission_review_changed')
    if not check['can_freeze']:raise ValueError('production_admission_research_gate_not_passed')
    if check['frozen_plan']is not None:return check
    plan_id=expected_identity_sha256
    plan={'schema_version':SCHEMA,'plan_id':plan_id,'identity':check['identity'],
        'frozen_at':datetime.now(timezone.utc).isoformat(),'parent_model_unchanged':True,
        'is_training_or_release_certificate':False}
    envelope={'plan':plan,'sha256':fingerprint(plan)}
    PLAN_ROOT.mkdir(parents=True,exist_ok=True)
    destination=PLAN_ROOT/(plan_id+'.json')
    # Publish only a completely written file. Hard-link creation is atomic and
    # fails if another request already froze the same identity; never replace it.
    fd,temporary=tempfile.mkstemp(prefix='.pending-',suffix='.tmp',dir=PLAN_ROOT)
    try:
        with os.fdopen(fd,'w',encoding='utf-8')as handle:
            handle.write(canonical(envelope));handle.flush();os.fsync(handle.fileno())
        try:os.link(temporary,destination)
        except FileExistsError:
            prior,_=_read_plan(destination)
            if prior['identity']!=check['identity']:raise ValueError('production_admission_plan_conflict')
    finally:
        Path(temporary).unlink(missing_ok=True)
    return inspect_production_admission(db,model_id,bundle_id=bundle_id)
