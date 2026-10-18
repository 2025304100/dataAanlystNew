"""Bind prospective journals to replayed inputs and mature T+5 outcomes.

Evidence reports cannot activate a model. They retain failed statistics and do
not move the original boundary, choose alternative dates or fit new weights.
"""
from datetime import date, datetime, timezone
from uuid import uuid4
import math

import pandas as pd

from app.services.factors import production_prediction_journal as journal
from app.services.factors import production_admission as admission
from app.services.factors import production_execution as execution
from app.services.factors import production_cost_evaluation as cost
from app.services.factors.input_replay import _json, _sha

REPORT_ROOT = admission.PROJECT / 'tmp/factor_independent_evaluations'


def review_independent_evidence(db, plan_id, *, bundle_id):
    contract, candidate = journal._candidate(db, plan_id)
    locked = cost.load_cost_policy(contract)
    if locked['record']['candidate'] != candidate:
        raise ValueError('production_evidence_frozen_candidate_changed')
    bundle, _, calendar = admission._bundle_context(bundle_id)
    if bundle['symbols'] != contract['protocol']['symbols']:
        raise ValueError('production_evidence_cohort_changed')
    boundary = date.fromisoformat(contract['protocol']['observed_through'])
    signals = [d for d in calendar if d > boundary][:50]
    local = journal._now().astimezone(journal.EXCHANGE_ZONE)
    closed = lambda d: d < local.date() or d == local.date() and (local.hour, local.minute) >= (15, 15)
    mature = [d for d in signals if calendar.index(d) + 5 < len(calendar)
        and closed(calendar[calendar.index(d) + 5])]
    records, missing = [], []
    for day in signals:
        try:
            record = journal._read_journal(contract, candidate, day)
            records.append({'signal_date': str(day), 'journal_sha256': record['sha256']})
        except FileNotFoundError:
            missing.append(str(day))
    blockers = []
    if len(signals) != 50: blockers.append('FIRST_50_POSTBOUNDARY_SIGNAL_DAYS_REQUIRED')
    if len(mature) != 50: blockers.append('FIRST_50_T_PLUS_5_OUTCOMES_NOT_MATURE')
    if missing: blockers.append('MISSING_PROSPECTIVE_JOURNALS_NO_DATE_SUBSTITUTION')
    identity = {'schema_version': 1, 'plan_id': plan_id, 'candidate': candidate,
        'cost_policy_sha256': locked['sha256'], 'source': bundle,
        'signal_dates': [str(d) for d in signals], 'journals': records}
    return {'identity': identity, 'sha256': admission.fingerprint(identity),
        'ready': not blockers, 'blockers': blockers, 'mature_signal_count': len(mature),
        'missing_journal_dates': missing, 'qualification_certificate': False, 'read_only': True}


def replay_journal(db, contract, candidate, envelope):
    """Recompute saved predictions from their ORIGINAL, time-verified bundle."""
    from app.services.market_history_evidence import load_trusted_history_dataset
    from app.services.factors.factor_engine import calculate_stock_factors, resolve_frozen_factor_specs
    from app.services.factors.trusted_history_training import build_training_warehouse
    from app.services.factors.scoring_bridge import _load_factor_rows, evaluate_linear_factor_model
    from app.services.factors.store import close_owned_warehouse_path
    report = envelope['prediction']; source = report['source']
    bundle, _, _ = admission._bundle_context(source['bundle_id'])
    if bundle != source:
        raise ValueError('production_evidence_journal_source_changed')
    dataset = load_trusted_history_dataset(bundle['dataset_id'])
    day = date.fromisoformat(report['signal_date']); issued = datetime.fromisoformat(report['issued_at'])
    if (dataset.manifest_sha256 != bundle['dataset_manifest_sha256'] or dataset.calendar[-1] != day
            or list(dataset.symbols) != contract['protocol']['symbols']):
        raise ValueError('production_evidence_journal_dataset_changed')
    times = journal._availability(dataset)
    for row in dataset.bars.itertuples():
        captured = times.get((row.symbol, row.trade_date.date()))
        if captured is None or captured > issued:
            raise ValueError('production_evidence_journal_receipt_arrived_late')
    specs = resolve_frozen_factor_specs(db, (contract['protocol']['factor_set_id'],))
    warehouse = None
    try:
        batch = 'independent-replay-' + uuid4().hex
        warehouse = build_training_warehouse(dataset, batch)
        calculated = calculate_stock_factors(warehouse, start_date=day, end_date=day, trade_dates=(day,),
            calc_batch_id=batch, frozen_factor_specs=specs, selected_features_only=True)
        rows, _ = _load_factor_rows(warehouse, trade_date=day, factor_calc_batch_id=calculated.calc_batch_id,
            extra_factor_codes=tuple(candidate['coefficients']), expected_versions=candidate['feature_versions'])
        predicted = evaluate_linear_factor_model(rows, candidate['coefficients'], candidate['intercept'])['alpha_raw']
        expected = {s: {'linear_signal': predicted.get(s),
            'features': {code: rows.get(s, {}).get(code, {}).get('normalized_value')
                for code in candidate['coefficients']}} for s in contract['protocol']['symbols']}
        if (report['predictions'] != expected or report['predicted_symbols'] != len(predicted)
                or report['full_cohort_denominator'] != len(expected)
                or len(predicted) / len(expected) < .90 or report.get('labels_used') is not False):
            raise ValueError('production_evidence_saved_prediction_does_not_replay')
        return {'date': day, 'predictions': {s: expected[s]['linear_signal'] for s in expected}}
    finally:
        if warehouse is not None: close_owned_warehouse_path(warehouse.path)


