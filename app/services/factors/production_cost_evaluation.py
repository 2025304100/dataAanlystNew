"""Deterministic offline cash replay under the already frozen cost contract.

This module never creates orders, publishes Scores or certifies a model. Raw
price provenance and independent sample eligibility remain separate checks.
"""
from dataclasses import asdict, replace
from datetime import date, datetime, timezone, timedelta
import math

from app.services.simulation_matching_engine import (
    CostModelConfig, MarketBar, OrderPlan, OrderSide, match_order_plan,
)
from app.services.factors import production_admission as admission
from app.services.factors import production_execution as execution
from app.services.factors import production_prediction_journal as journal
from app.services.factors.input_replay import _json

POLICY_VERSION = 'top_quintile_five_sleeves_cash_v1'


def policy_definition(contract):
    return {'schema_version': 1, 'plan_id': contract['plan_id'],
        'execution_sha256': admission.fingerprint(contract),
        'cost_sha256': admission.fingerprint(contract['cost']),
        'independent_after': contract['protocol']['observed_through'],
        'version': POLICY_VERSION, 'required_signal_days': 50,
        'signal_selection': 'first_50_exchange_sessions_after_original_boundary',
        'stock_selection': 'top_ceiling_20pct_finite_journal_predictions_then_symbol_ascending',
        'entry': 'T_plus_1_open', 'exit': 'T_plus_5_close_offline_label_horizon',
        'cash_allocation': 'five_equal_initial_cash_sleeves_rotating_by_signal_index',
        'position_allocation': 'equal_sleeve_cash_budget_before_fills_no_replacement',
        'lot_size': 100, 'buy_sizing': 'largest_affordable_whole_lot_including_all_fees',
        'buy_untradable': 'keep_cash_without_substitution',
        'missing_execution_bar': 'block_evaluation_not_assume_halted',
        'unfilled_exit': 'block_evaluation_not_discard_position',
        'prices': 'verified_unadjusted_CNY',
        'corporate_action': 'block_if_no_constant_adjustment_factor_within_one_cent_price_rounding',
        'price_rounding_quantum_CNY': .01,
        'execution_rules': 'shared_matcher_and_target_tradability_v1',
        'minimum_mean_daily_rank_ic_exclusive': 0., 'minimum_net_return_exclusive': 0.,
        'qualification_certificate': False, 'human_activation_required': True}


def freeze_cost_policy(db, plan_id):
    contract, candidate = journal._candidate(db, plan_id)
    policy = policy_definition(contract)
    path = execution.EXECUTION_ROOT / (plan_id + '.cost-policy.json')
    if path.exists():
        return load_cost_policy(contract)
    # Do not invent a policy after seeing any independent outcome or journal.
    now = datetime.now(timezone.utc)
    if (now.astimezone(timezone(timedelta(hours=8))).date().isoformat() > policy['independent_after']
            or journal.journal_status(db, plan_id)['journal_count']):
        raise ValueError('production_cost_policy_must_precede_independent_period')
    record = {'policy': policy, 'candidate': candidate, 'frozen_at': now.isoformat()}
    envelope = {'record': record, 'sha256': admission.fingerprint(record)}
    execution._publish(path, envelope)
    return envelope


def load_cost_policy(contract):
    envelope = _json(execution.EXECUTION_ROOT / (contract['plan_id'] + '.cost-policy.json'))
    record = envelope['record']
    if (envelope.get('sha256') != admission.fingerprint(record)
            or record['policy'] != policy_definition(contract)):
        raise ValueError('production_cost_policy_changed')
    frozen = datetime.fromisoformat(record['frozen_at'])
    if (frozen.tzinfo is None or frozen.astimezone(timezone(timedelta(hours=8))).date().isoformat()
            > record['policy']['independent_after']):
        raise ValueError('production_cost_policy_frozen_too_late')
    return envelope


