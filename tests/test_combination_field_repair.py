"""Targeted compensation touches only proven empty fields, with durable evidence."""
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.async_task import AsyncTaskRecord
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services import universe_sync

pytestmark = pytest.mark.whitebox


def seeded(db):
    symbols = [UniverseSymbol(symbol=code, name=code, region="cn", market="sh", asset_type="stock")
               for code in ("600000", "600001")]
    db.add_all(symbols); db.flush()
    bars = [UniverseDailyBar(universe_symbol_id=s.id, trade_date=date(2026, 10, day),
                 open=10, high=12, low=9, close=11, volume=1000, amount=11000,
                 turnover_rate=None if day == 19 else 3, source="akshare:em:cn-stock:v1:qfq",
                 created_at=datetime(2026, 10, 20)) for s in symbols for day in (19, 20)]
    db.add_all(bars); db.commit()
    return symbols, bars


def response(turnover=2):
    frame = pd.DataFrame([dict(trade_date=date(2026, 10, 19), open=10.0, high=12.0, low=9.0, close=11.0,
                               volume=1000, amount=11000, turnover_rate=turnover)])
    frame.attrs["history_contract"] = dict(provider="eastmoney", endpoint="stock_zh_a_hist",
        version="cn-stock-shares-v1", volume_unit="share", amount_unit="CNY", turnover_unit="percentage_point")
    frame.attrs["production_price_mode"] = {"requested_adjustment": "qfq"}
    return frame


def test_targeted_preview_apply_preserves_every_other_row_and_field(db_session, monkeypatch):
    from app.services.universe_field_repair import repair_universe_fields
    symbols, bars = seeded(db_session)
    original = {bar.id: dict(db_session.execute(select(UniverseDailyBar.__table__).where(UniverseDailyBar.id == bar.id)).mappings().one()) for bar in bars}
    monkeypatch.setattr(universe_sync, "_fetch_universe_history", lambda *_: response())
    args = dict(symbol_ids=[symbols[0].id], start_date=date(2026, 10, 19), end_date=date(2026, 10, 20), fields=["turnover_rate"])
    preview = repair_universe_fields(db_session, **args)
    assert preview["filled_fields"] == 1 and preview["applied"] is False
    assert db_session.get(UniverseDailyBar, bars[0].id).turnover_rate is None
    result = repair_universe_fields(db_session, **args, apply=True, expected_preview_sha256=preview["preview_sha256"])
    assert result["outcome"] == "success" and result["applied"] is True
    after = {row["id"]: dict(row) for row in db_session.execute(select(UniverseDailyBar.__table__)).mappings()}
    assert after.keys() == original.keys()
    for row_id, before in original.items():
        changed = {key for key in before if before[key] != after[row_id][key]}
        assert changed == ({"turnover_rate", "created_at"} if row_id == bars[0].id else set())
    task = db_session.get(AsyncTaskRecord, result["audit_task_id"])
    assert task.status == "done" and '"before": null' in task.result_json and '"after": 2.0' in task.result_json
    again = repair_universe_fields(db_session, **args)
    assert again["filled_fields"] == 0 and again["outcome"] == "success"


@pytest.mark.parametrize("failure", ["source", "missing", "duplicate", "ohlc"])
def test_unproven_or_ambiguous_response_lists_failure_without_guessing(db_session, monkeypatch, failure):
    from app.services.universe_field_repair import repair_universe_fields
    symbols, bars = seeded(db_session)
    frame = response(None if failure == "missing" else 2)
    if failure == "source":
        frame.attrs["history_contract"].update(provider="tencent", endpoint="stock_zh_a_hist_tx")
    if failure == "duplicate":
        attrs = frame.attrs.copy(); frame = pd.concat([frame, frame], ignore_index=True); frame.attrs = attrs
    if failure == "ohlc":
        frame.loc[0, "close"] = 10.5
    monkeypatch.setattr(universe_sync, "_fetch_universe_history", lambda *_: frame)
    result = repair_universe_fields(db_session, symbol_ids=[symbols[0].id], start_date=date(2026, 10, 19),
                                    end_date=date(2026, 10, 19), fields=["turnover_rate"])
    assert result["outcome"] == "failed" and result["failures"] and result["filled_fields"] == 0
    assert db_session.get(UniverseDailyBar, bars[0].id).turnover_rate is None


def test_changed_preview_and_unknown_target_fail_closed(db_session, monkeypatch):
    from app.services.universe_field_repair import repair_universe_fields
    symbols, bars = seeded(db_session)
    monkeypatch.setattr(universe_sync, "_fetch_universe_history", lambda *_: response())
    args = dict(symbol_ids=[symbols[0].id], start_date=date(2026, 10, 19), end_date=date(2026, 10, 19), fields=["turnover_rate"])
    preview = repair_universe_fields(db_session, **args)
    bars[0].amount = 12345; db_session.commit()
    with pytest.raises(ValueError, match="preview_changed"):
        repair_universe_fields(db_session, **args, apply=True, expected_preview_sha256=preview["preview_sha256"])
    unknown = repair_universe_fields(db_session, **{**args, "symbol_ids": [999999]})
    assert unknown["outcome"] == "failed" and unknown["failures"][0]["reason"] == "symbol_not_found"
    assert db_session.get(UniverseDailyBar, bars[0].id).turnover_rate is None


