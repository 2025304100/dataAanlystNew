"""Real isolated factor data, version, training artifact and publication gates."""
from datetime import date, datetime, timedelta
import json

import pytest
from sqlalchemy import event, select

pytest.importorskip("duckdb")
pytest.importorskip("sklearn")

from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorModelRun, FactorVersion
from app.models.factor_governance import FactorModelMember
from app.models.factor_weight_snapshot import FactorWeightSnapshot
from app.models.score import Score
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.factors.definitions import seed_factor_definitions, _PARAMS, _SOURCE_MAPPINGS, FACTOR_BY_CODE
from app.services.factors.factor_engine import calculate_stock_factors
from app.services.factors.pipeline_task import _complete_calculation_dates, _qualified_factor_dates
from app.services.factors.ridge_model import _load_samples, train_rolling_ridge, ModelGate
from app.services.factors.scoring_bridge import materialize_factor_scores, materialize_factor_score_window
from app.services.factors.store import FactorWarehouse
from tests.test_whitebox_factor_engine import _bar
from tests.test_whitebox_ridge_model import _seed_training_data
from tests.test_whitebox_factor_scoring_bridge import _seed_bridge_inputs, _TRADE_DATE

pytestmark = pytest.mark.whitebox


def _upsert_bars(warehouse, rows):
    warehouse.upsert_daily_bars(rows, source_key="test.bars", watermark=9999)


def _frozen_turnover(db, *, formula=None, params=None, source=None):
    seed_factor_definitions(db)
    factor = db.scalar(select(Factor).where(Factor.code == "turnover_z20"))
    version = FactorVersion(
        factor_id=factor.id, version=2,
        formula_expr=formula or FACTOR_BY_CODE["turnover_z20"].formula,
        params_json=json.dumps(_PARAMS["turnover_z20"] if params is None else params),
        source_mapping_json=json.dumps(_SOURCE_MAPPINGS["turnover_z20"] if source is None else source),
        direction="nonlinear", validation_status="valid",
    )
    db.add(version)
    db.flush()
    factor_set = FactorSet(id="frozen-turnover-v2", name="Frozen", status="frozen", content_hash="test-hash")
    db.add(factor_set)
    db.flush()
    db.add(FactorSetMember(
        factor_set_id=factor_set.id, factor_id=factor.id, factor_version_id=version.id,
        factor_code="turnover_z20", factor_version=2, role="feature", missing_policy="exclude",
    ))
    db.flush()
    return factor_set


def test_frozen_version_is_computed_and_does_not_masquerade_as_builtin_v1(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "versions.duckdb")
    factor_set = _frozen_turnover(db_session)
    start = date(2026, 1, 1)
    _upsert_bars(warehouse, [
        _bar(symbol, start + timedelta(days=day), day * 10 + index,
             turnover_rate=day + index + 1)
        for day in range(22) for index, symbol in enumerate(["000001", "000002", "000003"])
    ])
    result = calculate_stock_factors(warehouse, db=db_session, factor_set_ids=(factor_set.id,),
                                     start_date=start, end_date=start + timedelta(days=21))
    with warehouse.connection(read_only=True) as conn:
        rows = conn.execute("""SELECT factor_version, raw_value FROM factor_values
            WHERE calc_batch_id=? AND factor_code='turnover_z20' AND symbol='000001'
            AND trade_date=? ORDER BY factor_version""", [result.calc_batch_id, start + timedelta(days=21)]).fetchall()
    assert [row[0] for row in rows] == [1, 2]
    assert rows[0][1] == pytest.approx(rows[1][1])


def test_changed_frozen_formula_uses_dsl_instead_of_old_builtin(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "dsl.duckdb")
    factor_set = _frozen_turnover(db_session, formula="close", params={},
                                 source={"table": "raw_daily_bars", "field": "close"})
    day = date(2026, 1, 5)
    _upsert_bars(warehouse, [_bar("000001", day, 1), _bar("000002", day, 2)])
    result = calculate_stock_factors(warehouse, db=db_session, factor_set_ids=(factor_set.id,),
                                     start_date=day, end_date=day)
    with warehouse.connection(read_only=True) as conn:
        values = conn.execute("""SELECT raw_value FROM factor_values
            WHERE calc_batch_id=? AND factor_code='turnover_z20' AND factor_version=2""",
                              [result.calc_batch_id]).fetchall()
    assert values == [(10.0,), (10.0,)]


def test_unsupported_frozen_formula_fails_without_publishing_a_batch(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "unsupported.duckdb")
    factor_set = _frozen_turnover(db_session, formula="unknown_private_input + 1")
    day = date(2026, 1, 5)
    _upsert_bars(warehouse, [_bar("000001", day, 1)])
    with pytest.raises(ValueError, match="unsupported_frozen_factor"):
        calculate_stock_factors(warehouse, db=db_session, factor_set_ids=(factor_set.id,), calc_batch_id="blocked")
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM factor_values WHERE calc_batch_id='blocked'").fetchone()[0] == 0


