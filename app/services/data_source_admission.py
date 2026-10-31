"""ADMIT-01 evaluation. Declarations and successful probes are never approvals."""
import hashlib
import json
import math
from pathlib import Path

CATALOG = Path(__file__).with_name('data_source_contract_catalog.json')
ROOT = Path(__file__).resolve().parents[2]


def prepare(record):
    fields = {'implementation_status', 'auth_required', 'auth_status', 'permission_proof_id',
              'permission_valid_until', 'admission_status', 'production_enabled', 'capacity_ok', 'valid_until', 'evidence'}
    if not isinstance(record, dict) or set(record) != fields:
        raise ValueError('Explicit ADMIT-01 fields required')
    value = json.loads(json.dumps(record, allow_nan=False))
    for name in ('auth_required', 'production_enabled', 'capacity_ok'):
        if type(value[name]) is not bool:
            raise ValueError(name)
    for name in ('valid_until', 'permission_valid_until'):
        number = value[name]
        if number is None and name == 'permission_valid_until':
            continue
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError(name)
    if not isinstance(value['evidence'], dict) or set(value['evidence']) != {'contract', 'control', 'review'}:
        raise ValueError('contract/control/review evidence required')
    for ref in value['evidence'].values():
        if not isinstance(ref, dict) or set(ref) != {'path', 'sha256'} or not isinstance(ref['path'], str):
            raise ValueError('Evidence path and sha256 required')
        ref['path'] = str(Path(ref['path']).resolve())
    value['catalog_sha256'] = hashlib.sha256(CATALOG.read_bytes()).hexdigest()
    return value


def decision(endpoint, publication, now):
    """Return reasons on every actual admission; missing/changed evidence closes it."""
    reasons = []
    item = next((i for i in json.loads(CATALOG.read_text(encoding='utf-8'))['items'] if i['endpoint'] == endpoint), None)
    if not item or not item['in_current_release'] or item['access_status'] != 'available_now' or item['implementation_status'] != 'implemented':
        return ['OUT_OF_RELEASE_OR_NOT_IMPLEMENTED']
    if not publication:
        return ['ADMISSION_NOT_REVIEWED']
    value = publication['record']
    for field, expected in (('implementation_status', 'validated'), ('admission_status', 'ready'),
                            ('production_enabled', True), ('capacity_ok', True)):
        if value.get(field) != expected or (expected is True and type(value.get(field)) is not bool):
            reasons.append(field.upper())
    if value.get('valid_until', 0) <= now:
        reasons.append('ADMISSION_EXPIRED')
    if value.get('catalog_sha256') != hashlib.sha256(CATALOG.read_bytes()).hexdigest():
        reasons.append('CATALOG_CHANGED')
    required = item['auth_required']
    if value.get('auth_required') is not required:
        reasons.append('AUTH_REQUIREMENT_MISMATCH')
    if required:
        proof = value.get('permission_proof_id')
        if (value.get('auth_status') != 'verified' or not isinstance(proof, str) or not proof.strip()
                or (value.get('permission_valid_until') or 0) <= now):
            reasons.append('PERMISSION_NOT_VALID')
    elif value.get('auth_status') != 'not_required' or value.get('permission_proof_id') is not None or value.get('permission_valid_until') is not None:
        reasons.append('PUBLIC_AUTH_STATE_MISMATCH')
    for role in ('contract', 'control', 'review'):
        ref = value.get('evidence', {}).get(role, {})
        try:
            content = Path(ref['path']).read_bytes()
            if hashlib.sha256(content).hexdigest() != ref['sha256']:
                reasons.append(role.upper() + '_EVIDENCE_CHANGED')
                continue
            receipt = json.loads(content)
            if (receipt.get('endpoint') != endpoint or receipt.get('kind') != role or receipt.get('status') != 'pass'
                    or receipt.get('package_version') != '1.4.4' or not isinstance(receipt.get('reviewer_id'), str)
                    or not receipt['reviewer_id'].strip()):
                reasons.append(role.upper() + '_EVIDENCE_SCOPE_MISMATCH')
            hashes = receipt.get('source_hashes')
            if not isinstance(hashes, dict) or not hashes:
                reasons.append(role.upper() + '_SOURCE_BINDING_MISSING')
                continue
            for name, expected_hash in hashes.items():
                source = (ROOT / name).resolve()
                if not source.is_relative_to(ROOT / 'app') or hashlib.sha256(source.read_bytes()).hexdigest() != expected_hash:
                    reasons.append(role.upper() + '_SOURCE_CHANGED')
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            reasons.append(role.upper() + '_EVIDENCE_MISSING')
    return reasons
