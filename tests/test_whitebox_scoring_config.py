from __future__ import annotations

import copy
import json
from datetime import date, timedelta

import pytest

from app.api.routes.scoring_configs import duplicate_scoring_config
from app.models.daily_bar import DailyBar
from app.models.score import Score
from app.models.scoring_config import ScoringConfig
from app.models.symbol import Symbol
from app.schemas.scoring_config import ScoringConfigDuplicate
from app.services import scoring_config_engine as engine
from app.services.scans import run_scan

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.routes import scoring_configs
from app.db.session import get_db

pytestmark = pytest.mark.whitebox


def _coverage_client(db_session):
    app = FastAPI()
    app.include_router(scoring_configs.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _coverage_score(db_session, symbol_id, day, version, *, asset_type="stock"):
    db_session.add(Score(
        symbol_id=symbol_id, trade_date=date(2026, 10, day),
        calc_batch_id=f"coverage-{asset_type}-{version}-{day}",
        quality_score=50, quality_grade="C", timing_score=50,
        stage="start", action="hold", priority_score=50,
        scoring_asset_type=asset_type, scoring_config_id=version,
        scoring_preset_key="coverage" if version is not None else None,
        scoring_preset_name="Coverage" if version is not None else None,
        scoring_config_version=version,
    ))
    db_session.flush()


def test_scoring_config_coverage_single_version_has_counts_and_date_bounds(db_session):
    symbol, _ = _add_symbol_with_bars(db_session)
    _coverage_score(db_session, symbol.id, 2, 1)
    _coverage_score(db_session, symbol.id, 8, 1)
    response = _coverage_client(db_session).get("/settings/scoring-configs/caliber-coverage?asset_type=stock")
    assert response.status_code == 200, response.text
    assert response.json() == [{
        "config_id": 1, "preset_key": "coverage", "preset_name": "Coverage", "version": 1,
        "trade_date_min": "2026-10-02", "trade_date_max": "2026-10-08", "score_count": 2,
    }]


def test_scoring_config_coverage_multiple_versions_are_sorted_by_first_date(db_session):
    symbol, _ = _add_symbol_with_bars(db_session)
    _coverage_score(db_session, symbol.id, 8, 1)
    _coverage_score(db_session, symbol.id, 2, 2)
    _coverage_score(db_session, symbol.id, 5, 2)
    response = _coverage_client(db_session).get("/settings/scoring-configs/caliber-coverage?asset_type=stock")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["version"] for row in rows] == [2, 1]
    assert [row["score_count"] for row in rows] == [2, 1]
    assert [row["trade_date_min"] for row in rows] == ["2026-10-02", "2026-10-08"]
    assert [row["trade_date_max"] for row in rows] == ["2026-10-05", "2026-10-08"]


def test_scoring_config_coverage_preserves_unknown_version(db_session):
    symbol, _ = _add_symbol_with_bars(db_session)
    _coverage_score(db_session, symbol.id, 1, None)
    response = _coverage_client(db_session).get("/settings/scoring-configs/caliber-coverage?asset_type=stock")
    assert response.status_code == 200, response.text
    assert response.json()[0]["version"] is None
    assert response.json()[0]["score_count"] == 1


def test_scoring_config_coverage_filters_asset_type_and_inclusive_dates(db_session):
    symbol, _ = _add_symbol_with_bars(db_session)
    _coverage_score(db_session, symbol.id, 1, 1)
    _coverage_score(db_session, symbol.id, 2, 1)
    _coverage_score(db_session, symbol.id, 8, 2)
    _coverage_score(db_session, symbol.id, 2, 3, asset_type="etf")
    response = _coverage_client(db_session).get(
        "/settings/scoring-configs/caliber-coverage?asset_type=stock&start_date=2026-10-02&end_date=2026-10-08"
    )
    assert response.status_code == 200, response.text
    assert [row["version"] for row in response.json()] == [1, 2]
    assert [row["score_count"] for row in response.json()] == [1, 1]


def test_scoring_config_coverage_rejects_invalid_asset_type(db_session):
    response = _coverage_client(db_session).get("/settings/scoring-configs/caliber-coverage?asset_type=bond")
    assert response.status_code == 422, response.text


def _stock_config(*, preset_key: str = "test_preset", external: bool = False) -> dict:
    config = copy.deepcopy(engine.STOCK_SYSTEM_PRESETS[0])
    config["preset_key"] = preset_key
    config["name"] = preset_key
    if external:
        config["dimensions"].append(
            {
                "key": "valuation",
                "name": "估值",
                "enabled": True,
                "score_bucket": "quality",
                "weight": 0.2,
                "filter": {"enabled": False, "operator": "gte", "value": 60},
                "factors": [
                    {
                        "key": "pe_score",
                        "source": "fundamental",
                        "weight": 1.0,
                        "direction": "higher_better",  # pe_score is already normalized
                    }
                ],
            }
        )
    return config


def _add_symbol_with_bars(db_session) -> tuple[Symbol, date]:
    symbol = Symbol(symbol="000001", name="Test Stock", asset_type="stock", market="sz", is_active=1)
    db_session.add(symbol)
    db_session.flush()
    start = date(2026, 1, 1)
    for idx in range(30):
        db_session.add(
            DailyBar(
                symbol_id=symbol.id,
                trade_date=start + timedelta(days=idx),
                open=10 + idx * 0.1,
                high=10.3 + idx * 0.1,
                low=9.8 + idx * 0.1,
                close=10.1 + idx * 0.1,
                volume=100000 + idx * 1000,
                amount=10000000 + idx * 100000,
                turnover_rate=2.0,
            )
        )
    db_session.flush()
    return symbol, start + timedelta(days=29)


