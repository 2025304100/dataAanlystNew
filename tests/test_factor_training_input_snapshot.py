"""The public model record can replay inputs after shared labels change."""
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import factor_models
from app.db.session import get_db
from app.services.factors.ridge_model import ModelGate, train_rolling_ridge
from tests.test_factor_ridge_session_lifecycle import _committed_training_inputs

pytestmark = pytest.mark.whitebox


def test_model_detail_replays_exact_inputs_after_shared_labels_change(db_session, tmp_path):
    warehouse, start = _committed_training_inputs(db_session, tmp_path)
    result = train_rolling_ridge(db_session, warehouse,
        factor_calc_batch_id="factor-train", target_calc_batch_id="target-train",
        factor_set_id="caller-transaction-set", data_cutoff_date=start+timedelta(days=90),
        window_days=70, validation_days=15, model_run_id="snapshot-public-model",
        gate=ModelGate(minimum_samples=100, minimum_symbols=5, minimum_trade_dates=30,
            minimum_validation_dates=10, minimum_validation_ic=0))
    db_session.commit()
    app = FastAPI()
    app.include_router(factor_models.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        response = client.get('/api/v1/factor-models/'+result.model_run_id)
        assert response.status_code == 200, response.text
        before = response.json()
        assert before["artifact_path"], "A new model needs immutable replayable training inputs."
        from app.services.factors.training_input_snapshot import load_training_input_snapshot
        samples, manifest = load_training_input_snapshot(before["artifact_path"])
        assert len(samples) == 700
        assert samples.iloc[0]["symbol"] == "000001"
        assert samples.iloc[0]["target"] == pytest.approx(-.058)
        assert manifest['row_count'] == before['sample_count']
        assert manifest['protocol_version'] == 'ridge-temporal-holdout-v2'
        # Change the original shared label through the existing warehouse API.
        row = samples.iloc[0]
        warehouse.upsert_records('factor_targets', [{
            'symbol':row.symbol, 'signal_date':row.trade_date,
            'entry_date':row.trade_date+timedelta(days=1), 'exit_date':row.exit_date,
            'target_code':'target_5d_return', 'target_value':9., 'is_tradable':True,
            'invalid_reason':None, 'calc_batch_id':'target-train',
            'created_at':datetime(2026, 10, 16),
        }])
        reloaded, same_manifest = load_training_input_snapshot(before['artifact_path'])
        pd.testing.assert_frame_equal(reloaded, samples)
        assert same_manifest == manifest
        assert client.get('/api/v1/factor-models/'+result.model_run_id).json() == before
        assert Path(before['artifact_path']).resolve().is_relative_to(tmp_path.resolve())


def _literal_snapshot(tmp_path):
    from app.services.factors.training_input_snapshot import write_training_input_snapshot
    return write_training_input_snapshot(artifact_root=tmp_path/'inputs',
        model_run_id='literal-candidate', protocol_version='ridge-temporal-holdout-v2',
        samples=pd.DataFrame([{'symbol':'000001','trade_date':date(2026, 7, 1),
            'ep_ttm':.125,'target':-.03125,'exit_date':date(2026, 7, 8)}]),
        feature_versions={'ep_ttm':1}, split_metadata={'training_sample_count':1})


def test_replay_rejects_changed_parquet_bytes(tmp_path):
    from app.services.factors.training_input_snapshot import load_training_input_snapshot
    evidence = _literal_snapshot(tmp_path)
    frame, _ = load_training_input_snapshot(evidence['artifact_path'])
    assert frame.target.tolist() == [-.03125]
    with Path(evidence['samples_path']).open('ab') as handle:
        handle.write(b'corrupted bytes')
    with pytest.raises(ValueError, match='checksum_mismatch'):
        load_training_input_snapshot(evidence['artifact_path'])


def test_existing_candidate_input_cannot_be_replaced(tmp_path):
    from app.services.factors.training_input_snapshot import (
        load_training_input_snapshot, write_training_input_snapshot)
    evidence = _literal_snapshot(tmp_path)
    frame, _ = load_training_input_snapshot(evidence['artifact_path'])
    frame.loc[0, 'target'] = 9.
    with pytest.raises(ValueError, match='immutable_conflict'):
        write_training_input_snapshot(artifact_root=tmp_path/'inputs',
            model_run_id='literal-candidate', protocol_version='ridge-temporal-holdout-v2',
            samples=frame, feature_versions={'ep_ttm':1},
            split_metadata={'training_sample_count':1})
    original, _ = load_training_input_snapshot(evidence['artifact_path'])
    assert original.target.tolist() == [-.03125]
