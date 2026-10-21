"""Bounded ETF history preparation in the existing local task queue."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import shutil
import threading

import pandas as pd

from sqlalchemy import select

from app.models.async_task import AsyncTaskRecord
from app.models.daily_bar import DailyBar
from app.models.market_data import MarketCalendar
from app.services import async_tasks, market_history_evidence as evidence
from app.services.factors.__facade__ import get_etf_training_requirements
from app.services.security_status.history_download import download_scope
from app.services.security_status import baostock_source
from app.core.config import settings
from app.db.session import get_session_local
from app.models.symbol import Symbol

TASK_TYPE = 'etf_history_prepare'
MAX_DAYS = 3660
MAX_SYMBOLS = 20
ARTIFACT_ROOT = settings.base_dir / 'tmp' / 'etf-history-preparations'
_DISPATCH_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


class ETFHistoryError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _datasets():
    from app.services.market_history_contract import cn_history_instrument
    found, errors, created = [], [], {}
    root = evidence.TRUSTED_HISTORY_DATASET_ROOT
    if root.exists():
        for path in sorted(root.glob('*/manifest.json')):
            try:
                if path.parent.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    continue
                manifest = json.loads(path.read_bytes())
                codes = manifest.get('cohort_symbols', [])
                if not codes or any(cn_history_instrument(code)['asset_type'] != 'etf' for code in codes):
                    continue
                dataset = evidence.load_trusted_history_dataset(path.parent.name)
                if dataset.identity.get('asset_type') != 'etf':
                    raise ValueError('Unreviewed ETF history')
                found.append(dataset)
                created[dataset.dataset_id] = str(manifest.get('created_at', ''))
            except (ValueError, OSError, KeyError, TypeError):
                errors.append(f'历史资料 {path.parent.name} 未通过文件核对，暂不能使用，请重新补充。')
    found.sort(key=lambda item: (item.calendar[-1], len(item.calendar), len(item.symbols), created[item.dataset_id], item.dataset_id), reverse=True)
    return found, errors


def _summary(dataset):
    minimum = get_etf_training_requirements()['minimum_symbols']
    reasons = []
    if len(dataset.symbols) < minimum:
        reasons.append(f'当前资料有{len(dataset.symbols)}只ETF，现有模型需要至少{minimum}只ETF进行同日比较。')
    if len(dataset.calendar) < 274:
        reasons.append(f'当前资料有{len(dataset.calendar)}个交易日，训练至少需要274个交易日。')
    counts = dataset.bars.groupby('symbol').trade_date.nunique()
    if any(int(counts.get(code, 0)) != len(dataset.calendar) for code in dataset.symbols):
        reasons.append('部分ETF缺少交易日行情，请先补齐历史数据。')
    return {'dataset_id': dataset.dataset_id, 'manifest_sha256': dataset.manifest_sha256,
            'symbols': list(dataset.symbols), 'symbol_count': len(dataset.symbols),
            'start_date': str(dataset.calendar[0]), 'end_date': str(dataset.calendar[-1]),
            'trade_date_count': len(dataset.calendar), 'bar_count': len(dataset.bars),
            'minimum_symbols': minimum, 'training_ready': not reasons,
            'training_message': ' '.join(reasons) or '行情资料检查通过，提交前还需检查所选训练特征。',
            'backtest_ready': False}


def _task_id(portfolio_id, request_id):
    return hashlib.sha256(f'{TASK_TYPE}|{portfolio_id}|{request_id}'.encode()).hexdigest()[:32]


def read_preparation(db, task_id):
    if not re.fullmatch(r'[a-f0-9]{32}', task_id):
        return None
    row = db.get(AsyncTaskRecord, task_id, populate_existing=True)
    if row is None or row.task_type != TASK_TYPE:
        return None
    task = async_tasks._task_to_read(row).model_dump(mode='json')
    report = None
    expected = (task.get('result') or {}).get('report_sha256')
    if expected:
        try:
            raw = (ARTIFACT_ROOT / task_id / 'report.json').read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError()
            report = json.loads(raw)
        except (OSError, ValueError):
            raise ETFHistoryError('这次补充记录的文件缺失或已改变，请重新补充；不会把它当作已完成。')
    return {'task': task, 'report': report}


def find_request(db, portfolio_id, request_id):
    return read_preparation(db, _task_id(portfolio_id, request_id)) or {'task': None, 'report': None}


def create_preparation(db, payload):
    request = payload.model_dump(mode='json')
    task_id = _task_id(payload.portfolio_id, payload.request_id)
    fingerprint = hashlib.sha256(_json(request).encode()).hexdigest()
    with _DISPATCH_LOCK:
        existing = db.get(AsyncTaskRecord, task_id, populate_existing=True)
        if existing:
            if existing.task_type != TASK_TYPE or existing.idempotency_key != 'request:' + fingerprint:
                raise ETFHistoryError('这次请求的内容已改变，请重新读取范围后补充。')
            return read_preparation(db, task_id)
        scope = preparation_scope(db, payload.portfolio_id, payload.start_date, payload.end_date)
        if not scope['can_prepare']:
            raise ETFHistoryError(' '.join(scope['reasons']))
        if scope['scope_sha256'] != payload.expected_scope_sha256:
            raise ETFHistoryError('组合成员或持仓已改变，请重新读取范围后补充。')
        if db.scalar(select(AsyncTaskRecord.id).where(AsyncTaskRecord.task_type == TASK_TYPE,
                           AsyncTaskRecord.status.in_(('queued', 'running'))).limit(1)):
            raise ETFHistoryError('已有ETF历史数据正在补充，请等它结束后再试。')
        datasets, _ = _datasets()
        request.update(symbols=scope['symbols'], portfolio_name=scope['portfolio_name'], history_kind='etf_prices')
        request['baselines'] = {d.dataset_id: d.manifest_sha256 for d in datasets
                                if d.dataset_id in {s['dataset_id'] for s in scope['symbols']}}
        task, created = async_tasks.create_async_task_once(TASK_TYPE, request, task_id=task_id, request_fingerprint=fingerprint)
        if created:
            with get_session_local()() as status_db:
                task = async_tasks._task_to_read(async_tasks._set_task(status_db, task.id, message='正在等待补充ETF历史数据'))
            async_tasks._start_worker(task.id, _prepare_worker)
        return {'task': task.model_dump(mode='json'), 'report': None}


def _save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(_json(value), encoding='utf-8')
    temporary.replace(path)


def _source_query(directory, capture, key, endpoint, parameters, required):
    refs = [item for item in capture.get('queries', []) if item.get('key') == key]
    if len(refs) != 1 or refs[0].get('file') != key + '.json':
        raise ETFHistoryError('没有取得完整的原始返回，请重试补充。')
    raw = (directory / (key + '.json')).read_bytes()
    if len(raw) > 8_000_000 or hashlib.sha256(raw).hexdigest() != refs[0].get('sha256'):
        raise ETFHistoryError('原始返回文件核对失败，请重试补充。')
    value = json.loads(raw)
    if value.get('endpoint') != endpoint or any(value.get('parameters', {}).get(k) != v for k, v in parameters.items()):
        raise ETFHistoryError('返回结果与本次请求不一致，请重试补充。')
    data = value.get('raw', {})
    fields, rows = data.get('fields', []), data.get('rows', [])
    if data.get('error_code') != '0':
        raise ETFHistoryError('数据来源返回失败，请稍后重试；未按空数据处理。')
    if (len(set(fields)) != len(fields) or not set(required).issubset(fields)
            or len(rows) > MAX_DAYS or any(not isinstance(r, list) or len(r) != len(fields) for r in rows)):
        raise ETFHistoryError('来源返回的字段或行数不正确，请重试补充。')
    return pd.DataFrame(rows, columns=fields), value


def _review_download(directory, request):
    report = {'portfolio_id': request['portfolio_id'], 'portfolio_name': request['portfolio_name'],
              'start_date': request['start_date'], 'end_date': request['end_date'], 'symbols': [],
              'errors': [], 'imported': False, 'dataset_id': None, 'backtest_ready': False,
              'message': '行情补充不代表交易资料齐全；历史停牌和逐日限价不完整时，成交回测继续停止。'}
    report.update(scope_kind=request.get('scope_kind', 'portfolio'), list_sha256=request.get('list_sha256'),
                  prepared=False, expected_trade_date_count=None, common_trade_date_count=None,
                  common_start_date=None, common_end_date=None)
    capture = json.loads((directory / 'capture.json').read_text(encoding='utf-8'))
    dates = {key: request[key] for key in ('start_date', 'end_date')}
    calendar, _ = _source_query(directory, capture, 'calendar', 'query_trade_dates', dates, ['calendar_date', 'is_trading_day'])
    expected_calendar = pd.date_range(request['start_date'], request['end_date']).strftime('%Y-%m-%d').tolist()
    if calendar.calendar_date.tolist() != expected_calendar or not calendar.is_trading_day.isin(['0', '1']).all():
        raise ETFHistoryError('所选范围的交易日历不完整，缺失日期暂时无法确认，请重试。')
    days = tuple(date.fromisoformat(value) for value in calendar.loc[calendar.is_trading_day.eq('1'), 'calendar_date'])
    if not days:
        raise ETFHistoryError('所选日期没有交易日，请调整日期范围。')
    report['expected_trade_date_count'] = len(days)
    frames, coverage = {}, []
    for symbol in request['symbols']:
        code, provider_code = symbol['symbol'], symbol['provider_code']
        summary = {'symbol': code, 'name': symbol['name'], 'missing_trade_dates': None, 'row_count': 0, 'error': None}
        report['symbols'].append(summary)
        try:
            basic, _ = _source_query(directory, capture, 'basic_' + code, 'query_stock_basic', {'code': provider_code}, ['code', 'ipoDate', 'outDate', 'type'])
            if len(basic) != 1 or basic.iloc[0]['code'] != provider_code or basic.iloc[0]['type'] != '5':
                raise ETFHistoryError('来源未确认该代码属于ETF，请检查代码和交易所。')
            ipo = date.fromisoformat(basic.iloc[0]['ipoDate'])
            out = date.fromisoformat(basic.iloc[0]['outDate']) if basic.iloc[0]['outDate'] else None
            outside_listing = ipo > days[0] or (out and out <= days[-1])
            if outside_listing and request.get('scope_kind') != 'training_list':
                raise ETFHistoryError('所选日期超出该ETF的上市范围，请调整开始或结束日期。')
            missing, pair = set(), {}
            for label, flag in [('raw', '3'), ('qfq', '2')]:
                frame, value = _source_query(directory, capture, label + '_' + code, 'query_history_k_data_plus',
                    {'code': provider_code, **dates, 'frequency': 'd', 'adjustflag': flag},
                    ['date', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adjustflag'])
                parsed = pd.to_datetime(frame.date, errors='coerce')
                if parsed.isna().any() or parsed.duplicated().any() or not frame.code.eq(provider_code).all() or not frame.adjustflag.eq(flag).all():
                    raise ETFHistoryError('日线存在重复日期、代码或复权方式不符，请重试补充。')
                observed = set(parsed.dt.date)
                if observed - set(days):
                    raise ETFHistoryError('日线包含所选交易日以外的数据，请重试补充。')
                missing.update(set(days) - observed)
                pair[label] = (frame, value)
            missing.update(day for day in days if day < ipo or (out and day >= out))
            summary.update(row_count=len(days) - len(missing), missing_trade_dates=[str(day) for day in sorted(missing)])
            coverage.append(set(days) - missing)
            if outside_listing:
                raise ETFHistoryError(f'该ETF在{ipo}上市' + (f'，{out}退市' if out else '')
                    + '，所选日期超出上市范围。请调整日期后重新检查整份清单，不会自动删除该ETF。')
            if missing:
                raise ETFHistoryError(f'缺少{len(missing)}个交易日的原始价或前复权价，请重试补充。')
            frames[code] = pair
        except (ValueError, OSError, KeyError, TypeError) as error:
            summary['error'] = str(error) if isinstance(error, ETFHistoryError) else '原始资料格式不完整，请重试补充。'
    if len(coverage) == len(request['symbols']):
        common = set.intersection(*coverage)
        report.update(common_trade_date_count=len(common), common_start_date=str(min(common)) if common else None,
                      common_end_date=str(max(common)) if common else None)
    if not capture.get('complete'):
        report['errors'].append('下载没有完成，已取得的原始文件保留，请重试补充。')
    return report, frames, days


def _prepare_dataset(task_id, directory, request, frames, days):
    sdk_version = json.loads((directory / 'capture.json').read_bytes()).get('sdk_version')
    if not isinstance(sdk_version, str) or not sdk_version:
        raise ETFHistoryError('原始返回缺少数据来源的版本记录，请重试补充。')
    target = evidence.TRUSTED_HISTORY_DATASET_ROOT / ('etf-' + task_id)
    target.mkdir(parents=True, exist_ok=False)
    refs, dividends = [], {}
    for symbol in request['symbols']:
        code = symbol['symbol']
        events = []
        if symbol.get('dataset_id'):
            baseline = evidence.load_trusted_history_dataset(symbol['dataset_id'])
            if baseline.manifest_sha256 != request['baselines'].get(baseline.dataset_id):
                raise ETFHistoryError('补充前核对过的历史文件已改变，请重新读取后补充。')
            manifest = json.loads((baseline.directory / 'manifest.json').read_bytes())
            for index, event in enumerate(manifest['evidence']['etf_adjustments'][code]['cash_dividends']):
                if not str(days[0]) <= event['ex_date'] <= str(days[-1]):
                    continue
                source = baseline.directory / event['source_path']
                relative = f'documents/{code}-{index}.pdf'
                dest = target / relative
                dest.parent.mkdir(exist_ok=True)
                shutil.copyfile(source, dest)
                events.append({**event, 'source_path': relative})
        dividends[code] = events
        for label, adjust in [('raw', ''), ('qfq', 'qfq')]:
            frame, obj = frames[code][label]
            refs.append(evidence.capture_baostock_history(artifact_root=target / 'receipts', capture_id=code + '-' + label,
                request=evidence.HistoryRequest(symbol['provider_code'].replace('.', ''), days[0], days[-1], adjust),
                raw_frame=frame, sdk_version=sdk_version, acquisition_metadata={
                    'endpoint': obj['endpoint'], 'parameters': obj['parameters'], 'fetched_at': obj.get('fetched_at'),
                    'source_capture': str(directory.resolve()), 'source_raw_sha256': hashlib.sha256((directory / (label + '_' + code + '.json')).read_bytes()).hexdigest()}))
    try:
        evidence.write_trusted_history_dataset(dataset_id=target.name, receipts=refs,
            cohort_symbols=tuple(sorted(frames)), calendar=days,
            plan_sha256=hashlib.sha256(_json(request).encode()).hexdigest(), etf_cash_dividends=dividends)
    except ValueError as error:
        if 'dividend' in str(error):
            raise ETFHistoryError('分红记录与前复权价格没有对上，可能新增了分红或原公告缺失。原始文件已保留，请补充分红公告并核对后重试；旧行情未改。') from error
        raise ETFHistoryError('原始价、前复权价或成交量核对未通过，旧行情未改，请检查来源后重试。') from error
    return evidence.load_trusted_history_dataset(target.name)


def _backup(db, directory, request):
    from app.models.universe import UniverseSymbol, UniverseDailyBar
    backup = {'symbols': request['symbols'], 'daily_bars': [], 'universe_symbols': [], 'universe_bars': []}
    def record(row):
        return {column.key: getattr(row, column.key) for column in row.__table__.columns}
    for symbol in request['symbols']:
        backup['daily_bars'].extend(record(row) for row in db.scalars(select(DailyBar).where(
            DailyBar.symbol_id == symbol['symbol_id'], DailyBar.trade_date.between(request['start_date'], request['end_date']))))
        for universe in db.scalars(select(UniverseSymbol).where(UniverseSymbol.symbol == symbol['symbol'], UniverseSymbol.asset_type == 'etf')):
            backup['universe_symbols'].append(record(universe))
            backup['universe_bars'].extend(record(row) for row in db.scalars(select(UniverseDailyBar).where(
                UniverseDailyBar.universe_symbol_id == universe.id, UniverseDailyBar.trade_date.between(request['start_date'], request['end_date']))))
    path = directory / 'before-import.json'
    if path.exists():
        raise ETFHistoryError('本次备份已存在，请查看已有结果，不能覆盖后再次导入。')
    _save(path, backup)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_worker(task_id):
    db = get_session_local()()
    report = None
    directory = ARTIFACT_ROOT / task_id
    try:
        row = db.get(AsyncTaskRecord, task_id)
        if row is None or row.status != 'queued' or row.cancel_requested:
            return
        request = json.loads(row.payload_json)
        training_list = request.get('scope_kind') == 'training_list'
        download_timeout = max(180, 12 * len(request['symbols'])) if training_list else 180
        directory.mkdir(parents=True, exist_ok=False)
        _save(directory / 'request.json', request)
        total = 1 + 3 * len(request['symbols'])
        async_tasks._set_task(db, task_id, status='running', stage='download', total=total,
            message='正在下载ETF原始价和前复权价', started_at=async_tasks._now(), heartbeat_at=async_tasks._now(), stage_budget_seconds=download_timeout + 120)

        def progress(processed, current):
            db.expire_all()
            row = db.get(AsyncTaskRecord, task_id)
            if row.cancel_requested or row.status not in {'queued', 'running'} or async_tasks.is_worker_stop_requested(task_id):
                raise baostock_source.DownloadCancelled()
            async_tasks._set_task(db, task_id, processed=min(processed, total), percent=min(85, processed / total * 85),
                message=f'正在下载ETF历史数据（{min(processed, total)}/{total}）', heartbeat_at=async_tasks._now())

        baostock_source.capture_history(request, directory, progress, timeout_seconds=download_timeout)
        progress(total, '检查历史资料')
        async_tasks._set_task(db, task_id, stage='review', percent=90, message='正在核对缺失日期、分红和复权价格')
        report, frames, days = _review_download(directory, request)
        if report['errors'] or any(item['error'] for item in report['symbols']):
            raise ETFHistoryError('部分历史数据没有取得完整，请查看缺失日期和原因后重试。')
        dataset = _prepare_dataset(task_id, directory, request, frames, days)
        progress(total, '保存已核对的行情')
        report['writes'] = []
        if not training_list:
            report['backup_sha256'] = _backup(db, directory, request)
            from app.services.market_data import import_verified_etf_history
            for symbol in request['symbols']:
                current = db.get(Symbol, symbol['symbol_id'], populate_existing=True)
                if current is None or current.symbol != symbol['symbol'] or current.asset_type != 'etf' or current.market.lower() != symbol['provider_code'][:2]:
                    raise ETFHistoryError('ETF代码、品种或交易所已改变，本次没有导入，请重新读取范围。')
                report['writes'].append(import_verified_etf_history(db, current, dataset.dataset_id))
        # Keep the final data writes and the terminal task record in one transaction.
        row = db.get(AsyncTaskRecord, task_id, populate_existing=True)
        if row.cancel_requested or async_tasks.is_worker_stop_requested(task_id):
            raise baostock_source.DownloadCancelled()
        report.update(prepared=True, imported=not training_list, dataset_id=dataset.dataset_id, training=_summary(dataset))
        _save(directory / 'report.json', report)
        async_tasks._set_task(db, task_id, status='done', stage='done', percent=100, processed=total,
            ok_count=len(request['symbols']), message='ETF训练历史数据已准备并核对，可以返回训练页面检查。' if training_list else 'ETF历史数据已补齐并核对，页面行情和基础行情已更新。',
            result_json=_json({'report_sha256': hashlib.sha256((directory / 'report.json').read_bytes()).hexdigest()}), finished_at=async_tasks._now())
    except Exception as error:
        logger.exception('ETF history preparation failed: %s', task_id)
        db.rollback()
        cancelled = isinstance(error, baostock_source.DownloadCancelled)
        message = ('补充已停止，原始文件保留，可以重试。' if cancelled else str(error)
                   if isinstance(error, (ETFHistoryError, baostock_source.SourceDownloadError))
                   else '本次补充没有完成，原始文件已保留，旧行情未改，请重试。')
        row = db.get(AsyncTaskRecord, task_id, populate_existing=True)
        if row is not None and row.status in {'queued', 'running'}:
            request = json.loads(row.payload_json)
            report = report or {'portfolio_id': request['portfolio_id'], 'start_date': request['start_date'],
                'end_date': request['end_date'], 'symbols': [
                    {'symbol': s['symbol'], 'name': s['name'], 'row_count': 0,
                     'missing_trade_dates': None, 'error': message} for s in request['symbols']],
                'errors': [], 'backtest_ready': False}
            report['imported'] = False
            report.update(prepared=False, scope_kind=request.get('scope_kind', 'portfolio'),
                          list_sha256=request.get('list_sha256'))
            for field in ('expected_trade_date_count', 'common_trade_date_count', 'common_start_date', 'common_end_date'):
                report.setdefault(field, None)
            report['errors'].append(message)
            for symbol in report['symbols']:
                if not symbol['error']:
                    symbol['error'] = message
            directory.mkdir(parents=True, exist_ok=True)
            _save(directory / 'report.json', report)
            status = 'cancelled' if cancelled else 'partial' if any(s['row_count'] for s in report['symbols']) else 'failed'
            async_tasks._append_error(row, {'code': 'ETF_HISTORY_INCOMPLETE', 'title_zh': 'ETF历史数据补充未完成',
                                           'detail_zh': message, 'retryable': True})
            async_tasks._set_task(db, task_id, status=status, stage=status, message=message, failed_count=len(request['symbols']),
                result_json=_json({'report_sha256': hashlib.sha256((directory / 'report.json').read_bytes()).hexdigest()}), finished_at=async_tasks._now())
    finally:
        db.close()


def list_datasets():
    datasets, errors = _datasets()
    return {'items': [_summary(dataset) for dataset in datasets], 'errors': errors}


def training_options(db, factor_set_id, use_training_list=False,
                     dataset_id=None, manifest_sha256=None, expected_list_sha256=None):
    from app.services.factors.__facade__ import get_etf_feature_readiness
    selected = (dataset_id, manifest_sha256, expected_list_sha256)
    if any(selected) and (not all(selected) or not use_training_list):
        raise ETFHistoryError('本次准备的资料记录不完整，请回到历史数据页重新查看训练条件。')
    datasets, errors = _datasets()
    list_info = {}
    if use_training_list:
        from app.services.etf_training_list import read_list
        saved = read_list(db)
        if dataset_id and saved['list_sha256'] != expected_list_sha256:
            raise ETFHistoryError('ETF训练清单已修改，请回到历史数据页按当前清单重新检查。')
        codes = {row['symbol'] for row in saved['symbols']}
        datasets = [d for d in datasets if set(d.symbols) == codes]
        if dataset_id:
            chosen = next((d for d in datasets if d.dataset_id == dataset_id), None)
            if chosen is None:
                raise ETFHistoryError('找不到本次准备的资料，或文件、ETF代码未通过核对，请回到历史数据页重新检查。')
            if chosen.manifest_sha256 != manifest_sha256:
                raise ETFHistoryError('本次准备的资料文件已有变化，请回到历史数据页重新检查。')
            datasets = [chosen]
        list_info = {'list_sha256': saved['list_sha256'], 'list_symbol_count': len(codes),
                     'list_message': 'ETF训练清单还是空的，请先填写代码并准备历史数据。' if not codes else
                         f'当前训练清单有{len(codes)}只ETF，只使用与这份清单一致的已核对资料。'
                         + ('尚未准备完成，请到历史数据入口查看缺失原因。' if not datasets else '')}
    items, cohorts = [], set()
    for dataset in datasets:
        if dataset.symbols in cohorts:
            continue
        cohorts.add(dataset.symbols)
        summary = _summary(dataset)
        feature = get_etf_feature_readiness(db, factor_set_id, dataset.dataset_id)
        summary.update(feature)
        summary['training_ready'] = summary['training_ready'] and feature['feature_ready']
        items.append(summary)
    return {'factor_set_id': factor_set_id, 'factor_ready': bool(items) and all(item['factor_ready'] for item in items),
            'items': items, 'errors': errors, **get_etf_training_requirements(), **list_info}


def preparation_scope(db, portfolio_id, start_date=None, end_date=None):
    original = download_scope(db, portfolio_id)
    symbols = [dict(row) for row in original['symbols'] if row['asset_type'] == 'etf']
    datasets, errors = _datasets()
    baseline = {row['symbol']: next((d for d in datasets if row['symbol'] in d.symbols), None) for row in symbols}
    end = end_date or date.fromisoformat(original['end_date'])
    start = start_date or min((d.calendar[0] for d in baseline.values() if d), default=end - timedelta(days=1095))
    if start > end or end >= datetime.now(timezone(timedelta(hours=8))).date() or (end - start).days >= MAX_DAYS:
        raise ETFHistoryError(f'请选择已经结束的日期，一次最多补充{MAX_DAYS}天，开始日期不能晚于结束日期。')
    calendar = dict(db.execute(select(MarketCalendar.trade_date, MarketCalendar.is_trading_day)
                              .where(MarketCalendar.trade_date.between(start, end))).all())
    calendar_complete = len(calendar) == (end - start).days + 1
    for row in symbols:
        dataset = baseline[row['symbol']]
        covered = {value.date() for value in dataset.bars.loc[dataset.bars.symbol.eq(row['symbol']), 'trade_date']} if dataset else set()
        expected = sorted(day for day, trading in calendar.items() if trading) if calendar_complete else (
            [day for day in dataset.calendar if start <= day <= end] if dataset and dataset.calendar[0] <= start <= end <= dataset.calendar[-1] else None)
        local_dates = set(db.scalars(select(DailyBar.trade_date).where(DailyBar.symbol_id == row['symbol_id'], DailyBar.trade_date.between(start, end))))
        row.update(dataset_id=dataset.dataset_id if dataset else None,
                   first_date=str(min(local_dates)) if local_dates else None,
                   last_date=str(max(local_dates)) if local_dates else None, row_count=len(local_dates),
                   missing_trade_dates=[str(day) for day in expected if day not in local_dates] if expected is not None else None,
                   unreviewed_dates=[str(day) for day in expected if day not in covered] if expected is not None else None)
    reasons = []
    if not symbols:
        reasons.append('当前组合没有ETF，请先在组合中添加ETF。')
    if len(symbols) > MAX_SYMBOLS:
        reasons.append(f'一次最多补充{MAX_SYMBOLS}只ETF，请缩小组合范围。')
    if any(not row['supported'] for row in symbols):
        reasons.append('当前来源只支持沪深ETF，请检查组合中的ETF代码和交易所。')
    latest = next((async_tasks._task_to_read(row).model_dump(mode='json') for row in db.scalars(
        select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == TASK_TYPE).order_by(AsyncTaskRecord.created_at.desc()))
        if json.loads(row.payload_json or '{}').get('portfolio_id') == portfolio_id), None)
    minimum = get_etf_training_requirements()['minimum_symbols']
    return {'portfolio_id': portfolio_id, 'portfolio_name': original['portfolio_name'], 'symbols': symbols,
            'scope_sha256': hashlib.sha256(_json(original['symbols']).encode()).hexdigest(),
            'start_date': str(start), 'end_date': str(end), 'max_days': MAX_DAYS, 'max_symbols': MAX_SYMBOLS,
            'can_prepare': not reasons, 'reasons': reasons, 'errors': errors,
            'calendar_complete': calendar_complete, 'latest_task': latest,
            'minimum_symbols': minimum, 'training_ready': False,
            'training_message': f'当前组合有{len(symbols)}只ETF，现有模型需要至少{minimum}只ETF；还需检查完整日期和训练特征。',
            'backtest_ready': False}
