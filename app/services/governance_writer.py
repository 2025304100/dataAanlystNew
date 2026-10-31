"""Single candidate fact writer. R4 decision evaluation belongs to M05.4.

No application/config/default database binding; no automatic source approval.
All fact, receipt, audit, availability, history, pointer and outbox mutations
are enclosed by one transaction on an explicitly migrated candidate engine.
"""
from datetime import datetime, timezone
from copy import deepcopy
from contextlib import nullcontext
import json
from uuid import uuid4
from datetime import timedelta
from types import MappingProxyType
from decimal import Decimal
from sqlalchemy import select, insert, update, func

from app.db import governance_schema as s
from app.db.governance_migration import migration_status
from app.services.data_contract_profiles import assess_profile,get_profile,ProfileAssessment
from app.services.governance_fact_contracts import WriterContract, WriterError, canonical, digest, hash_object, time_fields, instant, logical_identity


class FactWriter:
    def __init__(self, engine, *, contracts, clock=lambda: datetime.now(timezone.utc)):
        if migration_status(engine)['state'] != 'applied': raise WriterError('SCHEMA_NOT_READY')
        self.engine = engine
        self.contracts = MappingProxyType({c.version:WriterContract(c.document_json, c.evidence,c.sdk_evidence) for c in contracts})
        self.clock = clock

    def _prepare(self, request, *, with_gates=False):
        wanted={'dataset','key','contract_version','profile','instrument_version','calendar_version','wire_record','raw_response','source_uri','time','revision','flags','gate_results','field_sources','batch_id'}
        if type(request) is not dict or set(request)-{'price_evidence','suspension_evidence','etf_evidence','index_evidence'}!=wanted: raise WriterError('REQUEST_FIELDS_INVALID')
        if request['dataset']!='index' and request.get('index_evidence') is not None:raise WriterError('INDEX_EVIDENCE_UNEXPECTED')
        if request['dataset']!='etf' and request.get('etf_evidence') is not None:raise WriterError('ETF_EVIDENCE_UNEXPECTED')
        if type(request['raw_response']) is not bytes or not 0<len(request['raw_response'])<=32*1024*1024: raise WriterError('RAW_EVIDENCE_REQUIRED')
        for name,limit in (('source_uri',4096),('batch_id',128)):
            if not isinstance(request[name],str) or not request[name].strip() or len(request[name])>limit: raise WriterError('REQUEST_FIELDS_INVALID')
        if type(request['wire_record']) is not dict: raise WriterError('REQUEST_FIELDS_INVALID')
        if type(request['flags']) is not dict or set(request['flags'])!=({'is_suspended','synthetic_bar'} if request['dataset']=='daily_bars' else set()) or any(type(v) is not bool for v in request['flags'].values()): raise WriterError('FLAGS_INVALID')
        if request['dataset']!='daily_bars' and request.get('suspension_evidence') is not None:raise WriterError('SUSPENSION_EVIDENCE_UNEXPECTED')
        if type(request['gate_results']) is not dict or any(v not in {'pass','fail','insufficient','not_applicable'} for v in request['gate_results'].values()): raise WriterError('GATES_INVALID')
        if not isinstance(request['contract_version'],str) or not isinstance(request['profile'],str): raise WriterError('REQUEST_FIELDS_INVALID')
        contract = self.contracts.get(request['contract_version'])
        if contract is None: raise WriterError('UNIT_CONTRACT_UNREGISTERED')
        doc = contract.document
        if request['dataset']!=doc['adapter']['dataset'] or not request['profile'].startswith(request['dataset']+'/'): raise WriterError('PROFILE_DATASET_MISMATCH')
        withdrawn=type(request['revision']) is dict and request['revision'].get('kind')=='withdrawal'
        if withdrawn:
            record=request['wire_record']
            if set(record)!={'retraction_reason'} or not isinstance(record['retraction_reason'],str) or not 0<len(record['retraction_reason'])<=4096:raise WriterError('TOMBSTONE_INVALID')
            payload={'retracted':True,'retraction_reason':record['retraction_reason']}
        else:
            if request['dataset']=='daily_bars':
                from app.services.governance_price_evidence import verify_price_evidence
                verify_price_evidence(request.get('price_evidence'),request['raw_response'],request['source_uri'],request['contract_version'])
            payload = contract.normalize(request)
        key,key_json = logical_identity(request['dataset'],request['key'])
        normalized_key=json.loads(key_json)
        time=request['time']
        times=time_fields(time,doc['publication_timezone'])
        source_tag=doc['adapter']['provider']+':'+doc['adapter']['endpoint']
        # This layer only writes single-source facts. Cross-source composition
        # requires the dataset dependency validators in M06/M07.
        sources=request['field_sources']
        etf_gates={}
        if request['dataset']=='etf' and not withdrawn:
            from app.services.governance_etf import validate_etf_request
            etf_gates,expected_sources=validate_etf_request(self,request,payload,doc)
            if sources!=expected_sources:raise WriterError('FIELD_SOURCES_INVALID')
        elif type(sources) is not dict or set(sources)-set(payload) or any(v!=source_tag for v in sources.values()): raise WriterError('FIELD_SOURCES_INVALID')
        for name in set(payload)&set(normalized_key)-{'dataset'}:
            if payload[name] is not None and payload[name]!=normalized_key[name]: raise WriterError('PAYLOAD_KEY_MISMATCH')
        if 'definition_id' in payload and payload['definition_id']!=doc['definition_id']: raise WriterError('DEFINITION_INVALID')
        if request['dataset']=='macro' and not withdrawn and payload.get('unit')!=doc['fields']['value']['normalized_unit']: raise WriterError('NORMALIZED_UNIT_MISMATCH')
        payload_json = canonical(payload); payload_hash = digest(payload_json.encode('utf-8'))
        rev = request['revision']; raw = request['raw_response']
        if type(rev) is not dict or set(rev)!={'kind','token','order','order_evidence','supersedes_version'} or rev['kind'] not in {'original','correction','withdrawal','repair'}: raise WriterError('REVISION_INVALID')
        if rev['token'] is not None and (not isinstance(rev['token'],str) or not 0<len(rev['token'])<=256): raise WriterError('REVISION_TOKEN_INVALID')
        if rev['order'] is not None:
            if doc.get('revision_order')!='integer' or type(rev['order']) is not int or not 0<=rev['order']<2**63 or not rev['token'] or type(rev['order_evidence']) is not bytes or not rev['order_evidence']: raise WriterError('ORDER_EVIDENCE_REQUIRED')
        elif rev['order_evidence'] is not None: raise WriterError('ORDER_EVIDENCE_REQUIRED')
        if (rev['kind']=='original') != (rev['supersedes_version'] is None): raise WriterError('REVISION_PARENT_INVALID')
        if request['dataset']=='macro':
            from app.services.governance_macro import validate_macro_request
            validate_macro_request(self,request,payload,doc)
        identity = {'dataset':request['dataset'],'logical_key':key,'source_tag':source_tag,
            'contract_version':contract.version,'definition_id':doc['definition_id'],'source_revision_token':rev['token'] or 'content:'+payload_hash,
            'normalized_payload_hash':payload_hash,'publication_evidence_hash':digest(time['publication_evidence']) if time['publication_evidence'] else None,
            'supersedes_version':rev['supersedes_version']}
        fields = {**{k:identity[k] for k in ('dataset','logical_key','source_tag','contract_version','definition_id','source_revision_token','normalized_payload_hash','publication_evidence_hash','supersedes_version')},
            'data_version':hash_object('fact-v1',identity),'key_json':key_json,'payload_json':payload_json,'raw_hash':digest(raw),'field_sources':request['field_sources'],
            'revision_kind':rev['kind'],'source_revision_order':rev['order'],'source_revision_evidence_hash':digest(rev['order_evidence']) if rev['order_evidence'] else None}
        fields.update(times)
        event=fields['event_date']
        for name in ('trade_date','report_period','valuation_date','period','effective_date'):
            if name in normalized_key and normalized_key[name]!=event.isoformat(): raise WriterError('EVENT_KEY_MISMATCH')
        if request['dataset']!='share_capital' and (fields['valid_from']!=event or fields['valid_to']!=event+timedelta(days=1)): raise WriterError('VALID_INTERVAL_INVALID')
        if 'definition_id' in request['key'] and request['key']['definition_id']!=doc['definition_id']: raise WriterError('DEFINITION_INVALID')
        from app.services.governance_master_data import MasterDataService
        master=MasterDataService(self.engine)
        identity=None
        if 'instrument_id' in request['key']:
            identity=master.validate_lifecycle(request['instrument_version'],request['key']['instrument_id'],event)
            if identity['asset_type']!=doc['adapter']['asset_type']: raise WriterError('ASSET_TYPE_MISMATCH')
        elif request['instrument_version'] is not None: raise WriterError('ENTITY_NAMESPACE_INVALID')
        if request['dataset']=='daily_bars':
            if event not in master.trading_days(request['calendar_version'],identity['exchange'],event,event+timedelta(days=1)): raise WriterError('NOT_TRADING_DAY')
            from app.services.market_history_contract import cn_stock_code_family
            if identity['asset_type']=='stock':
                rows=master.instrument_snapshot(request['instrument_version'])['codes']
                codes=[r['code'] for r in rows if r['instrument_id']==identity['instrument_id'] and r['valid_from']<=event.isoformat() and (r['valid_to'] is None or event.isoformat()<r['valid_to'])]
                if len(codes)!=1 or cn_stock_code_family(codes[0])!=doc['adapter']['instrument_family']: raise WriterError('INSTRUMENT_FAMILY_MISMATCH')
        if request['dataset'] in {'announcement','share_capital','corporate_action'} and not withdrawn:
            from app.services.governance_events import validate_event_request
            validate_event_request(self,request,payload,fields,doc)
        if request['dataset']=='announcement' and withdrawn:
            from app.services.governance_releases import validate_announcement_withdrawal
            validate_announcement_withdrawal(request,doc)
        gates = {**request['gate_results'], **dict.fromkeys(('identity','unit_contract','definition','time'), 'pass')}
        for rule,status in etf_gates.items():
            if gates.get(rule)!='fail':gates[rule]=status
        if request['dataset']=='daily_bars' and not withdrawn:
            from app.services.governance_price_gates import evaluate_price_gate, evaluate_suspension_bar
            for rule in ('P01','P02'):
                measured=evaluate_price_gate(rule,asset_type=identity['asset_type'],inputs=payload)['status']
                if gates.get(rule)!='fail' and measured!='pass': gates[rule]=measured
                elif gates.get(rule) is None: gates[rule]=measured
            suspension=request.get('suspension_evidence')
            if request['flags']['is_suspended'] or request['flags']['synthetic_bar']:
                from app.services.governance_price_gates import verify_suspension_evidence
                proof=verify_suspension_evidence(suspension,instrument_id=identity['instrument_id'],trade_date=event)
                measured=evaluate_suspension_bar(is_suspended=request['flags']['is_suspended'],synthetic_bar=request['flags']['synthetic_bar'],
                    official_status_evidence=proof['status_bytes'],official_reference=proof['official_reference'],
                    previous_raw_close=proof['previous_raw_close'],action_on_day=proof['action_on_day'],
                    **{k:payload.get(k) for k in ('raw_open','raw_high','raw_low','raw_close','volume_shares','amount_cny','turnover_rate_pct')})
                if measured['status']!='pass':gates['P01']=measured['status']
            elif suspension is not None:raise WriterError('SUSPENSION_EVIDENCE_UNEXPECTED')
        if request['dataset']=='index' and not withdrawn:
            from app.services.governance_index import validate_index_request
            validate_index_request(self,request,payload,doc)
            from app.services.governance_price_gates import evaluate_price_gate
            measured=evaluate_price_gate('P12',asset_type='index',inputs={**payload,
                'volume_unit':doc['fields'].get('volume',{}).get('normalized_unit'),
                'amount_unit':doc['fields'].get('amount',{}).get('normalized_unit')})['status']
            if gates.get('P12')!='fail' and measured!='pass':gates['P12']=measured
            elif gates.get('P12') is None:gates['P12']=measured
        frozen=get_profile(request['profile'])
        quality = ProfileAssessment('partial','partial',frozen.required_fields,(),(),frozen.version) if withdrawn else assess_profile(payload, request['profile'], gate_results=gates)
        if quality.invalid_fields: raise WriterError('PROFILE_PAYLOAD_INVALID:'+','.join(quality.invalid_fields))
        return (fields, quality, gates) if with_gates else (fields, quality)

    def preview(self, request):
        fields, quality = self._prepare(request)
        return {'dataset':fields['dataset'],'logical_key':fields['logical_key'],'data_version':fields['data_version'],'request_quality_status':quality.quality_status}

    def write(self, request, decision=None, *, connection=None, record_outcome=None):
        request=deepcopy(request)
        decision=deepcopy(decision)
        fields, quality, gates = self._prepare(request, with_gates=True)
        now = instant(self.clock())
        fields['observed_at'] = now
        command = decision if decision is not None else {'dataset':fields['dataset'],'logical_key':fields['logical_key'],'data_version':fields['data_version'],
            'expected_pointer_revision':None,'event':'stage','to_state':'claimed','actor':'writer:stage','evidence':b'stage-only'}
        if type(command) is not dict or set(command)!={'dataset','logical_key','data_version','expected_pointer_revision','event','to_state','actor','evidence'}: raise WriterError('DECISION_FIELDS_INVALID')
        if not isinstance(command['actor'],str) or not 0<len(command['actor'])<=128 or type(command['evidence']) is not bytes or not command['evidence']: raise WriterError('DECISION_EVIDENCE_REQUIRED')
        if command['event']!='stage' and (type(command['expected_pointer_revision']) is not int or not 0<=command['expected_pointer_revision']<2**63): raise WriterError('POINTER_REVISION_INVALID')
        for key in ('dataset','logical_key','data_version'):
            if command[key] != fields[key]: raise WriterError('DECISION_BINDING_MISMATCH')
        recorded={'T02':'claimed','T05':'stale','T06':'recovery_candidate','T07':'recovery_pending','T10':'conflict','T13':'retry'}
        event = command['event']; operation = 'stage' if event == 'stage' else 'record' if event in recorded else 'activate'
        if record_outcome is not None:
            if record_outcome not in {'ignored','conflict'} or event not in {'T09','T08'}:raise WriterError('RECORD_OUTCOME_INVALID')
            operation='record'
        validation={'profile':request['profile'],'instrument_version':request['instrument_version'],'calendar_version':request['calendar_version'],
            'gates':gates,'command':{k:v for k,v in command.items() if k!='evidence'},'authorization_evidence_hash':digest(command['evidence'])}
        price_evidence=request.get('price_evidence') if request['dataset']=='daily_bars' and fields['revision_kind']!='withdrawal' else None
        suspension_evidence=request.get('suspension_evidence')
        etf_evidence=request.get('etf_evidence')
        index_evidence=request.get('index_evidence')
        if index_evidence is not None and fields['revision_kind']!='withdrawal':validation['index_version']=json.loads(index_evidence)['index_version']
        if suspension_evidence is not None:validation['suspension_evidence_hash']=digest(suspension_evidence['status_bytes'])
        if record_outcome is not None:validation['record_outcome']=record_outcome
        decision_id = hash_object('decision-v1', validation)
        # Acquisition bytes are observations, not a new source decision.
        if price_evidence is not None: validation['price_evidence_hash']=digest(price_evidence)
        if etf_evidence is not None:validation['etf_evidence_hash']=digest(etf_evidence)
        if index_evidence is not None:validation['index_evidence_hash']=digest(index_evidence)
        validation_bytes=canonical(validation).encode('utf-8'); evidence=digest(validation_bytes)
        idem = hash_object('write-v1', {'dataset':fields['dataset'],'logical_key':fields['logical_key'],'data_version':fields['data_version'],'operation_kind':operation,'decision_id':decision_id})
        if connection is not None and (connection.engine is not self.engine or not connection.in_transaction()): raise WriterError('WRITER_TRANSACTION_INVALID')
        with (nullcontext(connection) if connection is not None else self.engine.begin()) as db:
            self._key_lock(db, fields)
            previous = db.execute(select(s.fact_current).where(s.fact_current.c.dataset==fields['dataset'],s.fact_current.c.logical_key==fields['logical_key']).with_for_update()).mappings().first()
            stored = db.execute(select(s.fact_versions).where(s.fact_versions.c.data_version==fields['data_version']).with_for_update()).mappings().first()
            if stored and any(stored[k]!=v for k,v in fields.items() if k not in {'observed_at','raw_hash'}): raise WriterError('HASH_COLLISION')
            result = db.execute(select(s.write_receipts.c.result_json).where(s.write_receipts.c.idempotency_key==idem).with_for_update()).scalar_one_or_none()
            if result is not None:
                self._evidence(db, request['raw_response'], request['source_uri'], now)
                if etf_evidence is not None:self._evidence(db,etf_evidence,'writer://etf-dependency-evidence',now)
                if index_evidence is not None:self._evidence(db,index_evidence,'writer://index-source-evidence',now)
                if price_evidence is not None:self._evidence(db,price_evidence,'writer://raw-price-acquisition',now)
                self._observe(db, fields, request['batch_id'], now)
                return result
            revision = previous['pointer_revision'] if previous else 0
            if operation!='stage' and command['expected_pointer_revision'] != revision: raise WriterError('POINTER_STALE')
            self._parent_chain(db, fields)
            accepted = operation=='activate'
            outcome=record_outcome or ('accepted' if accepted else recorded.get(event,'staged'))
            if accepted and stored and now<stored['observed_at']: raise WriterError('CLOCK_NOT_MONOTONIC')
            if accepted and quality.quality_status!='complete' and fields['revision_kind']!='withdrawal': raise WriterError('QUALITY_BLOCKED')
            if accepted and fields['public_available_at'] is not None and now<fields['public_available_at']: raise WriterError('NOT_YET_PUBLIC')
            if accepted and event not in {'T01','T03','T04','T08','T09','T11','T12'}: raise WriterError('DECISION_EVENT_INVALID')
            if accepted and command['to_state'] not in {'canonical','backup_temp'}: raise WriterError('DECISION_STATE_INVALID')
            if accepted and command['event']=='T01' and previous: raise WriterError('DECISION_CONTEXT_INVALID')
            if accepted and previous:
                old=db.execute(select(s.fact_versions).where(s.fact_versions.c.data_version==previous['data_version']).with_for_update()).mappings().one()
                if fields['data_version']==previous['data_version']: accepted=False; outcome='already_current'
                elif fields['source_tag']!=old['source_tag'] and event not in {'T08','T11'}: accepted=False; outcome='conflict'
                elif event=='T09':
                    if fields['supersedes_version']!=old['data_version']: raise WriterError('REVISION_PARENT_NOT_CURRENT')
                    left,right=fields['source_revision_order'],old['source_revision_order']
                    if left is None or right is None or fields['contract_version']!=old['contract_version']: accepted=False; outcome='conflict'
                    elif left<right: accepted=False; outcome='ignored'
                    elif left==right: accepted=False; outcome='conflict'
                elif event not in {'T03','T08','T11','T12'}: raise WriterError('DECISION_CONTEXT_INVALID')
            if accepted and fields['revision_kind']=='withdrawal' and event not in {'T11','T12'}:raise WriterError('TOMBSTONE_APPROVAL_REQUIRED')
            self._evidence(db, request['raw_response'], request['source_uri'], now)
            if etf_evidence is not None:self._evidence(db,etf_evidence,'writer://etf-dependency-evidence',now)
            if index_evidence is not None:self._evidence(db,index_evidence,'writer://index-source-evidence',now)
            self._evidence(db, validation_bytes, 'writer://validation-decision', now)
            if price_evidence is not None:self._evidence(db,price_evidence,'writer://raw-price-acquisition',now)
            if suspension_evidence is not None:self._evidence(db,suspension_evidence['status_bytes'],suspension_evidence['status_source_uri'],now)
            self._evidence(db, command['evidence'], 'writer://decision-authorization', now)
            contract=self.contracts[request['contract_version']]
            self._evidence(db,contract.evidence,'writer://unit-contract-evidence',now)
            self._evidence(db,contract.document_json.encode('utf-8'),'writer://unit-contract-document',now)
            if contract.sdk_evidence: self._evidence(db,contract.sdk_evidence,'writer://sdk-decoding-evidence',now)
            if request['revision']['order_evidence']: self._evidence(db,request['revision']['order_evidence'],'writer://revision-order-evidence',now)
            if request['time']['publication_evidence']:
                self._evidence(db,request['time']['publication_evidence'],'writer://publication-evidence',now)
            if stored is None: db.execute(insert(s.fact_versions).values(**fields))
            db.execute(insert(s.source_decisions).values(decision_id=decision_id,dataset=fields['dataset'],logical_key=fields['logical_key'],candidate_version=fields['data_version'],previous_version=previous['data_version'] if previous else None,
                event=event,from_state=previous['source_state'] if previous else 'none',to_state=command['to_state'] if accepted or operation=='record' else 'conflict' if outcome=='conflict' else 'claimed',actor=command['actor'],evidence_hash=evidence,decided_at=now,expected_pointer_revision=revision,result=outcome))
            if accepted:
                if db.execute(select(s.version_availability).where(s.version_availability.c.data_version==fields['data_version']).with_for_update()).first() is None:
                    db.execute(insert(s.version_availability).values(data_version=fields['data_version'],available_at=now,decision_id=decision_id))
                history = db.execute(select(s.acceptance_history).where(s.acceptance_history.c.dataset==fields['dataset'],s.acceptance_history.c.logical_key==fields['logical_key'],s.acceptance_history.c.system_to.is_(None)).with_for_update()).mappings().all()
                if previous:
                    if len(history)!=1 or history[0]['pointer_revision']!=revision: raise WriterError('ACCEPTANCE_HISTORY_INVALID')
                    if now<=history[0]['system_from']: raise WriterError('CLOCK_NOT_MONOTONIC')
                    db.execute(update(s.acceptance_history).where(s.acceptance_history.c.dataset==fields['dataset'],s.acceptance_history.c.logical_key==fields['logical_key'],s.acceptance_history.c.pointer_revision==revision).values(system_to=now))
                elif history: raise WriterError('ACCEPTANCE_HISTORY_INVALID')
                revision += 1
                core_profile = fields['dataset']+'/core'
                try: core_quality=assess_profile(json.loads(fields['payload_json']),core_profile,gate_results=gates).quality_status
                except ValueError: core_quality=quality.quality_status
                current = dict(dataset=fields['dataset'],logical_key=fields['logical_key'],data_version=fields['data_version'],pointer_revision=revision,
                    source_state=command['to_state'],quality_profile=request['profile'],quality_status=core_quality,
                    provenance_mode='composite' if fields['dataset']=='etf' and any(v!=fields['source_tag'] for v in request['field_sources'].values()) else 'single',updated_at=now)
                if previous: db.execute(update(s.fact_current).where(s.fact_current.c.dataset==fields['dataset'],s.fact_current.c.logical_key==fields['logical_key']).values(**current))
                else: db.execute(insert(s.fact_current).values(**current))
                db.execute(insert(s.acceptance_history).values(dataset=fields['dataset'],logical_key=fields['logical_key'],pointer_revision=revision,data_version=fields['data_version'],
                    source_state=command['to_state'],quality_status=core_quality,system_from=now,system_to=None,decision_id=decision_id))
            result = {'data_version':fields['data_version'],'logical_key':fields['logical_key'],'write_token':uuid4().hex+uuid4().hex,
                'decision_id':decision_id,'accepted':accepted,'outcome':outcome,'request_quality_status':quality.quality_status,'pointer_revision':revision}
            db.execute(insert(s.write_receipts).values(idempotency_key=idem,write_token=result['write_token'],dataset=fields['dataset'],logical_key=fields['logical_key'],data_version=fields['data_version'],operation_kind=operation,decision_id=decision_id,created_at=now,result_json=result))
            self._observe(db, fields, request['batch_id'], now)
            outbox = hash_object('outbox-v1', {'write_token':result['write_token']})
            high_water={fields['source_tag']:{fields['logical_key']:{k:fields[k] for k in ('data_version','source_revision_token','source_revision_order')}}} if accepted else {}
            db.execute(insert(s.governance_outbox).values(outbox_id=outbox,write_token=result['write_token'],version_ids=[fields['data_version']],source_high_water=high_water,state='pending',created_at=now,delivered_at=None))
            db.execute(insert(s.outbox_members).values(outbox_id=outbox,data_version=fields['data_version']))
            return result

    @staticmethod
    def _parent_chain(db, fields):
        seen={fields['data_version']}; parent=fields['supersedes_version']
        while parent is not None:
            if parent in seen: raise WriterError('VERSION_CYCLE')
            seen.add(parent)
            row=db.execute(select(s.fact_versions).where(s.fact_versions.c.data_version==parent).with_for_update()).mappings().first()
            if not row or row['dataset']!=fields['dataset'] or row['logical_key']!=fields['logical_key']: raise WriterError('REVISION_PARENT_INVALID')
            parent=row['supersedes_version']

    @staticmethod
    def _key_lock(db, fields):
        from sqlalchemy.dialects.mysql import insert as mysql_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        values = {k:fields[k] for k in ('dataset','logical_key','key_json')}
        if db.dialect.name=='mysql':
            statement = mysql_insert(s.fact_keys).values(**values).on_duplicate_key_update(key_json=s.fact_keys.c.key_json)
        else:
            statement = sqlite_insert(s.fact_keys).values(**values).on_conflict_do_nothing(index_elements=['dataset','logical_key'])
        db.execute(statement)
        saved = db.execute(select(s.fact_keys.c.key_json).where(s.fact_keys.c.dataset==fields['dataset'],s.fact_keys.c.logical_key==fields['logical_key']).with_for_update()).scalar_one()
        if saved!=fields['key_json']: raise WriterError('HASH_COLLISION')

    @staticmethod
    def _evidence(db, raw, uri, now):
        from sqlalchemy.dialects.mysql import insert as mysql_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        values = dict(raw_hash=digest(raw),raw_bytes=raw,source_uri=uri,observed_at=now)
        if db.dialect.name=='mysql': statement=mysql_insert(s.raw_evidence).values(**values).on_duplicate_key_update(raw_hash=s.raw_evidence.c.raw_hash)
        else: statement=sqlite_insert(s.raw_evidence).values(**values).on_conflict_do_nothing(index_elements=['raw_hash'])
        db.execute(statement)
        if db.execute(select(s.raw_evidence.c.raw_bytes).where(s.raw_evidence.c.raw_hash==values['raw_hash']).with_for_update()).scalar_one()!=raw: raise WriterError('HASH_COLLISION')

    @staticmethod
    def _observe(db, fields, batch_id, now):
        db.execute(insert(s.fact_observations).values(observation_id=uuid4().hex+uuid4().hex,data_version=fields['data_version'],raw_hash=fields['raw_hash'],observed_at=now,batch_id=batch_id))

    def fact(self, version, *, connection=None):
        with (nullcontext(connection) if connection is not None else self.engine.connect()) as db:
            row = db.execute(select(s.fact_versions).where(s.fact_versions.c.data_version==version)).mappings().one()
            self._verify_fact(row)
            result = dict(row); result['payload'] = json.loads(result.pop('payload_json'))
            result['key'] = json.loads(result.pop('key_json'))
            result['available_at'] = db.execute(select(s.version_availability.c.available_at).where(s.version_availability.c.data_version==version)).scalar_one_or_none()
            return result

    @staticmethod
    def _verify_fact(row):
        names=('dataset','logical_key','source_tag','contract_version','definition_id','source_revision_token','normalized_payload_hash','publication_evidence_hash','supersedes_version')
        try:
            valid=(digest(row['key_json'].encode('utf-8'))==row['logical_key'] and canonical(json.loads(row['key_json']))==row['key_json']
                and digest(row['payload_json'].encode('utf-8'))==row['normalized_payload_hash'] and canonical(json.loads(row['payload_json']))==row['payload_json']
                and hash_object('fact-v1',{k:row[k] for k in names})==row['data_version'])
        except (ValueError,KeyError,TypeError): valid=False
        if not valid: raise WriterError('HASH_COLLISION')

    def evidence(self, raw_hash, *, connection=None):
        with (nullcontext(connection) if connection is not None else self.engine.connect()) as db:
            row=dict(db.execute(select(s.raw_evidence).where(s.raw_evidence.c.raw_hash==raw_hash)).mappings().one())
            if digest(row['raw_bytes'])!=raw_hash: raise WriterError('HASH_COLLISION')
            return row

    def price_evidence(self, version):
        """Return verified acquisition receipts; old unproven facts stay unproven."""
        from app.services.governance_price_evidence import verify_price_evidence
        fact=self.fact(version)
        if fact['dataset']!='daily_bars' or fact['revision_kind']=='withdrawal':raise WriterError('RAW_PRICE_FACT_REQUIRED')
        raw=self.evidence(fact['raw_hash']);found={}
        with self.engine.connect() as db:
            hashes=db.execute(select(s.source_decisions.c.evidence_hash).where(s.source_decisions.c.candidate_version==version)).scalars().all()
        for evidence_hash in hashes:
            try:
                validation=json.loads(self.evidence(evidence_hash)['raw_bytes'])
                if validation['command']['data_version']!=version:raise WriterError('HASH_COLLISION')
                capture_hash=validation.get('price_evidence_hash')
                if capture_hash:
                    captured=self.evidence(capture_hash);receipt=captured['raw_bytes']
                    document=json.loads(receipt)
                    if document.get('raw_hash')!=fact['raw_hash']:continue
                    verify_price_evidence(receipt,raw['raw_bytes'],raw['source_uri'],fact['contract_version'])
                    found[capture_hash]={'evidence_hash':capture_hash,'evidence':receipt,'document':document,'observed_at':captured['observed_at']}
            except (KeyError,ValueError,TypeError) as exc:
                raise WriterError('RAW_PRICE_EVIDENCE_INVALID') from exc
        if not found:raise WriterError('RAW_PRICE_EVIDENCE_REQUIRED')
        return [found[key] for key in sorted(found)]

    def current(self, dataset, logical_key, *, connection=None):
        with (nullcontext(connection) if connection is not None else self.engine.connect()) as db:
            row = db.execute(select(s.fact_current).where(s.fact_current.c.dataset==dataset,s.fact_current.c.logical_key==logical_key)).mappings().first()
            return dict(row) if row else None

    def audit(self, logical_key):
        with self.engine.connect() as db:
            versions = select(s.fact_versions.c.data_version).where(s.fact_versions.c.logical_key==logical_key)
            counts = {}
            for name,table,column in [('facts',s.fact_versions,'data_version'),('decisions',s.source_decisions,'candidate_version'),('receipts',s.write_receipts,'data_version'),
                ('observations',s.fact_observations,'data_version'),('outbox',s.outbox_members,'data_version'),('history',s.acceptance_history,'data_version')]:
                counts[name] = db.scalar(select(func.count()).select_from(table).where(table.c[column].in_(versions)))
            return {'counts':counts,'history':[dict(x) for x in db.execute(select(s.acceptance_history).where(s.acceptance_history.c.logical_key==logical_key).order_by(s.acceptance_history.c.pointer_revision)).mappings()],
                'outbox':[dict(x) for x in db.execute(select(s.governance_outbox).join(s.outbox_members).where(s.outbox_members.c.data_version.in_(versions))).mappings()]}