def _leaf_sources(dataset):
    from app.services.market_history_evidence import TRUSTED_HISTORY_DATASET_ROOT
    manifest = _json(dataset.directory / 'manifest.json')
    if manifest['schema_version'] == 1:
        return [(dataset.directory, dataset.manifest_sha256)]
    return [(TRUSTED_HISTORY_DATASET_ROOT / ref['dataset_id'], ref['manifest_sha256'])
        for ref in manifest['composition']['parents']]


def verified_raw_prices(dataset, needed_days, *, as_of):
    """Only unadjusted receipts referenced by the pinned, verified dataset.

    A qfq receipt, an unproven adjustment flag, a late/future timestamp or
    inconsistent duplicate raw observation can never become a cash price.
    """
    from app.services.market_history_evidence import load_history_evidence
    prices, references, seen = {}, [], set()
    for directory, digest in _leaf_sources(dataset):
        if _sha(directory / 'manifest.json') != digest:
            raise ValueError('production_evidence_raw_parent_changed')
        manifest = _json(directory / 'manifest.json')
        for ref in manifest['evidence']['receipts']:
            if ref['sha256'] in seen: continue
            seen.add(ref['sha256'])
            path = (directory / ref['path']).resolve()
            if not path.is_relative_to(directory.resolve()):
                raise ValueError('production_evidence_raw_receipt_path_escape')
            metadata = _json(path)
            if metadata.get('actual_adjustment') != '': continue
            frame, receipt = load_history_evidence({**ref, 'path': str(path)})
            captured = datetime.fromisoformat(receipt['captured_at'].replace('Z','+00:00'))
            units = receipt['unit_contract']
            if (captured.tzinfo is None or captured > as_of or units.get('volume_unit') != 'share'
                    or units.get('amount_unit') != 'CNY'):
                raise ValueError('production_evidence_raw_receipt_time_or_unit')
            used = 0
            symbol = receipt['request']['symbol'][-6:]
            for row in frame.to_dict('records'):
                day = pd.Timestamp(row['trade_date']).date()
                if day not in needed_days or symbol not in dataset.symbols: continue
                key = symbol, day
                value = {k: float(row[k]) for k in ('open','high','low','close','volume','amount')}
                if key in prices and prices[key] != value:
                    raise ValueError('production_evidence_conflicting_raw_execution_prices')
                prices[key] = value; used += 1
            if used: references.append({'receipt_sha256': ref['sha256'], 'captured_at': captured.isoformat(), 'used_rows': used})
    if not prices:
        raise ValueError('production_evidence_verified_unadjusted_prices_missing')
    return prices, references


def daily_rank_ic(signals, target_panel, symbols):
    values = []; coverage = []
    for signal in signals:
        group = target_panel[target_panel.signal_date == signal['date']]
        if group.symbol.duplicated().any() or not set(group.symbol).issubset(symbols):
            raise ValueError('production_evidence_target_scope_changed')
        pairs = [(signal['predictions'].get(r.symbol), r.target_value) for r in group.itertuples()
            if r.is_tradable and signal['predictions'].get(r.symbol) is not None
            and math.isfinite(signal['predictions'][r.symbol]) and pd.notna(r.target_value)
            and math.isfinite(r.target_value)]
        if len(pairs) / len(symbols) < .90:
            raise ValueError('production_evidence_insufficient_joint_target_coverage')
        xs, ys = zip(*pairs)
        correlation = pd.Series(xs).corr(pd.Series(ys), method='spearman')
        if not math.isfinite(correlation):
            raise ValueError('production_evidence_daily_rank_ic_undefined')
        values.append(float(correlation))
        coverage.append({'signal_date': str(signal['date']), 'sample_count': len(pairs), 'full_cohort_denominator': len(symbols)})
    return {'method': 'mean_daily_spearman_equal_date_weight', 'daily_ic': values,
        'mean_daily_ic': sum(values) / len(values), 'coverage': coverage}


