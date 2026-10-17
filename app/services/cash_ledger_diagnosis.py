"""Read-only cash-book arithmetic, independent of model/decision execution."""
from __future__ import annotations

from decimal import Decimal
from typing import Any


def diagnose_cash_ledger(rows: list[dict[str, Any]], trades: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Use recorded movements, not initial capital plus a single day's fills.

    A ledger without an opening deposit has an unknown opening balance. We
    can still check adjacent entries, but cannot certify its complete history.
    Preserve sub-cent legacy values in the evidence; never round each row or
    scale the tolerance with portfolio size (which hid real cent-level gaps).
    """
    if not rows:
        return {"status": "NOT_CHECKED", "reason": "CASH_LEDGER_MISSING", "differences": []}
    ordered = sorted(rows, key=lambda r: r['id'])
    decimal = lambda value: Decimal(str(value))
    first = ordered[0]
    complete = first['entry_type'] == 'deposit' and abs(
        decimal(first['balance_after']) - decimal(first['amount'])
    ) <= Decimal('0.01')
    opening = Decimal(0) if complete else decimal(first['balance_after']) - decimal(first['amount'])
    expected = opening
    previous = opening
    differences = []
    for row in ordered:
        amount, actual = decimal(row['amount']), decimal(row['balance_after'])
        expected += amount
        local_expected = previous + amount
        if abs(actual - local_expected) > Decimal('0.01'):
            differences.append(dict(ledger_id=row['id'], expected=float(local_expected),
                actual=float(actual), delta=float(actual-local_expected)))
        previous = actual
    delta = previous - expected
    execution_differences = []
    trade_expected = None
    if trades is not None and complete:
        movements = {}
        for trade in trades:
            side = str(trade['side']).lower()
            if side not in ('buy', 'sell'):
                execution_differences.append({'order_id': trade['order_id'], 'reason': 'UNKNOWN_TRADE_SIDE'})
                continue
            gross, fee = abs(decimal(trade['amount'])), abs(decimal(trade['fee'] or 0))
            change = -(gross+fee) if side == 'buy' else gross-fee
            movements[trade['order_id']] = movements.get(trade['order_id'], Decimal(0)) + change
        cash_by_order = {}
        trading_rows = [r for r in ordered if str(r['entry_type']).lower() in ('buy', 'sell')]
        attributed = all(r.get('ref_type') == 'sim_order' and r.get('ref_id') is not None for r in trading_rows)
        if attributed:
            for row in trading_rows:
                key = row['ref_id']
                cash_by_order[key] = cash_by_order.get(key, Decimal(0)) + decimal(row['amount'])
            for key in sorted(set(movements) | set(cash_by_order)):
                wanted, found = movements.get(key, Decimal(0)), cash_by_order.get(key, Decimal(0))
                if key not in movements or key not in cash_by_order or abs(found-wanted) > Decimal('0.01'):
                    execution_differences.append(dict(order_id=key, expected=float(wanted), actual=float(found)))
        else:
            wanted = sum(movements.values(), Decimal(0))
            found = sum((decimal(r['amount']) for r in trading_rows), Decimal(0))
            if abs(found-wanted) > Decimal('0.01'):
                execution_differences.append(dict(order_id=None, expected=float(wanted), actual=float(found)))
        other_cash = sum((decimal(r['amount']) for r in ordered if r not in trading_rows), Decimal(0))
        trade_expected = other_cash + sum(movements.values(), Decimal(0))
    broken = bool(differences or execution_differences) or abs(delta) > Decimal('0.01')
    return dict(status='BROKEN' if broken else ('CONSISTENT' if complete else 'PARTIAL'),
        opening_verified=complete, scope='current_full_cash_ledger', entry_count=len(ordered),
        expected_cash=float(expected), actual_cash=float(previous), delta=float(delta),
        tolerance=0.01, differences=differences,
        execution_differences=execution_differences,
        trade_derived_cash=None if trade_expected is None else float(trade_expected),
        repair_requires_confirmation=True, historical_asof_certified=False)
