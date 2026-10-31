"""R4 policy and source transitions for explicitly migrated isolated candidates.

No scheduler, default database, supplier request or production admission wiring.
"""
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime,timezone,timedelta
from decimal import Decimal,localcontext
from zoneinfo import ZoneInfo
from uuid import uuid4
import json
from pathlib import Path
from sqlalchemy import select,insert,update,func
from app.db import governance_schema as base
from app.db import governance_source_schema as s
from app.db.governance_migration import migrate_candidate,migration_status
from app.services.governance_fact_contracts import WriterError,canonical,digest,hash_object,instant,logical_identity
from app.services.data_contract_profiles import get_profile
from app.services.governance_source_interfaces import source_interface

REVISION='DSG-R4-001'

def migrate_source_state(engine):
    migrate_candidate(engine)
    return migrate_candidate(engine,metadata=s.metadata,revision=REVISION)

def scope_key(doc):
    return hash_object('source-policy-scope-v1',{k:doc[k] for k in ('dataset','quality_profile','asset_type','instrument_family','definition_id')})


class SourceStateService:
    def __init__(self,writer,*,review_authority=None):
        if migration_status(writer.engine,metadata=s.metadata,revision=REVISION)['state']!='applied': raise WriterError('R4_SCHEMA_NOT_READY')
        self.writer=writer;self.engine=writer.engine
        self.review_authority=review_authority

    def publish_policy(self,spec,*,evidence,expected_revision):
        wanted={'dataset','quality_profile','asset_type','instrument_family','definition_id','members','comparison_policy','effective_from','effective_to'}
        if type(spec) is not dict or set(spec)!=wanted or type(evidence) is not bytes or not evidence: raise WriterError('POLICY_INVALID')
        spec=json.loads(canonical(spec))
        try:get_profile(spec['quality_profile'])
        except (ValueError,TypeError):raise WriterError('PROFILE_UNREGISTERED') from None
        if not spec['quality_profile'].startswith(spec['dataset']+'/'):raise WriterError('POLICY_DATASET_MISMATCH')
        members=spec['members']
        if not isinstance(members,list) or not members: raise WriterError('POLICY_MEMBERS_INVALID')
        catalog=json.loads(Path(__file__).with_name('data_source_contract_catalog.json').read_text(encoding='utf-8'))['items']
        ids=set();primaries=0;ordinals=[]
        for m in members:
            if type(m) is not dict or set(m)!={'interface_id','role','ordinal','capability_requirements'}: raise WriterError('POLICY_MEMBERS_INVALID')
            item=next((i for i in catalog if i['interface_id']==m['interface_id']),None)
            local=m['interface_id'] in {'LOCAL:etf_size','LOCAL:etf_tracking'}
            if not local and (not item or not item['in_current_release']): raise WriterError('SOURCE_OUT_OF_RELEASE')
            bound=[c for c in self.writer.contracts.values() if source_interface(c)==m['interface_id'] and c.document['adapter']['dataset']==spec['dataset']]
            if not bound and item and spec['dataset'] not in item['dataset'].split('/'):raise WriterError('POLICY_DATASET_MISMATCH')
            if m['interface_id'] in ids or m['role'] not in {'primary','backup','official','enrichment','validation'}: raise WriterError('POLICY_MEMBERS_INVALID')
            ids.add(m['interface_id']);primaries+=m['role']=='primary'
            if type(m['ordinal']) is not int or m['ordinal']<0: raise WriterError('POLICY_MEMBERS_INVALID')
            if m['role']=='backup':ordinals.append(m['ordinal'])
            elif m['ordinal']!=0: raise WriterError('POLICY_MEMBERS_INVALID')
            if type(m['capability_requirements']) is not list or set(m['capability_requirements'])-{'raw_evidence','family_unit_contract'}: raise WriterError('CAPABILITY_UNVERIFIED')
            contracts=[c.document for c in bound]
            if not any(all(c['adapter'][k]==spec[k] for k in ('dataset','asset_type','instrument_family')) and c['definition_id']==spec['definition_id'] for c in contracts): raise WriterError('POLICY_CONTRACT_UNVERIFIED')
        if primaries!=1 or sorted(ordinals)!=list(range(1,len(ordinals)+1)): raise WriterError('POLICY_ROLES_INVALID')
        if spec['comparison_policy']!=('bars-p13-v1' if spec['dataset']=='daily_bars' else 'payload-exact-v1'): raise WriterError('COMPARISON_POLICY_INVALID')
        first=instant(spec['effective_from']);end=instant(spec['effective_to']) if spec['effective_to'] else None
        if end is not None and end<=first: raise WriterError('POLICY_INTERVAL_INVALID')
        spec['effective_from']=json.loads(canonical(first.replace(tzinfo=timezone.utc)))
        spec['effective_to']=json.loads(canonical(end.replace(tzinfo=timezone.utc))) if end else None
        now=instant(self.writer.clock());version=hash_object('source-policy-v1',{**spec,'evidence_hash':digest(evidence),'schema_version':'source-policy-1.4.4'})
        scope=scope_key(spec)
        with self.engine.begin() as db:
            self.writer._evidence(db,evidence,'r4://policy-review',now)
            saved=db.execute(select(base.source_policies).where(base.source_policies.c.policy_version==version)).mappings().first()
            if not saved: db.execute(insert(base.source_policies).values(policy_version=version,**{k:spec[k] for k in wanted-{'effective_from','effective_to'}},effective_from=first,effective_to=end,evidence_hash=digest(evidence),schema_version='source-policy-1.4.4'))
            self._insert_once(db,s.policy_heads,dict(scope_key=scope,policy_version=version,revision=0),['scope_key'])
            head=db.execute(select(s.policy_heads).where(s.policy_heads.c.scope_key==scope).with_for_update()).mappings().one()
            if head['policy_version']==version and head['revision']>0:return version
            if type(expected_revision) is not int or expected_revision!=head['revision']: raise WriterError('POLICY_STALE')
            db.execute(update(s.policy_heads).where(s.policy_heads.c.scope_key==scope).values(policy_version=version,revision=head['revision']+1))
        return version

    @staticmethod
    def _insert_once(db,table,values,keys):
        if db.dialect.name=='mysql':
            from sqlalchemy.dialects.mysql import insert as native
            query=native(table).values(**values).on_duplicate_key_update(**{keys[0]:table.c[keys[0]]})
        else:
            from sqlalchemy.dialects.sqlite import insert as native
            query=native(table).values(**values).on_conflict_do_nothing(index_elements=keys)
        db.execute(query)

    def _active_policy(self,db,version):
        policy=db.execute(select(base.source_policies).where(base.source_policies.c.policy_version==version)).mappings().first()
        if not policy:raise WriterError('POLICY_UNREGISTERED')
        self._verify_policy(db,policy)
        head=db.execute(select(s.policy_heads).where(s.policy_heads.c.scope_key==scope_key(policy)).with_for_update(read=True)).mappings().one()
        if head['policy_version']!=version:raise WriterError('POLICY_STALE')
        now=instant(self.writer.clock())
        if now<policy['effective_from'] or policy['effective_to'] is not None and now>=policy['effective_to']:raise WriterError('POLICY_EXPIRED')
        return policy

    def _policy(self,db,version,fields,request):
        policy=self._active_policy(db,version)
        contract=self.writer.contracts[request['contract_version']].document
        if any(policy[k]!=contract['adapter'][k] for k in ('dataset','asset_type','instrument_family')) or policy['definition_id']!=fields['definition_id'] or policy['quality_profile']!=request['profile']: raise WriterError('POLICY_SCOPE_MISMATCH')
        interface=source_interface(self.writer.contracts[request['contract_version']])
        member=next((m for m in policy['members'] if m['interface_id']==interface),None)
        if not member:raise WriterError('SOURCE_NOT_IN_POLICY')
        return policy,member

    @staticmethod
    def _verify_policy(db,policy):
        document={k:policy[k] for k in ('dataset','quality_profile','asset_type','instrument_family','definition_id','members','comparison_policy','evidence_hash','schema_version')}
        for key in ('effective_from','effective_to'):document[key]=json.loads(canonical(policy[key].replace(tzinfo=timezone.utc))) if policy[key] is not None else None
        evidence=db.execute(select(base.raw_evidence.c.raw_bytes).where(base.raw_evidence.c.raw_hash==policy['evidence_hash'])).scalar_one_or_none()
        if hash_object('source-policy-v1',document)!=policy['policy_version'] or evidence is None or digest(evidence)!=policy['evidence_hash']:raise WriterError('HASH_COLLISION')

    def submit(self,request,*,policy_version,expected_revision,failures=(),lease=None,connection=None,recovery_calendar_version=None):
        request=deepcopy(request);fields,quality=self.writer._prepare(request)
        context={'recovery_calendar_version':recovery_calendar_version} if recovery_calendar_version is not None else None
        identity={'data_version':fields['data_version'],'policy_version':policy_version,'expected_revision':expected_revision,'failures':sorted(set(failures)),'batch_id':request['batch_id'],'lease_token':lease['lease_token'] if lease else None}
        if context:identity['context']=context
        operation=hash_object('r4-submit-v1',identity)
        with (nullcontext(connection) if connection is not None else self.engine.begin()) as db:
            policy,member=self._policy(db,policy_version,fields,request)
            self.writer._key_lock(db,fields)
            replay=db.execute(select(s.transitions).where(s.transitions.c.transition_id==operation).with_for_update()).mappings().first()
            if replay:
                command=dict(replay['result_json']['command'])
                command['evidence']=db.execute(select(base.raw_evidence.c.raw_bytes).where(base.raw_evidence.c.raw_hash==replay['evidence_hash']).with_for_update(read=True)).scalar_one()
                self.writer.write(request,command,connection=db,record_outcome=replay['result_json'].get('record_outcome'))
                return replay['result_json']['response']
            previous=self._current(db,fields)
            if expected_revision!=(previous['pointer_revision'] if previous else 0):raise WriterError('POINTER_STALE')
            pending_migration=db.execute(select(s.legacy.c.migration_id).where(s.legacy.c.state.in_(['claimed','conflict']),s.legacy.c.claimed_version==fields['data_version']).limit(1).with_for_update()).first()
            if pending_migration:raise WriterError('MIGRATION_APPROVAL_REQUIRED')
            if lease:
                item=self._check_lease(db,lease)
                if item['dataset']!=fields['dataset'] or item['logical_key']!=fields['logical_key'] or not previous or previous['data_version']!=item['backup_data_version']:raise WriterError('RECOVERY_KEY_MISMATCH')
            elif previous and previous['source_state']=='backup_temp':
                held=db.execute(select(s.recoveries).where(s.recoveries.c.dataset==fields['dataset'],s.recoveries.c.logical_key==fields['logical_key'],s.recoveries.c.backup_data_version==previous['data_version'])).mappings().one()
                if held['status']=='running' and held['lease_until']>instant(self.writer.clock()):raise WriterError('RECOVERY_LEASE_HELD')
            known=db.execute(select(base.fact_versions).where(base.fact_versions.c.dataset==fields['dataset'],base.fact_versions.c.logical_key==fields['logical_key'],base.fact_versions.c.source_tag==fields['source_tag'],base.fact_versions.c.contract_version==fields['contract_version'],base.fact_versions.c.revision_kind!='repair').with_for_update()).mappings().all()
            orders=[r['source_revision_order'] for r in known if r['source_revision_order'] is not None]
            stale_order=bool(orders and fields['source_revision_order'] is not None and fields['source_revision_order']<max(orders))
            ambiguous_order=any(r['source_revision_order']==fields['source_revision_order'] and r['data_version']!=fields['data_version'] for r in known) if fields['source_revision_order'] is not None else False
            event='T01';state='canonical'
            if fields['revision_kind']=='repair':
                event='T10';state='conflict';stale_order=False;ambiguous_order=False
            elif stale_order:event='T09';state='claimed'
            elif ambiguous_order:event='T09';state='conflict'
            elif member['role'] not in {'primary','backup'}:event='T10';state='conflict'
            elif not previous:
                if member['role']=='backup':
                    self._require_failures(db,policy,member,fields,request,failures)
                    event='T04';state='backup_temp'
            elif previous['source_state']=='canonical':
                old=db.execute(select(base.fact_versions).where(base.fact_versions.c.data_version==previous['data_version'])).mappings().one()
                event='T05' if member['role']=='backup' and failures else 'T09' if fields['source_tag']==old['source_tag'] else 'T10'
                if event=='T05':self._require_failures(db,policy,member,fields,request,failures)
                if fields['revision_kind']=='repair':event='T10'
                if fields['revision_kind']=='withdrawal':
                    proven=fields['source_tag']==old['source_tag'] and fields['contract_version']==old['contract_version'] and fields['supersedes_version']==old['data_version'] and fields['source_revision_order'] is not None and old['source_revision_order'] is not None and fields['source_revision_order']>old['source_revision_order']
                    event='T12' if proven else 'T10'
                state='conflict' if event=='T10' else 'claimed' if event=='T05' else 'canonical'
            else:
                if member['role']!='primary':event='T10';state='conflict'
                elif not self._next_trading_day(request,fields,recovery_calendar_version):event='T06';state='recovery_pending'
                else:
                    item=db.execute(select(s.recoveries).where(s.recoveries.c.dataset==fields['dataset'],s.recoveries.c.logical_key==fields['logical_key'],s.recoveries.c.backup_data_version==previous['data_version']).with_for_update()).mappings().one()
                    self._execute(db,request,fields,policy,previous,'T07','recovery_pending',hash_object('r4-step-v1',{'operation':operation,'event':'T07'}),extra_context=context)
                    old=db.execute(select(base.fact_versions).where(base.fact_versions.c.data_version==previous['data_version'])).mappings().one()
                    comparison=self._compare(policy,old,fields)
                    event='T08' if comparison=='pass' else 'T10';state='canonical' if event=='T08' else 'conflict'
            record_outcome='conflict' if ambiguous_order else None
            if stale_order:
                record_outcome='ignored'
            elif event=='T09' and previous and previous['data_version']!=fields['data_version']:
                old=db.execute(select(base.fact_versions).where(base.fact_versions.c.data_version==previous['data_version'])).mappings().one()
                if fields['source_revision_order'] is None or old['source_revision_order'] is None or fields['contract_version']!=old['contract_version'] or fields['source_revision_order']==old['source_revision_order']:
                    state='conflict';record_outcome='conflict'
            response=self._execute(db,request,fields,policy,previous,event,state,operation,record_outcome=record_outcome,extra_context=context)
            now=instant(self.writer.clock())
            if response['accepted'] and event=='T04':
                self._insert_once(db,s.recoveries,dict(dataset=fields['dataset'],logical_key=fields['logical_key'],backup_data_version=fields['data_version'],policy_version=policy_version,
                    status='pending',attempts=0,next_attempt_at=now,last_error=None,candidate_version=None,lease_until=None,lease_owner=None,lease_token=None,completed_at=None,created_at=now,escalated_at=None),['dataset','logical_key','backup_data_version'])
            elif previous and previous['source_state']=='backup_temp':
                values={'candidate_version':fields['data_version']}
                if response['accepted']:values.update(status='done',completed_at=now,lease_until=None,lease_owner=None,lease_token=None,last_error=None)
                elif event=='T10':values.update(status='conflict',last_error='RECOVERY_COMPARISON_FAILED',lease_until=None,lease_owner=None,lease_token=None)
                db.execute(update(s.recoveries).where(s.recoveries.c.dataset==fields['dataset'],s.recoveries.c.logical_key==fields['logical_key'],s.recoveries.c.backup_data_version==previous['data_version']).values(**values))
            return response

    @staticmethod
    def _current(db,fields):
        return db.execute(select(base.fact_current).where(base.fact_current.c.dataset==fields['dataset'],base.fact_current.c.logical_key==fields['logical_key']).with_for_update()).mappings().first()

    def _execute(self,db,request,fields,policy,previous,event,state,operation,authorization=None,record_outcome=None,extra_context=None):
        revision=previous['pointer_revision'] if previous else 0
        packet={'source_policy_version':policy['policy_version'],'event':event,'expected_revision':revision,'operation_id':operation,'authorization':authorization,'record_outcome':record_outcome}
        if extra_context is not None:packet['context']=extra_context
        proof=canonical(packet).encode('utf-8')
        command={k:fields[k] for k in ('dataset','logical_key','data_version')}
        command.update(expected_pointer_revision=revision,event=event,to_state=state,actor='r4:auto',evidence=proof)
        result=self.writer.write(request,command,connection=db,record_outcome=record_outcome)
        response={**result,'event':event,'policy_version':policy['policy_version'],'transition_id':operation}
        self._candidate(db,fields,policy['policy_version'],state if result['outcome'] not in {'ignored','conflict'} else 'conflict' if result['outcome']=='conflict' else 'claimed')
        self._put_transition(db,transition_id=operation,dataset=fields['dataset'],logical_key=fields['logical_key'],policy_version=policy['policy_version'],event=event,
            candidate_version=fields['data_version'],decision_id=result['decision_id'],previous_version=previous['data_version'] if previous else None,expected_revision=revision,
            evidence_hash=digest(proof),happened_at=instant(self.writer.clock()),result_json={'response':response,'command':{k:v for k,v in command.items() if k!='evidence'},'record_outcome':record_outcome})
        return response

    @staticmethod
    def _put_transition(db,**values):
        sequence=db.scalar(select(s.transitions.c.sequence_no).where(s.transitions.c.dataset==values['dataset'],s.transitions.c.logical_key==values['logical_key']).order_by(s.transitions.c.sequence_no.desc()).limit(1).with_for_update()) or 0
        db.execute(insert(s.transitions).values(**values,sequence_no=sequence+1))

    def record_failure(self,dataset,key,*,policy_version,expected_revision,interface_id,reason,evidence,batch_id,connection=None):
        if reason not in {'failed','empty','expired','circuit_open'} or type(evidence) is not bytes or not evidence or not isinstance(batch_id,str) or not batch_id:raise WriterError('FAILURE_EVIDENCE_REQUIRED')
        logical,encoded=logical_identity(dataset,key);fields={'dataset':dataset,'logical_key':logical,'key_json':encoded}
        with (nullcontext(connection) if connection is not None else self.engine.begin()) as db:
            policy=self._active_policy(db,policy_version)
            if not policy or policy['dataset']!=dataset or not any(m['interface_id']==interface_id and m['role'] in {'primary','backup'} for m in policy['members']):raise WriterError('SOURCE_NOT_IN_POLICY')
            self.writer._key_lock(db,fields);previous=self._current(db,fields)
            revision=previous['pointer_revision'] if previous else 0
            if revision!=expected_revision:raise WriterError('POINTER_STALE')
            event='T05' if previous and previous['source_state']=='canonical' else 'T13' if previous else 'T04'
            packet={'dataset':dataset,'logical_key':logical,'policy_version':policy_version,'expected_revision':expected_revision,'interface_id':interface_id,'reason':reason,'evidence_hash':digest(evidence),'batch_id':batch_id}
            operation=hash_object('r4-failure-v1',packet)
            saved=db.execute(select(s.transitions.c.result_json).where(s.transitions.c.transition_id==operation)).scalar_one_or_none()
            if saved:return saved['response']
            now=instant(self.writer.clock());proof=canonical(packet).encode('utf-8')
            self.writer._evidence(db,evidence,'r4://source-failure',now);self.writer._evidence(db,proof,'r4://failure-context',now)
            response={'event':event,'accepted':False,'outcome':'stale' if previous else 'missing','transition_id':operation,**packet}
            self._put_transition(db,transition_id=operation,dataset=dataset,logical_key=logical,policy_version=policy_version,event=event,candidate_version=None,decision_id=None,previous_version=previous['data_version'] if previous else None,expected_revision=revision,evidence_hash=digest(proof),happened_at=now,result_json={'response':response})
            if event=='T13':
                item=db.execute(select(s.recoveries).where(s.recoveries.c.dataset==dataset,s.recoveries.c.logical_key==logical,s.recoveries.c.backup_data_version==previous['data_version']).with_for_update()).mappings().first()
                if item and item['status'] not in {'conflict','done'} and not (item['status']=='running' and item['lease_until']>now):
                    db.execute(update(s.recoveries).where(*self._item_where(item)).values(**self._retry_values(item,now,reason,operation)))
            return response

    @staticmethod
    def _require_failures(db,policy,member,fields,request,failures):
        required={m['interface_id'] for m in policy['members'] if m['role']=='primary' or m['role']=='backup' and m['ordinal']<member['ordinal']}
        proven=set()
        for identity in failures:
            row=db.execute(select(s.transitions).where(s.transitions.c.transition_id==identity)).mappings().first()
            if row and row['dataset']==fields['dataset'] and row['logical_key']==fields['logical_key'] and row['policy_version']==policy['policy_version']:
                result=row['result_json']['response']
                if result.get('batch_id')==request['batch_id'] and result.get('reason') in {'failed','empty','expired','circuit_open'}:proven.add(result['interface_id'])
        if not required<=proven:raise WriterError('PRIMARY_FAILURE_REQUIRED')

    def _next_trading_day(self,request,fields,recovery_calendar_version=None):
        from app.services.governance_master_data import MasterDataService
        zone=self.writer.contracts[request['contract_version']].document['publication_timezone']
        today=self.writer.clock().astimezone(ZoneInfo(zone)).date()
        event=fields['event_date']
        if today<=event:return False
        master=MasterDataService(self.engine)
        version=recovery_calendar_version or request['calendar_version']
        if version is None:raise WriterError('RECOVERY_CALENDAR_REQUIRED')
        calendar=master.calendar_snapshot(version)
        return bool(master.trading_days(version,calendar['exchange'],event+timedelta(days=1),today+timedelta(days=1)))

    @staticmethod
    def _compare(policy,old,new):
        if any(old[k]!=new[k] for k in ('dataset','logical_key','definition_id','event_date')):return 'incompatible'
        left=json.loads(old['payload_json']);right=json.loads(new['payload_json'])
        if left.get('currency')!=right.get('currency'):return 'incompatible'
        if policy['comparison_policy']=='payload-exact-v1':
            required=get_profile(policy['quality_profile']).required_fields
            if any(left.get(k) is None or right.get(k) is None for k in required):return 'insufficient'
            return 'pass' if all(left[k]==right[k] for k in required) else 'conflict'
        if any(left.get(k) is None or right.get(k) is None for k in ('raw_close','volume_shares')):return 'insufficient'
        with localcontext() as ctx:
            ctx.prec=80
            p,b=Decimal(right['raw_close']),Decimal(left['raw_close'])
            if abs(p-b)>max(Decimal('0.01'),abs(p)*Decimal('0.005')):return 'conflict'
            for field,tolerance in (('volume_shares','0.01'),('amount_cny','0.02')):
                if left.get(field) is not None and right.get(field) is not None:
                    p,b=Decimal(right[field]),Decimal(left[field])
                    if abs(p-b)/max(abs(p),Decimal('0.00000001'))>Decimal(tolerance):return 'conflict'
        return 'pass'

    def lease_recoveries(self,worker,*,limit=100):
        if not isinstance(worker,str) or not 0<len(worker)<=128 or type(limit) is not int or not 1<=limit<=1000:raise WriterError('LEASE_ARGUMENT_INVALID')
        now=instant(self.writer.clock())
        with self.engine.begin() as db:
            if db.dialect.name=='sqlite':db.exec_driver_sql('BEGIN IMMEDIATE')
            db.execute(update(s.recoveries).where(s.recoveries.c.status!='done',s.recoveries.c.escalated_at.is_(None),s.recoveries.c.created_at<=now-timedelta(hours=24)).values(escalated_at=now))
            rows=db.execute(select(s.recoveries).where(s.recoveries.c.next_attempt_at<=now,
                (s.recoveries.c.status=='pending')|((s.recoveries.c.status=='running')&(s.recoveries.c.lease_until<=now))).order_by(s.recoveries.c.created_at,s.recoveries.c.logical_key).limit(limit).with_for_update()).mappings().all()
            result=[]
            for row in rows:
                values=dict(lease_token=uuid4().hex+uuid4().hex,lease_owner=worker,lease_until=now+timedelta(seconds=120),status='running')
                db.execute(update(s.recoveries).where(*self._item_where(row)).values(**values))
                pointer=db.execute(select(base.fact_current).where(base.fact_current.c.dataset==row['dataset'],base.fact_current.c.logical_key==row['logical_key'])).mappings().first()
                result.append({**dict(row),**values,'expected_revision':pointer['pointer_revision'] if pointer else 0})
            return result

    @staticmethod
    def _item_where(item):
        return [s.recoveries.c[k]==item[k] for k in ('dataset','logical_key','backup_data_version')]

    def _check_lease(self,db,lease):
        try:row=db.execute(select(s.recoveries).where(*self._item_where(lease)).with_for_update()).mappings().first()
        except (KeyError,TypeError):raise WriterError('LEASE_STALE') from None
        if not row or row['status']!='running' or row['lease_until']<=instant(self.writer.clock()) or row['lease_token']!=lease.get('lease_token') or row['lease_owner']!=lease.get('lease_owner'):raise WriterError('LEASE_STALE')
        return row

    def fail_recovery(self,lease,*,error,evidence):
        if not isinstance(error,str) or not error or type(evidence) is not bytes or not evidence:raise WriterError('FAILURE_EVIDENCE_REQUIRED')
        with self.engine.begin() as db:
            key=db.execute(select(base.fact_keys.c.key_json).where(base.fact_keys.c.dataset==lease['dataset'],base.fact_keys.c.logical_key==lease['logical_key'])).scalar_one()
            self.writer._key_lock(db,{'dataset':lease['dataset'],'logical_key':lease['logical_key'],'key_json':key})
            operation=hash_object('r4-retry-v1',{'lease_token':lease['lease_token']})
            cached=db.execute(select(s.transitions.c.result_json).where(s.transitions.c.transition_id==operation).with_for_update()).scalar_one_or_none()
            if cached:return cached['response']
            item=self._check_lease(db,lease);now=instant(self.writer.clock());attempts=item['attempts']+1
            values=self._retry_values(item,now,error,item['lease_token'])
            db.execute(update(s.recoveries).where(*self._item_where(item)).values(**values))
            self.writer._evidence(db,evidence,'r4://recovery-failure',now)
            response={'event':'T13','accepted':False,'outcome':'retry','transition_id':operation,'attempts':attempts}
            self._put_transition(db,transition_id=operation,dataset=item['dataset'],logical_key=item['logical_key'],policy_version=item['policy_version'],event='T13',candidate_version=item['candidate_version'],decision_id=None,previous_version=item['backup_data_version'],expected_revision=lease['expected_revision'],evidence_hash=digest(evidence),happened_at=now,result_json={'response':response})
            return response

    @staticmethod
    def _retry_values(item,now,error,salt):
        attempts=item['attempts']+1;base_delay=min(60*2**min(attempts,6),3600)
        jitter=int(digest((salt+str(attempts)).encode())[:8],16)%1001/10000
        values=dict(status='pending',attempts=attempts,next_attempt_at=now+timedelta(seconds=base_delay*(1+jitter)),last_error=error[:4096],lease_until=None,lease_owner=None,lease_token=None)
        if now-item['created_at']>=timedelta(hours=24) and item['escalated_at'] is None:values['escalated_at']=now
        return values

    def fetch_recovery(self,lease,*,controller,task_id,fetch):
        """The only recovery I/O seam: every supplier call passes M04 controls."""
        from app.services.data_source_control import RequestController
        if not isinstance(controller,RequestController):raise WriterError('REQUEST_CONTROLLER_REQUIRED')
        with self.engine.begin() as db:
            item=self._check_lease(db,lease)
            policy=db.execute(select(base.source_policies).where(base.source_policies.c.policy_version==item['policy_version'])).mappings().one()
            head=db.execute(select(s.policy_heads).where(s.policy_heads.c.scope_key==scope_key(policy))).mappings().one()
            policy=self._active_policy(db,head['policy_version'])
            primary=next(m['interface_id'] for m in policy['members'] if m['role']=='primary')
            catalog=json.loads(Path(__file__).with_name('data_source_contract_catalog.json').read_text(encoding='utf-8'))['items']
            endpoint=next(i['endpoint'] for i in catalog if i['interface_id']==primary and i['in_current_release'])
        try:
            request=controller.execute(endpoint,fetch,deepcopy(lease),task_id=task_id,max_attempts=1)
            fields,_=self.writer._prepare(request)
            if fields['dataset']!=item['dataset'] or fields['logical_key']!=item['logical_key']:raise WriterError('RECOVERY_KEY_MISMATCH')
            return self.submit(request,policy_version=policy['policy_version'],expected_revision=lease['expected_revision'],lease=lease)
        except Exception as error:
            try:self.fail_recovery(lease,error=type(error).__name__+':'+str(error),evidence=(type(error).__name__+':'+str(error)).encode('utf-8'))
            except WriterError:pass  # An expired/superseded lease cannot alter the new worker's item.
            raise

    def _candidate(self,db,fields,policy,state):
        values={k:fields[k] for k in ('dataset','logical_key','data_version')}
        self._insert_once(db,s.candidates,{**values,'candidate_state':state,'policy_version':policy,'updated_at':instant(self.writer.clock())},list(values))
        db.execute(update(s.candidates).where(*(s.candidates.c[k]==v for k,v in values.items())).values(candidate_state=state,policy_version=policy,updated_at=instant(self.writer.clock())))

    def _context(self,db,request,fields,policy,expected,purpose,migration_id=None,legacy_key=None):
        previous=self._current(db,fields)
        mapping=None
        if purpose=='migration':
            row=db.execute(select(s.legacy).where(s.legacy.c.migration_id==migration_id,s.legacy.c.legacy_key==legacy_key)).mappings().first()
            if not row or row['claimed_version']!=fields['data_version']:raise WriterError('MIGRATION_NOT_CLAIMED')
            mapping=row['mapping_evidence_hash']
        return hash_object('r4-review-context-v1',{'dataset':fields['dataset'],'logical_key':fields['logical_key'],'data_version':fields['data_version'],
            'previous_version':previous['data_version'] if previous else None,'policy_version':policy,'expected_revision':expected,'purpose':purpose,
            'migration_id':migration_id,'legacy_key':legacy_key,'mapping_evidence_hash':mapping,'instrument_version':request['instrument_version'],'calendar_version':request['calendar_version']})

    def review_context(self,request,*,policy_version,expected_revision,purpose,migration_id=None,legacy_key=None):
        if purpose not in {'arbitration','migration'}:raise WriterError('REVIEW_PURPOSE_INVALID')
        fields,_=self.writer._prepare(request)
        with self.engine.begin() as db:return self._context(db,request,fields,policy_version,expected_revision,purpose,migration_id,legacy_key)

    def arbitrate(self,request,*,policy_version,expected_revision,reviews):
        return self._approved(request,policy_version,expected_revision,reviews,'arbitration')

    def approve_migration(self,request,*,migration_id,legacy_key,policy_version,expected_revision,reviews):
        return self._approved(request,policy_version,expected_revision,reviews,'migration',migration_id,legacy_key)

    def _approved(self,request,policy_version,expected_revision,reviews,purpose,migration_id=None,legacy_key=None):
        from app.services.governance_reviews import ReviewAuthority
        if not isinstance(self.review_authority,ReviewAuthority):raise WriterError('TWO_HUMAN_APPROVALS_REQUIRED')
        request=deepcopy(request);fields,_=self.writer._prepare(request)
        try:references=[{k:r[k] for k in ('context_hash','reviewer_id','signature','evidence_hash')} for r in reviews]
        except (TypeError,KeyError):raise WriterError('TWO_HUMAN_APPROVALS_REQUIRED') from None
        operation=hash_object('r4-approved-v1',{'data_version':fields['data_version'],'policy_version':policy_version,'expected_revision':expected_revision,'purpose':purpose,'migration_id':migration_id,'legacy_key':legacy_key,'reviews':references})
        with self.engine.begin() as db:
            policy,member=self._policy(db,policy_version,fields,request)
            self.writer._key_lock(db,fields)
            cached=db.execute(select(s.transitions).where(s.transitions.c.transition_id==operation).with_for_update()).mappings().first()
            if cached:
                command={**cached['result_json']['command'],'evidence':db.execute(select(base.raw_evidence.c.raw_bytes).where(base.raw_evidence.c.raw_hash==cached['evidence_hash']).with_for_update(read=True)).scalar_one()}
                self.writer.write(request,command,connection=db)
                return cached['result_json']['response']
            previous=self._current(db,fields)
            if expected_revision!=(previous['pointer_revision'] if previous else 0):raise WriterError('POINTER_STALE')
            context=self._context(db,request,fields,policy_version,expected_revision,purpose,migration_id,legacy_key)
            packets=self.review_authority.verify(context,reviews,now=instant(self.writer.clock()))
            candidate=db.execute(select(s.candidates).where(s.candidates.c.dataset==fields['dataset'],s.candidates.c.logical_key==fields['logical_key'],s.candidates.c.data_version==fields['data_version'])).mappings().first()
            if not candidate or candidate['candidate_state']!=('conflict' if purpose=='arbitration' else 'claimed'):raise WriterError('CANDIDATE_STATE_INVALID')
            for review in reviews:self.writer._evidence(db,review['evidence'],'r4://human-review-evidence',instant(self.writer.clock()))
            if purpose=='migration' and (previous or member['role']!='primary'):
                result=self._execute(db,request,fields,policy,previous,'T10','conflict',operation,authorization={'context_hash':context,'approvals':packets,'reason':'MIGRATION_CONFLICT'})
                db.execute(update(s.legacy).where(s.legacy.c.migration_id==migration_id,s.legacy.c.legacy_key==legacy_key).values(state='conflict'))
                return result
            result=self._execute(db,request,fields,policy,previous,'T11' if purpose=='arbitration' else 'T03','canonical',operation,authorization={'context_hash':context,'approvals':packets})
            if result['accepted']:
                db.execute(update(s.legacy).where(s.legacy.c.claimed_version==fields['data_version'],s.legacy.c.state.in_(['claimed','conflict'])).values(state='canonical'))
                if previous and previous['source_state']=='backup_temp':
                    db.execute(update(s.recoveries).where(s.recoveries.c.dataset==fields['dataset'],s.recoveries.c.logical_key==fields['logical_key'],s.recoveries.c.backup_data_version==previous['data_version']).values(status='done',candidate_version=fields['data_version'],completed_at=instant(self.writer.clock()),lease_until=None,lease_owner=None,lease_token=None,last_error=None))
            return result

    def register_legacy(self,migration_id,legacy_key,*,raw,source_uri):
        if not isinstance(migration_id,str) or not 0<len(migration_id)<=128 or not isinstance(legacy_key,str) or not 0<len(legacy_key)<=256 or type(raw) is not bytes or not raw or not isinstance(source_uri,str) or not source_uri:raise WriterError('LEGACY_EVIDENCE_REQUIRED')
        with self.engine.begin() as db:
            now=instant(self.writer.clock());self.writer._evidence(db,raw,source_uri,now)
            self._insert_once(db,s.legacy,dict(migration_id=migration_id,legacy_key=legacy_key,raw_hash=digest(raw),state='legacy',claimed_version=None,mapping_evidence_hash=None,created_at=now),['migration_id','legacy_key'])
            row=db.execute(select(s.legacy).where(s.legacy.c.migration_id==migration_id,s.legacy.c.legacy_key==legacy_key)).mappings().one()
            if row['raw_hash']!=digest(raw):raise WriterError('LEGACY_IMMUTABLE')
            return dict(row)

    def claim_legacy(self,request,*,migration_id,legacy_key,policy_version,expected_revision,mapping_evidence):
        if type(mapping_evidence) is not bytes or not mapping_evidence:raise WriterError('MAPPING_EVIDENCE_REQUIRED')
        request=deepcopy(request);fields,_=self.writer._prepare(request)
        with self.engine.begin() as db:
            policy,_=self._policy(db,policy_version,fields,request);self.writer._key_lock(db,fields)
            legacy=db.execute(select(s.legacy).where(s.legacy.c.migration_id==migration_id,s.legacy.c.legacy_key==legacy_key).with_for_update()).mappings().first()
            if not legacy or legacy['raw_hash']!=fields['raw_hash']:raise WriterError('LEGACY_EVIDENCE_MISMATCH')
            operation=hash_object('r4-claim-v1',{'migration_id':migration_id,'legacy_key':legacy_key,'data_version':fields['data_version'],'mapping_hash':digest(mapping_evidence),'policy_version':policy_version,'expected_revision':expected_revision})
            cached=db.execute(select(s.transitions).where(s.transitions.c.transition_id==operation).with_for_update()).mappings().first()
            if cached:
                command={**cached['result_json']['command'],'evidence':db.execute(select(base.raw_evidence.c.raw_bytes).where(base.raw_evidence.c.raw_hash==cached['evidence_hash']).with_for_update(read=True)).scalar_one()}
                self.writer.write(request,command,connection=db)
                return cached['result_json']['response']
            if legacy['state']!='legacy':raise WriterError('LEGACY_ALREADY_CLAIMED')
            previous=self._current(db,fields)
            if expected_revision!=(previous['pointer_revision'] if previous else 0):raise WriterError('POINTER_STALE')
            self.writer._evidence(db,mapping_evidence,'r4://legacy-mapping',instant(self.writer.clock()))
            result=self._execute(db,request,fields,policy,previous,'T02','claimed',operation,authorization={'migration_id':migration_id,'legacy_key':legacy_key,'mapping_hash':digest(mapping_evidence)})
            db.execute(update(s.legacy).where(s.legacy.c.migration_id==migration_id,s.legacy.c.legacy_key==legacy_key).values(state='claimed',claimed_version=fields['data_version'],mapping_evidence_hash=digest(mapping_evidence)))
            return result

    def resolve(self,dataset,logical_key):
        state=self.inspect(dataset,logical_key);current=state['current']
        if not current:return {'data_version':None,'missing':{'reason':'NOT_INGESTED'}}
        fact=self.writer.fact(current['data_version'])
        if fact['revision_kind']=='withdrawal':return {'data_version':current['data_version'],'missing':{'reason':'RETRACTED'}}
        return {'data_version':current['data_version'],'fact':fact,'degraded':state['degraded'],'quality_status':current['quality_status'],'quality_profile':current['quality_profile'],'missing':None}

    def inspect(self,dataset,logical_key):
        with self.engine.connect() as db:
            current=db.execute(select(base.fact_current).where(base.fact_current.c.dataset==dataset,base.fact_current.c.logical_key==logical_key)).mappings().first()
            return {'current':dict(current) if current else None,'degraded':bool(current and current['source_state']=='backup_temp'),
                'candidates':[dict(r) for r in db.execute(select(s.candidates).where(s.candidates.c.dataset==dataset,s.candidates.c.logical_key==logical_key)).mappings()],
                'recoveries':[dict(r) for r in db.execute(select(s.recoveries).where(s.recoveries.c.dataset==dataset,s.recoveries.c.logical_key==logical_key)).mappings()],
                'transitions':[dict(r) for r in db.execute(select(s.transitions).where(s.transitions.c.dataset==dataset,s.transitions.c.logical_key==logical_key).order_by(s.transitions.c.sequence_no)).mappings()]}
