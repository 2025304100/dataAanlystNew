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
    if 'etf_training_request_id' in hp or payload.source_hint == 'factor_center:etf_train':
        request = etf_request_from_payload(payload.model_dump())
        return etf_request_identity(request)
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


def etf_request_identity(request: dict):
    request_id = request.get('request_id')
    if (not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id)
            or not isinstance(request.get('factor_set_id'), str) or not 1 <= len(request['factor_set_id']) <= 64
            or not isinstance(request.get('dataset_id'), str)
            or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', request['dataset_id'])
            or not isinstance(request.get('manifest_sha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', request['manifest_sha256'])
            or not isinstance(request.get('note'), str) or len(request['note']) > 300):
        raise ValueError('ETF训练记录不完整，请重新读取这次训练。')
    task_id = etf_request_task_id(request_id)
    fingerprint = hashlib.sha256(json.dumps({**request, 'note': request['note'].strip()}, sort_keys=True).encode()).hexdigest()
    return task_id, fingerprint


def etf_request_task_id(request_id: str):
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise ValueError('ETF训练编号不正确，请重新读取这次训练。')
    return hashlib.sha256(('etf-train:' + request_id).encode()).hexdigest()[:32]


def etf_request_from_payload(payload: dict):
    hp = payload.get('hyperparameters_json') or {}
    factor_set_id = payload.get('factor_set_id')
    if (payload.get('train_model') is not True or payload.get('materialize_scores') is not False
            or payload.get('mirror_only') is not False or payload.get('universe_source') is not None
            or payload.get('source_hint') != 'factor_center:etf_train'
            or payload.get('window_days') != 250 or payload.get('validation_days') != 50
            or hp.get('factor_set_id') != factor_set_id
            or payload.get('factor_scope_json') != {'factor_set_id': factor_set_id, 'asset_type': 'etf'}
            or set(hp) - {'factor_set_id', 'note', 'expected_trusted_history_manifest_sha256',
                         'etf_training_request_id', 'fit_policy', 'trusted_history_dataset',
                         'research_evaluation_status'}
            or hp.get('fit_policy', 'cross_sectional_rank_v1') != 'cross_sectional_rank_v1'):
        raise ValueError('ETF训练参数与原记录不一致，本次未启动训练。')
    request = {'request_id': hp.get('etf_training_request_id'), 'factor_set_id': factor_set_id,
               'dataset_id': payload.get('trusted_history_dataset_id'),
               'manifest_sha256': hp.get('expected_trusted_history_manifest_sha256'),
               'note': hp.get('note', '').strip()}
    etf_request_identity(request)
    return request


def validate_etf_training_task(task, request_id: str):
    """Recover only a task whose saved request and fingerprint agree."""
    try:
        request = etf_request_from_payload(json.loads(task.payload_json or '{}'))
        task_id, fingerprint = etf_request_identity(request)
        if (request['request_id'] != request_id or task.id != task_id
                or task.task_type != 'factor_pipeline' or task.idempotency_key != 'request:' + fingerprint):
            raise ValueError('invalid_etf_identity')
        return request
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('ETF训练记录核对失败，请保留当前记录，核实后再训练。') from None
