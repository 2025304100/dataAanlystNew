"""R1 P01-P13 candidate price gates over explicit, normalized inputs.

No implicit source lookup or calendar padding occurs here. A caller must pin
the versioned facts and official reference values before evaluating a gate.
"""
from decimal import Decimal, InvalidOperation
import json
from urllib.parse import urlsplit

from app.services.governance_fact_contracts import WriterError, canonical, instant

STATES = {'pass', 'fail', 'insufficient', 'not_applicable'}
RULES = {f'P{i:02d}' for i in range(1, 14)}
EPS = Decimal('0.00000001')


def _d(value):
    if value is None:
        return None
    if isinstance(value, (bool, float)):
        raise WriterError('PRICE_GATE_INPUT_INVALID')
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise WriterError('PRICE_GATE_INPUT_INVALID') from None


def _numbers(data, *names):
    return [_d(data.get(name)) for name in names]


def _result(state, reason, value=None):
    return {'status': state, 'reason': reason, 'value': None if value is None else format(value, 'f')}


def _ratio(left, right):
    return abs(left - right) / max(abs(right), EPS)


def _same(data, *names):
    values = [data.get(name) for name in names]
    return all(value is not None for value in values) and len(set(values)) == 1


def _ohlc(data, prefix):
    opening, high, low, close = _numbers(data, *(prefix + name for name in ('open', 'high', 'low', 'close')))
    if any(value is None for value in (opening, high, low, close)):
        return _result('insufficient', 'OHLC_MISSING')
    if min(opening, high, low, close) <= 0 or low > min(opening, close) + EPS or max(opening, close) > high + EPS or low > high + EPS:
        return _result('fail', 'OHLC_INVALID')
    return _result('pass', 'OHLC_VALID')


def _p02(d):
    volume, amount, low, high = _numbers(d, 'volume_shares', 'amount_cny', 'raw_low', 'raw_high')
    if volume is None:
        return _result('insufficient', 'VOLUME_MISSING')
    if volume < 0 or amount is not None and amount < 0:
        return _result('fail', 'NEGATIVE_TURNOVER')
    if amount is None:
        return _result('insufficient', 'AMOUNT_MISSING')
    if volume == 0:
        return _result('pass' if amount == 0 else 'fail', 'NO_TRADE' if amount == 0 else 'AMOUNT_WITHOUT_VOLUME')
    if low is None or high is None:
        return _result('insufficient', 'HIGH_LOW_MISSING')
    if low <= 0 or high < low:
        return _result('fail', 'HIGH_LOW_INVALID')
    vwap = amount / volume
    tolerance = max(Decimal('0.01'), high * Decimal('0.005'))
    return _result('pass' if low - tolerance <= vwap <= high + tolerance else 'fail', 'VWAP_IN_RANGE' if low - tolerance <= vwap <= high + tolerance else 'VWAP_OUT_OF_RANGE', vwap)


def _p03(d):
    close, shares, market = _numbers(d, 'raw_close', 'shares', 'market_cap_cny')
    if d.get('share_kind') not in {'total', 'float'}:
        return _result('insufficient', 'SHARE_KIND_REQUIRED')
    if d.get('currency') is None:
        return _result('insufficient', 'CURRENCY_MISSING')
    if d['currency'] != 'CNY':
        return _result('fail', 'CURRENCY_MISMATCH')
    if d.get('price_date') is None or not _same(d, 'price_date', 'share_date', 'market_date'):
        return _result('insufficient', 'DATE_MISMATCH')
    if None in (close, shares, market):
        return _result('insufficient', 'DEPENDENCY_MISSING')
    if close <= 0 or shares <= 0 or market < 0:
        return _result('fail', 'INPUT_INVALID')
    calculated = close * shares
    return _result('pass' if _ratio(market, calculated) <= Decimal('0.01') else 'fail', 'MARKET_CAP_MATCH' if _ratio(market, calculated) <= Decimal('0.01') else 'MARKET_CAP_MISMATCH', calculated)


def _p04(d):
    volume, shares, reported = _numbers(d, 'volume_shares', 'float_shares', 'turnover_rate_pct')
    if d.get('share_definition') is None or d.get('reported_share_definition') is None:
        return _result('insufficient', 'SHARE_DEFINITION_MISSING')
    if d['share_definition'] != d['reported_share_definition']:
        return _result('insufficient', 'SHARE_DEFINITION_MISMATCH')
    if None in (volume, shares, reported):
        return _result('insufficient', 'DEPENDENCY_MISSING')
    if volume < 0 or shares <= 0 or reported < 0:
        return _result('fail', 'INPUT_INVALID')
    calculated = volume / shares * 100
    ok = abs(calculated - reported) <= Decimal('0.01') or _ratio(reported, calculated) <= Decimal('0.02')
    return _result('pass' if ok else 'fail', 'TURNOVER_MATCH' if ok else 'TURNOVER_MISMATCH', calculated)