def _add_scoring_config(db_session, *, key: str, version: int, active: int = 0, external: bool = False) -> ScoringConfig:
    config = _stock_config(preset_key=key, external=external)
    row = ScoringConfig(
        asset_type="stock",
        preset_key=key,
        name=config["name"],
        description="test",
        version=version,
        config_json=json.dumps(config, ensure_ascii=False),
        preset_source="user",
        is_system=0,
        is_active=active,
        is_latest=1,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_scoring_config_version_does_not_overwrite_previous_score(db_session):
    symbol, trade_date = _add_symbol_with_bars(db_session)
    cfg_v1 = _add_scoring_config(db_session, key="my_model", version=1)
    cfg_v2 = _add_scoring_config(db_session, key="my_model_v2", version=2)

    score_v1 = engine.calculate_symbol_score_with_config(db_session, symbol, trade_date, cfg_v1)
    db_session.flush()
    score_v1_id = score_v1.id
    score_v1_batch = score_v1.calc_batch_id

    score_v2 = engine.calculate_symbol_score_with_config(db_session, symbol, trade_date, cfg_v2)
    db_session.flush()

    rows = db_session.query(Score).filter(Score.symbol_id == symbol.id, Score.trade_date == trade_date).all()
    assert len(rows) == 2
    assert score_v1_id != score_v2.id
    assert score_v1_batch != score_v2.calc_batch_id
    old_score = db_session.get(Score, score_v1_id)
    assert old_score.scoring_config_id == cfg_v1.id
    assert old_score.scoring_config_version == 1


def test_external_factors_are_only_requested_when_enabled(db_session, monkeypatch):
    symbol, trade_date = _add_symbol_with_bars(db_session)
    cfg_plain = _add_scoring_config(db_session, key="plain", version=1, external=False)
    cfg_external = _add_scoring_config(db_session, key="external", version=1, external=True)
    requested: list[set[str]] = []

    def fake_external(_db, _symbol, _trade_date, required_factor_keys=None):
        requested.append(set(required_factor_keys or set()))
        return {}

    monkeypatch.setattr(engine, "_compute_external_factors", fake_external)

    engine.calculate_symbol_score_with_config(db_session, symbol, trade_date, cfg_plain)
    engine.calculate_symbol_score_with_config(db_session, symbol, trade_date, cfg_external)

    assert requested[0] == set()
    assert requested[1] == {"pe_score"}


def test_duplicate_scoring_config_rewrites_config_json_identity(db_session):
    src = _add_scoring_config(db_session, key="source_key", version=1)

    duplicated = duplicate_scoring_config(
        src.id,
        ScoringConfigDuplicate(new_preset_key="copied_key", new_name="我的副本"),
        db_session,
    )

    assert duplicated.preset_key == "copied_key"
    assert duplicated.name == "我的副本"
    assert duplicated.config is not None
    assert duplicated.config["preset_key"] == "copied_key"
    assert duplicated.config["name"] == "我的副本"
    assert duplicated.config["preset_source"] == "user"


def test_run_scan_uses_active_scoring_config_snapshot(db_session):
    symbol, trade_date = _add_symbol_with_bars(db_session)
    cfg_v1 = _add_scoring_config(db_session, key="scan_v1", version=1, active=0)
    cfg_v2 = _add_scoring_config(db_session, key="scan_v2", version=2, active=1)

    score_v1 = Score(
        symbol_id=symbol.id,
        trade_date=trade_date,
        calc_batch_id="sc-v1",
        quality_score=50,
        quality_grade="C",
        timing_score=50,
        stage="start",
        action="open",
        priority_score=51,
        scoring_asset_type="stock",
        scoring_config_id=cfg_v1.id,
        scoring_preset_key=cfg_v1.preset_key,
        scoring_config_version=cfg_v1.version,
        scoring_config_snapshot_json=cfg_v1.config_json,
        dimension_scores_json="{}",
    )
    score_v2 = Score(
        symbol_id=symbol.id,
        trade_date=trade_date,
        calc_batch_id="sc-v2",
        quality_score=80,
        quality_grade="A",
        timing_score=82,
        stage="start",
        action="open",
        priority_score=83,
        scoring_asset_type="stock",
        scoring_config_id=cfg_v2.id,
        scoring_preset_key=cfg_v2.preset_key,
        scoring_config_version=cfg_v2.version,
        scoring_config_snapshot_json=cfg_v2.config_json,
        dimension_scores_json="{}",
    )
    db_session.add_all([score_v1, score_v2])
    db_session.flush()

    scan_run = run_scan(
        db=db_session,
        scope_snapshot={"asset_types": ["stock"]},
        filters_snapshot={"min_score": 0},
        portfolio_id=None,
        portfolio_rule_id=None,
        run_name="test-scan",
        preset_id=None,
    )
    db_session.flush()

    snapshot = json.loads(scan_run.filters_snapshot)
    assert snapshot["scoring_config_id"] == cfg_v2.id
    result = scan_run.results[0]
    assert result.priority_score == 83
