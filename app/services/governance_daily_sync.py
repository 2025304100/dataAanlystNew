"""Candidate daily ingestion from captured, unnormalised SDK records.

The capture envelope is an archive format, not a supplier HTTP format or an
ADMIT-01 certificate. No default engine, production route or source promotion.
TDX is blocked until its quantity contract is verified; its integer layout
alone cannot establish shares. Adjusted upstream prices never enter raw facts.
"""
from copy import deepcopy
from datetime import timedelta
import json

from app.services.governance_fact_contracts import WriterError, canonical, day, digest, logical_identity
from app.services.governance_master_data import MasterDataService


def capture_sdk_history(contract, *, symbol, start, end_exclusive, fetcher):
    """Acquire native SDK columns under interface-manager request controls.

    `fetcher` is the raw SDK function, before any DataFrame unit normalisation.
    The archived bytes explicitly describe SDK records, not original HTTP bytes.
    Installed SDK version/field evidence must be pinned in the writer contract.
    """
    from app.services.data_source_execution import source_boundary
    doc=contract.document
    if doc['kind']!='daily_adapter' or doc['sdk'] is None:
        raise WriterError('SDK_EVIDENCE_REQUIRED')
    adapter=doc['adapter'];endpoint=adapter['endpoint']
    start,end=day(start),day(end_exclusive)
    if end<=start:
        raise WriterError('INTERVAL_INVALID')
    if endpoint not in {'stock_zh_a_hist','fund_etf_hist_em','stock_zh_a_daily','stock_zh_a_hist_tx'}:
        raise WriterError('DAILY_ADAPTER_REQUIRED')
    if not isinstance(symbol,str) or len(symbol)!=8 or symbol[:2] not in {'sh','sz','bj'} or not symbol[2:].isdigit():
        raise WriterError('CAPTURE_SYMBOL_MISMATCH')
    kwargs={'symbol':symbol[2:] if adapter['provider']=='eastmoney' else symbol,
            'start_date':start.strftime('%Y%m%d'),'end_date':(end-timedelta(days=1)).strftime('%Y%m%d'),'adjust':''}
    if adapter['provider']=='eastmoney':kwargs['period']='daily'
    frame=source_boundary(endpoint)(fetcher)(**kwargs)
    if adapter['provider'] in {'tencent','sina'}:
        receipt=frame.attrs.get('history_response',{})
        if receipt.get('actual_adjustment') != '' or receipt.get('symbol') != symbol:
            raise WriterError('RAW_PRICE_EVIDENCE_REQUIRED')
        if adapter['provider']=='tencent' and receipt.get('returned_mode')!='day':
            raise WriterError('RAW_PRICE_BASIS_CONFLICT')
    if adapter['provider'] in {'tencent','sina'}:
        receipt=frame.attrs.get('history_response',{})
        if receipt.get('actual_adjustment') != '' or receipt.get('symbol') != symbol:
            raise WriterError('RAW_PRICE_EVIDENCE_REQUIRED')
        if adapter['provider']=='tencent' and receipt.get('returned_mode')!='day':
            raise WriterError('RAW_PRICE_BASIS_CONFLICT')
    if frame.attrs.get('unit_normalization') is not None:
        raise WriterError('NORMALIZED_INPUT_FORBIDDEN')
    if not frame.columns.is_unique:
        raise WriterError('CAPTURE_ROW_INVALID')
    date_field='日期' if adapter['provider']=='eastmoney' else 'date'
    if not frame.empty and date_field not in frame.columns:
        raise WriterError('CAPTURE_ROW_DATE_MISMATCH')
    import pandas as pd
    rows=[]
    for record in frame.to_dict(orient='records'):
        on=pd.Timestamp(record[date_field]).date().isoformat()
        wire={name:(None if pd.isna(value) else str(value)) for name,value in record.items()}
        wire[date_field]=on
        rows.append({'trade_date':on,'wire_record':wire})
    return canonical({'format':'sdk-wire-records-v1','provider':adapter['provider'],'endpoint':endpoint,
                      'symbol':symbol,'request_basis':'raw','response_basis':'raw','rows':rows}).encode('utf-8')


