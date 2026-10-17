"""Stable identity for a single reviewed research submission, not a new fit."""
from __future__ import annotations
import hashlib
import json
import re


def research_request_identity(model_id: str, request_id: str, manifest_sha256: str, note: str):
    if (not isinstance(model_id, str) or not model_id or len(model_id) > 64
            or not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id)
            or not isinstance(manifest_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', manifest_sha256)
            or not isinstance(note, str) or len(note) > 300):
        raise ValueError('research_request_identity_invalid')
    task_id = hashlib.sha256(('research-retrain:' + model_id + ':' + request_id).encode()).hexdigest()[:32]
    fingerprint = hashlib.sha256(json.dumps({'model_id': model_id, 'request_id': request_id,
        'manifest_sha256': manifest_sha256, 'note': note.strip()}, sort_keys=True).encode()).hexdigest()
    return task_id, fingerprint


def pipeline_research_request(payload):
    hp = payload.hyperparameters_json or {}
    request_id = hp.get('research_retrain_request_id')
    if request_id is None:
        return None
    if (not payload.trusted_history_dataset_id or not payload.train_model or payload.materialize_scores
            or payload.source_hint != 'factor_center:research_retrain'):
        raise ValueError('research_request_requires_isolated_retraining')
    return research_request_identity(hp.get('source_model_id'), request_id,
        hp.get('expected_trusted_history_manifest_sha256'), hp.get('note', ''))


def validate_research_task(task, model_id: str, request_id: str):
    """GET recovery also verifies context; never expose an unrelated task."""
    try:
        payload = json.loads(task.payload_json or '{}')
        hp = payload['hyperparameters_json']
        if (not payload.get('trusted_history_dataset_id') or payload.get('train_model') is not True
                or payload.get('materialize_scores') is not False or payload.get('mirror_only') is not False
                or payload.get('source_hint') != 'factor_center:research_retrain'):
            raise ValueError('invalid_research_context')
        expected_id, fingerprint = research_request_identity(model_id, request_id,
            hp['expected_trusted_history_manifest_sha256'], hp.get('note', ''))
        if (task.id != expected_id or task.task_type != 'factor_pipeline'
                or hp['source_model_id'] != model_id or hp['research_retrain_request_id'] != request_id
                or task.idempotency_key != 'request:' + fingerprint):
            raise ValueError('invalid_research_identity')
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('research_request_task_context_conflict') from None
