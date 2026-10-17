"""Immutable, checksummed local inputs for replaying a trained candidate.

These artifacts contain already joined model samples, not a copy of the live
warehouse. Shared target refreshes cannot change their feature/label values.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

import duckdb
import numpy as np
import pandas as pd


_SCHEMA_VERSION = 1
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _canonical_samples(samples: pd.DataFrame, feature_versions: dict[str, int]) -> pd.DataFrame:
    features = sorted(feature_versions)
    if not features or any(not isinstance(version, int) or isinstance(version, bool)
                           or version < 1 for version in feature_versions.values()):
        raise ValueError('training_snapshot_invalid_features')
    columns = ['symbol', 'trade_date', *features, 'target', 'exit_date']
    if samples.empty or not set(columns).issubset(samples.columns):
        raise ValueError('training_snapshot_missing_samples')
    result = samples[columns].copy()
    if result.symbol.isna().any():
        raise ValueError('training_snapshot_invalid_symbol')
    result['symbol'] = result.symbol.astype(str)
    if result.symbol.str.strip().eq('').any():
        raise ValueError('training_snapshot_invalid_symbol')
    for column in ('trade_date', 'exit_date'):
        parsed = pd.to_datetime(result[column], errors='coerce')
        if parsed.isna().any():
            raise ValueError('training_snapshot_invalid_date')
        result[column] = parsed.dt.date
    for column in [*features, 'target']:
        result[column] = pd.to_numeric(result[column], errors='coerce').astype('float64')
    if not np.isfinite(result[[*features, 'target']].to_numpy()).all():
        raise ValueError('training_snapshot_non_finite_samples')
    if result.duplicated(['symbol', 'trade_date']).any():
        raise ValueError('training_snapshot_duplicate_samples')
    return result.sort_values(['trade_date', 'symbol']).reset_index(drop=True)


def fingerprint_training_inputs(samples: pd.DataFrame, feature_versions: dict[str, int]) -> str:
    """Identify the exact finite joined window, independent of input row order."""
    frame = _canonical_samples(samples, feature_versions)
    header = json.dumps({'schema_version': _SCHEMA_VERSION,
        'features': feature_versions, 'columns': list(frame.columns)}, sort_keys=True).encode()
    digest = hashlib.sha256(header)
    digest.update(pd.util.hash_pandas_object(frame, index=False).to_numpy(dtype='<u8').tobytes())
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence(path: Path, manifest: dict) -> dict:
    return {'artifact_path': str(path.resolve()),
        'samples_path': str((path.parent/'inputs.parquet').resolve()),
        'sha256': manifest['sha256'], 'input_fingerprint': manifest['input_fingerprint'],
        'row_count': manifest['row_count'], 'schema_version': manifest['schema_version']}


def write_training_input_snapshot(*, artifact_root: Path, model_run_id: str,
    protocol_version: str, samples: pd.DataFrame, feature_versions: dict[str, int],
    split_metadata: dict) -> dict:
    """Create once; never replace a prior model's input or split evidence."""
    if not isinstance(model_run_id, str) or not _RUN_ID.fullmatch(model_run_id):
        raise ValueError('training_snapshot_invalid_model_id')
    if not protocol_version:
        raise ValueError('training_snapshot_missing_protocol')
    frame = _canonical_samples(samples, feature_versions)
    fingerprint = fingerprint_training_inputs(frame, feature_versions)
    split = json.loads(json.dumps(split_metadata, sort_keys=True, default=str, allow_nan=False))
    base = Path(artifact_root).resolve()
    destination = base/model_run_id
    manifest_path = destination/'manifest.json'
    if destination.exists():
        _, existing = load_training_input_snapshot(manifest_path)
        if (existing['model_run_id'] != model_run_id or existing['protocol_version'] != protocol_version
            or existing['input_fingerprint'] != fingerprint or existing['split_metadata'] != split):
            raise ValueError('training_snapshot_immutable_conflict')
        return _evidence(manifest_path, existing)
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f'.{model_run_id}-', dir=base)).resolve()
    try:
        data_path = staging/'inputs.parquet'
        with duckdb.connect(':memory:') as connection:
            connection.register('training_samples', frame)
            connection.execute('COPY training_samples TO ? (FORMAT PARQUET, COMPRESSION ZSTD)',
                               [str(data_path)])
        manifest = {'schema_version': _SCHEMA_VERSION, 'model_run_id': model_run_id,
            'protocol_version': protocol_version, 'feature_versions': feature_versions,
            'row_count': len(frame), 'columns': list(frame.columns),
            'data_file': 'inputs.parquet', 'sha256': _file_sha256(data_path),
            'input_fingerprint': fingerprint, 'split_metadata': split,
            'pandas_version': pd.__version__,
            'created_at': datetime.now(timezone.utc).isoformat()}
        (staging/'manifest.json').write_text(json.dumps(manifest, sort_keys=True,
            ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        # A model record is persisted only after this complete directory exists.
        # Destination collisions fail rather than replacing immutable evidence.
        if destination.exists():
            raise ValueError('training_snapshot_immutable_conflict')
        staging.rename(destination)
        return _evidence(manifest_path, manifest)
    finally:
        if staging.exists():
            assert staging.parent == base and staging.name.startswith(f'.{model_run_id}-')
            shutil.rmtree(staging)


def load_training_input_snapshot(manifest_path: str | Path) -> tuple[pd.DataFrame, dict]:
    """Verify both serialized bytes and canonical sample content before replay."""
    path = Path(manifest_path).resolve()
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if (manifest['schema_version'] != _SCHEMA_VERSION
            or manifest['data_file'] != 'inputs.parquet'
            or manifest['model_run_id'] != path.parent.name):
            raise ValueError('training_snapshot_invalid_manifest')
        data_path = path.parent/'inputs.parquet'
        if _file_sha256(data_path) != manifest['sha256']:
            raise ValueError('training_snapshot_checksum_mismatch')
        with duckdb.connect(':memory:') as connection:
            frame = _canonical_samples(connection.execute('SELECT * FROM read_parquet(?)',
                [str(data_path)]).fetchdf(), manifest['feature_versions'])
        if (len(frame) != manifest['row_count'] or list(frame.columns) != manifest['columns']
            or fingerprint_training_inputs(frame, manifest['feature_versions']) != manifest['input_fingerprint']):
            raise ValueError('training_snapshot_content_mismatch')
        return frame, manifest
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ValueError('training_snapshot_unreadable') from exc
