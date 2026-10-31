"""Candidate announcement, share-capital and corporate-action admission rules.

The bytes/contract/link are auditable acquisition evidence. They do not certify a
live exchange endpoint; supplier admission and complete as-of selection are later
gates. All persistence stays in the single FactWriter transaction.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy.exc import NoResultFound

from app.services.governance_fact_contracts import WriterError, digest

_HOSTS = {'SSE': 'sse.com.cn', 'SZSE': 'szse.cn', 'BSE': 'bse.cn', 'CNINFO': 'cninfo.com.cn'}
_ACTION_TYPES = {'cash_dividend', 'stock_dividend', 'rights_issue', 'split', 'consolidation', 'combined'}
_CAPITAL_TYPES = _ACTION_TYPES | {'issuance', 'conversion', 'repurchase', 'unlock', 'other'}
_SHARE_DEFINITION = 'cn-issued-tradable-freefloat-v1'


def _official(request, doc):
    provider = doc['adapter']['provider'].upper()
    host = _HOSTS.get(provider)
    parsed = urlsplit(request['source_uri'])
    if host is None or parsed.scheme != 'https' or not parsed.hostname or not (
        parsed.hostname == host or parsed.hostname.endswith('.' + host)
    ) or parsed.username or parsed.password:
        raise WriterError('EVENT_AUTHORITY_UNVERIFIED')
    return provider


def _number(payload, name, *, missing=False):
    value = payload.get(name)
    if value is None:
        if missing: return None
        raise WriterError('EVENT_PARAMETERS_MISSING:' + name)
    try:
        number = Decimal(value)
        if not number.is_finite(): raise ValueError()
        return number
    except (TypeError, ValueError, InvalidOperation):
        raise WriterError('EVENT_PARAMETERS_INVALID:' + name) from None


def _action(payload, event_date):
    kind = payload['event_type']
    if kind not in _ACTION_TYPES: raise WriterError('EVENT_TYPE_UNKNOWN')
    cash, stock, rights = (_number(payload, k) for k in ('cash_dividend', 'stock_ratio', 'rights_ratio'))
    price = _number(payload, 'rights_price', missing=True)
    split = _number(payload, 'split_ratio', missing=True)
    if cash < 0 or stock <= -1 or rights < 0 or (price is not None and price <= 0):
        raise WriterError('EVENT_PARAMETERS_INVALID')
    if rights > 0 and price is None or rights == 0 and price is not None:
        raise WriterError('EVENT_RIGHTS_PRICE_INVALID')
    if kind == 'cash_dividend' and not (cash > 0 and stock == rights == 0):
        raise WriterError('EVENT_TYPE_PARAMETERS_MISMATCH')
    if kind == 'stock_dividend' and not (stock > 0 and cash == rights == 0):
        raise WriterError('EVENT_TYPE_PARAMETERS_MISMATCH')
    if kind == 'rights_issue' and not (rights > 0 and cash == stock == 0):
        raise WriterError('EVENT_TYPE_PARAMETERS_MISMATCH')
    if kind in {'split', 'consolidation'}:
        if split is None or split <= 0 or stock != split - 1 or cash != 0 or rights != 0 or (
            kind == 'split' and split <= 1 or kind == 'consolidation' and split >= 1
        ):
            raise WriterError('EVENT_SPLIT_CONVERSION_INVALID')
    elif split is not None:
        raise WriterError('EVENT_SPLIT_CONVERSION_INVALID')
    if kind == 'combined' and cash == stock == rights == 0:
        raise WriterError('EVENT_TYPE_PARAMETERS_MISMATCH')
    record = payload.get('record_date')
    if record is not None and date.fromisoformat(record) >= event_date:
        raise WriterError('EVENT_RECORD_DATE_INVALID')


def _capital(payload, key):
    if payload['share_class'] != key['share_class'] or payload['share_definition'] != _SHARE_DEFINITION:
        raise WriterError('SHARE_DEFINITION_MISMATCH')
    if payload['event_type'] not in _CAPITAL_TYPES:
        raise WriterError('EVENT_TYPE_UNKNOWN')
    total = _number(payload, 'total_shares')
    floating = _number(payload, 'float_shares')
    free = _number(payload, 'free_float_shares', missing=True)
    if total <= 0 or floating < 0 or floating > total or free is not None and (free < 0 or free > floating):
        raise WriterError('SHARE_CAPITAL_BOUNDS_INVALID')


def validate_event_request(writer, request, payload, fields, doc):
    """Validate decoded fields before FactWriter creates any fact or decision."""
    dataset = request['dataset']
    authority = _official(request, doc)
    if dataset == 'announcement':
        if request['key']['authority'] != authority or payload['authority'] != authority:
            raise WriterError('EVENT_AUTHORITY_MISMATCH')
        if payload['content_hash'] != fields['raw_hash'] or fields['publication_evidence_hash'] != fields['raw_hash']:
            raise WriterError('ANNOUNCEMENT_CONTENT_UNBOUND')
        if fields['public_available_at'] is None:
            raise WriterError('ANNOUNCEMENT_PUBLICATION_UNKNOWN')
        if fields['published_at'] is not None:
            if payload.get('published_at') != fields['published_at'].isoformat(timespec='microseconds') + 'Z' or payload.get('publication_date') is not None:
                raise WriterError('ANNOUNCEMENT_PUBLICATION_MISMATCH')
        elif payload.get('published_at') is not None or payload.get('publication_date') != fields['publication_date'].isoformat():
            raise WriterError('ANNOUNCEMENT_PUBLICATION_MISMATCH')
        local_day = fields['published_at'].replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(doc['publication_timezone'])).date() if fields['published_at'] else fields['publication_date']
        if local_day != fields['event_date']:
            raise WriterError('ANNOUNCEMENT_PUBLICATION_DAY_MISMATCH')
        return
    if fields['publication_evidence_hash'] is None or fields['public_available_at'] is None:
        raise WriterError('EVENT_PUBLICATION_EVIDENCE_REQUIRED')
    try:
        notice = writer.fact(payload['announcement_data_version'])
        raw = writer.evidence(notice['raw_hash'])
    except (NoResultFound, KeyError):
        raise WriterError('EVENT_ANNOUNCEMENT_NOT_ADMITTED') from None
    if notice['dataset'] != 'announcement' or notice['revision_kind'] == 'withdrawal' or notice['available_at'] is None or (
        notice['key']['authority'] != authority or notice['key']['announcement_id'] != payload['announcement_id']
        or notice['source_tag'].split(':', 1)[0].upper() != authority
        or notice['payload']['content_hash'] != notice['raw_hash']
        or notice['publication_evidence_hash'] != notice['raw_hash']
        or fields['publication_evidence_hash'] != notice['raw_hash']
        or fields['public_available_at'] != notice['public_available_at']
        or fields['raw_hash'] != notice['raw_hash']
    ):
        raise WriterError('EVENT_ANNOUNCEMENT_BINDING_INVALID')
    if digest(raw['raw_bytes']) != notice['raw_hash']:
        raise WriterError('EVENT_ANNOUNCEMENT_BINDING_INVALID')
    if dataset == 'corporate_action':
        event_id = request['key']['authority_event_id']
        if not event_id.startswith(authority + ':') or not event_id[len(authority) + 1:]:
            raise WriterError('EVENT_ID_AUTHORITY_NAMESPACE_INVALID')
        if fields['valid_from'] != fields['event_date'] or fields['valid_to'] is None or (
            fields['valid_to'] - fields['valid_from']).days != 1:
            raise WriterError('EVENT_INTERVAL_INVALID')
        _action(payload, fields['event_date'])
    else:
        if fields['valid_from'] != fields['event_date']:
            raise WriterError('SHARE_CAPITAL_INTERVAL_INVALID')
        _capital(payload, request['key'])
