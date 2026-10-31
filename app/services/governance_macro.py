"""M07.6 single-source macro observations and explicitly reviewed releases.

Current FRED CSVs have no vintage/publication history. Exact calendar lags retain
gaps. Official values enter only through byte-bound reviewed extraction; no
AkShare/Jin10 positional column is relabelled as an official primary source.
All fact persistence delegates to the candidate FactWriter.
"""
import base64
import csv
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from io import StringIO
import json
from urllib.parse import urlsplit, parse_qs

from sqlalchemy import select
from app.db import governance_schema as schema
from app.services.data_contract_profiles import assess_profile, GLOBAL_GATES
from app.services.governance_fact_contracts import WriterContract, WriterError, canonical, day, digest, instant, logical_identity, time_fields
from app.services.governance_releases import validate_release_uri

# Existing six FRED fallback products only. us_10y_yield is NOT a FRED alias.
FRED_PRODUCTS = {
    'CPIAUCSL': ('us_cpi_yoy', 'yoy', 'percentage_point', 'SA_index'),
    'CPILFESL': ('us_core_cpi_mom', 'mom', 'percentage_point', 'SA_index'),
    'PPIACO': ('us_ppi', 'yoy', 'percentage_point', 'NSA_index'),
    'INDPRO': ('us_industrial_production', 'yoy', 'percentage_point', 'SA_index'),
    'PAYEMS': ('us_non_farm', 'monthly_change_10k', '10k_people', 'SA_thousand_people'),
    'UNRATE': ('us_unemployment', 'level', 'percentage_point', 'SA_percent'),
}
# Flow/stock, term and units are part of the definition, never inferred from value size.
OFFICIAL_PRODUCTS = {
    'nbs_cpi': ('NBS', 'cn_cpi_yoy', 'monthly', 'percentage_point', '1', 'national_all_items_yoy'),
    'nbs_ppi': ('NBS', 'cn_ppi_yoy', 'monthly', 'percentage_point', '1', 'national_industrial_producer_yoy'),
    'nbs_pmi': ('NBS', 'cn_pmi', 'monthly', 'point', '1', 'manufacturing_pmi'),
    'pboc_m2': ('PBOC', 'cn_m2_yoy', 'monthly', 'percentage_point', '1', 'm2_stock_yoy'),
    'pboc_social_financing': ('PBOC', 'cn_social_financing', 'monthly', 'CNY_100M', '100000000', 'afre_monthly_flow'),
    'pboc_new_credit': ('PBOC', 'cn_new_credit', 'monthly', 'CNY_100M', '100000000', 'rmb_loan_monthly_flow'),
    'pboc_lpr_1y': ('PBOC', 'cn_lpr_1y', 'monthly', 'percentage_point', '1', 'authorized_lpr_1y'),
    'pboc_lpr_5y': ('PBOC', 'cn_lpr_5y', 'monthly', 'percentage_point', '1', 'authorized_lpr_over_5y'),
    'chinabond_10y': ('ChinaBond', 'cn_10y_yield', 'daily', 'percentage_point', '1', 'china_government_yield_curve_10y'),
    'chinamoney_lpr_1y': ('Chinamoney', 'cn_lpr_1y', 'monthly', 'percentage_point', '1', 'authorized_lpr_1y'),
    'chinamoney_lpr_5y': ('Chinamoney', 'cn_lpr_5y', 'monthly', 'percentage_point', '1', 'authorized_lpr_over_5y'),
}


def _spec(product):
    if product.startswith('fred:') and product[5:] in FRED_PRODUCTS:
        series = product[5:]; key, transform, unit, native = FRED_PRODUCTS[series]
        return {'provider': 'FRED', 'endpoint': product, 'indicator_key': key, 'frequency': 'monthly',
                'unit': unit, 'native_unit': native, 'scale': '1', 'transform': transform,
                'definition': {'schema': 'macro-observation-v1', 'series': series, 'native_definition': native,
                               'transform': transform, 'frequency': 'monthly', 'unit': unit}}
    if product not in OFFICIAL_PRODUCTS: raise WriterError('MACRO_PRODUCT_UNREGISTERED')
    provider, key, frequency, native, scale, meaning = OFFICIAL_PRODUCTS[product]
    unit = 'CNY' if native == 'CNY_100M' else native
    return {'provider': provider, 'endpoint': 'public_release:' + product, 'indicator_key': key,
            'frequency': frequency, 'unit': unit, 'native_unit': native, 'scale': scale, 'transform': 'level',
            'definition': {'schema': 'macro-observation-v1', 'meaning': meaning, 'frequency': frequency, 'unit': unit}}


