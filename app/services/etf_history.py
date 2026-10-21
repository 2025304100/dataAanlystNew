"""Local ETF price-adjustment review; never posts dividends to an account."""
from __future__ import annotations

from datetime import date
import hashlib
import math
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import pandas as pd


def verify_etf_price_adjustment(raw: pd.DataFrame, adjusted: pd.DataFrame,
                                dividends: list[dict], *, directory: Path) -> dict:
    """Reconcile qfq scale changes to documented cash events, in fund units.

    This verifies a retrospective price series. It certifies neither what was
    available on a historical day nor cash reinvestment or execution returns.
    """
    prices = ['open', 'high', 'low', 'close']
    required = {'trade_date', *prices, 'volume', 'amount'}
    prepared = []
    for frame in (raw, adjusted):
        if frame.empty or not required.issubset(frame):
            raise ValueError('trusted_history_etf_price_fields_missing')
        frame = frame.copy()
        frame['trade_date'] = pd.to_datetime(frame.trade_date, errors='coerce')
        if frame.trade_date.isna().any() or frame.trade_date.duplicated().any():
            raise ValueError('trusted_history_etf_price_dates_invalid')
        frame = frame.set_index('trade_date').sort_index()
        values = frame[prices + ['volume', 'amount']].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values[:, :4] <= 0).any() or (values[:, 4:] < 0).any():
            raise ValueError('trusted_history_etf_price_values_invalid')
        prepared.append(frame)
    original, qfq = prepared
    if not original.index.equals(qfq.index):
        raise ValueError('trusted_history_etf_raw_adjusted_dates_differ')
    if not np.array_equal(original[['volume', 'amount']].to_numpy(dtype=float),
                          qfq[['volume', 'amount']].to_numpy(dtype=float)):
        raise ValueError('trusted_history_etf_adjusted_quantities_changed')
    ratios = qfq[prices].to_numpy(dtype=float) / original[prices].to_numpy(dtype=float)
    scale = ratios[:, 3]
    if not np.allclose(ratios, scale[:, None], rtol=0, atol=2e-6) or (scale > 1 + 2e-6).any():
        raise ValueError('trusted_history_etf_qfq_price_scale_invalid')
    if not isinstance(dividends, list):
        raise ValueError('trusted_history_etf_dividends_invalid')
    events = {}
    root = directory.resolve()
    for event in dividends:
        try:
            day = pd.Timestamp(date.fromisoformat(event['ex_date']))
            cash = float(event['cash_per_unit'])
            url = urlsplit(event['source_url'])
            source = (root / event['source_path']).resolve()
            if (day in events or day not in original.index or day == original.index[0]
                    or not math.isfinite(cash) or cash <= 0
                    or url.scheme != 'https' or not url.netloc
                    or not source.is_relative_to(root) or source.is_symlink()
                    or not isinstance(event['source_sha256'], str)):
                raise ValueError('Invalid cash event')
            payload = source.read_bytes()
            if (len(payload) > 4_000_000 or not payload.startswith(b'%PDF')
                    or hashlib.sha256(payload).hexdigest() != event['source_sha256']):
                raise ValueError('Changed dividend document')
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise ValueError('trusted_history_etf_dividend_evidence_invalid') from exc
        events[day] = cash
    previous_close = original.close.to_numpy(dtype=float)[:-1]
    cash = np.asarray([events.get(day, 0.) for day in original.index[1:]])
    expected = 1 - cash / previous_close
    actual = scale[:-1] / scale[1:]
    if (expected <= 0).any() or not np.allclose(actual, expected, rtol=0, atol=2e-6):
        raise ValueError('trusted_history_etf_dividend_adjustment_mismatch')
    return {'rows_checked': len(original), 'cash_dividends_checked': len(events),
            'reviewed_ex_dates': [day.date().isoformat() for day in sorted(events)],
            'price_basis': 'qfq', 'return_basis': 'adjusted_price_return',
            'cash_dividend_accounting_verified': False, 'point_in_time_certified': False,
            'quantities_unchanged': True, 'volume_unit': 'fund_unit', 'amount_unit': 'CNY'}
