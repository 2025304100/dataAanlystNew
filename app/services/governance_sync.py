"""Candidate unified sync: bounded explicit work, fenced leases and live coverage.

No daemon/default DB, source auto-admission or production routing. Adapters only
prepare requests; FactWriter/R4 remain the only fact/acceptance executors. Task
completion records durable execution, never an everlasting coverage certificate.
"""
import base64
from contextvars import ContextVar
from copy import deepcopy
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
import json
from uuid import uuid4
from sqlalchemy import select, insert, update

from app.db import governance_schema as facts, governance_sync_schema as sync, governance_source_schema as source_schema
from app.db.governance_migration import migrate_candidate, migration_status
from app.services.data_contract_profiles import get_profile, assess_profile, GLOBAL_GATES
from app.services.governance_fact_contracts import WriterError, canonical, digest, hash_object, logical_identity, instant, day

REVISION = 'DSG-SYNC-001'
TERMINAL = {'done', 'partial', 'empty', 'failed', 'cancelled'}
ADAPTERS = {'daily_bars', 'fundamentals', 'activity', 'etf', 'index', 'macro', 'announcement', 'prepared'}
_active_attempt = ContextVar('governance_sync_attempt', default=None)


def install_sync_schema(engine): return migrate_candidate(engine, metadata=sync.metadata, revision=REVISION)


def _pack(value):
    if type(value) is bytes: return {'$bytes': base64.b64encode(value).decode()}
    if type(value) is dict:
        if '$bytes' in value: raise WriterError('SYNC_RESERVED_FIELD')
        return {k: _pack(v) for k, v in value.items()}
    if type(value) in {list, tuple}: return [_pack(v) for v in value]
    return value


def _unpack(value):
    if type(value) is dict:
        if set(value) == {'$bytes'}: return base64.b64decode(value['$bytes'], validate=True)
        return {k: _unpack(v) for k, v in value.items()}
    if type(value) is list: return [_unpack(v) for v in value]
    return value


def prepare_requests(writer, adapter, capture, options):
    """Reuse M06/M07 validators; no second normalizer or implicit fallback."""
    if adapter == 'daily_bars':
        from app.services.governance_daily_sync import DailyBarSyncService
        return DailyBarSyncService(writer).requests(capture, **options)
    if adapter == 'fundamentals':
        from app.services.governance_fundamentals import FundamentalSyncService
        return FundamentalSyncService(writer).requests(capture, **options)
    if adapter == 'activity':
        from app.services.governance_activity_sync import ActivitySyncService
        return ActivitySyncService(writer).requests(capture, **options)
    if adapter == 'etf':
        from app.services.governance_etf import ETFService
        return ETFService(writer).source_requests(capture, **options)
    if adapter == 'index':
        from app.services.governance_index import IndexService
        return IndexService(writer).quote_requests(capture, **options)
    if adapter == 'macro':
        from app.services.governance_macro import MacroSyncService
        return MacroSyncService(writer).requests(capture, **options)
    if adapter == 'announcement':
        from app.services.governance_releases import AnnouncementSyncService
        return [AnnouncementSyncService(writer).request(capture, **options)]
    if adapter == 'prepared' and type(capture) is list and not options:
        # Corporate actions/share-capital and explicitly derived ETF inputs use
        # their already reviewed request builders, then the SAME writer gates.
        return deepcopy(capture)
    raise WriterError('SYNC_ADAPTER_INVALID')


class SyncCancelled(WriterError): pass


