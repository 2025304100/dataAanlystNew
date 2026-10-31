"""Candidate M06.4 selection of accepted price/event versions and calendar windows.

This reads the candidate acceptance history. M08 will restrict the same selection
to a published snapshot manifest; this service does not publish a snapshot.
"""
from datetime import timedelta, timezone
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import governance_schema as s
from app.services.governance_fact_contracts import WriterError, canonical, day, digest, instant
from app.services.governance_master_data import MasterDataService
from app.services.governance_price_gates import evaluate_price_gate
from app.services.governance_prices import ALGORITHM, PriceService


class AsOfPriceService:
    def __init__(self, writer):
        self.writer = writer
        self.engine = writer.engine
        self.price = PriceService(writer)

    @staticmethod
    def _latest(facts, mode, cutoff, histories):
        """One visible accepted version per business key; branching stays ambiguous."""
        grouped = {}
        for version, fact in facts.items():
            if mode == 'public_known':
                known = fact['public_available_at']
                if known is None or known > cutoff or fact['publication_evidence_hash'] is None:
                    continue
            else:
                intervals = histories[version]
                if not any(h['system_from'] <= cutoff and (h['system_to'] is None or cutoff < h['system_to']) for h in intervals):
                    continue
                if fact['available_at'] is None or fact['available_at'] > cutoff:
                    continue
            grouped.setdefault((fact['dataset'], fact['logical_key']), []).append(fact)
        chosen = []
        for options in grouped.values():
            if mode == 'system_known':
                if len(options) != 1:raise WriterError('ASOF_ACCEPTANCE_AMBIGUOUS')
                selected = options[0]
            else:
                by_id = {x['data_version']: x for x in options}
                parents = {x['supersedes_version'] for x in options}
                tips = [x for x in options if x['data_version'] not in parents]
                if len(tips) != 1:raise WriterError('ASOF_VERSION_AMBIGUOUS')
                selected = tips[0]
                seen = set()
                cursor = selected
                while cursor is not None:
                    version = cursor['data_version']
                    if version in seen:raise WriterError('ASOF_VERSION_CYCLE')
                    seen = seen | {version}
                    cursor = by_id.get(cursor['supersedes_version'])
                if seen != set(by_id):raise WriterError('ASOF_VERSION_AMBIGUOUS')
            chosen.append(selected)
        return chosen

    def _accepted(self, instrument_id, mode, cutoff):
        with self.engine.connect() as db:
            rows = db.execute(select(s.acceptance_history).where(s.acceptance_history.c.dataset.in_(['daily_bars','corporate_action']))).mappings().all()
        histories = {}
        for row in rows:
            if row['quality_status'] != 'complete':continue
            histories.setdefault(row['data_version'], []).append(row)
        facts = {}
        for version in histories:
            fact = self.writer.fact(version)
            if fact['key'].get('instrument_id') != instrument_id:continue
            if fact['dataset'] == 'daily_bars':self.price.raw(version)
            if fact['publication_evidence_hash']:self.writer.evidence(fact['publication_evidence_hash'])
            facts[version] = fact
        return self._latest(facts, mode, cutoff, histories)

    def _closed_days(self, calendar_version, exchange, as_of, session_closes, schedule_evidence):
        if type(session_closes) is not dict or type(schedule_evidence) is not bytes or not schedule_evidence or canonical(session_closes).encode() != schedule_evidence:
            raise WriterError('SESSION_CLOSE_EVIDENCE_REQUIRED')
        calendar = MasterDataService(self.engine)
        document = calendar.calendar_snapshot(calendar_version)
        local = as_of.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
        if local < day(document['start']) or local >= day(document['end_exclusive']):
            raise WriterError('ASOF_CALENDAR_RANGE_UNKNOWN')
        end = local + timedelta(days=1)
        days = calendar.trading_days(calendar_version, exchange, day(document['start']), end)
        if set(session_closes) != {d.isoformat() for d in days}:
            raise WriterError('SESSION_CLOSE_COVERAGE_INVALID')
        closed = []
        for on in days:
            value = session_closes[on.isoformat()]
            close = instant(value)
            if close.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date() != on:
                raise WriterError('SESSION_CLOSE_DATE_INVALID')
            if close <= as_of:closed.append(on)
        return days, closed

    def _boundary(self, older, newer, version_pair):
        if type(version_pair) is not dict or set(version_pair) != {'older_source_version','newer_source_version'}:
            return 'SOURCE_BOUNDARY_UNVERIFIED'
        try:
            left = self.price.raw(version_pair['older_source_version'])
            right = self.price.raw(version_pair['newer_source_version'])
        except Exception:
            return 'SOURCE_BOUNDARY_UNVERIFIED'
        if left['event_date'] != right['event_date'] or right['event_date'] != newer['event_date'] or (
            left['source_tag'] != older['source_tag'] or right['source_tag'] != newer['source_tag']
        ):
            return 'SOURCE_BOUNDARY_UNVERIFIED'
        inputs = {'primary_observation':left['event_date'].isoformat(),'backup_observation':right['event_date'].isoformat(),
            'primary_definition':left['definition_id'],'backup_definition':right['definition_id'],
            'primary_raw_close':left['payload']['raw_close'],'backup_raw_close':right['payload']['raw_close'],
            'primary_volume_shares':left['payload']['volume_shares'],'backup_volume_shares':right['payload']['volume_shares'],
            'primary_amount_cny':left['payload'].get('amount_cny'),'backup_amount_cny':right['payload'].get('amount_cny')}
        result = evaluate_price_gate('P13',asset_type='stock',inputs=inputs)
        return 'pass' if result['status']=='pass' else 'SOURCE_BOUNDARY_UNVERIFIED'

    def plan(self, *, instrument_id, instrument_version, calendar_version, as_of,
             knowledge_mode, window_size, session_closes, schedule_evidence,
             boundary_comparisons=None):
        if type(instrument_id) is not int or instrument_id <= 0 or knowledge_mode not in {'public_known','system_known'} or type(window_size) is not int or not 1 <= window_size <= 5000:
            raise WriterError('ASOF_PRICE_ARGUMENT_INVALID')
        cutoff = instant(as_of)
        if cutoff > instant(self.writer.clock()):raise WriterError('ASOF_FUTURE')
        master = MasterDataService(self.engine)
        document = master.instrument_snapshot(instrument_version)
        entity = next((r for r in document['identities'] if r['instrument_id']==instrument_id),None)
        if entity is None:raise WriterError('ASOF_INSTRUMENT_UNKNOWN')
        if entity['asset_type'] not in {'stock','etf'}:raise WriterError('ASOF_ASSET_TYPE_INVALID')
        days, closed = self._closed_days(calendar_version, entity['exchange'], cutoff, session_closes, schedule_evidence)
        if not closed:raise WriterError('ASOF_NO_CLOSED_SESSION')
        base = closed[-1]
        master.validate_lifecycle(instrument_version,instrument_id,base)
        window = closed[-window_size:]
        if len(window) != window_size:raise WriterError('ASOF_WINDOW_HISTORY_SHORT')
        selected = self._accepted(instrument_id, knowledge_mode, cutoff)
        raws = {f['event_date']:f for f in selected if f['dataset']=='daily_bars' and f['revision_kind']!='withdrawal'}
        actions = [f for f in selected if f['dataset']=='corporate_action' and f['revision_kind']!='withdrawal' and window[0] < f['event_date'] <= base]
        missing = [d.isoformat() for d in window if d not in raws]
        rows = [{'trade_date':d.isoformat(),'data_version':raws[d]['data_version'] if d in raws else None,
                 'source_tag':raws[d]['source_tag'] if d in raws else None,
                 'status':'present' if d in raws else 'missing'} for d in window]
        boundaries = []
        comparisons = boundary_comparisons or {}
        if type(comparisons) is not dict:raise WriterError('ASOF_PRICE_ARGUMENT_INVALID')
        for previous,on in zip(window,window[1:]):
            if previous in raws and on in raws and raws[previous]['source_tag'] != raws[on]['source_tag']:
                result = self._boundary(raws[previous],raws[on],comparisons.get(on.isoformat()))
                boundaries.append({'trade_date':on.isoformat(),'older_source':raws[previous]['source_tag'],
                    'newer_source':raws[on]['source_tag'],'status':result})
        event_inputs = []
        previous_raws = []
        for action in actions:
            before = [d for d in days if d < action['event_date']]
            previous = before[-1] if before else None
            if previous not in raws:
                missing.append('previous_raw:'+action['event_date'].isoformat())
                continue
            event_inputs.append({'data_version':action['data_version'],'previous_raw_data_version':raws[previous]['data_version']})
            previous_raws.append(raws[previous]['data_version'])
        if len({a['event_date'] for a in actions}) != len(actions):
            raise WriterError('ASOF_EVENT_ORDER_AMBIGUOUS')
        definitions = {raws[d]['definition_id'] for d in window if d in raws}
        status = ('SOURCE_BOUNDARY_UNVERIFIED' if any(b['status'] != 'pass' for b in boundaries)
            else 'MIXED_ADJUSTMENT_BASIS' if len(definitions)>1 else 'WINDOW_GAP' if missing else 'pass')
        raw_ids = sorted({r['data_version'] for r in raws.values() if r['event_date'] in window} | set(previous_raws))
        spec = {'instrument_id':instrument_id,'instrument_version':instrument_version,'calendar_version':calendar_version,
            'base_date':base.isoformat(),'as_of':as_of,'knowledge_mode':knowledge_mode,'algorithm_version':ALGORITHM,
            'raw_data_versions':raw_ids,'event_inputs':event_inputs}
        return {'status':status,'base_date':base.isoformat(),'as_of':as_of,'knowledge_mode':knowledge_mode,
            'selection_scope':'candidate_accepted_history','reconstruction':knowledge_mode=='public_known',
            'schedule_evidence_hash':digest(schedule_evidence),'rows':rows,'missing':missing,'source_breaks':boundaries,
            'event_version_ids':sorted(a['data_version'] for a in actions),'factor_spec':spec}

    def freeze_window(self, **request):
        plan = self.plan(**request)
        if plan['status'] != 'pass':
            return {**plan,'adj_factor_version':None,'gate_P07':{'status':'insufficient','reason':plan['status'],'value':None}}
        spec = plan['factor_spec']
        evidence = canonical({'selection_scope':plan['selection_scope'],'schedule_evidence_hash':plan['schedule_evidence_hash'],
            'event_version_ids':plan['event_version_ids'],'rows':plan['rows'],'source_breaks':plan['source_breaks']}).encode()
        factor = self.price.freeze_factors(spec,evidence=evidence)
        adjusted = []
        for row in plan['rows']:
            version = row['data_version']
            value = self.price.derive(version,factor)
            fact = self.price.raw(version)
            known = fact['available_at'] if spec['knowledge_mode']=='system_known' else fact['public_available_at']
            adjusted.append({**row,'adj_close':value['adj_close'],'adj_factor_version':factor,
                'base_date':plan['base_date'],'definition_id':fact['definition_id'],
                'available_at':known.replace(tzinfo=timezone.utc).isoformat()})
        gate = evaluate_price_gate('P07',asset_type='stock',inputs={'rows':adjusted,
            'expected_dates':[r['trade_date'] for r in plan['rows']],'as_of':plan['as_of'],
            'adj_factor_version':factor,'base_date':plan['base_date'],
            'definition_id':adjusted[0]['definition_id']})
        return {**plan,'rows':adjusted,'adj_factor_version':factor,'gate_P07':gate}