def _config(cost, side, *, zero_cost=False):
    fees = cost['fees']
    for key in ('buy_commission_rate','sell_commission_rate','min_commission','stamp_tax_rate',
                'transfer_fee_rate','slippage_buy_bps','slippage_sell_bps'):
        value = fees[key]
        if (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value < 0
                or 'rate' in key and value > 1 or 'bps' in key and value > 10000):
            raise ValueError('production_cost_invalid_frozen_fee:' + key)
    cfg = CostModelConfig(commission_rate=fees['buy_commission_rate' if side is OrderSide.BUY else 'sell_commission_rate'],
        min_commission=fees['min_commission'], stamp_tax_rate=fees['stamp_tax_rate'],
        transfer_fee_rate=fees['transfer_fee_rate'], slippage_buy_bps=fees['slippage_buy_bps'],
        slippage_sell_bps=fees['slippage_sell_bps'])
    return replace(cfg, commission_rate=0., min_commission=0., stamp_tax_rate=0., transfer_fee_rate=0.,
        slippage_buy_bps=0., slippage_sell_bps=0.) if zero_cost else cfg


def _bar(row, day, *, side, previous_close, constraints=None):
    fields = ('open', 'high', 'low', 'close', 'volume', 'amount')
    if row is None or any(isinstance(row.get(k), bool) or not isinstance(row.get(k), (int, float))
            or not math.isfinite(row[k]) for k in fields):
        raise ValueError('production_cost_missing_or_invalid_execution_bar')
    if (any(row[k] <= 0 for k in ('open', 'high', 'low', 'close')) or row['volume'] < 0 or row['amount'] < 0
            or not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']):
        raise ValueError('production_cost_invalid_execution_bar')
    one_price = abs(row['high'] - row['low']) <= 1e-8
    # Align with the existing label tradeability rule; do not guess exchange
    # limit prices from a generic 10% default or replace skipped selected stocks.
    locked_buy = one_price and row['open'] / previous_close - 1 >= .095
    locked_sell = one_price and row['close'] / previous_close - 1 <= -.095
    explicit = {}
    if constraints is not None:
        mode = constraints['price_limit_mode']
        upper, lower = constraints['upper_limit_price'], constraints['lower_limit_price']
        explicit = dict(price_limit_mode=mode, upper_limit_price=upper, lower_limit_price=lower,
            tick_size=constraints['tick_size'])
        locked_buy = bool(mode == 'LIMITED' and one_price and row['open'] >= upper)
        locked_sell = bool(mode == 'LIMITED' and one_price and row['close'] <= lower)
    return MarketBar(trade_date=day, pre_close=previous_close, **{k: row[k] for k in fields},
        halted=row['volume'] == 0 or row['amount'] == 0 or bool(constraints is not None and not constraints['is_tradable']),
        **explicit,
        limit_up_locked=side is OrderSide.BUY and locked_buy,
        limit_down_locked=side is OrderSide.SELL and locked_sell)


def _affordable_quantity(budget, bar, cfg):
    if not math.isfinite(bar.open) or bar.open <= 0:
        raise ValueError('production_cost_invalid_slipped_price')
    lower, upper = 0, int(budget / bar.open) // 100
    while lower < upper:
        mid = (lower + upper + 1) // 2
        trial = match_order_plan(OrderPlan('cost-sizing',0,0,bar.trade_date,'NEXT_OPEN',OrderSide.BUY,mid*100),bar,cfg=cfg)
        if trial.final_status != 'FILLED':
            return 0
        debit = trial.filled_quantity * trial.executed_price + trial.total_cost
        if debit <= budget:
            lower = mid
        else:
            upper = mid - 1
    return lower * 100


def replay_cash_sleeves(*, signals, calendar, symbols, raw_bars, cost, qfq_closes, zero_cost=False,
        execution_constraints=None):
    """Pure arithmetic, not an evidence validator; raw bars must be verified.

    Each signal has ``date`` and its BEFORE-ENTRY ``predictions``. Rankings are
    fixed before inspecting future tradability. T+5 cash may fund that sleeve's
    next T+6 entry, but can never fund an earlier entry in another sleeve.
    """
    if (len(signals) != 50 or not calendar or list(calendar) != sorted(set(calendar))
            or len(set(symbols)) != len(symbols) or not symbols
            or [s['date'] for s in signals] != sorted({s['date'] for s in signals})
            or cost.get('minimum_lot_size') != 100 or cost.get('currency') != 'CNY'):
        raise ValueError('production_cost_invalid_fixed_scope')
    capital = cost['evaluation_capital']
    if isinstance(capital, bool) or not isinstance(capital, (int, float)) or not math.isfinite(capital) or capital <= 0:
        raise ValueError('production_cost_invalid_capital')
    positions = {d: i for i, d in enumerate(calendar)}
    if any(s['date'] not in positions for s in signals):
        raise ValueError('production_cost_signal_outside_calendar')
    indices = [positions[s['date']] for s in signals]
    if indices != list(range(indices[0], indices[0] + 50)) or indices[-1] + 5 >= len(calendar):
        raise ValueError('production_cost_requires_contiguous_mature_signal_dates')
    buy_cfg, sell_cfg = _config(cost, OrderSide.BUY, zero_cost=zero_cost), _config(cost, OrderSide.SELL, zero_cost=zero_cost)
    cash = [float(capital) / 5.] * 5
    available_after = [None] * 5
    traces, total_fees, trade_count = [], 0., 0
    for n, signal in enumerate(signals):
        i = positions[signal['date']]; entry, exit_day = calendar[i + 1], calendar[i + 5]
        sleeve = n % 5
        if available_after[sleeve] is not None and entry <= available_after[sleeve]:
            raise ValueError('production_cost_future_cash_reuse')
        predictions = signal['predictions']
        if set(predictions) != set(symbols):
            raise ValueError('production_cost_prediction_cohort_changed')
        ranked = sorted(((s, v) for s, v in predictions.items() if v is not None
            and not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)), key=lambda item: (-item[1], item[0]))
        if len(ranked) / len(symbols) < .90:
            raise ValueError('production_cost_insufficient_prediction_coverage')
        selected = [s for s, _ in ranked[:math.ceil(len(ranked) * .2)]]
        starting_cash = cash[sleeve]; budget = starting_cash / len(selected)
        sleeve_net_change = 0.; orders = []
        for symbol in selected:
            # Unknown bars cannot be treated as known suspensions or replaced.
            prior = raw_bars.get((symbol, signal['date']))
            if not prior:
                raise ValueError('production_cost_missing_previous_close')
            for close in (prior.get('close'),):
                if close is None or not math.isfinite(close) or close <= 0:
                    raise ValueError('production_cost_missing_previous_close')
            entry_bar = _bar(raw_bars.get((symbol, entry)), entry, side=OrderSide.BUY, previous_close=prior['close'],
                constraints=execution_constraints[(symbol,entry)] if execution_constraints is not None else None)
            quantity = _affordable_quantity(budget, entry_bar, buy_cfg)
            plan = OrderPlan(f'{n}-{symbol}-buy', 0, cost['portfolio_id'], entry, 'NEXT_OPEN', OrderSide.BUY, quantity)
            buy = match_order_plan(plan, entry_bar, cfg=buy_cfg)
            item = {'symbol': symbol, 'selected_before_outcome': True, 'buy': asdict(buy)}
            if buy.final_status != 'FILLED':
                item['outcome'] = 'cash_retained_no_replacement'; orders.append(item); continue
            # Rounded qfq/raw prices need not yield identical floating ratios.
            # Test whether one constant adjustment factor can explain ALL six
            # prices within their fixed one-cent rounding intervals instead.
            factor_lower, factor_upper = 0., float('inf')
            for day in calendar[i:i + 6]:
                raw = raw_bars.get((symbol, day)); qfq = qfq_closes.get((symbol, day))
                raw_close = raw.get('close') if raw else None
                if (raw_close is None or not math.isfinite(raw_close) or raw_close <= .005
                        or qfq is None or not math.isfinite(qfq) or qfq <= .005):
                    raise ValueError('production_cost_adjustment_evidence_missing')
                factor_lower = max(factor_lower, (qfq - .005) / (raw_close + .005))
                factor_upper = min(factor_upper, (qfq + .005) / (raw_close - .005))
            if factor_lower > factor_upper:
                raise ValueError('production_cost_corporate_action_requires_explicit_treatment')
            before_exit = raw_bars.get((symbol, calendar[i + 4]))
            if not before_exit or before_exit.get('close') is None or not math.isfinite(before_exit['close']) or before_exit['close'] <= 0:
                raise ValueError('production_cost_missing_previous_close')
            debit = buy.filled_quantity * buy.executed_price + buy.total_cost
            if debit > budget + 1e-7:
                raise ValueError('production_cost_buy_exceeds_allocated_cash')
            exit_bar = _bar(raw_bars.get((symbol, exit_day)), exit_day, side=OrderSide.SELL, previous_close=before_exit['close'],
                constraints=execution_constraints[(symbol,exit_day)] if execution_constraints is not None else None)
            sell_plan = OrderPlan(f'{n}-{symbol}-sell', 0, cost['portfolio_id'], exit_day, 'T_CLOSE', OrderSide.SELL, buy.filled_quantity)
            sell = match_order_plan(sell_plan, exit_bar, cfg=sell_cfg)
            if sell.final_status != 'FILLED' or sell.filled_quantity != buy.filled_quantity:
                raise ValueError('production_cost_exit_unresolved_no_position_discard')
            credit = sell.filled_quantity * sell.executed_price - sell.total_cost
            sleeve_net_change += credit - debit
            total_fees += buy.total_cost + sell.total_cost; trade_count += 1
            item.update(sell=asdict(sell), entry_debit=debit, exit_credit=credit, net_profit=credit - debit)
            orders.append(item)
        cash[sleeve] += sleeve_net_change
        if cash[sleeve] < -1e-7:
            raise ValueError('production_cost_negative_sleeve_cash')
        available_after[sleeve] = exit_day
        traces.append({'signal_date': str(signal['date']), 'entry_date': str(entry), 'exit_date': str(exit_day),
            'sleeve': sleeve, 'starting_cash': starting_cash, 'ending_cash': cash[sleeve], 'orders': orders})
    ending = sum(cash)
    return {'policy_version': POLICY_VERSION, 'capital': capital, 'ending_cash': ending,
        'net_return': ending / capital - 1., 'fees_paid': total_fees, 'round_trips': trade_count,
        'sleeve_ending_cash': cash, 'traces': traces, 'zero_cost_counterfactual': zero_cost,
        'qualification_certificate': False, 'orders_submitted': 0, 'model_activated': False}