def macro_contract(product, *, evidence):
    spec = _spec(product)
    fields = {name: {'wire_field': name, 'native_unit': 'text', 'normalized_unit': 'text', 'scale': '1', 'offset': '0'}
              for name in ('indicator_key', 'period', 'frequency', 'unit')}
    fields['value'] = {'wire_field': 'value', 'native_unit': spec['unit'], 'normalized_unit': spec['unit'],
                       'scale': '1', 'offset': '0'}  # extraction converts the explicitly pinned native scale
    return WriterContract.profile(dataset='macro', profile='macro/core', provider=spec['provider'],
        endpoint=spec['endpoint'], asset_type='macro', instrument_family='economic_indicator',
        definition=spec['definition'], fields=fields, evidence=evidence,
        publication_timezone='America/New_York' if spec['provider'] == 'FRED' else 'Asia/Shanghai')


def _number(value):
    if value is None or value == '.': return None
    if type(value) not in {str, int} or type(value) is bool: raise WriterError('INVALID_NUMERIC_VALUE')
    try:
        number = Decimal(value)
        if not number.is_finite() or abs(number) >= Decimal('1e20'): raise ValueError()
        return number
    except (ValueError, InvalidOperation): raise WriterError('INVALID_NUMERIC_VALUE') from None


def _body(body):
    if type(body) is not bytes or not 0 < len(body) <= 16 * 1024 * 1024: raise WriterError('CAPTURE_INVALID')
    return base64.b64encode(body).decode('ascii')


def capture_fred(product):
    """Reuse fred_source's real controlled entry, with original CSV bytes."""
    if product not in {'fred:' + series for series in FRED_PRODUCTS}: raise WriterError('MACRO_PRODUCT_UNREGISTERED')
    from app.services.fred_source import fetch_fred_csv
    return canonical({'format': 'macro-capture-v1', 'product': product, 'kind': 'current_csv',
                      'body_b64': _body(fetch_fred_csv(product[5:], raw=True)), 'release': None}).encode()


def reviewed_release_capture(product, body, *, records, review_evidence, source_uri):
    """Explicit source-document extraction, not automatic HTML/PDF value guessing.

    Each record carries period/frequency/native_unit/value/vintage and publication
    precision. The review hashes the entire extraction and actual supplier body.
    FRED archival CSVs use the separate reviewed-vintage importer; current CSV
    capture above never accepts publication metadata.
    """
    spec = _spec(product)
    if spec['provider'] == 'FRED': raise WriterError('FRED_ARCHIVE_REQUIRES_VINTAGE_RELEASE')
    validate_release_uri(spec['provider'], source_uri)
    if type(review_evidence) is not bytes or not review_evidence: raise WriterError('RELEASE_REVIEW_REQUIRED')
    if type(records) is not list: raise WriterError('CAPTURE_INVALID')
    return canonical({'format': 'macro-capture-v1', 'product': product, 'kind': 'reviewed_release',
        'body_b64': _body(body), 'release': {'source_uri': source_uri, 'records': records,
            'review_b64': _body(review_evidence)}}).encode()


def reviewed_fred_vintage_capture(product, body, *, vintage, publication_precision,
                                  published_at=None, publication_date=None, release_evidence, review_evidence,
                                  release_kind='observation'):
    """Import an actual archived vintage, never infer one from download time.

    Caller must provide the archived CSV AND its vintage release evidence. This
    is an offline reviewed import; no keyed FRED/ALFRED API is introduced.
    """
    if _spec(product)['provider'] != 'FRED': raise WriterError('MACRO_PRODUCT_UNREGISTERED')
    if not isinstance(vintage, str) or not 0 < len(vintage) <= 128: raise WriterError('VINTAGE_REQUIRED')
    release = {'vintage': vintage, 'publication_precision': publication_precision, 'published_at': published_at,
               'publication_date': publication_date, 'release_evidence_b64': _body(release_evidence),
               'review_b64': _body(review_evidence), 'release_kind': release_kind}
    if release_kind not in {'observation', 'withdrawal'}: raise WriterError('MACRO_RELEASE_KIND_INVALID')
    if publication_precision not in {'exact', 'date'}: raise WriterError('VINTAGE_RELEASE_TIME_REQUIRED')
    return canonical({'format': 'macro-capture-v1', 'product': product, 'kind': 'reviewed_vintage',
                      'body_b64': _body(body), 'release': release}).encode()