def test_train_and_score_refuse_v1_values_for_a_v2_snapshot(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "wrong_version.duckdb")
    _seed_training_data(warehouse, trade_dates=8, symbols=2)
    with pytest.raises(ValueError, match="factor_value_version_mismatch:turnover_z20"):
        _load_samples(warehouse, factor_calc_batch_id="factor-train", target_calc_batch_id="target-train",
                      data_cutoff_date=date(2026, 1, 1), feature_codes=("turnover_z20",),
                      feature_versions={"turnover_z20": 2})
    _, _, model = _seed_bridge_inputs(db_session, warehouse)
    for member in model.weights:
        if member.factor_code == "turnover_z20":
            member.factor_version = 2
    db_session.flush()
    with pytest.raises(ValueError, match="factor_value_version_mismatch:turnover_z20"):
        materialize_factor_scores(db_session, warehouse, trade_date=_TRADE_DATE,
                                  factor_calc_batch_id="factor-score-test", model_run_id=model.id, mode="ridge")
    assert db_session.scalars(select(Score).where(Score.weight_mode == "ridge")).all() == []


def test_partial_latest_bar_day_is_skipped_and_mirror_lag_is_detected(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "partial.duckdb")
    first, partial = date(2026, 1, 5), date(2026, 1, 6)
    _upsert_bars(warehouse, [
        *[_bar(f"{index:06d}", first, index) for index in range(1, 11)],
        _bar("000001", partial, 11),
    ])
    evidence = _complete_calculation_dates(db_session, warehouse, start_date=first, end_date=partial)
    assert evidence["accepted_dates"] == [first]
    assert evidence["skipped_dates"][0]["coverage"] == pytest.approx(0.1)
    # The MySQL-side local source may advance after the mirror read.
    for index in range(1, 13):
        symbol = UniverseSymbol(symbol=f"{index:06d}", asset_type="stock", market="sh", region="cn")
        db_session.add(symbol)
        db_session.flush()
        db_session.add(UniverseDailyBar(universe_symbol_id=symbol.id, trade_date=first, close=10))
    db_session.flush()
    evidence = _complete_calculation_dates(db_session, warehouse, start_date=first, end_date=partial)
    assert evidence["accepted_dates"] == []
    assert evidence["skipped_dates"][0]["expected_symbols"] == 12


def test_full_intraday_cross_section_cannot_publish_before_market_close(db_session, tmp_path):
    from datetime import timezone
    warehouse = FactorWarehouse(tmp_path / "intraday.duckdb")
    day = date(2026, 10, 13)
    _upsert_bars(warehouse, [_bar("000001", day, 1), _bar("000002", day, 2)])
    evidence = _complete_calculation_dates(db_session, warehouse, start_date=day, end_date=day,
                                          as_of=datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc))
    assert evidence["accepted_dates"] == []
    assert evidence["skipped_dates"][0]["reason"] == "DAILY_BAR_NOT_CLOSED"


def test_factor_publication_gate_checks_each_day_and_each_frozen_version(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "coverage.duckdb")
    _, _, model = _seed_bridge_inputs(db_session, warehouse)
    _upsert_bars(warehouse, [_bar(f"{index:06d}", _TRADE_DATE, index) for index in range(1, 5)])
    expected = {m.factor_code: m.factor_version for m in model.weights}
    assert _qualified_factor_dates(warehouse, batch_id="factor-score-test", expected_versions=expected)["accepted_dates"] == [_TRADE_DATE]
    expected["turnover_z20"] = 2
    evidence = _qualified_factor_dates(warehouse, batch_id="factor-score-test", expected_versions=expected)
    assert evidence["accepted_dates"] == []
    assert evidence["skipped_dates"][0]["coverage_by_factor"]["turnover_z20"] == 0.0


def _train(db, warehouse, monkeypatch):
    monkeypatch.setattr("app.services.factors.ridge_model.run_training_eligibility_gates", lambda **kw: [])
    start = _seed_training_data(warehouse)
    return train_rolling_ridge(db, warehouse, factor_calc_batch_id="factor-train", target_calc_batch_id="target-train",
                              data_cutoff_date=start + timedelta(days=90), window_days=70, validation_days=15,
                              gate=ModelGate(minimum_samples=500, minimum_symbols=5, minimum_trade_dates=60,
                                             minimum_validation_dates=10, minimum_validation_ic=0.5))


