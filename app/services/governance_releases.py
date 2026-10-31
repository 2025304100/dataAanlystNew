"""Candidate public release acquisition and announcement preparation.

No scheduler, production admission, default database or HTML-to-economic-value
guessing. A reviewed extraction pins the source bytes, field meanings and release
times. Downloads still traverse the existing source controller's PLAN endpoints.
"""
from copy import deepcopy
from datetime import timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.services.governance_fact_contracts import WriterContract, WriterError, canonical, day, digest, instant

PUBLIC_RELEASES = {
    'NBS': ('stats.gov.cn', 'PLAN:NBS_direct'),
    'PBOC': ('pbc.gov.cn', 'PLAN:PBOC_direct'),
    'ChinaBond': ('chinabond.com.cn', 'PLAN:ChinaBond_direct'),
    'Chinamoney': ('chinamoney.com.cn', 'PLAN:Chinamoney_direct'),
    'SSE': ('sse.com.cn', 'PLAN:SSE_direct'),
    'SZSE': ('szse.cn', 'PLAN:SZSE_direct'),
    'BSE': ('bse.cn', 'PLAN:BSE'),
    'CNINFO': ('cninfo.com.cn', 'PLAN:CNINFO_web'),
}


def validate_release_uri(provider, source_uri):
    try:
        host = PUBLIC_RELEASES[provider][0]
        url = urlsplit(source_uri)
        valid = (url.scheme == 'https' and url.hostname and
                 (url.hostname == host or url.hostname.endswith('.' + host)) and
                 not url.username and not url.password and url.port in {None, 443} and not url.fragment)
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise WriterError('RELEASE_AUTHORITY_UNVERIFIED')


def capture_public_release(provider, source_uri, *, should_cancel=None):
    """Bounded GET of an explicitly selected public page/PDF, never a crawler.

    No token endpoint, redirects, cookies or implicit retry/fallback. The caller
    supplies the registered release URI; admission remains fail-closed upstream.
    """
    import requests
    from app.services.data_source_execution import source_boundary
    validate_release_uri(provider, source_uri)
    def cancelled():
        if should_cancel is not None and should_cancel():
            raise WriterError('CAPTURE_CANCELLED')
    def acquire():
        cancelled()
        with requests.get(source_uri, timeout=(5, 15), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise WriterError('RELEASE_HTTP_STATUS_INVALID')
            chunks = []; size = 0
            for chunk in response.iter_content(chunk_size=65536):
                cancelled()
                if not chunk: continue
                size += len(chunk)
                if size > 16 * 1024 * 1024: raise WriterError('CAPTURE_TOO_LARGE')
                chunks.append(chunk)
            cancelled()
            if not size: raise WriterError('RELEASE_EMPTY_BODY')
            return b''.join(chunks)
    return source_boundary(PUBLIC_RELEASES[provider][1])(acquire)()


def announcement_contract(authority, *, evidence):
    if authority not in {'SSE', 'SZSE', 'BSE', 'CNINFO'}:
        raise WriterError('RELEASE_AUTHORITY_UNVERIFIED')
    units = {'authority': 'text', 'announcement_id': 'text', 'content_hash': 'hash',
             'published_at': 'time', 'publication_date': 'date'}
    return WriterContract.profile(dataset='announcement', profile='announcement/core', provider=authority,
        endpoint='public_release', asset_type='stock', instrument_family='cn_stock',
        definition={'schema': 'announcement-body-v1', 'identity': 'authority_stable_announcement_id'},
        fields={name: {'wire_field': name, 'native_unit': unit, 'normalized_unit': unit, 'scale': '1', 'offset': '0'}
                for name, unit in units.items()}, evidence=evidence, publication_timezone='Asia/Shanghai')


def validate_announcement_withdrawal(request, doc):
    """A tombstone still needs an official body and known release evidence."""
    authority = doc['adapter']['provider']
    validate_release_uri(authority, request['source_uri'])
    if request['key']['authority'] != authority: raise WriterError('EVENT_AUTHORITY_MISMATCH')
    if request['time']['publication_precision'] not in {'exact', 'date'}:
        raise WriterError('ANNOUNCEMENT_PUBLICATION_UNKNOWN')
    if request['time']['publication_evidence'] != request['raw_response']:
        raise WriterError('ANNOUNCEMENT_CONTENT_UNBOUND')


class AnnouncementSyncService:
    def __init__(self, writer): self.writer = writer

    def request(self, body, *, authority, announcement_id, contract_version, source_uri, batch_id,
                publication_precision, published_at=None, publication_date=None, event_date=None,
                revision=None, retraction_reason=None):
        """Preserve the official body and local publication day, including weekends.

        Publication fields are explicit reviewed source metadata; body alone is
        not a timestamp extractor. Revisions retain the stable announcement ID.
        Withdrawals require the original event_date and a parent version.
        """
        validate_release_uri(authority, source_uri)
        contract = self.writer.contracts.get(contract_version)
        if contract is None: raise WriterError('UNIT_CONTRACT_UNREGISTERED')
        if contract.document != announcement_contract(authority, evidence=contract.evidence).document:
            raise WriterError('ANNOUNCEMENT_CONTRACT_MISMATCH')
        if type(body) is not bytes or not 0 < len(body) <= 16 * 1024 * 1024:
            raise WriterError('RAW_EVIDENCE_REQUIRED')
        if not isinstance(announcement_id, str) or not announcement_id.strip(): raise WriterError('LOGICAL_KEY_INVALID')
        rev = deepcopy(revision or {'kind': 'original', 'token': None, 'order': None,
                                   'order_evidence': None, 'supersedes_version': None})
        withdrawn = rev.get('kind') == 'withdrawal'
        if publication_precision == 'exact':
            released = instant(published_at).replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
        elif publication_precision == 'date': released = day(publication_date)
        else: raise WriterError('ANNOUNCEMENT_PUBLICATION_UNKNOWN')
        on = day(event_date) if withdrawn and event_date is not None else released
        if not withdrawn and event_date is not None and day(event_date) != released:
            raise WriterError('ANNOUNCEMENT_PUBLICATION_MISMATCH')
        if withdrawn and event_date is None: raise WriterError('WITHDRAWAL_EVENT_DATE_REQUIRED')
        record = {'authority': authority, 'announcement_id': announcement_id, 'content_hash': digest(body),
                  'published_at': published_at, 'publication_date': publication_date}
        if withdrawn: record = {'retraction_reason': retraction_reason}
        request = {'dataset': 'announcement', 'key': {'authority': authority, 'announcement_id': announcement_id},
            'contract_version': contract_version, 'profile': 'announcement/core', 'instrument_version': None,
            'calendar_version': None, 'wire_record': record, 'raw_response': body, 'source_uri': source_uri,
            'time': {'event_date': on.isoformat(), 'publication_precision': publication_precision,
                     'published_at': published_at, 'publication_date': publication_date,
                     'valid_from': on.isoformat(), 'valid_to': (on + timedelta(days=1)).isoformat(),
                     'publication_evidence': body},
            'revision': rev, 'flags': {}, 'gate_results': {}, 'field_sources': {}, 'batch_id': batch_id}
        self.writer.preview(request)
        return request

    def ingest(self, requests):
        with self.writer.engine.begin() as connection:
            results = [self.writer.write(request, connection=connection) for request in requests]
        return {'status': 'staged' if results else 'empty', 'results': results,
                'coverage_complete': False, 'production_admission': 'not_certified'}

    def read_as_of(self, **kwargs):
        from app.services.governance_macro import release_as_of
        return release_as_of(self.writer, dataset='announcement', **kwargs)