def _decode(capture, product):
    try:
        if type(capture) is not bytes or not 0 < len(capture) <= 32 * 1024 * 1024: raise ValueError()
        doc = json.loads(capture)
        if set(doc) != {'format', 'product', 'kind', 'body_b64', 'release'} or doc['format'] != 'macro-capture-v1' or doc['product'] != product or canonical(doc).encode() != capture: raise ValueError()
        body = base64.b64decode(doc['body_b64'], validate=True)
        _body(body)
        return doc, body
    except (ValueError, TypeError, KeyError, UnicodeError): raise WriterError('CAPTURE_INVALID') from None


def _period(value, frequency):
    on = day(value)
    if frequency not in {'monthly', 'daily'} or frequency == 'monthly' and on.day != 1:
        raise WriterError('MACRO_PERIOD_FREQUENCY_INVALID')
    return on


def _lag(on, months):
    year, month = divmod(on.year * 12 + on.month - 1 - months, 12)
    if year < 1: return None
    return date(year, month + 1, 1)


def _csv_rows(body, series, transform):
    try:
        reader = csv.DictReader(StringIO(body.decode('utf-8-sig'), newline=''))
        if reader.fieldnames not in [['observation_date', series], ['DATE', series]]: raise ValueError()
        dates = reader.fieldnames[0]; values = {}
        for row in reader:
            if None in row or set(row) != {dates, series}: raise ValueError()
            on = _period(row[dates], 'monthly')
            if on in values: raise WriterError('DUPLICATE_MACRO_PERIOD')
            values[on] = _number(row[series])
        result = []
        for on, value in sorted(values.items()):
            if transform != 'level':
                previous = values.get(_lag(on, 12 if transform == 'yoy' else 1))
                if previous is None or value is None or transform != 'monthly_change_10k' and previous == 0:
                    value = None
                else:
                    with localcontext() as context:
                        context.prec = 80
                        value = (value - previous) / 10 if transform == 'monthly_change_10k' else (value / previous - 1) * 100
            result.append((on, str(value) if value is not None else None))
        return result
    except (ValueError, UnicodeError, csv.Error): raise WriterError('FRED_CSV_SCHEMA_INVALID') from None


def _publication(row, period, zone, evidence):
    time = {'event_date': period.isoformat(), 'publication_precision': row['publication_precision'],
            'published_at': row['published_at'], 'publication_date': row['publication_date'],
            'valid_from': period.isoformat(), 'valid_to': (period + timedelta(days=1)).isoformat(),
            'publication_evidence': evidence if row['publication_precision'] != 'unknown' else None}
    fields = time_fields(time, zone)
    if fields['public_available_at'] is not None and fields['public_available_at'].date() < period:
        raise WriterError('MACRO_RELEASE_BEFORE_PERIOD')
    return time


def _records(capture, product, source_uri, contract):
    spec = _spec(product); doc, body = _decode(capture, product)
    zone = contract.document['publication_timezone']; result = []
    if spec['provider'] == 'FRED':
        uri = urlsplit(source_uri)
        if uri.scheme != 'https' or uri.hostname != 'fred.stlouisfed.org' or uri.path != '/graph/fredgraph.csv' or uri.username or uri.password or uri.port not in {None, 443} or uri.fragment or parse_qs(uri.query) != {'id': [product[5:]]}:
            raise WriterError('FRED_SOURCE_URI_INVALID')
        if doc['kind'] == 'current_csv' and doc['release'] is None:
            release = {'publication_precision': 'unknown', 'published_at': None, 'publication_date': None, 'vintage': None, 'release_kind': 'observation'}
        elif doc['kind'] == 'reviewed_vintage' and type(doc['release']) is dict:
            release = doc['release']
            if set(release) != {'vintage', 'publication_precision', 'published_at', 'publication_date', 'release_evidence_b64', 'review_b64', 'release_kind'} or not isinstance(release['vintage'], str) or not 0 < len(release['vintage']) <= 128 or release['publication_precision'] not in {'exact', 'date'} or release['release_kind'] not in {'observation', 'withdrawal'}:
                raise WriterError('VINTAGE_RELEASE_TIME_REQUIRED')
            try:
                for key in ['release_evidence_b64', 'review_b64']: _body(base64.b64decode(release[key], validate=True))
            except (ValueError, TypeError): raise WriterError('RELEASE_REVIEW_REQUIRED') from None
        else: raise WriterError('FRED_CURRENT_HAS_NO_VINTAGE')
        for on, value in _csv_rows(body, product[5:], spec['transform']):
            result.append((on, value, release['vintage'], _publication(release, on, zone, capture), release['release_kind']))
    else:
        validate_release_uri(spec['provider'], source_uri)
        release = doc['release']
        if doc['kind'] != 'reviewed_release' or type(release) is not dict or set(release) != {'source_uri', 'records', 'review_b64'} or release['source_uri'] != source_uri or type(release['records']) is not list:
            raise WriterError('CAPTURE_INVALID')
        try: _body(base64.b64decode(release['review_b64'], validate=True))
        except (ValueError, TypeError): raise WriterError('RELEASE_REVIEW_REQUIRED') from None
        seen = set()
        for row in release['records']:
            if type(row) is not dict or set(row) != {'period', 'frequency', 'native_unit', 'value', 'vintage', 'publication_precision', 'published_at', 'publication_date', 'release_kind'}:
                raise WriterError('MACRO_RELEASE_FIELDS_INVALID')
            if row['frequency'] != spec['frequency'] or row['native_unit'] != spec['native_unit']:
                raise WriterError('MACRO_FREQUENCY_UNIT_MISMATCH')
            if row['vintage'] is not None and (not isinstance(row['vintage'], str) or not 0 < len(row['vintage']) <= 128): raise WriterError('VINTAGE_REQUIRED')
            if row['release_kind'] not in {'observation', 'withdrawal'}: raise WriterError('MACRO_RELEASE_KIND_INVALID')
            on = _period(row['period'], row['frequency'])
            if on in seen: raise WriterError('DUPLICATE_MACRO_PERIOD')
            seen.add(on)
            value = _number(row['value'])
            if value is not None:
                with localcontext() as context:
                    context.prec = 80; value *= Decimal(spec['scale'])
            result.append((on, str(value) if value is not None else None, row['vintage'], _publication(row, on, zone, capture), row['release_kind']))
    return result