def test_real_training_purges_overlapping_labels_and_persists_weights_atomically(db_session, tmp_path, monkeypatch):
    result = _train(db_session, FactorWarehouse(tmp_path / "train.duckdb"), monkeypatch)
    assert result.status == "validated"
    model = db_session.get(FactorModelRun, result.model_run_id)
    snapshot = db_session.get(FactorWeightSnapshot, result.model_run_id)
    assert snapshot is not None
    assert snapshot.train_mode == "warehouse"
    assert json.loads(snapshot.weights_raw_json) == result.coefficients
    assert len(model.weights) == len(result.coefficients)
    assert model.train_end_date + timedelta(days=5) < model.validation_start_date
    assert json.loads(model.metrics_json)["purged_training_sample_count"] == 50


def test_artifact_failure_rolls_back_the_model_and_all_members(db_session, tmp_path, monkeypatch):
    def reject_snapshot(session, context, instances):
        if any(isinstance(row, FactorWeightSnapshot) for row in session.new):
            raise RuntimeError("simulated artifact store failure")
    event.listen(db_session, "before_flush", reject_snapshot)
    try:
        with pytest.raises(ValueError, match="model_artifact_persistence_failed"):
            _train(db_session, FactorWarehouse(tmp_path / "atomic.duckdb"), monkeypatch)
    finally:
        event.remove(db_session, "before_flush", reject_snapshot)
    db_session.commit()
    assert db_session.scalars(select(FactorModelRun)).all() == []
    assert db_session.scalars(select(FactorModelMember)).all() == []
    assert db_session.scalars(select(FactorWeightSnapshot)).all() == []


def test_history_scores_publish_each_day_without_backdating_to_manual(db_session, tmp_path, monkeypatch):
    warehouse = FactorWarehouse(tmp_path / "history.duckdb")
    symbols, manuals, model = _seed_bridge_inputs(db_session, warehouse)
    now = datetime(2026, 10, 13, 8)
    monkeypatch.setattr("app.services.factors.scoring_bridge._utcnow_naive", lambda: now)
    model.created_at = datetime(2026, 1, 8)
    second = _TRADE_DATE + timedelta(days=1)
    third = second + timedelta(days=1)
    for manual in manuals:
        manual.published_at = datetime(2026, 1, 5, 8)
        payload = {c.name: getattr(manual, c.name) for c in Score.__table__.columns if c.name != "id"}
        payload["trade_date"] = second
        payload["calc_batch_id"] = "manual-second"
        db_session.add(Score(**payload))
    db_session.flush()
    with warehouse.connection(read_only=True) as conn:
        rows = conn.execute("SELECT * FROM factor_values WHERE calc_batch_id='factor-score-test'").fetchdf()
    rows["trade_date"] = second
    warehouse.upsert_frame("factor_values", rows)
    result = materialize_factor_score_window(db_session, warehouse, trade_dates=[second, _TRADE_DATE, third],
                                            factor_calc_batch_id="factor-score-test", model_run_id=model.id,
                                            mode="ridge", pipeline_run_id="history-window")
    assert result["status"] == "PARTIAL"
    assert result["unpublished_dates"] == [third]
    assert result["materialized_count"] == 6
    scores = db_session.scalars(select(Score).where(Score.weight_mode == "ridge")).all()
    assert {score.trade_date for score in scores} == {_TRADE_DATE, second}
    assert all(score.published_at == now and score.first_published_at == now for score in scores)
    assert all(score.published_at.date() > score.trade_date for score in scores)


def test_requested_score_window_cannot_silently_succeed_with_no_scores(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "empty.duckdb")
    warehouse.initialize()
    _, _, model = _seed_bridge_inputs(db_session, warehouse)
    with pytest.raises(ValueError, match="no_factor_scores_published"):
        materialize_factor_score_window(db_session, warehouse, trade_dates=[date(2026, 2, 1)],
                                        factor_calc_batch_id="factor-score-test", model_run_id=model.id,
                                        mode="ridge")


def test_score_retry_preserves_publication_and_manual_revision_creates_a_new_fact(db_session, tmp_path, monkeypatch):
    warehouse = FactorWarehouse(tmp_path / "immutable.duckdb")
    _, manuals, model = _seed_bridge_inputs(db_session, warehouse)
    first_publication = datetime(2027, 1, 1)
    monkeypatch.setattr("app.services.factors.scoring_bridge._utcnow_naive", lambda: first_publication)
    kwargs = dict(trade_date=_TRADE_DATE, factor_calc_batch_id="factor-score-test", model_run_id=model.id, mode="ridge")
    first = materialize_factor_scores(db_session, warehouse, **kwargs)
    db_session.flush()
    monkeypatch.setattr("app.services.factors.scoring_bridge._utcnow_naive", lambda: datetime(2027, 1, 2))
    retry = materialize_factor_scores(db_session, warehouse, **kwargs)
    db_session.flush()
    assert retry.calc_batch_id == first.calc_batch_id
    original = db_session.scalars(select(Score).where(Score.calc_batch_id == first.calc_batch_id)).all()
    assert all(score.published_at == first_publication for score in original)
    manuals[0].quality_score = 90
    manuals[0].revision_at = datetime(2027, 1, 2)
    db_session.flush()
    revision = materialize_factor_scores(db_session, warehouse, **kwargs)
    db_session.flush()
    assert revision.calc_batch_id != first.calc_batch_id
    assert all(score.published_at == first_publication for score in original)