class GovernanceSyncService:
    def __init__(self, writer, *, source_service=None):
        if migration_status(writer.engine, metadata=sync.metadata, revision=REVISION)['state'] != 'applied':
            raise WriterError('SYNC_SCHEMA_NOT_READY')
        if source_service is not None and source_service.writer is not writer: raise WriterError('SYNC_WRITER_MISMATCH')
        self.writer = writer; self.engine = writer.engine; self.source = source_service

    def create(self, spec, *, idempotency_key):
        spec = deepcopy(spec)
        optional = {'failures', 'recovery_calendar_version'}
        wanted = {'dataset', 'profile', 'mode', 'adapter', 'options', 'expected_keys', 'expected_known',
                  'contract_versions', 'instrument_version', 'calendar_version', 'policy_version', 'allow_degraded'}
        if type(spec) is not dict or set(spec) - optional != wanted or any(type(spec[n]) is not str for n in ('dataset','profile','adapter','mode')) or spec['adapter'] not in ADAPTERS or spec['mode'] not in {'incremental', 'backfill', 'force'}:
            raise WriterError('SYNC_SPEC_INVALID')
        spec.setdefault('failures', {}); spec.setdefault('recovery_calendar_version', None)
        if type(spec['failures']) is not dict or any(type(v) is not list or any(type(t) is not str or len(t) != 64 for t in v) for v in spec['failures'].values()):
            raise WriterError('FAILURE_EVIDENCE_REQUIRED')
        if type(idempotency_key) is not str or not 0 < len(idempotency_key) <= 128: raise WriterError('SYNC_IDEMPOTENCY_INVALID')
        if type(spec['expected_known']) is not bool or type(spec['allow_degraded']) is not bool or type(spec['options']) is not dict:
            raise WriterError('SYNC_SPEC_INVALID')
        profile = get_profile(spec['profile'])
        if not profile.profile_id.startswith(spec['dataset'] + '/'):
            raise WriterError('SYNC_PROFILE_INVALID')
        if type(spec['expected_keys']) is not list or len(spec['expected_keys']) > 10000 or type(spec['contract_versions']) is not list or not spec['contract_versions']:
            raise WriterError('SYNC_SCOPE_INVALID')
        if len(set(spec['contract_versions'])) != len(spec['contract_versions']): raise WriterError('SYNC_SCOPE_INVALID')
        for version in spec['contract_versions']:
            c = self.writer.contracts.get(version)
            if c is None or c.document['adapter']['dataset'] != spec['dataset']:
                raise WriterError('UNIT_CONTRACT_UNREGISTERED')
        definitions = {self.writer.contracts[v].document['definition_id'] for v in spec['contract_versions']}
        if len(definitions) != 1 or any('definition_id' in key and key['definition_id'] not in definitions for key in spec['expected_keys']):
            raise WriterError('SYNC_DEFINITION_MISMATCH')
        keys = [logical_identity(spec['dataset'], k)[0] for k in spec['expected_keys']]
        if len(keys) != len(set(keys)) or not spec['expected_known'] and keys: raise WriterError('SYNC_SCOPE_INVALID')
        self._validate_scope(spec)
        if spec['policy_version'] is not None and self.source is None: raise WriterError('SYNC_SOURCE_EXECUTOR_REQUIRED')
        document = canonical(_pack(spec)); spec_hash = digest(document.encode())
        job_id = hash_object('governance-sync-job-v1', {'idempotency_key': idempotency_key})
        now = instant(self.writer.clock())
        with self.engine.begin() as db:
            values = dict(job_id=job_id, spec_json=document, spec_hash=spec_hash, status='queued', attempts=0,
                          cancel_requested=0, lease_token=None, lease_until=None, result_json=None, created_at=now, updated_at=now)
            self._insert_once(db, values)
            row = db.execute(select(sync.jobs).where(sync.jobs.c.job_id == job_id).with_for_update()).mappings().one()
            if row['spec_hash'] != spec_hash or row['spec_json'] != document: raise WriterError('SYNC_IDEMPOTENCY_CONFLICT')
        return job_id

    def _validate_scope(self, spec):
        from app.services.governance_master_data import MasterDataService
        master = MasterDataService(self.engine); dataset = spec['dataset']
        contract = self.writer.contracts[spec['contract_versions'][0]].document
        if spec['recovery_calendar_version'] is not None: master.calendar_snapshot(spec['recovery_calendar_version'])
        if dataset in {'macro', 'announcement'} and (spec['instrument_version'] is not None or spec['calendar_version'] is not None):
            raise WriterError('SYNC_NAMESPACE_INVALID')
        for key in spec['expected_keys']:
            if dataset == 'macro':
                from app.services.governance_macro import _period
                if key['frequency'] != contract['definition']['frequency']: raise WriterError('SYNC_SCOPE_INVALID')
                _period(key['period'], key['frequency'])
            if 'instrument_id' not in key: continue
            on = next((day(key[n]) for n in ['trade_date','report_period','valuation_date','effective_date'] if n in key), None)
            if on is None:
                if 'observation_at' in key:
                    on = instant(key['observation_at']).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(contract['publication_timezone'])).date()
                else:
                    snapshot = master.instrument_snapshot(spec['instrument_version'])
                    if not any(r['instrument_id']==key['instrument_id'] for r in snapshot['members']): raise WriterError('INSTRUMENT_UNMAPPED')
                    continue  # action identity has no event date; writer validates the prepared source time
            identity = master.validate_lifecycle(spec['instrument_version'], key['instrument_id'], on)
            if identity['asset_type'] != contract['adapter']['asset_type']: raise WriterError('ASSET_TYPE_MISMATCH')
            trading = dataset in {'daily_bars','valuation','capital_flow','lhb','hot_rank'} or dataset == 'etf' and spec['profile'] in {'etf/iopv','etf/tracking'}
            if trading and on not in master.trading_days(spec['calendar_version'], identity['exchange'], on, on + timedelta(days=1)):
                raise WriterError('NOT_TRADING_DAY')

    @staticmethod
    def _insert_once(db, values):
        if db.dialect.name == 'mysql':
            from sqlalchemy.dialects.mysql import insert as native
            query = native(sync.jobs).values(**values).on_duplicate_key_update(job_id=sync.jobs.c.job_id)
        else:
            from sqlalchemy.dialects.sqlite import insert as native
            query = native(sync.jobs).values(**values).on_conflict_do_nothing(index_elements=['job_id'])
        db.execute(query)

    def _row(self, db, job_id, *, lock=True):
        if lock and db.dialect.name == 'sqlite':
            # SQLite has no SELECT FOR UPDATE. Obtain its writer reservation
            # before reading a mutable control row, including claim/cancel CAS.
            db.execute(update(sync.jobs).where(sync.jobs.c.job_id == job_id).values(job_id=sync.jobs.c.job_id))
        query = select(sync.jobs).where(sync.jobs.c.job_id == job_id)
        row = db.execute(query.with_for_update() if lock else query).mappings().one()
        if digest(row['spec_json'].encode()) != row['spec_hash']: raise WriterError('SYNC_SPEC_HASH_MISMATCH')
        return row

    def inspect(self, job_id):
        with self.engine.begin() as db:
            if db.dialect.name == 'sqlite': db.exec_driver_sql('BEGIN')
            row = dict(self._row(db, job_id, lock=False))
            history = [dict(r) for r in db.execute(select(sync.attempts).where(sync.attempts.c.job_id == job_id).order_by(sync.attempts.c.attempt_no)).mappings()]
        row['spec'] = _unpack(json.loads(row.pop('spec_json')))
        result = row.pop('result_json'); row['result'] = json.loads(result) if result else None
        for item in history:
            result = item.pop('result_json'); item['result'] = json.loads(result) if result else None
        row['attempt_history'] = history
        return row

    def cancel(self, job_id):
        with self.engine.begin() as db:
            row = self._row(db, job_id)
            if row['status'] in TERMINAL: return row['status']
            status = 'cancelled' if row['status'] == 'queued' else 'running'
            db.execute(update(sync.jobs).where(sync.jobs.c.job_id == job_id).values(cancel_requested=1, status=status, updated_at=instant(self.writer.clock())))
        return status

    def retry(self, job_id):
        with self.engine.begin() as db:
            row = self._row(db, job_id)
            if row['status'] not in {'failed', 'partial', 'empty'}: raise WriterError('SYNC_RETRY_STATE_INVALID')
            db.execute(update(sync.jobs).where(sync.jobs.c.job_id == job_id).values(status='queued', cancel_requested=0,
                lease_token=None, lease_until=None, updated_at=instant(self.writer.clock())))

    def claim(self, job_id):
        now = instant(self.writer.clock())
        with self.engine.begin() as db:
            row = self._row(db, job_id)
            if row['status'] in TERMINAL: return None
            if row['cancel_requested']:
                if row['status'] == 'running':
                    self._finish(db, row, 'cancelled', {'reason': 'SYNC_CANCELLED', 'coverage_certificate': False})
                else:
                    db.execute(update(sync.jobs).where(sync.jobs.c.job_id == job_id).values(status='cancelled', updated_at=now))
                return None
            if row['status'] == 'running' and row['lease_until'] > now: raise WriterError('SYNC_LEASE_HELD')
            if row['status'] == 'running':
                db.execute(update(sync.attempts).where(sync.attempts.c.job_id == job_id, sync.attempts.c.attempt_no == row['attempts']).values(
                    status='expired', finished_at=now, result_json=canonical({'reason': 'SYNC_LEASE_EXPIRED'})))
            token = uuid4().hex + uuid4().hex
            db.execute(update(sync.jobs).where(sync.jobs.c.job_id == job_id).values(status='running', attempts=row['attempts'] + 1,
                lease_token=token, lease_until=now + timedelta(seconds=120), updated_at=now))
            db.execute(insert(sync.attempts).values(job_id=job_id, attempt_no=row['attempts']+1, lease_token=token,
                status='running', started_at=now, lease_until=now+timedelta(seconds=120), finished_at=None, result_json=None))
        return token

    def _fence(self, row, token):
        if row['status'] != 'running' or row['lease_token'] != token or row['lease_until'] <= instant(self.writer.clock()):
            raise WriterError('SYNC_LEASE_STALE')
        if row['cancel_requested']: raise SyncCancelled('SYNC_CANCELLED')

    def renew(self, job_id, token):
        with self.engine.begin() as db:
            row = self._row(db, job_id); self._fence(row, token)
            db.execute(update(sync.jobs).where(sync.jobs.c.job_id == job_id).values(
                lease_until=instant(self.writer.clock()) + timedelta(seconds=120), updated_at=instant(self.writer.clock())))
            db.execute(update(sync.attempts).where(sync.attempts.c.job_id == job_id, sync.attempts.c.attempt_no == row['attempts']).values(
                lease_until=instant(self.writer.clock()) + timedelta(seconds=120)))

    def record_failure(self, job_id, key, *, interface_id, reason, evidence):
        if self.source is None: raise WriterError('SYNC_SOURCE_EXECUTOR_REQUIRED')
        active = _active_attempt.get()
        if active is None or active[0] != job_id: raise WriterError('SYNC_LEASE_CONTEXT_REQUIRED')
        with self.engine.begin() as db:
            row = self._row(db, job_id); self._fence(row, active[1])
            spec = _unpack(json.loads(row['spec_json'])); logical, _ = logical_identity(spec['dataset'], key)
            if spec['policy_version'] is None or spec['expected_known'] and key not in spec['expected_keys']:
                raise WriterError('SYNC_REQUEST_SCOPE_MISMATCH')
            current = self.writer.current(spec['dataset'], logical, connection=db)
            return self.source.record_failure(spec['dataset'], key, policy_version=spec['policy_version'],
                expected_revision=current['pointer_revision'] if current else 0, interface_id=interface_id,
                reason=reason, evidence=evidence, batch_id='sync:' + job_id + ':' + str(row['attempts']), connection=db)

    def _finish(self, db, row, status, document):
        now = instant(self.writer.clock()); encoded = canonical(document)
        changed = db.execute(update(sync.attempts).where(sync.attempts.c.job_id == row['job_id'], sync.attempts.c.attempt_no == row['attempts'],
            sync.attempts.c.status == 'running', sync.attempts.c.lease_token == row['lease_token']).values(status=status, finished_at=now, result_json=encoded)).rowcount
        if changed != 1: raise WriterError('SYNC_ATTEMPT_INCONSISTENT')
        db.execute(update(sync.jobs).where(sync.jobs.c.job_id == row['job_id']).values(status=status, result_json=encoded,
            lease_token=None, lease_until=None, updated_at=now))

    def run(self, job_id, acquire, *, should_cancel=None):
        token = self.claim(job_id)
        if token is None: return {**self.inspect(job_id), 'coverage': self.coverage(job_id)}
        def checkpoint():
            if should_cancel is not None and should_cancel(): raise SyncCancelled('SYNC_CANCELLED')
            with self.engine.connect() as db: self._fence(self._row(db, job_id), token)
        try:
            from app.services.data_source_execution import data_task
            from app.services.data_source_control import current_controller, PolicyDenied
            checkpoint(); spec = self.inspect(job_id)['spec']
            batch_id = 'sync:' + job_id + ':' + str(self.inspect(job_id)['attempts'])
            plan = self.coverage(job_id)
            # Force visits every explicit point. Other modes visit all gaps,
            # including internal ones; the callback receives immutable copies.
            work = spec['expected_keys'] if spec['mode'] == 'force' else [r['key'] for r in plan['points'] if not r['covered']]
            if spec['expected_known'] and spec['expected_keys'] and not work:
                with self.engine.begin() as db:
                    row = self._row(db, job_id); self._fence(row, token)
                    self._finish(db, row, 'done', {'results': [], 'reason': 'already_covered', 'coverage_certificate': False})
                return {**self.inspect(job_id), 'coverage': self.coverage(job_id)}
            try: active = current_controller()
            except PolicyDenied as error:
                if str(error) != 'TASK_CONTEXT_REQUIRED': raise
                active = None  # data_task below creates the explicitly bound task
            with data_task(task_id=active.task_identity() if active is not None else 'sync:' + job_id, lane='backfill' if spec['mode'] == 'backfill' else 'incremental',
                           cancelled=lambda: bool(self.inspect(job_id)['cancel_requested']) or bool(should_cancel and should_cancel())):
                marker = _active_attempt.set((job_id, token))
                try: capture = acquire(deepcopy(work))
                finally: _active_attempt.reset(marker)
                checkpoint()
                if spec['adapter'] != 'prepared' and (type(capture) is not bytes or not 0 < len(capture) <= 32 * 1024 * 1024):
                    raise WriterError('RAW_EVIDENCE_REQUIRED')
                options = deepcopy(spec['options'])
                if spec['adapter'] != 'prepared': options['batch_id'] = batch_id
                requests = prepare_requests(self.writer, spec['adapter'], capture, options)
                checkpoint()
            expected = {logical_identity(spec['dataset'], k)[0] for k in spec['expected_keys']}; seen = set()
            for request in requests:
                preview = self.writer.preview(request)
                if request['dataset'] != spec['dataset'] or request['profile'] != spec['profile'] or request['contract_version'] not in spec['contract_versions'] or request['instrument_version'] != spec['instrument_version'] or request['calendar_version'] != spec['calendar_version']:
                    raise WriterError('SYNC_REQUEST_SCOPE_MISMATCH')
                if preview['logical_key'] in seen or spec['expected_known'] and preview['logical_key'] not in expected:
                    raise WriterError('SYNC_REQUEST_SCOPE_MISMATCH')
                seen.add(preview['logical_key'])
                request['batch_id'] = batch_id
            with self.engine.begin() as db:
                row = self._row(db, job_id); self._fence(row, token); results = []
                if type(capture) is bytes:
                    self.writer._evidence(db, capture, 'sync://capture/' + job_id, instant(self.writer.clock()))
                for request in sorted(requests, key=lambda r: logical_identity(r['dataset'], r['key'])[0]):
                    if should_cancel is not None and should_cancel(): raise SyncCancelled('SYNC_CANCELLED')
                    _, quality = self.writer._prepare(request)
                    if spec['policy_version'] is not None and (quality.quality_status == 'complete' or request['revision']['kind'] == 'withdrawal'):
                        logical = logical_identity(spec['dataset'], request['key'])[0]
                        observed = db.execute(select(source_schema.transitions).where(source_schema.transitions.c.dataset == spec['dataset'],
                            source_schema.transitions.c.logical_key == logical, source_schema.transitions.c.policy_version == spec['policy_version'])).mappings().all()
                        failures = set(spec['failures'].get(logical, [])) | {r['transition_id'] for r in observed if r['result_json'].get('response', {}).get('batch_id') == batch_id}
                        current = db.execute(select(facts.fact_current).where(facts.fact_current.c.dataset == spec['dataset'],
                            facts.fact_current.c.logical_key == logical_identity(spec['dataset'], request['key'])[0]).with_for_update()).mappings().first()
                        result = self.source.submit(request, policy_version=spec['policy_version'],
                            expected_revision=current['pointer_revision'] if current else 0, connection=db,
                            failures=sorted(failures),
                            recovery_calendar_version=spec['recovery_calendar_version'])
                    else: result = self.writer.write(request, connection=db)
                    results.append(result)
                if should_cancel is not None and should_cancel(): raise SyncCancelled('SYNC_CANCELLED')
                self._fence(self._row(db, job_id), token)
                missing_work = {logical_identity(spec['dataset'], k)[0] for k in work} - seen
                status = 'empty' if not results else 'done' if all(r['accepted'] for r in results) and not missing_work else 'partial'
                document = {'results': results, 'received': len(requests), 'capture_hash': digest(capture) if type(capture) is bytes else None,
                            'coverage_certificate': False}
                self._finish(db, row, status, document)
        except Exception as error:
            from app.services.data_source_control import PolicyDenied
            cancelled = isinstance(error, SyncCancelled) or isinstance(error, PolicyDenied) and str(error) in {'CANCELLED', 'TASK_CANCELLED'}
            with self.engine.begin() as db:
                row = self._row(db, job_id)
                # A late/expired worker never overwrites its successor's result.
                if row['status'] == 'running' and row['lease_token'] == token:
                    self._finish(db, row, 'cancelled' if cancelled or row['cancel_requested'] else 'failed',
                        {'error_type': type(error).__name__, 'reason': str(error)[:512], 'coverage_certificate': False})
            raise
        return {**self.inspect(job_id), 'coverage': self.coverage(job_id)}

    def coverage(self, job_id):
        spec = self.inspect(job_id)['spec']; points = []
        with self.engine.begin() as db:
            if db.dialect.name == 'sqlite': db.exec_driver_sql('BEGIN')
            for key in spec['expected_keys']:
                logical, _ = logical_identity(spec['dataset'], key)
                current = db.execute(select(facts.fact_current).where(facts.fact_current.c.dataset == spec['dataset'], facts.fact_current.c.logical_key == logical)).mappings().first()
                point = {'key': key, 'data_version': current['data_version'] if current else None, 'covered': False, 'reason': 'missing', 'degraded': False}
                if current:
                    fact = self.writer.fact(current['data_version'], connection=db)
                    if fact['revision_kind'] == 'withdrawal': point['reason'] = 'withdrawn'
                    elif fact['definition_id'] != self.writer.contracts[spec['contract_versions'][0]].document['definition_id']: point['reason'] = 'definition_mismatch'
                    elif fact['available_at'] is None or fact['available_at'] > instant(self.writer.clock()): point['reason'] = 'not_available'
                    elif current['source_state'] not in {'canonical', 'backup_temp'}: point['reason'] = 'source_unusable'
                    else:
                        history = db.execute(select(facts.acceptance_history).where(facts.acceptance_history.c.dataset == spec['dataset'],
                            facts.acceptance_history.c.logical_key == logical, facts.acceptance_history.c.data_version == current['data_version'],
                            facts.acceptance_history.c.system_to.is_(None))).mappings().all()
                        if len(history) != 1: raise WriterError('SYNC_ACCEPTANCE_INCONSISTENT')
                        evidence_hash = db.execute(select(facts.source_decisions.c.evidence_hash).where(
                            facts.source_decisions.c.decision_id == history[0]['decision_id'])).scalar_one()
                        validated = json.loads(self.writer.evidence(evidence_hash, connection=db)['raw_bytes'])
                        quality = assess_profile(fact['payload'], spec['profile'], gate_results=validated['gates'])
                        dependencies = self._dependencies(fact, db)
                        point['degraded'] = current['source_state'] == 'backup_temp'
                        point['reason'] = 'partial' if quality.quality_status != 'complete' else 'dependency_blocked' if not dependencies else 'degraded' if point['degraded'] else 'complete'
                        point['covered'] = quality.quality_status == 'complete' and dependencies and (not point['degraded'] or spec['allow_degraded'])
                points.append(point)
        total = len(points) if spec['expected_known'] else None; covered = sum(p['covered'] for p in points)
        return {'points': points, 'expected_points': total, 'covered_points': covered,
                'denominator_status': 'unknown' if total is None else 'zero' if total == 0 else 'known',
                'coverage_ratio': format(Decimal(covered) / Decimal(total), '.8f') if total else None,
                'coverage_complete': bool(total and covered == total), 'basis': 'current_accepted_versions',
                'production_admission': 'not_certified'}

    def _dependencies(self, fact, db):
        versions = [fact['payload'].get(n) for n in ('announcement_data_version', 'nav_data_version', 'shares_data_version') if fact['payload'].get(n)]
        for version in versions:
            dependency = self.writer.fact(version, connection=db); current = self.writer.current(dependency['dataset'], dependency['logical_key'], connection=db)
            if not current or current['data_version'] != version or dependency['revision_kind'] == 'withdrawal' or dependency['available_at'] is None:
                return False
        if fact['dataset'] == 'etf' and fact['key']['profile'] == 'etf/tracking':
            from app.services.governance_etf import _tracking
            proof = json.loads(self.writer.evidence(fact['raw_hash'])['raw_bytes'])
            # Calendar pin is recorded in the immutable selected series.
            from app.services.governance_etf import _series
            series, _ = _series(self.writer, proof['nav_return_version'])
            _, status, _ = _tracking(self.writer, proof, fact['key'], series['calendar_version'])
            return status == 'pass'
        return True
