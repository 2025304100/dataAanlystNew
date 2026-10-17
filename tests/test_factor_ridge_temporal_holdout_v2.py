"""Temporal Ridge contracts through real training and public model reads.

SQLite control data and DuckDB observations are temporary fixtures. No fit,
label selection, gate, or warehouse read is mocked.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import factor_models
from app.db.session import get_db
from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorModelRun, FactorVersion
from app.models.factor_runtime import FactorSystemConfig
from app.services.factors.ridge_model import ModelGate, train_rolling_ridge
from app.services.factors.store import FactorWarehouse


pytestmark = pytest.mark.whitebox


@pytest.fixture
def temporal_inputs(db_session, tmp_path):
    factor = Factor(code="holdout_feature", name="Temporal signal", direction="positive",
                    category="technical", status="active", source_type="manual", frequency="daily")
    db_session.add(factor)
    db_session.flush()
    version = FactorVersion(factor_id=factor.id, version=1, formula_expr="close",
                            direction="positive", params_json="{}", validation_status="valid")
    db_session.add(version)
    db_session.flush()
    frozen = FactorSet(id="temporal-set", name="Temporal fixture", status="frozen", content_hash="temporal-v1")
    db_session.add(frozen)
    db_session.flush()
    db_session.add(FactorSetMember(factor_set_id=frozen.id, factor_id=factor.id,
                                  factor_version_id=version.id, factor_code=factor.code,
                                  factor_version=1, role="feature", missing_policy="exclude"))
    path = tmp_path / "temporal.duckdb"
    db_session.add(FactorSystemConfig(id=1, feature_enabled=1, warehouse_path=str(path),
        extra_json=json.dumps({"gates": {"coverage_threshold": 0.0, "ic_min": 0.0,
                                         "ic_max": 1.01, "min_sample_count": 0}})))
    db_session.commit()
    warehouse = FactorWarehouse(path)
    start, created = date(2025, 1, 1), datetime(2025, 1, 1)
    factors, targets = [], []
    for day in range(80):
        signal = start + timedelta(days=day)
        for index in range(8):
            symbol = f"{index + 1:06d}"
            value = (index - 3.5) / 2 + .003 * day
            factors.append(dict(symbol=symbol, trade_date=signal, factor_code=factor.code,
                factor_version=1, raw_value=value, winsorized_value=value, normalized_value=value,
                is_imputed=False, imputation_method=None, eligible=True,
                data_cutoff_at=created, calc_batch_id="temporal-factors", created_at=created))
            targets.append(dict(symbol=symbol, signal_date=signal, entry_date=signal + timedelta(days=1),
                exit_date=signal + timedelta(days=5), target_code="target_5d_return",
                target_value=.025 * value + .00001 * day * index, is_tradable=True,
                invalid_reason=None, calc_batch_id="temporal-targets", created_at=created))
    warehouse.upsert_frame("factor_values", pd.DataFrame(factors))
    warehouse.upsert_frame("factor_targets", pd.DataFrame(targets))
    # Coverage is measured against the market-input universe, not only rows
    # surviving a complete-case label join.
    bars = [dict(symbol=row["symbol"], trade_date=row["trade_date"], adjust="qfq",
                 close=10.0, source="fixture", source_origin="local", source_row_id=index + 1,
                 ingested_at=created, batch_id="temporal-bars") for index, row in enumerate(factors)]
    warehouse.upsert_daily_bars(bars, source_key="temporal-bars", watermark=len(bars))
    app = FastAPI()
    app.include_router(factor_models.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield warehouse, start, client


def train(db, warehouse, start, **overrides):
    gate = overrides.pop("gate", ModelGate(minimum_samples=100, minimum_symbols=4,
        minimum_trade_dates=20, minimum_validation_dates=5, minimum_validation_ic=0.0))
    return train_rolling_ridge(db, warehouse, factor_calc_batch_id="temporal-factors",
        target_calc_batch_id="temporal-targets", factor_set_id="temporal-set",
        data_cutoff_date=start + timedelta(days=90), window_days=70, validation_days=15,
        gate=gate, **overrides)


def read(client, model_id):
    response = client.get(f"/api/v1/factor-models/{model_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_outer_labels_cannot_select_alpha_or_change_saved_coefficients(db_session, temporal_inputs):
    warehouse, start, client = temporal_inputs
    original = train(db_session, warehouse, start, model_run_id="original-outer-labels")
    before = read(client, original.model_run_id)
    # Last 15 dates are the reserved outer holdout. Inverting only those
    # outcomes must alter validation, never the alpha or fitted artifact.
    with warehouse.connection() as conn:
        conn.execute("""UPDATE factor_targets SET target_value=-50*target_value
            WHERE calc_batch_id='temporal-targets' AND signal_date>=?""",
            [start + timedelta(days=65)])
    changed = train(db_session, warehouse, start, model_run_id="perturbed-outer-labels")
    after = read(client, changed.model_run_id)
    assert original.selected_alpha == changed.selected_alpha
    assert [row["coefficient"] for row in before["weights"]] == [row["coefficient"] for row in after["weights"]]
    assert before["hyperparameters"]["final_intercept"] == after["hyperparameters"]["final_intercept"]
    assert before["hyperparameters"]["training_gate_evidence"] == after["hyperparameters"]["training_gate_evidence"]
    assert before["metrics"]["validation_ic"] > 0 > after["metrics"]["validation_ic"]


def test_saved_model_reproduces_holdout_metrics_and_has_double_purge_counts(db_session, temporal_inputs):
    import numpy as np
    from sklearn.metrics import mean_squared_error

    warehouse, start, client = temporal_inputs
    result = train(db_session, warehouse, start)
    model = read(client, result.model_run_id)
    hp, metrics = model["hyperparameters"], model["metrics"]
    # 70 days × 8 names, last 15 for holdout; equality at either exit boundary
    # removes five full dates. Inner tuning is floor(50 * .2) = 10 dates.
    assert metrics["total_sample_count"] == 560
    assert metrics["training_sample_count"] == 400
    assert metrics["validation_sample_count"] == 120
    assert metrics["outer_purged_sample_count"] == 40
    assert metrics["inner_training_sample_count"] == 280
    assert metrics["tuning_sample_count"] == 80
    assert metrics["inner_purged_sample_count"] == 40
    assert metrics["tuning_start_date"] == (start + timedelta(days=50)).isoformat()
    assert metrics["inner_training_end_date"] == (start + timedelta(days=44)).isoformat()
    assert hp["protocol_version"] == "ridge-temporal-holdout-v2"
    assert hp["coefficient_stage"] == "outer_training_purged"
    assert hp["coefficient_storage_dtype"] == "float32"
    assert hp["coefficient_readback_significant_digits"] == 6
    coefficient = model["weights"][0]["coefficient"]
    assert coefficient == float(format(float(np.float32(coefficient)), ".6g"))
    with warehouse.connection(read_only=True) as conn:
        holdout = conn.execute("""SELECT f.normalized_value, t.target_value
            FROM factor_values f JOIN factor_targets t
            ON f.symbol=t.symbol AND f.trade_date=t.signal_date
            WHERE f.calc_batch_id='temporal-factors' AND t.calc_batch_id='temporal-targets'
            AND f.trade_date>=? ORDER BY f.trade_date, f.symbol""", [start + timedelta(days=65)]).fetchdf()
    prediction = holdout.normalized_value * coefficient + hp["final_intercept"]
    assert metrics["validation_ic"] == pytest.approx(prediction.corr(holdout.target_value, method="spearman"), abs=1e-14)
    assert metrics["validation_mse"] == pytest.approx(mean_squared_error(holdout.target_value, prediction), rel=1e-14)
    assert metrics["validation_ic_method"] == "pooled_spearman"
    assert metrics["validation_daily_ic_count"] == 15
    evidence = hp["training_gate_evidence"]["holdout_feature"]
    assert evidence["n_rows"] == 400
    assert evidence["start_date"] == (start + timedelta(days=10)).isoformat()
    assert evidence["end_date"] == (start + timedelta(days=59)).isoformat()


def test_actual_fit_sample_gate_cannot_be_satisfied_by_holdout_or_purged_rows(db_session, temporal_inputs):
    warehouse, start, client = temporal_inputs
    result = train_rolling_ridge(db_session, warehouse, factor_calc_batch_id="temporal-factors",
        target_calc_batch_id="temporal-targets", factor_set_id="temporal-set",
        data_cutoff_date=start + timedelta(days=90), window_days=70, validation_days=15,
        gate=ModelGate(minimum_samples=401, minimum_symbols=4, minimum_trade_dates=20,
                       minimum_validation_dates=5, minimum_validation_ic=0.0))
    model = read(client, result.model_run_id)
    assert result.sample_count == model["sample_count"] == 560
    assert model["metrics"]["training_sample_count"] == 400
    assert result.status == "rejected"
    assert "sample_count:400<401" in result.rejection_reasons
    assert model["activated_at"] is None


def test_protocol_change_never_reuses_or_overwrites_an_old_run(db_session, temporal_inputs):
    import hashlib

    warehouse, start, client = temporal_inputs
    identity = dict(mode="warehouse", train_mode="warehouse", factor_calc_batch_id="temporal-factors",
                    target_calc_batch_id="temporal-targets", factor_set_id="temporal-set",
                    data_cutoff_date=start + timedelta(days=90), window_days=70, validation_days=15,
                    alphas=(0.1, 1.0, 10.0, 100.0), features=("holdout_feature",))
    legacy_id = "ridge-" + hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:24]
    db_session.add(FactorModelRun(id=legacy_id, status="rejected", hyperparameters_json=json.dumps(identity, default=str),
                                 metrics_json='{"validation_ic":-0.123}', sample_count=99))
    db_session.commit()
    before = read(client, legacy_id)
    result = train(db_session, warehouse, start)
    assert not result.reused and result.model_run_id != legacy_id
    assert read(client, legacy_id) == before
    again = train(db_session, warehouse, start)
    assert again.reused and again.model_run_id == result.model_run_id
    with pytest.raises(ValueError, match="model_run_protocol_conflict"):
        train(db_session, warehouse, start, model_run_id=legacy_id)
    assert read(client, legacy_id) == before


def test_same_batch_changed_labels_create_new_identity_and_explicit_id_cannot_alias_inputs(db_session, temporal_inputs):
    warehouse, start, client = temporal_inputs
    result = train(db_session, warehouse, start)
    before = read(client, result.model_run_id)
    with warehouse.connection() as conn:
        conn.execute("""UPDATE factor_targets SET target_value=-target_value
            WHERE calc_batch_id='temporal-targets' AND signal_date>=?""", [start + timedelta(days=65)])
    updated = train(db_session, warehouse, start)
    assert updated.model_run_id != result.model_run_id and not updated.reused
    assert read(client, result.model_run_id) == before
    with pytest.raises(ValueError, match="model_run_input_conflict"):
        train(db_session, warehouse, start, model_run_id=result.model_run_id)
    assert read(client, result.model_run_id) == before


def test_legacy_non_finite_feature_or_target_never_enters_training(db_session, temporal_inputs):
    warehouse, start, client = temporal_inputs
    with warehouse.connection() as conn:
        conn.execute("UPDATE factor_values SET normalized_value='Infinity'::DOUBLE WHERE symbol='000001' AND trade_date=?",
                     [start + timedelta(days=20)])
        conn.execute("UPDATE factor_targets SET target_value='Infinity'::DOUBLE WHERE symbol='000002' AND signal_date=?",
                     [start + timedelta(days=25)])
    result = train(db_session, warehouse, start)
    model = read(client, result.model_run_id)
    assert model["sample_count"] == 558
    assert model["metrics"]["training_sample_count"] == 398
    assert model["metrics"]["inner_training_sample_count"] == 278
    assert model["hyperparameters"]["training_gate_evidence"]["holdout_feature"]["n_rows"] == 398
    assert result.coefficients and result.validation_ic is not None


@pytest.mark.parametrize("damage", ["deleted", "tampered", "metadata_mismatch", "missing_fitted_artifact"])
def test_fitted_candidate_reuse_requires_its_verified_immutable_snapshot(db_session, temporal_inputs, damage):
    from pathlib import Path

    warehouse, start, client = temporal_inputs
    result = train(db_session, warehouse, start)
    detail = read(client, result.model_run_id)
    manifest_path = Path(detail["artifact_path"])
    if damage == "deleted":
        manifest_path.unlink()
    elif damage == "tampered":
        with (manifest_path.parent / "inputs.parquet").open("ab") as handle:
            handle.write(b"corrupted")
    else:
        model = db_session.get(FactorModelRun, result.model_run_id)
        hp = json.loads(model.hyperparameters_json)
        if damage == "metadata_mismatch":
            hp["input_snapshot"]["input_fingerprint"] = "different-input-fingerprint"
        else:
            hp["input_snapshot"] = None
            model.artifact_path = None
        model.hyperparameters_json = json.dumps(hp)
        db_session.commit()
    preserved = read(client, result.model_run_id)
    with pytest.raises(ValueError, match="model_run_snapshot_invalid"):
        train(db_session, warehouse, start)
    assert read(client, result.model_run_id) == preserved


@pytest.mark.parametrize("configuration", ["model_gate", "governance_gate"])
def test_changed_release_gate_creates_new_candidate_and_cannot_reuse_explicit_run_id(
    db_session, temporal_inputs, configuration,
):
    warehouse, start, client = temporal_inputs
    original = train(db_session, warehouse, start)
    before = read(client, original.model_run_id)
    kwargs = {}
    if configuration == "model_gate":
        kwargs["gate"] = ModelGate(minimum_samples=401, minimum_symbols=4, minimum_trade_dates=20,
                                  minimum_validation_dates=5, minimum_validation_ic=0.0)
    else:
        config = db_session.get(FactorSystemConfig, 1)
        values = json.loads(config.extra_json)
        values["gates"]["min_sample_count"] = 401
        config.extra_json = json.dumps(values)
        db_session.commit()
    updated = train(db_session, warehouse, start, **kwargs)
    assert not updated.reused and updated.model_run_id != original.model_run_id
    assert updated.status == "rejected"
    assert "sample_count:400<401" in ";".join(updated.rejection_reasons)
    with pytest.raises(ValueError, match="model_run_identity_conflict"):
        train(db_session, warehouse, start, model_run_id=original.model_run_id, **kwargs)
    assert read(client, original.model_run_id) == before


def test_pre_admission_rejected_candidate_without_a_fit_can_be_reused_without_artifact(db_session, temporal_inputs):
    warehouse, start, client = temporal_inputs
    config = db_session.get(FactorSystemConfig, 1)
    values = json.loads(config.extra_json)
    values["gates"]["ic_min"] = 1.1
    config.extra_json = json.dumps(values)
    db_session.commit()
    rejected = train(db_session, warehouse, start)
    before = read(client, rejected.model_run_id)
    assert rejected.status == "rejected" and not rejected.coefficients
    assert before["artifact_path"] is None and before["hyperparameters"]["input_snapshot"] is None
    reused = train(db_session, warehouse, start)
    assert reused.reused and reused.model_run_id == rejected.model_run_id
    assert read(client, rejected.model_run_id) == before


def test_model_gate_uses_the_production_float_text_readback_coefficient(db_session, temporal_inputs):
    warehouse, start, client = temporal_inputs
    # The outer fit is 50 days × 8 symbols. Independently worked centred Sxx:
    # 50 * (sum((index-3.5)^2)/4) + 8 * .003^2 * sum((day-34.5)^2)
    # = 525 + .7497. Compensating the known single alpha gives this raw slope.
    raw_coefficient = 1.782201968596827e-5
    target_slope = raw_coefficient * (1 + 0.1 / 525.7497)
    with warehouse.connection() as conn:
        conn.execute("""UPDATE factor_targets t SET target_value=f.normalized_value * ?
            FROM factor_values f WHERE t.symbol=f.symbol AND t.signal_date=f.trade_date
            AND t.calc_batch_id='temporal-targets' AND f.calc_batch_id='temporal-factors'""", [target_slope])
    result = train(db_session, warehouse, start, alphas=(0.1,))
    model = read(client, result.model_run_id)
    # MySQL FLOAT's direct text protocol returns six significant digits,
    # whereas coefficient+0e0 exposes a different binary32-as-double number.
    assert result.coefficients["holdout_feature"] == model["weights"][0]["coefficient"] == 1.7822e-5
    assert model["hyperparameters"]["coefficient_storage_dtype"] == "float32"
    assert model["hyperparameters"]["coefficient_readback_significant_digits"] == 6
    with warehouse.connection(read_only=True) as conn:
        holdout = conn.execute("""SELECT f.normalized_value, t.target_value
            FROM factor_values f JOIN factor_targets t ON f.symbol=t.symbol AND f.trade_date=t.signal_date
            WHERE f.calc_batch_id='temporal-factors' AND t.calc_batch_id='temporal-targets'
            AND f.trade_date>=? ORDER BY f.trade_date, f.symbol""", [start + timedelta(days=65)]).fetchdf()
    prediction = holdout.normalized_value * model["weights"][0]["coefficient"] + model["hyperparameters"]["final_intercept"]
    assert model["metrics"]["validation_ic"] == pytest.approx(prediction.corr(holdout.target_value, method="spearman"), abs=1e-14)
    assert model["metrics"]["validation_mse"] == pytest.approx(((holdout.target_value-prediction)**2).mean(), rel=1e-14)
