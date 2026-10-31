"""Frozen writer contracts and R3 canonical identities; no network or DDL."""
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
import hashlib
import json
import unicodedata
import re
import math
from decimal import Decimal, localcontext, ROUND_HALF_UP, InvalidOperation
from zoneinfo import ZoneInfo
from pathlib import Path

from app.services.data_source_contracts import resolve_contract, normalize_record
from app.services.data_contract_profiles import get_profile


class WriterError(ValueError):
    pass


def canonical(value):
    def convert(v):
        if isinstance(v, str): return unicodedata.normalize('NFC', v)
        if type(v) is date: return v.isoformat()
        if isinstance(v, datetime):
            if v.tzinfo is None or v.utcoffset() is None: raise WriterError('TIME_OFFSET_REQUIRED')
            return v.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        if type(v) is dict:
            result = {}
            for key, item in v.items():
                if not isinstance(key, str): raise WriterError('INVALID_CANONICAL_KEY')
                key = convert(key)
                if key in result: raise WriterError('CANONICAL_KEY_COLLISION')
                result[key] = convert(item)
            return result
        if type(v) in (list, tuple): return [convert(x) for x in v]
        if v is None or type(v) in (int, bool): return v
        raise WriterError('NON_CANONICAL_VALUE')
    return json.dumps(convert(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value): return hashlib.sha256(value).hexdigest()
def hash_object(domain, value): return digest(canonical({'domain':domain, 'value':value}).encode('utf-8'))


def day(value):
    if type(value) is date: return value
    if isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        try: return date.fromisoformat(value)
        except ValueError: pass
    raise WriterError('DATE_INVALID')


def instant(value):
    try:
        value = datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
        if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None: raise ValueError()
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, AttributeError): raise WriterError('TIME_OFFSET_REQUIRED') from None


KEY_FIELDS = {
    'daily_bars':'instrument_id trade_date','valuation':'instrument_id trade_date definition_id',
    'financial':'instrument_id report_period report_type definition_id','capital_flow':'instrument_id trade_date flow_definition',
    'lhb':'instrument_id trade_date reason_scope dedup_rule_id','hot_rank':'instrument_id ranking_method observation_at',
    'etf':'instrument_id valuation_date profile share_class','index':'index_id trade_date definition_id',
    'macro':'indicator_key period frequency definition_id','announcement':'authority announcement_id',
    'share_capital':'instrument_id effective_date share_class','corporate_action':'instrument_id authority_event_id'}


def logical_identity(dataset, key):
    if dataset not in KEY_FIELDS or type(key) is not dict: raise WriterError('LOGICAL_KEY_INVALID')
    wanted = set(KEY_FIELDS[dataset].split())
    if dataset=='etf' and key.get('profile') in {'iopv','etf/iopv'}: wanted.add('observation_at')
    if set(key)!=wanted: raise WriterError('LOGICAL_KEY_INVALID')
    result={'dataset':dataset}
    for name,value in key.items():
        if name=='instrument_id':
            if type(value) is not int or not 0<value<2**63: raise WriterError('ID_INVALID')
        elif name in {'trade_date','report_period','valuation_date','effective_date','period'}: value=day(value).isoformat()
        elif name=='observation_at': value=canonical(instant(value).replace(tzinfo=timezone.utc))[1:-1]
        else:
            if not isinstance(value,str) or not value.strip() or len(value)>256: raise WriterError('LOGICAL_KEY_INVALID')
            if name=='definition_id' and not re.fullmatch('[0-9a-f]{64}',value): raise WriterError('DEFINITION_INVALID')
        result[name]=value
    if dataset=='financial' and key['report_type'] not in {'annual','q1','half_year','q3'}: raise WriterError('REPORT_TYPE_INVALID')
    encoded=canonical(result)
    return digest(encoded.encode('utf-8')),encoded