def independent_evaluation_status(db, plan_id):
    contract, candidate = journal._candidate(db, plan_id)
    try:
        locked = load_cost_policy(contract)
        if locked['record']['candidate'] != candidate:
            raise ValueError('production_cost_frozen_candidate_changed')
        frozen = True
    except FileNotFoundError:
        frozen = False
    status = journal.journal_status(db, plan_id)
    from app.services.factors.production_independent_evidence import saved_report_summaries
    reports = saved_report_summaries(contract,candidate,locked['sha256']) if frozen else []
    only = reports[0] if len(reports)==1 else None
    blockers = ([] if frozen else ['COST_POLICY_NOT_FROZEN'])
    if not reports:
        blockers += ['FIRST_50_MATURE_JOURNALED_SIGNAL_DAYS_REQUIRED','VERIFIED_UNADJUSTED_EXECUTION_PRICES_REQUIRED']
    elif len(reports)>1:
        blockers.append('MULTIPLE_REPORTS_REQUIRE_REVIEW_NO_AUTOMATIC_SELECTION')
    elif not only['evidence_pass']:
        blockers.append('INDEPENDENT_STATISTICAL_OR_NET_COST_GATE_FAILED')
    if not status.get('qualification_issued'):
        blockers.append('QUALIFICATION_ISSUANCE_AND_RELEASE_REVERIFICATION_REQUIRED')
    return {'plan_id': plan_id, 'candidate_model_id': candidate['model_id'],
        'cost_policy_frozen': frozen, 'cost_sha256': admission.fingerprint(contract['cost']),
        'journal_count': status['journal_count'], 'required_signal_days': 50,
        'mature_independent_days_verified': 50 if reports else 0, 'execution_price_evidence_verified': bool(reports),
        'cash_replay_engine_supported': True, 'independent_evidence_binding_supported': True,
        'qualification_issuer_supported': True,
        'net_cost_result': only, 'saved_evaluations': reports, 'qualification_issued': status.get('qualification_issued',False),
        'model_activated': status.get('model_activated',False),
        'blockers': blockers, 'read_only': True}