def validate_macro_request(writer, request, payload, doc):
    """Reconstruct byte-bound period, unit, transform and release time at writer.

    This also covers tombstones, so a withdrawal cannot invent a public timestamp
    or attach to a different indicator/period. New candidate macros require the
    frozen M07.6 contract; arbitrary generic profiles cannot bypass this seam.
    """
    product = doc['adapter']['endpoint']
    if product.startswith('public_release:'): product = product.split(':', 1)[1]
    contract = writer.contracts[request['contract_version']]
    if doc != macro_contract(product, evidence=contract.evidence).document: raise WriterError('MACRO_CONTRACT_MISMATCH')
    if request['instrument_version'] is not None or request['calendar_version'] is not None:
        raise WriterError('MACRO_NAMESPACE_INVALID')
    spec = _spec(product)
    records = [r for r in _records(request['raw_response'], product, request['source_uri'], contract)
               if r[0].isoformat() == request['key']['period']]
    if len(records) != 1: raise WriterError('MACRO_PERIOD_UNBOUND')
    on, value, vintage, time, release_kind = records[0]
    expected_key = {'indicator_key': spec['indicator_key'], 'period': on.isoformat(), 'frequency': spec['frequency'], 'definition_id': doc['definition_id']}
    if request['key'] != expected_key or request['time'] != time: raise WriterError('MACRO_RELEASE_UNBOUND')
    if (request['revision']['kind'] == 'withdrawal') != (release_kind == 'withdrawal'):
        raise WriterError('MACRO_WITHDRAWAL_UNBOUND')
    if vintage is not None and request['revision']['token'] != 'vintage:' + vintage or vintage is None and request['revision']['token'] is not None:
        raise WriterError('MACRO_VINTAGE_UNBOUND')
    if request['revision']['kind'] != 'withdrawal':
        wire = {'indicator_key': spec['indicator_key'], 'period': on.isoformat(), 'frequency': spec['frequency'], 'value': value, 'unit': spec['unit']}
        expected = contract.normalize({**request, 'wire_record': wire})
        if payload != expected: raise WriterError('MACRO_VALUE_UNBOUND')