@pytest.mark.parametrize('identity_field,replacement', [('market', 'bj'), ('symbol', '600099'),
    ('asset_type', 'etf'), ('region', 'us')])
def test_identity_changed_during_fetch_rejects_every_write(db_session, monkeypatch, identity_field, replacement):
    from sqlalchemy.orm import Session
    from app.services.universe_field_repair import repair_universe_fields
    symbols, bars = seeded(db_session)
    symbol_id, row_id = symbols[0].id, bars[0].id
    args = dict(symbol_ids=[symbol_id], start_date=date(2026, 10, 19), end_date=date(2026, 10, 19), fields=['turnover_rate'])
    monkeypatch.setattr(universe_sync, '_fetch_universe_history', lambda *_: response())
    preview = repair_universe_fields(db_session, **args)
    before = dict(db_session.execute(select(UniverseDailyBar.__table__).where(UniverseDailyBar.id == row_id)).mappings().one())
    def fetch_then_change_identity(instrument, *_):
        assert instrument.id == symbol_id
        with Session(db_session.get_bind()) as other:
            setattr(other.get(UniverseSymbol, symbol_id), identity_field, replacement)
            other.commit()
        return response()
    monkeypatch.setattr(universe_sync, '_fetch_universe_history', fetch_then_change_identity)
    with pytest.raises(ValueError, match='preview_changed'):
        repair_universe_fields(db_session, **args, apply=True, expected_preview_sha256=preview['preview_sha256'])
    db_session.expire_all()
    assert dict(db_session.execute(select(UniverseDailyBar.__table__).where(UniverseDailyBar.id == row_id)).mappings().one()) == before
    assert db_session.scalars(select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == 'universe_field_repair')).all() == []


@pytest.mark.parametrize('identity_field,replacement', [('market', 'bj'), ('symbol', '600099'),
    ('asset_type', 'etf'), ('region', 'us')])
def test_identity_change_invalidates_saved_preview_even_when_provider_values_match(db_session, monkeypatch, identity_field, replacement):
    from app.services.universe_field_repair import repair_universe_fields
    symbols, bars = seeded(db_session)
    symbol_id, row_id = symbols[0].id, bars[0].id
    args = dict(symbol_ids=[symbol_id], start_date=date(2026, 10, 19), end_date=date(2026, 10, 19), fields=['turnover_rate'])
    monkeypatch.setattr(universe_sync, '_fetch_universe_history', lambda *_: response())
    preview = repair_universe_fields(db_session, **args)
    setattr(db_session.get(UniverseSymbol, symbol_id), identity_field, replacement)
    db_session.commit()
    with pytest.raises(ValueError, match='preview_changed'):
        repair_universe_fields(db_session, **args, apply=True, expected_preview_sha256=preview['preview_sha256'])
    assert db_session.get(UniverseDailyBar, row_id).turnover_rate is None


def test_audit_preserves_exact_old_and_new_availability_once_per_row(db_session, monkeypatch):
    import json
    from app.services.universe_field_repair import repair_universe_fields
    symbols, bars = seeded(db_session)
    old = datetime(2026, 10, 20, 23, 59, 59, 123456)
    bars[0].created_at = old
    bars[0].amount = None
    db_session.commit()
    monkeypatch.setattr(universe_sync, '_fetch_universe_history', lambda *_: response())
    args = dict(symbol_ids=[symbols[0].id], start_date=date(2026, 10, 19), end_date=date(2026, 10, 19), fields=['amount', 'turnover_rate'])
    preview = repair_universe_fields(db_session, **args)
    result = repair_universe_fields(db_session, **args, apply=True, expected_preview_sha256=preview['preview_sha256'])
    db_session.expire_all()
    actual = db_session.get(UniverseDailyBar, bars[0].id)
    audit = json.loads(db_session.get(AsyncTaskRecord, result['audit_task_id']).result_json)
    assert result['filled_fields'] == 2 and len(result['availability_changes']) == 1
    time_change = result['availability_changes'][0]
    assert time_change['row_id'] == actual.id
    assert time_change['before'] == {'type': 'datetime', 'value': old.isoformat(timespec='microseconds')}
    assert time_change['after'] == {'type': 'datetime', 'value': actual.created_at.isoformat(timespec='microseconds')}
    assert audit['availability_changes'] == result['availability_changes']
    assert datetime.fromisoformat(audit['availability_changes'][0]['before']['value']) == old