def time_fields(time, zone):
    if type(time) is not dict or set(time)!={'event_date','publication_precision','published_at','publication_date','valid_from','valid_to','publication_evidence'}: raise WriterError('TIME_FIELDS_INVALID')
    if time['publication_evidence'] is not None and (type(time['publication_evidence']) is not bytes or not time['publication_evidence']): raise WriterError('PUBLICATION_EVIDENCE_REQUIRED')
    event = day(time['event_date']); first = day(time['valid_from']); end = day(time['valid_to']) if time['valid_to'] else None
    if end is not None and end<=first: raise WriterError('VALID_INTERVAL_INVALID')
    precision=time['publication_precision']; exact=None; local_date=None; public=None
    if precision=='exact':
        exact=instant(time['published_at']); public=exact
        if time['publication_date'] is not None: raise WriterError('PUBLICATION_FIELDS_INVALID')
    elif precision=='date':
        if time['published_at'] is not None: raise WriterError('PUBLICATION_FIELDS_INVALID')
        local_date=day(time['publication_date'])
        try: public=datetime.combine(local_date+timedelta(days=1),datetime.min.time(),ZoneInfo(zone)).astimezone(timezone.utc).replace(tzinfo=None)
        except OverflowError: raise WriterError('DATE_INVALID') from None
    elif precision=='unknown':
        if time['published_at'] is not None or time['publication_date'] is not None: raise WriterError('PUBLICATION_FIELDS_INVALID')
    else: raise WriterError('PUBLICATION_PRECISION_INVALID')
    if precision!='unknown' and (type(time['publication_evidence']) is not bytes or not time['publication_evidence']): raise WriterError('PUBLICATION_EVIDENCE_REQUIRED')
    return dict(event_date=event,valid_from=first,valid_to=end,published_at=exact,publication_date=local_date,publication_precision=precision,public_available_at=public)