def _p05(d):
    low, high, lower, upper, tick = _numbers(d, 'raw_low', 'raw_high', 'official_lower', 'official_upper', 'tick')
    if any(x is None for x in (low, high, lower, upper, tick)) or not d.get('official_rule_version'):
        return _result('insufficient', 'OFFICIAL_LIMIT_MISSING')
    if tick <= 0 or lower <= 0 or upper < lower:
        return _result('fail', 'OFFICIAL_LIMIT_INVALID')
    ok = low >= lower - tick and high <= upper + tick
    return _result('pass' if ok else 'fail', 'WITHIN_LIMIT' if ok else 'PRICE_LIMIT_EXCEEDED')


def _p06(d):
    previous, cash, stock, rights, rights_price, official, tick = _numbers(d, 'previous_raw_close', 'cash_dividend', 'stock_ratio', 'rights_ratio', 'rights_price', 'official_ex_reference', 'tick')
    if None in (previous, cash, stock, rights, official, tick) or rights != 0 and rights_price is None:
        return _result('insufficient', 'EVENT_OR_REFERENCE_MISSING')
    if previous <= 0 or cash < 0 or stock <= -1 or rights < 0 or tick <= 0 or official <= 0 or rights_price is not None and rights_price < 0:
        return _result('fail', 'EVENT_PARAMETERS_INVALID')
    calculated = (previous - cash + (rights_price or Decimal(0)) * rights) / (1 + stock + rights)
    if calculated <= 0:
        return _result('fail', 'NONPOSITIVE_REFERENCE')
    ok = abs(calculated - official) <= tick
    return _result('pass' if ok else 'fail', 'REFERENCE_MATCH' if ok else 'REFERENCE_MISMATCH', calculated)


def _p07(d):
    rows = d.get('rows')
    if not isinstance(rows, list) or not rows:
        return _result('insufficient', 'WINDOW_MISSING')
    expected = d.get('expected_dates')
    if not isinstance(expected, list) or not expected:
        return _result('insufficient', 'CALENDAR_MISSING')
    if len(rows) != len(expected) or [r.get('trade_date') for r in rows] != expected:
        return _result('insufficient', 'WINDOW_GAP')
    if not d.get('as_of') or not d.get('adj_factor_version') or not d.get('base_date'):
        return _result('insufficient', 'FACTOR_BINDING_MISSING')
    for row in rows:
        if any(row.get(k) != d[k] for k in ('adj_factor_version', 'base_date')) or row.get('definition_id') != d.get('definition_id'):
            return _result('fail', 'MIXED_ADJUSTMENT_BASIS')
        value = _d(row.get('adj_close'))
        if value is None:
            return _result('insufficient', 'ADJUSTED_PRICE_MISSING')
        if value <= 0:
            return _result('fail', 'ADJUSTED_PRICE_INVALID')
        if row.get('available_at') is None or instant(row['available_at']) > instant(d['as_of']):
            return _result('insufficient', 'NOT_KNOWN_AS_OF')
    return _result('pass', 'WINDOW_VALID')


def _p08(d):
    price, iopv = _numbers(d, 'raw_price', 'iopv')
    if price is None or iopv is None:
        return _result('insufficient', 'PRICE_OR_IOPV_MISSING')
    if price <= 0 or iopv <= 0:
        return _result('fail', 'PRICE_OR_IOPV_INVALID')
    if d.get('currency') != d.get('iopv_currency') or d.get('valuation_market') != d.get('iopv_market') or not d.get('currency') or not d.get('valuation_market'):
        return _result('insufficient', 'MARKET_BASIS_MISMATCH')
    if not d.get('price_observation_at') or not d.get('iopv_observation_at') or abs((instant(d['price_observation_at'])-instant(d['iopv_observation_at'])).total_seconds()) > 60:
        return _result('insufficient', 'TIME_BASIS_MISMATCH')
    return _result('pass', 'PREMIUM_CALCULATED', (price / iopv - 1) * 100)