class MacroSyncService:
    def __init__(self, writer): self.writer = writer

    def requests(self, capture, *, product, contract_version, start, end_exclusive, source_uri, batch_id, revisions=None):
        contract = self.writer.contracts.get(contract_version)
        if contract is None: raise WriterError('UNIT_CONTRACT_UNREGISTERED')
        if contract.document != macro_contract(product, evidence=contract.evidence).document: raise WriterError('MACRO_CONTRACT_MISMATCH')
        start, end = day(start), day(end_exclusive)
        if end <= start: raise WriterError('CAPTURE_SCOPE_INVALID')
        revisions = deepcopy(revisions or {}); result = []; spec = _spec(product)
        for on, value, vintage, time, release_kind in _records(capture, product, source_uri, contract):
            if not start <= on < end: continue  # full CSV supplies lag inputs; only requested periods are staged
            revision = revisions.get(on.isoformat(), {'kind': 'original', 'token': 'vintage:' + vintage if vintage else None,
                'order': None, 'order_evidence': None, 'supersedes_version': None})
            wire = {'indicator_key': spec['indicator_key'], 'period': on.isoformat(), 'frequency': spec['frequency'], 'unit': spec['unit'], 'value': value}
            if revision['kind'] == 'withdrawal': wire = {'retraction_reason': 'explicit reviewed source withdrawal'}
            request = {'dataset': 'macro', 'key': {'indicator_key': spec['indicator_key'], 'period': on.isoformat(),
                'frequency': spec['frequency'], 'definition_id': contract.document['definition_id']},
                'contract_version': contract_version, 'profile': 'macro/core', 'instrument_version': None, 'calendar_version': None,
                'wire_record': wire, 'raw_response': capture, 'source_uri': source_uri, 'time': time, 'revision': revision,
                'flags': {}, 'gate_results': {}, 'field_sources': {}, 'batch_id': batch_id}
            self.writer.preview(request); result.append(request)
        if set(revisions) - {r['key']['period'] for r in result}: raise WriterError('UNBOUND_REVISION')
        return result

    def ingest(self, capture, **kwargs):
        requests = self.requests(capture, **kwargs)
        with self.writer.engine.begin() as connection:
            results = [self.writer.write(request, connection=connection) for request in requests]
        return {'status': 'staged' if results else 'empty', 'results': results, 'coverage_complete': False,
                'production_admission': 'not_certified', 'capture_hash': digest(capture)}

    def read_as_of(self, **kwargs): return release_as_of(self.writer, dataset='macro', **kwargs)


def release_as_of(writer, *, dataset, key, as_of, knowledge_mode):
    """Candidate accepted chain only; M08 will bind published snapshot members."""
    if dataset not in {'macro', 'announcement'} or knowledge_mode not in {'public_known', 'system_known'}:
        raise WriterError('ASOF_REQUEST_INVALID')
    cutoff = instant(as_of); logical, _ = logical_identity(dataset, key)
    with writer.engine.connect() as db:
        rows = db.execute(select(schema.acceptance_history).where(schema.acceptance_history.c.dataset == dataset,
            schema.acceptance_history.c.logical_key == logical)).mappings().all()
    versions = {r['data_version'] for r in rows if knowledge_mode == 'public_known' or
                r['system_from'] <= cutoff and (r['system_to'] is None or cutoff < r['system_to'])}
    facts = {version: writer.fact(version) for version in versions}; eligible = set()
    for version, fact in facts.items():
        known = fact['public_available_at'] if knowledge_mode == 'public_known' else fact['available_at']
        if known is not None and known <= cutoff:
            if knowledge_mode == 'public_known': writer.evidence(fact['publication_evidence_hash'])
            eligible.add(version)
        elif knowledge_mode == 'public_known' and known is None and fact['available_at'] is not None and fact['available_at'] <= cutoff:
            eligible.add(version)  # unknown public time blocks only while this is a chain tip
    if not eligible: return {'status': 'missing', 'data_version': None}
    if knowledge_mode == 'system_known' and len(eligible) != 1: raise WriterError('ASOF_ACCEPTANCE_AMBIGUOUS')
    ancestors = set()
    for version in eligible:
        visited = {version}; cursor = facts[version]
        while cursor['supersedes_version'] is not None:
            parent = cursor['supersedes_version']
            if parent in visited: raise WriterError('ASOF_VERSION_AMBIGUOUS')
            visited.add(parent); ancestors.add(parent)
            # Unknown or not-yet-public intermediates establish lineage only;
            # their payload never becomes an eligible public observation.
            if parent not in facts: facts[parent] = writer.fact(parent)
            cursor = facts[parent]
    tips = eligible - ancestors
    if len(tips) != 1: raise WriterError('ASOF_VERSION_AMBIGUOUS')
    fact = facts[tips.pop()]
    if knowledge_mode == 'public_known' and fact['public_available_at'] is None:
        return {'status': 'PUBLICATION_UNKNOWN', 'data_version': None}
    if fact['revision_kind'] == 'withdrawal': return {'status': 'withdrawn', 'data_version': fact['data_version']}
    quality = assess_profile(fact['payload'], dataset + '/core', gate_results=dict.fromkeys(GLOBAL_GATES, 'pass'))
    return {'status': 'pass' if quality.quality_status == 'complete' else 'QUALITY_BLOCKED',
            'data_version': fact['data_version'], 'payload': fact['payload'], 'definition_id': fact['definition_id'],
            'quality_status': quality.quality_status, 'source_tag': fact['source_tag'], 'available_at': fact['available_at']}