def test_real_market_inputs_flow_through_factor_fit_activation_and_model_scores(db_session, tmp_path):
    from tests.factor_real_pipeline_helpers import build_real_factor_pipeline
    from app.services.factor_model_contract import assess_factor_model_readiness
    from app.services.factors.runtime import activate_factor_model

    pipeline = build_real_factor_pipeline(db_session, tmp_path)
    readiness = assess_factor_model_readiness(db_session, model_run_id=pipeline["model_run_id"])
    assert readiness.ready, readiness.to_dict()
    runtime = activate_factor_model(db_session, pipeline["model_run_id"], mode="ridge", actor="isolated_test")
    db_session.commit()
    assert runtime.active_model_run_id == pipeline["model_run_id"]
    result = materialize_factor_scores(db_session, pipeline["warehouse"], trade_date=pipeline["trade_date"],
                                      factor_calc_batch_id=pipeline["factors"].calc_batch_id,
                                      model_run_id=pipeline["model_run_id"], mode="ridge")
    assert result.materialized_count == len(pipeline["symbols"])
    scores = db_session.scalars(select(Score).where(Score.calc_batch_id == result.calc_batch_id)).all()
    assert min(score.model_alpha_score for score in scores) < max(score.model_alpha_score for score in scores)
    assert all(score.factor_set_id == pipeline["factor_set"].id for score in scores)
    for score in scores:
        assert json.loads(score.factor_member_versions_json)["ep_ttm"]["version"] == 2
        detail = json.loads(score.factor_scores_json)["_dynamic_model"]
        assert detail["factors"]["ep_ttm"]["factor_version"] == 2
        assert score.published_at >= score.factor_data_cutoff_at
        assert score.published_at.date() > score.trade_date


def test_real_model_scores_exist_without_a_manual_base_and_report_unmapped_symbols(db_session, tmp_path):
    from sqlalchemy import delete
    from tests.factor_real_pipeline_helpers import build_real_factor_pipeline
    pipeline = build_real_factor_pipeline(db_session, tmp_path, model_run_id="standalone-model")
    db_session.execute(delete(Score))
    db_session.commit()
    result = materialize_factor_scores(db_session, pipeline["warehouse"], trade_date=pipeline["trade_date"],
                                      factor_calc_batch_id=pipeline["factors"].calc_batch_id,
                                      model_run_id=pipeline["model_run_id"], mode="ridge")
    assert result.manual_score_count == 0
    assert result.materialized_count == result.factor_only_score_count == len(pipeline["symbols"])
    scores = db_session.scalars(select(Score).where(Score.calc_batch_id == result.calc_batch_id)).all()
    assert len({score.model_alpha_score for score in scores}) > 1
    assert all(score.action == "hold" and score.stage == "neutral" for score in scores)
    assert all(json.loads(score.scoring_config_snapshot_json)["source"] == "MODEL_FACTOR" for score in scores)
    assert all(json.loads(score.factor_member_versions_json)["ep_ttm"]["version"] == 2 for score in scores)
    # Model values for an unmapped warehouse symbol participate in the real
    # cross-sectional ranking but cannot create an invented business ID.
    with pipeline["warehouse"].connection(read_only=True) as conn:
        frame = conn.execute("""SELECT * FROM factor_values WHERE calc_batch_id=?
            AND factor_code='ep_ttm' AND factor_version=2 AND trade_date=? LIMIT 1""",
            [pipeline["factors"].calc_batch_id, pipeline["trade_date"]]).fetchdf()
    frame["symbol"] = "999999"
    pipeline["warehouse"].upsert_frame("factor_values", frame)
    missing = materialize_factor_scores(db_session, pipeline["warehouse"], trade_date=pipeline["trade_date"],
                                       factor_calc_batch_id=pipeline["factors"].calc_batch_id,
                                       model_run_id=pipeline["model_run_id"], mode="ridge", pipeline_run_id="unmapped-check")
    assert missing.missing_symbol_mapping_count == 1
    assert missing.materialized_count == len(pipeline["symbols"])