def run_independent_evaluation(db, plan_id, *, bundle_id, expected_sha256, progress=lambda *args: None):
    from app.services.market_history_evidence import load_trusted_history_dataset
    from app.services.factors.trusted_history_training import build_training_warehouse
    from app.services.factors.target_engine import preview_targets
    from app.services.factors.store import close_owned_warehouse_path
    review = review_independent_evidence(db, plan_id, bundle_id=bundle_id)
    if not review['ready']:
        raise ValueError('production_evidence_not_ready:' + ','.join(review['blockers']))
    if review['sha256'] != expected_sha256:
        raise ValueError('production_evidence_review_changed')
    identity = review['identity']; contract, candidate = journal._candidate(db, plan_id)
    output = REPORT_ROOT / plan_id / (expected_sha256 + '.json')
    previous = None
    if output.exists():
        previous = _json(output)
        if previous.get('sha256') != admission.fingerprint(previous['evaluation']) or previous['evaluation']['identity'] != identity:
            raise ValueError('production_evidence_saved_evaluation_changed')
    progress('verifying_mature_outcome_receipts', 5)
    dataset = load_trusted_history_dataset(identity['source']['dataset_id'])
    if dataset.manifest_sha256 != identity['source']['dataset_manifest_sha256']:
        raise ValueError('production_evidence_outcome_dataset_changed')
    signals = []
    for n, item in enumerate(identity['journals']):
        envelope = journal._read_journal(contract, candidate, item['signal_date'])
        if envelope['sha256'] != item['journal_sha256']:
            raise ValueError('production_evidence_journal_changed_during_dispatch')
        signals.append(replay_journal(db, contract, candidate, envelope))
        progress('replaying_prospective_predictions', 10 + int(55 * (n + 1) / 50))
    calendar = dataset.calendar
    first = calendar.index(signals[0]['date']); last = calendar.index(signals[-1]['date']) + 5
    raw, refs = verified_raw_prices(dataset, set(calendar[first:last + 1]), as_of=journal._now())
    qfq = {(r.symbol, r.trade_date.date()): float(r.close) for r in dataset.bars.itertuples()}
    warehouse = None
    try:
        progress('building_independent_tradable_targets', 75)
        warehouse = build_training_warehouse(dataset, 'independent-targets-' + uuid4().hex)
        targets = preview_targets(warehouse, start_date=signals[0]['date'], end_date=signals[-1]['date'],
            target_horizon=5, symbols=dataset.symbols, as_of_exit_date=calendar[last])
        rank_ic = daily_rank_ic(signals, targets, dataset.symbols)
    finally:
        if warehouse is not None: close_owned_warehouse_path(warehouse.path)
    progress('replaying_frozen_cash_costs', 85)
    cash = cost.replay_cash_sleeves(signals=signals, calendar=calendar, symbols=dataset.symbols,
        raw_bars=raw, qfq_closes=qfq, cost=contract['cost'])
    db.expire_all()
    if review_independent_evidence(db, plan_id, bundle_id=bundle_id)['sha256'] != expected_sha256:
        raise ValueError('production_evidence_changed_during_evaluation')
    statistical_pass = rank_ic['mean_daily_ic'] > 0
    net_pass = cash['net_return'] > 0
    report = {'schema_version': 1, 'identity': identity, 'rank_ic': rank_ic,
        'cash_evaluation': cash, 'raw_receipts': refs, 'independent_signal_dates': identity['signal_dates'],
        'statistical_pass': statistical_pass, 'net_cost_pass': net_pass,
        'evidence_pass': statistical_pass and net_pass, 'qualification_certificate': False,
        'model_activated': False, 'production_scores_published': 0,
        'finished_at': journal._now().isoformat()}
    envelope = {'evaluation': report, 'sha256': admission.fingerprint(report)}
    if previous:
        stable = lambda value: {k:v for k,v in value.items() if k != 'finished_at'}
        if stable(previous['evaluation']) != stable(report):
            raise ValueError('production_evidence_replayed_evaluation_changed')
        return previous
    progress('saving_independent_evidence', 95)
    execution._publish(output, envelope)
    return envelope


def saved_report_summaries(contract,candidate,policy_sha256):
    """Read saved results, never pick the best result or grant eligibility."""
    directory = REPORT_ROOT / contract['plan_id']; rows=[]
    for path in sorted(directory.glob('*.json')) if directory.exists() else ():
        if not admission.HEX.fullmatch(path.stem): continue
        envelope = _json(path); report = envelope['evaluation']; identity = report['identity']
        if (envelope.get('sha256') != admission.fingerprint(report)
                or path.stem != admission.fingerprint(identity) or identity['candidate'] != candidate
                or identity['plan_id'] != contract['plan_id'] or identity['cost_policy_sha256'] != policy_sha256
                or len(identity['signal_dates']) != 50 or report.get('qualification_certificate') is not False):
            raise ValueError('production_evidence_saved_report_binding_changed')
        rows.append({'review_sha256':path.stem,'report_sha256':envelope['sha256'],
            'bundle_id':identity['source']['bundle_id'],'mean_daily_ic':report['rank_ic']['mean_daily_ic'],
            'net_return':report['cash_evaluation']['net_return'],'evidence_pass':report['evidence_pass'],
            'finished_at':report['finished_at'],'qualification_certificate':False,
            'release_reverification_required':True})
    return rows
