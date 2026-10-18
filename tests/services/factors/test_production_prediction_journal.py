"""Real factor-engine journals, receipt times and no retroactive issuance."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.services.factors import production_prediction_journal as journal
from app.services.factors import production_admission as admission
from app.services.factors.factor_engine import FrozenFactorSpec


@pytest.fixture
def daily(tmp_path, monkeypatch, db_session, request):
    from tests.test_trusted_history_training_pipeline import _dataset
    from app.services import market_history_evidence as evidence
    from app.services.factors import factor_engine
    class CapturedBeforeClose(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2024, 2, 8, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(evidence, 'datetime', CapturedBeforeClose)
    dataset_id, days, directory = _dataset(tmp_path, monkeypatch, day_count=28,
        incomplete_day=27 if getattr(request, 'param', '') == 'incomplete' else None)
    assert str(days[-1]) == '2024-02-08'
    dataset = evidence.load_trusted_history_dataset(dataset_id)
    bundle = {'bundle_id': 'daily-input', 'dataset_id': dataset_id,
              'dataset_manifest_sha256': dataset.manifest_sha256,
              'last_date': str(days[-1]), 'symbols': list(dataset.symbols)}
    protocol = {'observed_through': str(days[-2]), 'symbols': list(dataset.symbols),
                'factor_set_id': 'frozen', 'feature_versions': {'turnover_z20': 1}}
    contract = {'plan_id': 'a' * 64, 'protocol': protocol}
    candidate = {'model_id': 'pending-model', 'coefficients': {'turnover_z20': -.25},
                 'intercept': .1, 'feature_versions': {'turnover_z20': 1}}
    monkeypatch.setattr(journal, '_candidate', lambda *args: (deepcopy(contract), deepcopy(candidate)))
    monkeypatch.setattr(admission, '_bundle_context', lambda *args: (deepcopy(bundle), None, days))
    monkeypatch.setattr(journal, 'JOURNAL_ROOT', tmp_path / 'journals')
    monkeypatch.setattr(journal, '_now', lambda: datetime(2024, 2, 8, 9, tzinfo=timezone.utc))
    specs = (FrozenFactorSpec('turnover_z20', 1, 1, 'exclude', True, None, frozenset({'raw_daily_bars'})),)
    monkeypatch.setattr(factor_engine, 'resolve_frozen_factor_specs', lambda *args: specs)
    return contract, candidate, dataset, bundle


def test_real_factor_predictions_are_same_day_immutable_and_sql_readonly(daily, db_session):
    contract, candidate, dataset, _ = daily
    statements = []
    def listen(conn, cursor, statement, *args):
        statements.append(statement.lstrip().split()[0].upper())
    event.listen(db_session.bind, 'before_cursor_execute', listen)
    try:
        result = journal.write_daily_prediction(db_session, contract['plan_id'], bundle_id='daily-input')
        again = journal.write_daily_prediction(db_session, contract['plan_id'], bundle_id='daily-input')
        status = journal.journal_status(db_session, contract['plan_id'])
    finally:
        event.remove(db_session.bind, 'before_cursor_execute', listen)
    assert result == again
    report = result['prediction']
    assert report['predicted_symbols'] == report['full_cohort_denominator'] == 24
    for row in report['predictions'].values():
        assert row['linear_signal'] == pytest.approx(.1 - .25 * row['features']['turnover_z20'])
    assert not report['labels_used'] and not report['model_activated']
    assert report['production_scores_published'] == 0 and not report['qualification_certificate']
    assert status['journal_count'] == 1 and not status['independent_validation_complete']
    assert status['qualification_issued'] is False
    assert not {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'} & set(statements)
    assert len(list((journal.JOURNAL_ROOT / contract['plan_id']).glob('*.json'))) == 1
    assert not any('target' in name for name in report)
    with pytest.raises(ValueError, match='day_already_locked'):
        journal.write_daily_prediction(db_session, contract['plan_id'], bundle_id='other-input')


@pytest.mark.parametrize('clock,reason', [
    (datetime(2024, 2, 7, 9, tzinfo=timezone.utc), 'no_postboundary'),
    (datetime(2024, 2, 8, 7, 14, tzinfo=timezone.utc), 'not_closed'),
    (datetime(2024, 2, 9, 9, tzinfo=timezone.utc), 'same_day_verified_cohort'),
])
def test_caller_cannot_backfill_or_issue_before_close(daily, db_session, monkeypatch, clock, reason):
    monkeypatch.setattr(journal, '_now', lambda: clock)
    with pytest.raises(ValueError, match=reason):
        journal.write_daily_prediction(db_session, daily[0]['plan_id'], bundle_id='daily-input')
    assert not journal.JOURNAL_ROOT.exists()


def test_future_receipt_and_changed_prediction_cannot_supply_evidence(daily, db_session, monkeypatch):
    contract, _, dataset, _ = daily
    original = journal._availability
    times = original(dataset)
    first = next(iter(times))
    times[first] = journal._now() + timedelta(seconds=1)
    monkeypatch.setattr(journal, '_availability', lambda _: times)
    with pytest.raises(ValueError, match='source_not_yet_available'):
        journal.write_daily_prediction(db_session, contract['plan_id'], bundle_id='daily-input')
    assert not journal.JOURNAL_ROOT.exists()
    monkeypatch.setattr(journal, '_availability', original)
    journal.write_daily_prediction(db_session, contract['plan_id'], bundle_id='daily-input')
    path = journal.JOURNAL_ROOT / contract['plan_id'] / '2024-02-08.json'
    envelope = json.loads(path.read_bytes())
    envelope['prediction']['predictions']['000001']['linear_signal'] = 99
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match='journal_changed'):
        journal.journal_status(db_session, contract['plan_id'])


def test_crossing_midnight_during_calculation_does_not_publish(daily, db_session, monkeypatch):
    calls = iter([datetime(2024, 2, 8, 15, 59, tzinfo=timezone.utc),
                  datetime(2024, 2, 9, 0, tzinfo=timezone.utc)])
    monkeypatch.setattr(journal, '_now', lambda: next(calls))
    with pytest.raises(ValueError, match='expired|not_closed'):
        journal.write_daily_prediction(db_session, daily[0]['plan_id'], bundle_id='daily-input')
    assert not journal.JOURNAL_ROOT.exists()


def test_http_forbids_client_dates_predictions_and_fake_certificates(daily, db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes.scoring_facade import router
    from app.db.session import get_db
    app = FastAPI(); app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    url = '/api/v1/scoring/production-candidates/' + daily[0]['plan_id'] + '/predictions'
    assert client.get(url + '?bundle_id=daily-input').json()['can_record_today'] is True
    for extra in ({'signal_date': '2024-02-08'}, {'issued_at': '2024-02-08T09:00:00Z'},
                  {'predictions': {}}, {'qualification_certificate': True}):
        assert client.post(url, json={'bundle_id': 'daily-input', **extra}).status_code == 422
    response = client.post(url, json={'bundle_id': 'daily-input'})
    assert response.status_code == 200, response.text
    assert client.get(url).json()['journal_count'] == 1
    assert client.get(url + '?bundle_id=daily-input').json()['can_record_today'] is False


@pytest.mark.parametrize('daily', ['incomplete'], indirect=True)
def test_missing_stocks_do_not_shrink_frozen_denominator(daily, db_session):
    with pytest.raises(ValueError, match='insufficient_full_cohort_coverage'):
        journal.write_daily_prediction(db_session, daily[0]['plan_id'], bundle_id='daily-input')
    assert not journal.JOURNAL_ROOT.exists()