def _p09(d):
    close, nav = _numbers(d, 'raw_close', 'nav_official')
    if close is None or nav is None:
        return _result('insufficient', 'CLOSE_OR_NAV_MISSING')
    if close <= 0 or nav <= 0:
        return _result('fail', 'CLOSE_OR_NAV_INVALID')
    if not _same(d, 'trade_date', 'valuation_date'):
        return _result('insufficient', 'VALUATION_DATE_MISMATCH')
    if not d.get('as_of') or not d.get('nav_available_at') or instant(d['nav_available_at']) > instant(d['as_of']):
        return _result('insufficient', 'NAV_NOT_KNOWN_AS_OF')
    if d.get('currency') != d.get('nav_currency') or d.get('currency') != 'CNY':
        return _result('insufficient', 'CURRENCY_MISMATCH')
    return _result('pass', 'OFFICIAL_PREMIUM_CALCULATED', (close / nav - 1) * 100)


def _p10(d):
    nav, shares, size = _numbers(d, 'nav_official', 'total_shares', 'fund_size_cny')
    if not _same(d, 'nav_date', 'shares_date', 'size_date') or not _same(d, 'nav_share_class', 'shares_share_class', 'size_share_class'):
        return _result('insufficient', 'VALUATION_OR_SHARE_CLASS_MISMATCH')
    if None in (nav, shares):
        return _result('insufficient', 'NAV_OR_SHARES_MISSING')
    if nav <= 0 or shares <= 0 or size is not None and size < 0:
        return _result('fail', 'INPUT_INVALID')
    calculated = nav * shares
    if size is None:
        return _result('pass', 'SIZE_CALCULATED', calculated)
    ok = _ratio(size, calculated) <= Decimal('0.005')
    return _result('pass' if ok else 'fail', 'SIZE_MATCH' if ok else 'SIZE_MISMATCH', calculated)


def _p11(d):
    nav, benchmark = d.get('nav_total_returns'), d.get('benchmark_total_returns')
    if d.get('nav_return_kind') != 'dividend_reinvested' or d.get('benchmark_return_kind') != 'total_return':
        return _result('insufficient', 'TOTAL_RETURN_REQUIRED')
    if not isinstance(nav, list) or not isinstance(benchmark, list) or len(nav) != 60 or len(benchmark) != 60 or not isinstance(d.get('nav_point_dates'), list) or len(d['nav_point_dates']) != 61:
        return _result('insufficient', 'WINDOW_60_REQUIRED')
    if d.get('nav_dates') != d.get('benchmark_dates') or not isinstance(d.get('nav_dates'), list) or len(d['nav_dates']) != 60 or d['nav_dates'] != d['nav_point_dates'][1:]:
        return _result('insufficient', 'COMMON_DATES_REQUIRED')
    differences = []
    for left, right in zip(nav, benchmark):
        a, b = _d(left), _d(right)
        if a is None or b is None:
            return _result('insufficient', 'RETURN_MISSING')
        differences.append(a - b)
    mean = sum(differences) / 60
    variance = sum((x - mean) ** 2 for x in differences) / 59
    return _result('pass', 'TRACKING_ERROR_CALCULATED', variance.sqrt() * Decimal(252).sqrt() * 100)


def _p12(d):
    status = _ohlc(d, 'index_')
    if status['status'] != 'pass':
        return status
    unit = d.get('volume_unit')
    if d.get('volume') is not None and unit in {None,'','unknown','not_provided'} or d.get('amount') is not None and not d.get('amount_unit'):
        return _result('insufficient', 'INDEX_VOLUME_UNIT_UNKNOWN')
    if d.get('volume') is not None and _d(d['volume']) < 0 or d.get('amount') is not None and _d(d['amount']) < 0:
        return _result('fail', 'INDEX_VOLUME_AMOUNT_INVALID')
    return _result('pass', 'INDEX_CONTRACT_VALID')


def _p13(d):
    basis = [d.get(k) for k in ('primary_observation', 'backup_observation', 'primary_definition', 'backup_definition')]
    if any(v is None for v in basis):
        return _result('insufficient', 'OBSERVATION_OR_DEFINITION_MISSING')
    if basis[0] != basis[1] or basis[2] != basis[3]:
        return _result('fail', 'INCOMPATIBLE_BASIS')
    pc, bc, pv, bv, pa, ba = _numbers(d, 'primary_raw_close', 'backup_raw_close', 'primary_volume_shares', 'backup_volume_shares', 'primary_amount_cny', 'backup_amount_cny')
    if None in (pc, bc, pv, bv):
        return _result('insufficient', 'REQUIRED_SOURCE_VALUE_MISSING')
    if min(pc, bc) <= 0 or min(pv, bv) < 0 or pa is not None and pa < 0 or ba is not None and ba < 0:
        return _result('fail', 'SOURCE_VALUE_INVALID')
    if abs(pc - bc) > max(Decimal('0.01'), pc * Decimal('0.005')) or _ratio(bv, pv) > Decimal('0.01') or pa is not None and ba is not None and _ratio(ba, pa) > Decimal('0.02'):
        return _result('fail', 'CONFLICT_CANDIDATE')
    return _result('pass', 'SOURCES_MATCH')


