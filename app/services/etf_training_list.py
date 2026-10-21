"""One local research list; it never adds portfolio members or positions."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.models.app_setting import AppSetting
from app.models.async_task import AsyncTaskRecord
from app.db.session import get_session_local
from app.services import async_tasks, etf_history_preparation as history
from app.services.etf_history_preparation import ETFHistoryError
from app.services.factors.__facade__ import get_etf_training_requirements
from app.services.market_history_contract import cn_history_instrument

SETTING_KEY = 'etf_training.list.v1'
MAX_SYMBOLS = 50
_LOCK = threading.Lock()


def _symbols(codes):
    result = []
    for code in codes:
        try:
            instrument = cn_history_instrument(code)
            if instrument['asset_type'] != 'etf':
                raise ValueError()
        except (ValueError, TypeError):
            raise ETFHistoryError(f'{code} 不是可识别的沪深ETF代码，请检查后保存。') from None
        result.append({'symbol': code, 'name': code,
                       'provider_code': instrument['market'] + '.' + code})
    return result


def read_list(db):
    row = db.get(AppSetting, SETTING_KEY, populate_existing=True)
    try:
        codes = json.loads(row.value_json)['codes'] if row else []
        if not isinstance(codes, list) or len(codes) > MAX_SYMBOLS or len(set(codes)) != len(codes):
            raise ValueError()
        symbols = _symbols(codes)
    except (ValueError, TypeError, KeyError):
        raise ETFHistoryError('ETF训练清单读取失败，请检查本地保存的记录；没有按空清单处理。') from None
    return {'name': 'ETF训练清单', 'symbols': symbols, 'symbol_count': len(symbols),
            'list_sha256': hashlib.sha256(json.dumps(codes, separators=(',', ':')).encode()).hexdigest(),
            'minimum_symbols': get_etf_training_requirements()['minimum_symbols'], 'max_symbols': MAX_SYMBOLS}


def save_list(db, codes_text, expected_list_sha256):
    tokens = re.split(r'[\s,，;；、]+', codes_text.strip()) if codes_text.strip() else []
    codes = []
    for token in tokens:
        if not re.fullmatch(r'\d{6}', token):
            raise ETFHistoryError(f'{token} 不是六位ETF代码，请用空格、逗号或换行分开。')
        if token not in codes:
            codes.append(token)
    if len(codes) > MAX_SYMBOLS:
        raise ETFHistoryError(f'训练清单最多保存{MAX_SYMBOLS}只ETF，请减少代码后保存。')
    _symbols(codes)
    with _LOCK, history._DISPATCH_LOCK:
        current = read_list(db)
        if current['list_sha256'] != expected_list_sha256:
            raise ETFHistoryError('ETF训练清单已在其他页面修改，请重新读取后保存。')
        if codes == [row['symbol'] for row in current['symbols']]:
            return current
        active_tasks = db.scalars(select(AsyncTaskRecord).where(
            AsyncTaskRecord.task_type == history.TASK_TYPE, AsyncTaskRecord.status.in_(('queued', 'running'))))
        if any(json.loads(task.payload_json or '{}').get('scope_kind') == 'training_list' for task in active_tasks):
            raise ETFHistoryError('ETF训练历史数据还在准备，请等任务结束后再改清单。')
        row = db.get(AppSetting, SETTING_KEY)
        if row is None:
            row = AppSetting(key=SETTING_KEY)
            db.add(row)
        row.value_json = json.dumps({'codes': codes}, ensure_ascii=False)
        row.updated_by = 'etf-training-list'
        db.commit()
        return read_list(db)


def preparation_scope(db, start_date=None, end_date=None):
    saved = read_list(db)
    datasets, errors = history._datasets()
    codes = {row['symbol'] for row in saved['symbols']}
    cohort = next((d for d in datasets if set(d.symbols) == codes), None)
    end = end_date or datetime.now(timezone(timedelta(hours=8))).date() - timedelta(days=1)
    start = start_date or (cohort.calendar[0] if cohort else end - timedelta(days=1095))
    if start > end or end >= datetime.now(timezone(timedelta(hours=8))).date() or (end - start).days >= history.MAX_DAYS:
        raise ETFHistoryError(f'请选择已经结束的日期，一次最多补充{history.MAX_DAYS}天，开始日期不能晚于结束日期。')
    # Calendar evidence comes from verified historical files, never today's status.
    calendars = {tuple(day for day in d.calendar if start <= day <= end) for d in datasets
                 if d.calendar[0] <= start <= end <= d.calendar[-1]}
    # A checked request may end on a holiday. Preserve its verified natural-day
    # boundary instead of guessing weekdays beyond the last price row.
    by_id = {dataset.dataset_id: dataset for dataset in datasets}
    for task_row in db.scalars(select(AsyncTaskRecord).where(
        AsyncTaskRecord.task_type == history.TASK_TYPE, AsyncTaskRecord.status == 'done')):
        request = json.loads(task_row.payload_json or '{}')
        if not (request.get('history_kind') == 'etf_prices'
                and request.get('start_date', '') <= str(start) <= str(end) <= request.get('end_date', '')):
            continue
        try:
            record = history.read_preparation(db, task_row.id)['report']
            dataset = by_id.get(record.get('dataset_id')) if record else None
            if (dataset and (record.get('prepared') or record.get('imported'))
                    and record.get('training', {}).get('manifest_sha256') == dataset.manifest_sha256):
                calendars.add(tuple(day for day in dataset.calendar if start <= day <= end))
        except ETFHistoryError:
            errors.append('已有准备记录的文件未通过核对，请重新准备历史数据。')
    expected = set(next(iter(calendars))) if len(calendars) == 1 else None
    if len(calendars) > 1:
        errors.append('已有历史资料的交易日期不一致，请重新准备，暂不能确认缺失日期。')
    rows, coverage = [], []
    for symbol in saved['symbols']:
        available = [d for d in datasets if symbol['symbol'] in d.symbols]
        dataset = max(available, key=lambda d: (
            d.calendar[0] <= start <= end <= d.calendar[-1],
            len([day for day in d.calendar if start <= day <= end])), default=None)
        observed = ({value.date() for value in dataset.bars.loc[
            dataset.bars.symbol.eq(symbol['symbol']), 'trade_date'] if start <= value.date() <= end}
            if dataset else None)
        missing = sorted(expected - observed) if expected is not None and observed is not None else None
        reason = ('还没有已核对的历史资料，请准备历史数据。' if dataset is None else
                  '交易日历尚未核实，暂不能确认缺失日期，请准备历史数据。' if expected is None else
                  f'缺少{len(missing)}个交易日的行情，请重新准备历史数据。' if missing else None)
        rows.append({**symbol, 'dataset_id': dataset.dataset_id if dataset else None,
                     'row_count': len(observed) if observed is not None else 0,
                     'first_date': str(min(observed)) if observed else None,
                     'last_date': str(max(observed)) if observed else None,
                     'missing_trade_dates': [str(day) for day in missing] if missing is not None else None,
                     'reason': reason})
        coverage.append(observed)
    common = set.intersection(*coverage) if coverage and all(days is not None for days in coverage) else None
    minimum = saved['minimum_symbols']
    messages = []
    if len(rows) < minimum:
        messages.append(f'清单有{len(rows)}只ETF，训练至少需要{minimum}只。')
    if expected is None or common is None:
        messages.append('共同交易日期尚未核实，请先准备历史数据。')
    elif common != expected:
        messages.append('部分ETF缺少行情，请查看每只ETF的缺失日期后重新准备。')
    if common is not None and len(common) < 274:
        messages.append(f'共同覆盖{len(common)}个交易日，训练至少需要274个交易日。')
    if codes and (cohort is None or expected is not None and (
            set(day for day in cohort.calendar if start <= day <= end) != expected
            or any(len(cohort.bars.loc[cohort.bars.symbol.eq(code)
                & cohort.bars.trade_date.dt.date.isin(expected)]) != len(expected) for code in codes))):
        messages.append('尚未把这份清单在所查日期的行情一起核对，请准备历史数据。')
    if errors:
        messages.extend(errors)
    latest = next((async_tasks._task_to_read(row).model_dump(mode='json') for row in db.scalars(
        select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == history.TASK_TYPE)
        .order_by(AsyncTaskRecord.created_at.desc(), AsyncTaskRecord.id.desc()))
        if (payload := json.loads(row.payload_json or '{}')).get('scope_kind') == 'training_list'
        and payload.get('list_sha256') == saved['list_sha256']), None)
    return {**saved, 'symbols': rows, 'start_date': str(start), 'end_date': str(end),
            'scope_sha256': hashlib.sha256(history._json({
                'list_sha256': saved['list_sha256'], 'start_date': start, 'end_date': end}).encode()).hexdigest(),
            'can_prepare': bool(rows), 'reasons': [] if rows else ['先填写ETF代码并保存清单，再准备历史数据。'],
            'errors': errors, 'max_days': history.MAX_DAYS, 'latest_task': latest,
            'calendar_complete': expected is not None,
            'expected_trade_date_count': len(expected) if expected is not None else None,
            'common_trade_date_count': len(common) if common is not None else None,
            'common_start_date': str(min(common)) if common else None,
            'common_end_date': str(max(common)) if common else None,
            'dataset_id': cohort.dataset_id if cohort else None,
            'training_dataset': history._summary(cohort) if cohort else None,
            'training_ready': not messages, 'training_message': ' '.join(messages) or '清单数量和日期覆盖已够，训练前还会检查所选特征。',
            'backtest_ready': False}


def read_preparation(db, task_id):
    row = db.get(AsyncTaskRecord, task_id, populate_existing=True)
    if row is None or row.task_type != history.TASK_TYPE or json.loads(row.payload_json).get('scope_kind') != 'training_list':
        return None
    return history.read_preparation(db, task_id)


def find_request(db, request_id):
    return read_preparation(db, history._task_id('training_list', request_id)) or {'task': None, 'report': None}


def create_preparation(db, payload):
    request = payload.model_dump(mode='json')
    task_id = history._task_id('training_list', payload.request_id)
    fingerprint = hashlib.sha256(history._json(request).encode()).hexdigest()
    with history._DISPATCH_LOCK:
        existing = db.get(AsyncTaskRecord, task_id, populate_existing=True)
        if existing:
            if existing.task_type != history.TASK_TYPE or existing.idempotency_key != 'request:' + fingerprint:
                raise ETFHistoryError('这次请求的内容已改变，请重新读取清单后准备。')
            return read_preparation(db, task_id)
        scope = preparation_scope(db, payload.start_date, payload.end_date)
        if not scope['can_prepare']:
            raise ETFHistoryError(' '.join(scope['reasons']))
        if scope['scope_sha256'] != payload.expected_scope_sha256:
            raise ETFHistoryError('ETF训练清单或日期已改变，请重新检查后准备。')
        if db.scalar(select(AsyncTaskRecord.id).where(AsyncTaskRecord.task_type == history.TASK_TYPE,
                          AsyncTaskRecord.status.in_(('queued', 'running'))).limit(1)):
            raise ETFHistoryError('已有ETF历史数据正在准备，请等它结束后再试。')
        datasets, _ = history._datasets()
        request.update(scope_kind='training_list', list_sha256=scope['list_sha256'],
                       portfolio_id=None, portfolio_name='ETF训练清单',
                       symbols=scope['symbols'], history_kind='etf_prices')
        request['baselines'] = {d.dataset_id: d.manifest_sha256 for d in datasets
                                if d.dataset_id in {s['dataset_id'] for s in scope['symbols']}}
        task, created = async_tasks.create_async_task_once(history.TASK_TYPE, request,
            task_id=task_id, request_fingerprint=fingerprint)
        if created:
            with get_session_local()() as status_db:
                task = async_tasks._task_to_read(async_tasks._set_task(status_db, task.id, message='正在等待准备ETF训练历史数据'))
            async_tasks._start_worker(task.id, history._prepare_worker)
        return {'task': task.model_dump(mode='json'), 'report': None}