class DailyBarSyncService:
    def __init__(self, writer):
        self.writer = writer
        self.master = MasterDataService(writer.engine)

    def plan(self, *, instrument_id, instrument_version, calendar_version, start, end_exclusive):
        """Find all internal gaps as well as the tail; never use max(date)."""
        start, end = day(start), day(end_exclusive)
        snapshot = self.master.instrument_snapshot(instrument_version)
        member = next((r for r in snapshot['members'] if r['instrument_id'] == instrument_id), None)
        identity = next((r for r in snapshot['identities'] if r['instrument_id'] == instrument_id), None)
        if member is None or identity is None:
            raise WriterError('INSTRUMENT_UNMAPPED')
        days = self.master.trading_days(calendar_version, identity['exchange'], start, end)
        rows = []
        for on in days:
            status = 'outside_lifecycle'
            current = None
            if on >= day(member['list_date']) and (member['last_trade_date'] is None or on <= day(member['last_trade_date'])):
                logical, _ = logical_identity('daily_bars', {'instrument_id':instrument_id, 'trade_date':on})
                current = self.writer.current('daily_bars', logical)
                status = 'covered' if current and current['quality_status'] == 'complete' and current['source_state'] in {'canonical','backup_temp'} else 'missing'
            rows.append({'trade_date':on.isoformat(), 'status':status,
                         'data_version':current['data_version'] if current else None})
        return {'rows':rows, 'request_dates':[r['trade_date'] for r in rows if r['status']=='missing'],
                'coverage_complete':all(r['status']!='missing' for r in rows),
                'instrument_version':instrument_version, 'calendar_version':calendar_version}

    def _prepare_capture(self, capture, *, contract_version, instrument_id, instrument_version,
               calendar_version, start, end_exclusive, source_uri, batch_id,
               revisions=None, suspension=None):
        """Validate a whole capture before atomic stage-only FactWriter writes.

        Corrections/withdrawals require explicit writer revision metadata. Missing
        rows remain gaps, including suspension days without official evidence.
        Caller invokes the existing reviewed source-state service separately.
        """
        contract = self.writer.contracts.get(contract_version)
        if contract is None:
            raise WriterError('UNIT_CONTRACT_UNREGISTERED')
        adapter = contract.document['adapter']
        if adapter['endpoint'] == 'TDX:day' or adapter['provider'].lower() == 'tdx':
            raise WriterError('TDX_QUANTITY_CONTRACT_UNVERIFIED')
        if contract.document['kind'] != 'daily_adapter':
            raise WriterError('DAILY_ADAPTER_REQUIRED')
        if type(capture) is not bytes or not 0 < len(capture) <= 32*1024*1024:
            raise WriterError('CAPTURE_INVALID')
        try:
            doc = json.loads(capture)
            if canonical(doc).encode('utf-8') != capture or set(doc) != {
                'format','provider','endpoint','symbol','request_basis','response_basis','rows'}:
                raise ValueError()
            if doc['format'] != 'sdk-wire-records-v1' or type(doc['rows']) is not list:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise WriterError('CAPTURE_INVALID') from None
        if (doc['provider'],doc['endpoint']) != (adapter['provider'],adapter['endpoint']):
            raise WriterError('CAPTURE_SOURCE_MISMATCH')
        if doc['request_basis'] != 'raw' or doc['response_basis'] != 'raw':
            raise WriterError('RAW_PRICE_BASIS_CONFLICT')
        start, end = day(start), day(end_exclusive)
        plan = self.plan(instrument_id=instrument_id,instrument_version=instrument_version,
                         calendar_version=calendar_version,start=start,end_exclusive=end)
        expected = {r['trade_date']:r['status'] for r in plan['rows']}
        revisions, suspension = deepcopy(revisions or {}), deepcopy(suspension or {})
        seen, selected, filtered = set(), [], []
        for row in doc['rows']:
            if type(row) is not dict or set(row) != {'trade_date','wire_record'}:
                raise WriterError('CAPTURE_ROW_INVALID')
            on = day(row['trade_date'])
            if on in seen:
                raise WriterError('DUPLICATE_TRADE_DATE')
            seen.add(on)
            # Resolve even out-of-range rows, so a mixed-symbol archive fails closed.
            member = self.master.validate_lifecycle(instrument_version,instrument_id,on)
            codes = self.master.instrument_snapshot(instrument_version)['codes']
            code = [r['code'] for r in codes if r['instrument_id']==instrument_id and
                    day(r['valid_from'])<=on and (r['valid_to'] is None or on<day(r['valid_to']))]
            prefix = {'SSE':'sh','SZSE':'sz','BSE':'bj'}[member['exchange']]
            if len(code)!=1 or doc['symbol'] != prefix+code[0]:
                raise WriterError('CAPTURE_SYMBOL_MISMATCH')
            if member['asset_type'] != adapter['asset_type']:
                raise WriterError('ASSET_TYPE_MISMATCH')
            if member['asset_type']=='etf' and adapter['instrument_family'] != prefix+'_etf':
                raise WriterError('INSTRUMENT_FAMILY_MISMATCH')
            if not start <= on < end:
                filtered.append(on.isoformat())
                continue
            if on.isoformat() not in expected:
                raise WriterError('NOT_TRADING_DAY')
            wire = row['wire_record']
            date_field = '日期' if adapter['provider']=='eastmoney' else 'date'
            if type(wire) is not dict or date_field in wire and wire[date_field] != on.isoformat():
                raise WriterError('CAPTURE_ROW_DATE_MISMATCH')
            proof = suspension.get(on.isoformat())
            request = dict(dataset='daily_bars',key={'instrument_id':instrument_id,'trade_date':on.isoformat()},
                contract_version=contract_version,profile='daily_bars/core',instrument_version=instrument_version,
                calendar_version=calendar_version,wire_record=wire,raw_response=capture,source_uri=source_uri,
                time={'event_date':on.isoformat(),'publication_precision':'unknown','published_at':None,
                      'publication_date':None,'valid_from':on.isoformat(),'valid_to':(on+timedelta(days=1)).isoformat(),
                      'publication_evidence':None},
                revision=revisions.get(on.isoformat(),{'kind':'original','token':None,'order':None,
                                                       'order_evidence':None,'supersedes_version':None}),
                flags={'is_suspended':proof is not None,'synthetic_bar':proof is not None},gate_results={},field_sources={},batch_id=batch_id,
                price_evidence=canonical({'request_basis':'raw','response_basis':'raw','raw_hash':digest(capture),
                                          'contract_version':contract_version}).encode('utf-8'))
            if proof is not None:
                request['suspension_evidence'] = proof
            self.writer.preview(request)
            selected.append(request)
        selected_dates={r['key']['trade_date'] for r in selected}
        if set(revisions)-selected_dates or set(suspension)-selected_dates:
            raise WriterError('UNBOUND_ROW_METADATA')
        return {'requests':selected,'plan':plan,'filtered_dates':filtered,'adapter':adapter,'capture_hash':digest(capture)}

    def requests(self, capture, **kwargs):
        return self._prepare_capture(capture, **kwargs)['requests']

    def ingest(self, capture, **kwargs):
        prepared=self._prepare_capture(capture, **kwargs)
        selected,plan,filtered,adapter=(prepared[n] for n in ('requests','plan','filtered_dates','adapter'))
        selected_dates={r['key']['trade_date'] for r in selected}
        # A single failed write rolls back the capture, including receipt/outbox.
        with self.writer.engine.begin() as connection:
            results=[self.writer.write(r,connection=connection) for r in sorted(selected,key=lambda r:r['key']['trade_date'])]
        return {'status':'staged' if results else 'empty','results':results,'filtered_dates':filtered,
                'missing_dates':[r['trade_date'] for r in plan['rows'] if r['status']=='missing' and r['trade_date'] not in selected_dates],
                'coverage_complete':plan['coverage_complete'],
                'source_tag':adapter['provider']+':'+adapter['endpoint'],'capture_hash':digest(capture),
                'production_admission':'not_certified'}