@dataclass(frozen=True)
class WriterContract:
    document_json: str
    evidence: bytes
    sdk_evidence: bytes | None = None

    def __post_init__(self):
        try: doc=self.document
        except (TypeError,ValueError): raise WriterError('CONTRACT_DOCUMENT_INVALID') from None
        common={'kind','adapter','definition','definition_id','evidence_hash','publication_timezone','sdk'}
        if type(doc) is not dict or doc.get('kind') not in {'daily_adapter','profile'}: raise WriterError('CONTRACT_DOCUMENT_INVALID')
        expected=common|({'revision_order'} if doc['kind']=='daily_adapter' else {'profile','profile_version','fields'})
        if set(doc)!=expected and not (doc['kind']=='profile' and set(doc)==expected|{'revision_order'}): raise WriterError('CONTRACT_DOCUMENT_INVALID')
        if doc.get('revision_order') not in {None,'integer'}: raise WriterError('ORDER_CONTRACT_INVALID')
        if type(self.evidence) is not bytes or not self.evidence or doc['evidence_hash']!=digest(self.evidence): raise WriterError('CONTRACT_EVIDENCE_INVALID')
        if doc['definition_id']!=digest(canonical(doc['definition']).encode('utf-8')): raise WriterError('DEFINITION_INVALID')
        if type(doc['definition']) is not dict or not doc['definition']: raise WriterError('DEFINITION_INVALID')
        sdk=doc['sdk']
        if sdk is not None and (type(sdk) is not dict or set(sdk)!={'name','version','evidence_hash'}): raise WriterError('SDK_EVIDENCE_REQUIRED')
        if _sdk_doc({k:sdk[k] for k in ('name','version')} if sdk else None,self.sdk_evidence)!=sdk: raise WriterError('SDK_EVIDENCE_REQUIRED')
        try:
            ZoneInfo(doc['publication_timezone'])
            adapter=doc['adapter']
            _source_in_release(adapter['provider'],adapter['endpoint'])
            if doc['kind']=='daily_adapter':
                original=resolve_contract(adapter['endpoint'],provider=adapter['provider'],dataset='daily_bars',asset_type=adapter['asset_type'],instrument_family=adapter['instrument_family']).describe()
                for field in original['fields']: field['multiplier']=str(field['multiplier'])
                if json.loads(canonical(original))!=adapter or doc['revision_order'] not in {None,'integer'}: raise WriterError('CONTRACT_DOCUMENT_INVALID')
            else:
                if set(adapter)!={'provider','endpoint','dataset','asset_type','instrument_family'} or any(not isinstance(v,str) or not v.strip() for v in adapter.values()): raise WriterError('CONTRACT_DOCUMENT_INVALID')
                frozen=get_profile(doc['profile'])
                if not doc['profile'].startswith(adapter['dataset']+'/') or doc['profile_version']!=frozen.version: raise WriterError('PROFILE_CONTRACT_MISMATCH')
                _profile_fields(frozen,doc['fields'])
        except WriterError: raise
        except (ValueError,TypeError,KeyError,AttributeError): raise WriterError('CONTRACT_DOCUMENT_INVALID') from None
        object.__setattr__(self,'document_json',canonical(doc))

    @property
    def document(self): return json.loads(self.document_json)
    @property
    def version(self): return hash_object('contract-v1', self.document)

    @classmethod
    def daily_adapter(cls, *, provider, endpoint, asset_type, instrument_family, definition, evidence, publication_timezone, revision_order=None, sdk=None, sdk_evidence=None):
        if type(evidence) is not bytes or not evidence: raise WriterError('CONTRACT_EVIDENCE_REQUIRED')
        ZoneInfo(publication_timezone)
        contract = resolve_contract(endpoint, provider=provider, dataset='daily_bars', asset_type=asset_type, instrument_family=instrument_family)
        description = contract.describe()
        for field in description['fields']: field['multiplier'] = str(field['multiplier'])
        definition_json = canonical(definition)
        if revision_order not in {None,'integer'}: raise WriterError('ORDER_CONTRACT_INVALID')
        document = {'kind':'daily_adapter','adapter':description,'definition':json.loads(definition_json),'revision_order':revision_order,'sdk':_sdk_doc(sdk,sdk_evidence),
            'definition_id':digest(definition_json.encode('utf-8')), 'evidence_hash':digest(evidence), 'publication_timezone':publication_timezone}
        return cls(canonical(document), evidence,sdk_evidence)

    @classmethod
    def profile(cls, *, dataset, profile, provider, endpoint, asset_type, instrument_family, definition, fields, evidence, publication_timezone, sdk=None, sdk_evidence=None, revision_order=None):
        frozen=get_profile(profile)
        if not profile.startswith(dataset+'/'): raise WriterError('PROFILE_DATASET_MISMATCH')
        if type(evidence) is not bytes or not evidence: raise WriterError('CONTRACT_EVIDENCE_REQUIRED')
        ZoneInfo(publication_timezone)
        _profile_fields(frozen,fields)
        encoded=canonical(definition)
        doc={'kind':'profile','adapter':{'provider':provider,'endpoint':endpoint,'dataset':dataset,'asset_type':asset_type,'instrument_family':instrument_family},
            'profile':profile,'profile_version':frozen.version,'fields':fields,'definition':json.loads(encoded),'definition_id':digest(encoded.encode()),
            'evidence_hash':digest(evidence),'publication_timezone':publication_timezone,'sdk':_sdk_doc(sdk,sdk_evidence)}
        if revision_order is not None:
            if revision_order!='integer':raise WriterError('ORDER_CONTRACT_INVALID')
            doc['revision_order']=revision_order
        return cls(canonical(doc),evidence,sdk_evidence)

    def normalize(self, request):
        doc = self.document; adapter = doc['adapter']
        wire={}
        for name,value in request['wire_record'].items():
            if type(value) is float:
                if doc['sdk'] is None: raise WriterError('SDK_FLOAT_EVIDENCE_REQUIRED')
                if not math.isfinite(value): raise WriterError('INVALID_NUMERIC_VALUE')
                value=str(value)  # Python shortest round-trip decimal; never Decimal(float).
            wire[name]=value
        if doc['kind']=='profile':
            if request['profile']!=doc['profile'] or get_profile(request['profile']).version!=doc['profile_version']: raise WriterError('PROFILE_CONTRACT_MISMATCH')
            if set(wire)-{r['wire_field'] for r in doc['fields'].values()}: raise WriterError('UNEXPECTED_WIRE_FIELD')
            return {name:convert_value(wire.get(rule['wire_field']),name,rule) for name,rule in doc['fields'].items()}
        value = normalize_record(wire, adapter['endpoint'], provider=adapter['provider'], dataset=adapter['dataset'],
            asset_type=adapter['asset_type'], instrument_family=adapter['instrument_family'], version=adapter['version'],
            price_basis='raw', price_evidence_hash=doc['evidence_hash'])
        return {**value.canonical_payload, **request['flags']}


def _sdk_doc(sdk,evidence):
    if sdk is None:
        if evidence is not None: raise WriterError('SDK_EVIDENCE_REQUIRED')
        return None
    if type(sdk) is not dict or set(sdk)!={'name','version'} or any(not isinstance(v,str) or not 0<len(v)<=128 for v in sdk.values()) or type(evidence) is not bytes or not evidence: raise WriterError('SDK_EVIDENCE_REQUIRED')
    return {**sdk,'evidence_hash':digest(evidence)}