_HANDLERS = {'P01': lambda d: _ohlc(d, 'raw_'), 'P02': _p02, 'P03': _p03, 'P04': _p04,
             'P05': _p05, 'P06': _p06, 'P07': _p07, 'P08': _p08, 'P09': _p09,
             'P10': _p10, 'P11': _p11, 'P12': _p12, 'P13': _p13}


def evaluate_price_gate(rule_id, *, asset_type, inputs):
    """Return a deterministic gate result; P13 fail is a conflict candidate only."""
    if rule_id not in RULES or asset_type not in {'stock', 'etf', 'index'} or type(inputs) is not dict:
        raise WriterError('PRICE_GATE_INPUT_INVALID')
    applicable = {'stock': {'P01', 'P02', 'P03', 'P04', 'P05', 'P06', 'P07', 'P13'},
                  'etf': {'P01', 'P02', 'P05', 'P07', 'P08', 'P09', 'P10', 'P11', 'P13'},
                  'index': {'P12'}}
    if rule_id not in applicable[asset_type]:
        return _result('not_applicable', 'ASSET_TYPE')
    return _HANDLERS[rule_id](inputs)


def evaluate_suspension_bar(*, is_suspended, synthetic_bar, official_status_evidence,
                            official_reference, previous_raw_close, action_on_day,
                            raw_open, raw_high, raw_low, raw_close,
                            volume_shares, amount_cny, turnover_rate_pct):
    """Validate a nontradable reference row without inventing an ex-right price."""
    if type(is_suspended) is not bool or type(synthetic_bar) is not bool or type(action_on_day) is not bool:
        raise WriterError('PRICE_GATE_INPUT_INVALID')
    if not is_suspended and not synthetic_bar:
        return {'status': 'not_applicable', 'reason': 'TRADABLE_BAR', 'tradable': True}
    if is_suspended != synthetic_bar:
        return {'status': 'fail', 'reason': 'SUSPENSION_FLAGS_CONFLICT', 'tradable': False}
    if type(official_status_evidence) is not bytes or not official_status_evidence:
        return {'status': 'insufficient', 'reason': 'OFFICIAL_STATUS_MISSING', 'tradable': False}
    official = _d(official_reference)
    previous = _d(previous_raw_close)
    reference = official if official is not None else None if action_on_day else previous
    if reference is None:
        return {'status': 'insufficient', 'reason': 'SUSPENSION_REFERENCE_MISSING', 'tradable': False}
    prices = [_d(v) for v in (raw_open, raw_high, raw_low, raw_close)]
    amounts = [_d(v) for v in (volume_shares, amount_cny, turnover_rate_pct)]
    if reference <= 0 or any(v is None or abs(v-reference) > EPS for v in prices) or any(v != 0 for v in amounts):
        return {'status': 'fail', 'reason': 'SUSPENSION_BAR_INVALID', 'tradable': False}
    return {'status': 'pass', 'reason': 'NONTRADABLE_REFERENCE', 'tradable': False}


def verify_suspension_evidence(proof, *, instrument_id, trade_date):
    """Bind candidate official-status bytes to the daily row and reference."""
    required = {'status_bytes', 'status_source_uri', 'official_reference', 'previous_raw_close', 'action_on_day'}
    if type(proof) is not dict or set(proof) != required or type(proof['status_bytes']) is not bytes or not proof['status_bytes'] or type(proof['status_source_uri']) is not str or type(proof['action_on_day']) is not bool:
        raise WriterError('SUSPENSION_EVIDENCE_REQUIRED')
    uri = urlsplit(proof['status_source_uri'])
    hosts = ('sse.com.cn', 'szse.cn', 'bse.cn')
    if uri.scheme != 'https' or not uri.hostname or not any(uri.hostname == h or uri.hostname.endswith('.'+h) for h in hosts) or uri.username or uri.password:
        raise WriterError('SUSPENSION_AUTHORITY_UNVERIFIED')
    try:
        payload = json.loads(proof['status_bytes'])
        if type(payload) is not dict or set(payload) != {'instrument_id', 'trade_date', 'is_suspended', 'official_reference'} or type(payload['is_suspended']) is not bool or not payload['is_suspended']:
            raise ValueError()
        if payload['instrument_id'] != instrument_id or payload['trade_date'] != trade_date.isoformat() or payload['official_reference'] != proof['official_reference']:
            raise ValueError()
        if canonical(payload).encode('utf-8') != proof['status_bytes']:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError):
        raise WriterError('SUSPENSION_EVIDENCE_INVALID') from None
    return proof
