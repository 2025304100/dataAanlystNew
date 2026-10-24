"""Verify a backup ETF qfq series against stored, explicitly typed history."""
from __future__ import annotations

from datetime import date
import math

import pandas as pd
from sqlalchemy import select

from app.models.daily_bar import DailyBar
from app.services.market_data_errors import MarketHistorySourceError
from app.services.market_data_sources.sources.tencent_etf_history import UNIT_CONTRACT_VERSION
from app.services.market_history_contract import resolve_cn_prefixed_symbol


def _conflict(reason: str) -> MarketHistorySourceError:
    return MarketHistorySourceError('ETF_QFQ_BASIS_UNVERIFIED',
        f'ETF备用行情尚不能与已保存行情衔接：{reason}；本次未写入行情。',
        source_label='腾讯财经 · ETF前复权日K',
        next_action='查看前复权口径核验结果，核实原有行情来源或除息调整后再补充；已成功的其他标的不必重跑。',
        retryable=False)


def validate_etf_backup_continuity(db, symbol, frame, *, start: date, end: date,
                                 bar_model=DailyBar, symbol_key=None) -> dict | None:
    response = frame.attrs.get('history_response', {})
    if response.get('provider') != 'tencent_etf':
        return None
    reference = frame.attrs.get('etf_basis_reference', {})
    if (reference.get('unit_contract') != UNIT_CONTRACT_VERSION
            or reference.get('symbol') != resolve_cn_prefixed_symbol(symbol.symbol, symbol.market, 'etf')
            or reference.get('start_date') != start.isoformat() or reference.get('end_date') != end.isoformat()
            or response.get('actual_adjustment') != 'qfq'):
        raise _conflict('来源或日期范围无法核验')
    reference_start = date.fromisoformat(reference['reference_start'])
    symbol_key = symbol_key if symbol_key is not None else DailyBar.symbol_id
    prior = list(db.scalars(select(bar_model).where(symbol_key == symbol.id,
        bar_model.trade_date >= reference_start, bar_model.trade_date < start)
        .order_by(bar_model.trade_date.desc()).limit(7)))
    any_prior = db.scalar(select(bar_model.id).where(symbol_key == symbol.id).limit(1))
    if any_prior is not None and len(prior) < 3:
        raise _conflict('缺少至少三个交易日的可比历史行情')
    by_day = {row['trade_date']: row for row in reference['rows']}
    maxima = {'price': 0., 'volume_fund_units': 0., 'amount_CNY': 0.}
    for bar in prior:
        if bar.source not in {'baostock:cn-etf:v1:qfq', 'akshare:em:cn-etf:v1:qfq', 'tencent:cn-etf:v1:qfq'}:
            raise _conflict('原有行情的前复权方式或成交单位不明确')
        row = by_day.get(bar.trade_date.isoformat())
        if row is None:
            raise _conflict('备用来源缺少重叠交易日')
        for field in ('open', 'high', 'low', 'close', 'volume', 'amount'):
            old, new = getattr(bar, field), row.get(field)
            if old is None or new is None or not math.isfinite(old) or not math.isfinite(new):
                raise _conflict('重叠行情的价格或成交数据不完整')
            # ETF tick = .001 CNY. Tencent named volume is derived from a
            # whole-lot wire field, so comparison allows at most half a lot.
            # These are fixed provider precision bounds, never size inference.
            tolerance = 50.000001 if field == 'volume' else .010001 if field == 'amount' else .000500001
            difference = abs(old - new)
            key = 'volume_fund_units' if field == 'volume' else 'amount_CNY' if field == 'amount' else 'price'
            maxima[key] = max(maxima[key], difference)
            if difference > tolerance:
                label = '价格复权基准' if key == 'price' else '成交量单位或数值' if field == 'volume' else '成交额单位或数值'
                raise _conflict(f'{bar.trade_date.isoformat()} 的{label}不一致')
    return {'status': 'PASSED' if prior else 'INITIAL_SERIES',
        'reference_trade_dates': sorted(bar.trade_date.isoformat() for bar in prior),
        'reference_count': len(prior), 'max_absolute_differences': maxima,
        'volume_unit': 'fund_unit', 'amount_unit': 'CNY',
        'price_basis': 'qfq', 'point_in_time_certified': False}


def validate_etf_backup_write(db, symbol, frame, *, bar_model=DailyBar, symbol_key=None) -> None:
    """Both business and universe writers enforce continuity before mutations."""
    if frame.attrs.get('history_response', {}).get('provider') != 'tencent_etf':
        return
    reference = frame.attrs.get('etf_basis_reference', {})
    try:
        start = date.fromisoformat(reference['start_date'])
        end = date.fromisoformat(reference['end_date'])
        if symbol.asset_type != 'etf' or frame.empty or not pd.to_datetime(
                frame.trade_date).between(pd.Timestamp(start), pd.Timestamp(end)).all():
            raise ValueError('target_range_invalid')
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise _conflict('标的或写入日期与已核实范围不符') from error
    frame.attrs['etf_basis_comparison'] = validate_etf_backup_continuity(
        db, symbol, frame, start=start, end=end, bar_model=bar_model, symbol_key=symbol_key)