def _source_in_release(provider,endpoint):
    """Scope guard only. A contract is never an ADMIT-01 production approval."""
    items=json.loads(Path(__file__).with_name('data_source_contract_catalog.json').read_text(encoding='utf-8'))['items']
    allowed={name for i in items if i['in_current_release'] for name in (i['provider_claim'].casefold(),i['provider_claim'].split('_',1)[0].split('/',1)[0].casefold())}
    if not isinstance(provider,str) or provider.casefold() not in allowed or not isinstance(endpoint,str) or not endpoint.strip() or endpoint.startswith('PLAN:') or len(provider+':'+endpoint)>128:
        raise WriterError('SOURCE_OUT_OF_RELEASE')


def _profile_fields(frozen,fields):
    allowed=set(frozen.required_fields+frozen.optional_fields)
    if type(fields) is not dict or not set(frozen.required_fields)<=set(fields) or set(fields)-allowed: raise WriterError('CONTRACT_FIELDS_INVALID')
    for name,rule in fields.items():
        if type(rule) is not dict or set(rule)!={'wire_field','native_unit','normalized_unit','scale','offset'} or any(not isinstance(rule[k],str) or not rule[k] for k in rule): raise WriterError('UNIT_CONTRACT_INVALID')
        for key in ('scale','offset'):
            try:
                value=Decimal(rule[key])
                if not value.is_finite() or (key=='scale' and value<=0): raise ValueError()
            except (ValueError,InvalidOperation): raise WriterError('UNIT_CONTRACT_INVALID') from None
        if numeric_spec(name,rule['normalized_unit']) is None and (rule['scale']!='1' or rule['offset']!='0'): raise WriterError('UNIT_CONTRACT_INVALID')
    if len({r['wire_field'] for r in fields.values()})!=len(fields): raise WriterError('CONTRACT_FIELDS_INVALID')


TEXT_FIELDS={'flow_definition','reason_scope','dedup_rule_id','ranking_method','currency','share_class','share_definition','event_type','change_reason','benchmark_id','index_id','indicator_key','period','frequency','unit','authority','announcement_id'}
HASH_FIELDS={'definition_id','content_hash','nav_data_version','shares_data_version','nav_return_version','benchmark_return_version','announcement_data_version'}


def numeric_spec(name, unit):
    if name in TEXT_FIELDS|HASH_FIELDS|{'valuation_date','publication_date','record_date','published_at','observation_at','rank','total','window','is_suspended','synthetic_bar'}: return None
    if name.startswith(('raw_','index_')) or name in {'nav_official','iopv'}: expected,precision,scale=('point' if name.startswith('index_') else 'CNY'),24,8
    elif name=='pct' or name.endswith(('_pct','_x')): expected,precision,scale=('percentage_point' if name=='pct' or name.endswith('_pct') else 'x'),24,10
    elif name in {'volume_shares','total_shares','float_shares','free_float_shares'}: expected,precision,scale='share',28,8
    elif name in {'stock_ratio','rights_ratio','split_ratio'}: expected,precision,scale='x',24,10
    elif name=='volume':
        if unit not in {'share','lot','index_native','unknown'}:raise WriterError('NORMALIZED_UNIT_MISMATCH')
        expected,precision,scale=unit,28,8
    elif name=='value': expected,precision,scale=unit,28,8
    else: expected,precision,scale='CNY',28,8
    if unit!=expected: raise WriterError('NORMALIZED_UNIT_MISMATCH')
    return precision,scale


def convert_value(value,name,rule):
    if value is None: return None
    spec=numeric_spec(name,rule['normalized_unit'])
    if spec is None:
        if name in {'published_at','observation_at'}: return canonical(instant(value).replace(tzinfo=timezone.utc))[1:-1]
        if name in {'valuation_date','publication_date','record_date'}: return day(value).isoformat()
        return json.loads(canonical(value))
    if isinstance(value,(bool,float)): raise WriterError('INVALID_NUMERIC_VALUE')
    try:
        with localcontext() as context:
            context.prec=80
            number=Decimal(str(value))*Decimal(rule['scale'])+Decimal(rule['offset'])
            if not number.is_finite(): raise ValueError()
            precision,scale=spec
            number=number.quantize(Decimal(1).scaleb(-scale),rounding=ROUND_HALF_UP)
            if abs(number)>=Decimal(10)**(precision-scale): raise ValueError()
            if number==0: number=abs(number)
            return format(number,'f')
    except (ValueError,InvalidOperation): raise WriterError('INVALID_NUMERIC_VALUE') from None
