"""Frozen execution contracts for new production candidates.

These files are admission inputs, never release certificates. Cost provenance,
cohort, source hashes and fit gates are locked before the first model fit.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, date
import json
import math
import os
from pathlib import Path
import tempfile

from sqlalchemy import select
from app.models.portfolio import Portfolio
from app.models.decision_engine import StrategyExecutionSnapshot
from app.models.factor_runtime import FactorRuntimeState
from app.services.simulation_matching_engine import CostModelConfig
from app.services.cost_model import resolve_stamp_tax_rate
from app.services.factors import production_admission as admission

EXECUTION_ROOT = admission.PROJECT / 'tmp/factor_production_executions'


def normalize_portfolio_cost(portfolio, snapshot=None):
    """Use the applied strategy, with explicit fallbacks from the same matcher.

    A configured zero fee is meaningful; it must never be replaced via ``or``.
    Historical project defaults are configuration assumptions, not legal rates.
    """
    raw = json.loads(snapshot.cost_config_json or '{}') if snapshot else {}
    if not isinstance(raw, dict):
        raise ValueError('production_execution_invalid_cost_config')
    defaults = CostModelConfig()
    fields = {
        'buy_commission_rate': (('buy_commission_pct', 'commission_rate'), portfolio.buy_fee_pct, 'portfolio.buy_fee_pct'),
        'sell_commission_rate': (('sell_commission_pct', 'commission_rate'), portfolio.sell_fee_pct, 'portfolio.sell_fee_pct'),
        'min_commission': (('min_commission',), defaults.min_commission, 'matching_engine.default'),
        'stamp_tax_rate': (('stamp_duty_pct', 'stamp_tax_rate', 'stamp_duty_rate'), resolve_stamp_tax_rate(), 'execution_date.default'),
        'transfer_fee_rate': (('transfer_fee_rate',), defaults.transfer_fee_rate, 'matching_engine.default'),
        'slippage_buy_bps': (('buy_slippage_bps', 'slippage_buy_bps'), defaults.slippage_buy_bps, 'matching_engine.default'),
        'slippage_sell_bps': (('sell_slippage_bps', 'slippage_sell_bps'), defaults.slippage_sell_bps, 'matching_engine.default'),
    }
    values, provenance = {}, {}
    for name, (aliases, fallback, source) in fields.items():
        present = [key for key in aliases if key in raw]
        if len(present) > 1 and any(raw[key] != raw[present[0]] for key in present):
            raise ValueError('production_execution_conflicting_cost_aliases:' + name)
        value = raw[present[0]] if present else fallback
        if name == 'stamp_tax_rate' and value is None:
            value = fallback
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('production_execution_invalid_cost:' + name)
        if ('rate' in name and value > 1) or ('bps' in name and value > 10000):
            raise ValueError('production_execution_invalid_cost:' + name)
        values[name] = float(value)
        provenance[name] = 'strategy_snapshot.' + present[0] if present else source
    capital = portfolio.total_capital
    ratio = portfolio.investable_ratio
    if (isinstance(capital, bool) or capital is None or not math.isfinite(capital) or capital <= 0
            or ratio is None or not math.isfinite(ratio) or not 0 < ratio <= 1
            or portfolio.currency != 'CNY'):
        raise ValueError('production_execution_invalid_evaluation_capital')
    return {'portfolio_id': portfolio.id, 'currency': 'CNY', 'total_capital': float(capital),
        'investable_ratio': float(ratio), 'evaluation_capital': float(capital * ratio),
        'fees': values, 'provenance': provenance, 'minimum_lot_size': 100,
        'source_snapshot_id': snapshot.id if snapshot else None,
        'source_snapshot_hash': snapshot.snapshot_hash if snapshot else None,
        'source_cost_config': raw,
        'regulatory_rates_verified': False,
        'assumption': 'Use the current portfolio/applied strategy configuration, including project matcher fallbacks.'}


def current_portfolio_cost(db, portfolio_id):
    if isinstance(portfolio_id, bool) or not isinstance(portfolio_id, int) or portfolio_id <= 0:
        raise ValueError('production_execution_invalid_portfolio')
    with db.no_autoflush:
        portfolio = db.get(Portfolio, portfolio_id)
        if portfolio is None:
            raise ValueError('production_execution_portfolio_not_found')
        snapshot = db.scalar(select(StrategyExecutionSnapshot).where(
            StrategyExecutionSnapshot.portfolio_id == portfolio_id,
            StrategyExecutionSnapshot.snapshot_type == 'save_and_apply',
            StrategyExecutionSnapshot.effective_from <= datetime.now(timezone.utc).replace(tzinfo=None),
        ).order_by(StrategyExecutionSnapshot.effective_from.desc(),
                   StrategyExecutionSnapshot.snapshot_no.desc()).limit(1))
        return normalize_portfolio_cost(portfolio, snapshot)


def current_runtime_baseline(db):
    with db.no_autoflush:
        runtime = db.get(FactorRuntimeState, 1)
        return ({'active_model_run_id': runtime.active_model_run_id,
                 'weight_mode': runtime.weight_mode, 'version': runtime.version} if runtime else None)


def _publish(path, envelope):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.pending-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(admission.canonical(envelope)); handle.flush(); os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if json.loads(path.read_bytes()) != envelope:
                raise ValueError('production_execution_contract_already_locked')
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_execution(plan_id):
    if not isinstance(plan_id, str) or not admission.HEX.fullmatch(plan_id):
        raise ValueError('production_execution_invalid_plan')
    path = EXECUTION_ROOT / (plan_id + '.json')
    envelope = json.loads(path.read_bytes())
    contract = envelope['contract']
    plan, plan_sha = admission._read_plan(admission.PLAN_ROOT / (plan_id + '.json'))
    if (envelope.get('sha256') != admission.fingerprint(contract) or contract.get('schema_version') != 1
            or contract.get('plan_id') != plan_id or contract.get('plan_sha256') != plan_sha
            or contract.get('protocol') != plan['identity']):
        raise ValueError('production_execution_contract_changed')
    return contract, envelope['sha256']


def review_execution(db, model_id, *, bundle_id, portfolio_id):
    check = admission.inspect_production_admission(db, model_id, bundle_id=bundle_id)
    plan = check['frozen_plan']
    if not plan or not check['can_freeze']:
        raise ValueError('production_execution_requires_frozen_research_protocol')
    if not next(g['passed'] for g in check['gates'] if g['code'] == 'PRODUCTION_SAMPLE_SPAN'):
        raise ValueError('production_execution_insufficient_training_history')
    identity = {'schema_version': 1, 'plan_id': plan['plan_id'],
        'plan_sha256': check['frozen_plan_sha256'], 'protocol': plan['identity'],
        'source': check['bundle'], 'cost': current_portfolio_cost(db, portfolio_id),
        'runtime_baseline': current_runtime_baseline(db),
        'training_cutoff': min(check['bundle']['last_date'], check['independent_after']),
        'source_historical_asof_certified': False,
        'qualification_policy': {'independent_signal_dates': 50,
            'selection': 'first_50_exchange_signal_dates_after_original_boundary',
            'prediction_journal_required_before_next_open': True,
            'mature_tradable_target_horizon': 5,
            'minimum_cross_section_coverage': .90,
            'minimum_mean_daily_rank_ic_exclusive': 0.,
            'minimum_net_return_exclusive': 0.,
            'cost_portfolio': 'long_only_top_quintile_equal_weight_five_nonoverlapping_sleeves_v1',
            'unadjusted_execution_prices_required_for_cash_costs': True,
            'activation': 'explicit_human_confirmation_after_certificate'}}
    path = EXECUTION_ROOT / (plan['plan_id'] + '.json')
    existing = load_execution(plan['plan_id']) if path.exists() else None
    if existing:
        frozen, sha = existing
        # A changing portfolio after the lock does not rewrite evaluation costs.
        if frozen['source'] != identity['source'] or frozen['cost']['portfolio_id'] != portfolio_id:
            raise ValueError('production_execution_locked_source_or_portfolio_conflict')
        return {'contract': frozen, 'sha256': sha, 'locked': True,
                'current_cost_differs': frozen['cost'] != identity['cost']}
    return {'contract': identity, 'sha256': admission.fingerprint(identity), 'locked': False,
            'current_cost_differs': False}


def freeze_execution(db, model_id, *, bundle_id, portfolio_id, expected_sha256):
    with admission._FREEZE_LOCK:
        review = review_execution(db, model_id, bundle_id=bundle_id, portfolio_id=portfolio_id)
        if review['sha256'] != expected_sha256:
            raise ValueError('production_execution_review_changed')
        if review['locked']:
            return review
        contract = {**review['contract'], 'locked_at': datetime.now(timezone.utc).isoformat()}
        # The lock time is included in the final immutable execution identity.
        envelope = {'contract': contract, 'sha256': admission.fingerprint(contract)}
        _publish(EXECUTION_ROOT / (contract['plan_id'] + '.json'), envelope)
        return {'contract': contract, 'sha256': envelope['sha256'], 'locked': True,
                'current_cost_differs': False}


def validate_training_identity(identity, *, cutoff, window_days, validation_days, alphas,
                               factor_set_id, fit_policy, gate):
    """Only an actual server-side frozen contract may create a pending candidate."""
    if not isinstance(identity, dict) or set(identity) != {'plan_id', 'execution_sha256'}:
        raise ValueError('production_execution_invalid_training_identity')
    contract, sha = load_execution(identity['plan_id'])
    protocol = contract['protocol']
    if (sha != identity['execution_sha256'] or str(cutoff) != contract['training_cutoff']
            or window_days != protocol['window_days'] or validation_days != protocol['validation_days']
            or list(alphas) != protocol['alphas'] or factor_set_id != protocol['factor_set_id']
            or fit_policy != protocol['fit_policy'] or gate != protocol['model_gate']):
        raise ValueError('production_execution_training_protocol_changed')
    return contract
