"""Tencent ETF daily prices with an explicit returned qfq mode and named units.

This is the endpoint used by Tencent's ETF page, rather than the stock SDK's
ambiguous sixth ``amount`` field. Its named volume is already in fund units
(the official chart divides it by 100 to display lots); amount is CNY. Raw and
qfq receipts are retained and checked together before any production write.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd
import requests

from app.services.market_history_contract import cn_history_instrument, resolve_cn_prefixed_symbol


TENCENT_ETF_URL = 'https://proxy.finance.qq.com/cgi/cgi-bin/stockinfoquery/kline/app/get'
TENCENT_ETF_TIMEOUT = (5, 15)
REFERENCE_CALENDAR_DAYS = 14
MAX_CALENDAR_DAYS = 640
RECEIPT_ROOT = Path(__file__).resolve().parents[4] / 'tmp' / 'market-history-etf'
UNIT_CONTRACT_VERSION = 'tencent-etf-named-fund-units-v1'
OFFICIAL_CHART_URL = 'https://st.gtimg.com/quotes/hs-fund/bundle.13362df9.js'


def _instrument(symbol) -> str:
    identity = cn_history_instrument(symbol.symbol)
    prefixed = resolve_cn_prefixed_symbol(symbol.symbol, symbol.market, symbol.asset_type)
    if (symbol.asset_type != 'etf' or identity['asset_type'] != 'etf'
            or identity['market'] + symbol.symbol != prefixed):
        raise ValueError('tencent_etf_instrument_mismatch')
    return prefixed


def parse_etf_response(payload: bytes, *, prefixed: str, start: date, end: date,
                       adjustment: str) -> pd.DataFrame:
    """Reject raw/hfq/wrong-security/invalid rows instead of relabelling them."""
    try:
        body = json.loads(payload)
        data = body['data']
        if (type(body.get('code')) is not int or body['code'] != 0
                or data['stockCode'] != prefixed or data['fqtype'] != adjustment):
            raise ValueError('instrument_or_adjustment_mismatch')
        fields = data['qt']['fields']
        # The provider's ETF page uses fields[61] as security type. Refuse
        # an undocumented layout or a stock/index masquerading as an ETF.
        if (len(fields) <= 82 or fields[2] != prefixed[2:]
                or fields[61] != 'ETF' or fields[82] != 'CNY'):
            raise ValueError('etf_identity_unverified')
        nodes = data['nodes']
        if not isinstance(nodes, list) or not nodes or len(nodes) > 640:
            raise ValueError('empty_or_truncated_history')
        rows = []
        for node in nodes:
            day = date.fromisoformat(node['date'])
            if node['date'] != day.isoformat() or not start <= day <= end:
                raise ValueError('out_of_range_date')
            row = {'trade_date': pd.Timestamp(day), **{
                target: float(node[key]) for target, key in (
                    ('open', 'open'), ('high', 'high'), ('low', 'low'),
                    ('close', 'last'), ('volume', 'volume'), ('amount', 'amount'))}}
            exchange = node.get('exchangeRaw', node.get('exchange'))
            row['turnover_rate'] = float(exchange) if exchange not in (None, '') else None
            values = np.asarray([row[key] for key in ('open', 'high', 'low', 'close', 'volume', 'amount')])
            if (not np.isfinite(values).all() or (values[:4] <= 0).any()
                    or (values[4:] < 0).any() or row['low'] > min(row['open'], row['close'])
                    or row['high'] < max(row['open'], row['close'])
                    or (row['turnover_rate'] is not None and (
                        not np.isfinite(row['turnover_rate']) or row['turnover_rate'] < 0))):
                raise ValueError('invalid_price_or_quantity')
            rows.append(row)
        frame = pd.DataFrame(rows).sort_values('trade_date').reset_index(drop=True)
        if frame.trade_date.duplicated().any():
            raise ValueError('duplicate_trade_date')
        return frame
    except (ValueError, KeyError, TypeError, IndexError, OverflowError) as error:
        raise ValueError('tencent_etf_response_unverified') from error


def _download(directory: Path, *, prefixed: str, start: date, end: date,
              adjustment: str) -> tuple[pd.DataFrame, dict]:
    params = {'code': prefixed, 'fromDate': start.isoformat(), 'toDate': end.isoformat(),
              'ktype': 'day', 'fqtype': adjustment, 'limit': 640}
    name = adjustment or 'raw'
    fetched_at = datetime.now(timezone.utc).isoformat()
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(TENCENT_ETF_URL, params=params, timeout=TENCENT_ETF_TIMEOUT,
                               allow_redirects=False)
        try:
            response.raise_for_status()
            if response.status_code != 200 or len(response.content) > 2_000_000:
                raise ValueError('tencent_etf_http_response_invalid')
            payload = response.content
        finally:
            response.close()
    (directory / (name + '.response.json')).write_bytes(payload)
    receipt = {'provider': 'tencent_etf', 'endpoint': TENCENT_ETF_URL, 'parameters': params,
               'fetched_at': fetched_at, 'response_sha256': hashlib.sha256(payload).hexdigest(),
               'raw_response': name + '.response.json'}
    (directory / (name + '.receipt.json')).write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return parse_etf_response(payload, prefixed=prefixed, start=start, end=end,
                              adjustment=adjustment), receipt


def fetch_tencent_etf_history(*, symbol, start_date: date, end_date: date,
                             adjust: str) -> pd.DataFrame:
    if (adjust != 'qfq' or type(start_date) is not date or type(end_date) is not date
            or start_date > end_date):
        raise ValueError('tencent_etf_requires_qfq_and_valid_dates')
    prefixed = _instrument(symbol)
    reference_start = start_date - timedelta(days=REFERENCE_CALENDAR_DAYS)
    if (end_date - reference_start).days + 1 > MAX_CALENDAR_DAYS:
        raise ValueError('tencent_etf_range_exceeds_640_calendar_days')
    directory = RECEIPT_ROOT / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    adjusted, qfq_receipt = _download(directory, prefixed=prefixed, start=reference_start,
                                     end=end_date, adjustment='qfq')
    raw, raw_receipt = _download(directory, prefixed=prefixed, start=reference_start,
                                end=end_date, adjustment='')
    if (not adjusted.trade_date.equals(raw.trade_date)
            or not np.array_equal(adjusted[['volume', 'amount']].to_numpy(),
                                  raw[['volume', 'amount']].to_numpy())):
        raise ValueError('tencent_etf_raw_qfq_dates_or_quantities_differ')
    selected = adjusted.loc[adjusted.trade_date.ge(pd.Timestamp(start_date))].copy()
    if selected.empty:
        raise ValueError('tencent_etf_target_range_empty')
    reference = adjusted.loc[adjusted.trade_date.lt(pd.Timestamp(start_date))].copy()
    reference['trade_date'] = reference.trade_date.dt.strftime('%Y-%m-%d')
    selected.attrs['history_contract'] = {
        'provider': 'tencent_etf', 'endpoint': 'fund_etf_hist_tx',
        'version': 'cn-etf-shares-v1', 'volume_unit': 'share',
        'amount_unit': 'CNY', 'turnover_unit': 'percentage_point'}
    selected.attrs['history_response'] = {
        'provider': 'tencent_etf', 'endpoint': TENCENT_ETF_URL, 'symbol': prefixed,
        'requested_adjustment': 'qfq', 'actual_adjustment': 'qfq',
        'adjustment_evidence': 'returned_fqtype_and_raw_quantity_pair',
        'receipt_id': directory.name, 'response_sha256': qfq_receipt['response_sha256'],
        'raw_response_sha256': raw_receipt['response_sha256'],
        'fetched_at': qfq_receipt['fetched_at'], 'raw_response_archived': True,
        'point_in_time_certified': False}
    selected.attrs['etf_basis_reference'] = {
        'symbol': prefixed, 'unit_contract': UNIT_CONTRACT_VERSION,
        'start_date': start_date.isoformat(), 'end_date': end_date.isoformat(),
        'reference_start': reference_start.isoformat(),
        'rows': reference.to_dict(orient='records')}
    return selected.reset_index(drop=True)


def probe_tencent_etf_history(*, symbol: str = '510300', market: str = 'sh') -> pd.DataFrame:
    """The interface-management probe uses the same validated adapter."""
    from types import SimpleNamespace
    end = date.today()
    return fetch_tencent_etf_history(symbol=SimpleNamespace(symbol=symbol, market=market, asset_type='etf'),
                                     start_date=end - timedelta(days=7), end_date=end, adjust='qfq')