@pytest.mark.parametrize('future_original', [False, True])
def test_repair_availability_never_precedes_old_time_or_observation(db_session, monkeypatch, future_original):
    from app.services.universe_field_repair import repair_universe_fields
    symbols, bars = seeded(db_session)
    original = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=5)
                if future_original else datetime(2026, 1, 1, 1, 2, 3, 123456))
    bars[0].created_at = original
    db_session.commit()
    row_id = bars[0].id
    monkeypatch.setattr(universe_sync, '_fetch_universe_history', lambda *_: response())
    args = dict(symbol_ids=[symbols[0].id], start_date=date(2026, 10, 19), end_date=date(2026, 10, 19), fields=['turnover_rate'])
    preview = repair_universe_fields(db_session, **args)
    result = repair_universe_fields(db_session, **args, apply=True, expected_preview_sha256=preview['preview_sha256'])
    db_session.expire_all()
    actual = db_session.get(UniverseDailyBar, row_id).created_at
    observed = datetime.fromisoformat(result['observed_at'])
    assert actual >= original and actual >= observed
    if future_original:
        assert actual == original
    else:
        # A whole-second ceiling stays conservative on legacy DATETIME(0).
        assert actual.microsecond == 0 and actual - observed < timedelta(seconds=1)
    assert datetime.fromisoformat(result['availability_changes'][0]['before']['value']) == original


def test_generic_range_task_does_not_claim_success_after_provider_failure(db_session, monkeypatch):
    from app.services import universe_sync_task
    symbols, _ = seeded(db_session)
    for item in symbols:
        item.is_synced = 1
        item.sync_failed = 0
    task = AsyncTaskRecord(id="range-repair-failure", task_type="universe_range_repair",
                           status="queued", stage="queued")
    db_session.add(task); db_session.commit()
    def unavailable(*_):
        raise RuntimeError("acceptance provider unavailable")
    monkeypatch.setattr(universe_sync, "_fetch_universe_history", unavailable)
    universe_sync_task._run_universe_range_repair(task.id, 1, 365, 90, ["cn-stock"])
    db_session.expire_all()
    saved = db_session.get(AsyncTaskRecord, task.id)
    import json
    result = json.loads(saved.result_json)
    assert saved.status == "failed" and result["outcome"] == "failed"
    assert set(result["cn-stock_repair"]["failed_ids"]) == {item.id for item in symbols}
    assert all("acceptance provider unavailable" in item["error"] for item in result["cn-stock_repair"]["failures"])


def test_http_field_compensation_preview_apply_and_invalid_payloads_are_atomic(db_session, monkeypatch):
    from fastapi.testclient import TestClient
    from app.db.session import get_db
    from app.main import app
    symbols, bars = seeded(db_session)
    monkeypatch.setattr(universe_sync, "_fetch_universe_history", lambda *_: response())
    def dependency():
        yield db_session
    app.dependency_overrides[get_db] = dependency
    try:
        client = TestClient(app)
        payload = dict(symbol_ids=[symbols[0].id], start_date="2026-10-19", end_date="2026-10-19", fields=["turnover_rate"])
        for changes in ({"symbol_ids": [True]}, {"symbol_ids": [symbols[0].id, symbols[0].id]},
                        {"fields": ["close"]}, {"end_date": "2026-10-18"}, {"apply": True}):
            rejected = client.post("/api/v1/universe/field-repair", json={**payload, **changes})
            assert rejected.status_code == 422, rejected.text
            assert db_session.get(UniverseDailyBar, bars[0].id).turnover_rate is None
        preview = client.post("/api/v1/universe/field-repair", json=payload)
        assert preview.status_code == 200 and preview.json()["applied"] is False
        assert preview.json()['instrument_snapshots'][0]['symbol'] == '600000'
        applied = client.post("/api/v1/universe/field-repair", json={**payload, "apply": True,
            "expected_preview_sha256": preview.json()["preview_sha256"]})
        assert applied.status_code == 200 and applied.json()["filled_fields"] == 1
        assert applied.json()['availability_changes'][0]['before']['type'] == 'datetime'
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("database_url,expected", [(None, "FIELD_REPAIR_DATABASE_URL"),
    ("mysql+pymysql://user:secret@127.0.0.1/gpfx", "Only explicitly named")])
def test_cli_has_no_default_database_and_refuses_production_before_connecting(tmp_path, database_url, expected):
    import os
    from pathlib import Path
    import subprocess
    import sys
    environment = dict(os.environ)
    environment.pop("FIELD_REPAIR_DATABASE_URL", None)
    if database_url:
        environment["FIELD_REPAIR_DATABASE_URL"] = database_url
    result = subprocess.run([sys.executable, "scripts/repair_universe_fields.py", "--expected-database", "gpfx",
        "--symbol-ids", "1", "--start-date", "2026-10-19", "--end-date", "2026-10-19",
        "--output", str(tmp_path/"forbidden.json")], cwd=Path(__file__).resolve().parents[1], env=environment,
        capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 2 and expected in result.stderr
    assert "secret" not in result.stdout + result.stderr
    assert not (tmp_path/"forbidden.json").exists()
