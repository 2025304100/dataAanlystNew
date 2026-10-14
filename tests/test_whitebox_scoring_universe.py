"""Scope reaches dispatch and publication without changing the model reference."""
from datetime import date
from datetime import datetime, timedelta
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.async_task import AsyncTaskRecord
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio, Position
from app.models.symbol import Symbol
from app.models.score import Score
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.schemas.async_task import FactorPipelineCreate
from app.schemas.errors import FactorSevenError
from app.services.factors import pipeline_task as pipeline
from app.services.factors.scoring_universe import freeze_score_universe, score_universe_summary
from tests.test_whitebox_factor_pipeline_startup import _setup_config
from tests.test_whitebox_factor_pipeline_runtime_pin import _runtime, _stub_calculation

pytestmark = pytest.mark.whitebox


def _symbol(db, code, *, active=1, asset='stock'):
    row = Symbol(symbol=code, name=code, asset_type=asset, market='CN', is_active=active)
    db.add(row)
    db.flush()
    return row


def _watchlist(db, name='scope-one'):
    row = Watchlist(name=name, list_type='watch')
    db.add(row)
    db.flush()
    return row


def _portfolio(db, name):
    row = Portfolio(name=name, account_type='paper', total_capital=1000,
                    investable_ratio=.8, cash_reserve_ratio=.2)
    db.add(row)
    db.flush()
    return row


def _payload(source, **kwargs):
    return FactorPipelineCreate(train_model=False, universe_source=source, **kwargs)


@pytest.mark.parametrize('values', [
    {'universe_source': 'watchlist'}, {'universe_source': 'positions'},
    {'universe_source': 'all', 'watchlist_id': 1}, {'portfolio_id': 1},
    {'universe_source': 'watchlist', 'watchlist_id': 1, 'portfolio_id': 1},
    {'universe_source': 'bad'},
])
def test_invalid_scope_does_not_fall_back_to_all(values):
    with pytest.raises(ValidationError):
        FactorPipelineCreate(train_model=False, **values)


def test_watchlist_scope_excludes_other_pools_archived_invalid_and_inactive(db_session):
    first, other = _watchlist(db_session), _watchlist(db_session, 'other')
    expected = []
    for code, state, active, pool in [
        ('000001', 'watching', 1, first), ('000002', 'ready', 1, first),
        ('000003', 'archived', 1, first), ('000004', 'invalid', 1, first),
        ('000005', 'watching', 0, first), ('000006', 'watching', 1, other),
    ]:
        symbol = _symbol(db_session, code, active=active)
        db_session.add(WatchlistItem(watchlist_id=pool.id, symbol_id=symbol.id, status=state))
        if code in ('000001', '000002'):
            expected.append(code)
    db_session.commit()
    payload = _payload('watchlist', watchlist_id=first.id)
    freeze_score_universe(db_session, payload)
    assert payload.score_symbols == expected
    assert score_universe_summary(payload) == {
        'source': 'watchlist', 'watchlist_id': first.id, 'portfolio_id': None,
        'symbol_count': 2, 'normalization': 'full_market',
    }


def test_positions_scope_only_includes_positive_current_portfolio_positions(db_session):
    first, other = _portfolio(db_session, 'first'), _portfolio(db_session, 'other')
    for code, active, quantity, portfolio in [
        ('000001', 1, 10, first), ('000002', 1, 0, first),
        ('000003', 0, 10, first), ('000004', 1, 10, other),
    ]:
        symbol = _symbol(db_session, code, active=active)
        db_session.add(Position(portfolio_id=portfolio.id, symbol_id=symbol.id,
                                asset_type='stock', quantity=quantity))
    db_session.commit()
    payload = _payload('positions', portfolio_id=first.id)
    freeze_score_universe(db_session, payload)
    assert payload.score_symbols == ['000001']


def test_unsupported_asset_scope_is_rejected_instead_of_scoring_another_universe(db_session):
    pool = _watchlist(db_session)
    etf = _symbol(db_session, '510300', asset='etf')
    foreign = _symbol(db_session, 'AAPL')
    foreign.market = 'us'
    db_session.add_all([WatchlistItem(watchlist_id=pool.id, symbol_id=symbol.id, status='watching')
                        for symbol in (etf, foreign)])
    db_session.commit()
    with pytest.raises(ValueError, match='没有可评分的 A 股股票'):
        freeze_score_universe(db_session, _payload('watchlist', watchlist_id=pool.id))


def test_all_is_read_only_synced_stock_scope_with_active_legacy_bars(db_session):
    for code, synced, asset, region in [
        ('000001', 1, 'stock', 'cn'), ('000002', 0, 'stock', 'cn'),
        ('000003', 1, 'stock', 'cn'), ('510300', 1, 'etf', 'cn'),
        ('AAPL', 1, 'stock', 'us'),
    ]:
        db_session.add(UniverseSymbol(symbol=code, name=code, asset_type=asset,
                                      market='CN', region=region, is_synced=synced))
    inactive = _symbol(db_session, '000003', active=0)
    legacy = _symbol(db_session, '000004')
    _symbol(db_session, '000005')  # Metadata alone does not mean synced.
    db_session.add(DailyBar(symbol_id=legacy.id, trade_date=date(2026, 1, 5),
                            open=10, high=11, low=9, close=10))
    db_session.commit()
    before = len(db_session.scalars(select(Symbol)).all())
    payload = _payload('all')
    freeze_score_universe(db_session, payload)
    assert payload.score_symbols == ['000001', '000004']
    assert len(db_session.scalars(select(Symbol)).all()) == before
    assert db_session.get(Symbol, inactive.id).is_active == 0
    assert not db_session.new


@pytest.mark.parametrize('source', ['watchlist', 'positions'])
def test_missing_or_empty_scope_never_creates_task(db_session, tmp_path, monkeypatch, source):
    _setup_config(db_session, tmp_path / 'empty.duckdb')
    row = _watchlist(db_session) if source == 'watchlist' else _portfolio(db_session, 'empty')
    db_session.commit()
    worker = Mock()
    monkeypatch.setattr(pipeline, '_start_worker', worker)
    key = 'watchlist_id' if source == 'watchlist' else 'portfolio_id'
    for identity in (row.id, 99999):
        with pytest.raises(ValueError, match='不存在|没有可评分'):
            pipeline.create_factor_pipeline_task(_payload(source, **{key: identity}))
    worker.assert_not_called()
    assert db_session.scalars(select(AsyncTaskRecord)).all() == []


def test_dispatch_freezes_pool_and_worker_keeps_the_submitted_recipients(db_session, tmp_path, monkeypatch):
    _setup_config(db_session, tmp_path / 'frozen.duckdb')
    _runtime(db_session)
    pool = _watchlist(db_session)
    first, later = _symbol(db_session, '000001'), _symbol(db_session, '000002')
    item = WatchlistItem(watchlist_id=pool.id, symbol_id=first.id, status='watching')
    db_session.add(item)
    db_session.commit()
    monkeypatch.setattr(pipeline, '_start_worker', Mock())
    task = pipeline.create_factor_pipeline_task(_payload('watchlist', watchlist_id=pool.id))
    frozen = FactorPipelineCreate.model_validate_json(task['payload_json'])
    assert frozen.score_symbols == ['000001']
    item.status = 'archived'
    db_session.add(WatchlistItem(watchlist_id=pool.id, symbol_id=later.id, status='watching'))
    db_session.commit()
    with pytest.raises(FactorSevenError) as caught:
        pipeline.create_factor_pipeline_task(_payload('watchlist', watchlist_id=pool.id))
    assert caught.value.error_code == 'FACTOR_PIPELINE_BUSY'
    scorer = _stub_calculation(monkeypatch)
    pipeline._run_factor_pipeline(task['id'])
    assert scorer.call_args.kwargs['target_symbols'] == ('000001',)
    # Context calculation deliberately stays unfiltered for model comparability.
    assert 'target_symbols' not in pipeline.calculate_stock_factors.call_args.kwargs
    db_session.expire_all()
    finished = db_session.get(AsyncTaskRecord, task['id'])
    assert finished.status == 'done'
    result = json.loads(finished.result_json)
    assert result['score_universe']['symbol_count'] == 1
    assert result['score_universe']['watchlist_id'] == pool.id


@pytest.mark.parametrize('source,ids', [
    ('watchlist', {'watchlist_id': 7}), ('positions', {'portfolio_id': 9}), ('all', {}),
])
def test_public_scoring_api_passes_scope_to_pipeline(monkeypatch, source, ids):
    from app.api.routes.scoring_facade import CreateScoringTaskRequest, api_create_scoring_task
    received = []
    monkeypatch.setattr(pipeline, 'create_factor_pipeline_task', lambda payload: received.append(payload) or {'id': 'scope'})
    request = CreateScoringTaskRequest(universe_source=source, **ids)
    assert api_create_scoring_task(request)['id'] == 'scope'
    assert received[0].universe_source == source
    for key, value in ids.items():
        assert getattr(received[0], key) == value


def test_legacy_unscoped_call_preserves_existing_all_market_behavior(db_session):
    payload = FactorPipelineCreate(train_model=False)
    freeze_score_universe(db_session, payload)
    assert payload.score_symbols is None
    assert score_universe_summary(payload) is None


def test_many_narrow_refreshes_preserve_non_target_latest_scores_and_lineage(db_session):
    first, outside = _symbol(db_session, '000001'), _symbol(db_session, '000002')
    origin = datetime(2026, 1, 5, 16)
    def score(symbol, batch, day, created, *, model='model-A', mode='ridge'):
        return Score(symbol_id=symbol.id, trade_date=day, weight_mode=mode,
                     quality_score=70, quality_grade='B', timing_score=70,
                     stage='start', action='open', priority_score=70,
                     factor_model_run_id=model, model_alpha_score=70,
                     factor_data_cutoff_at=datetime.combine(day, datetime.min.time()),
                     calc_batch_id=batch, created_at=created, published_at=created,
                     factor_scores_json=json.dumps({'_dynamic_model': {'factor_calc_batch_id': batch}}))
    current_outside = score(outside, 'original-full', date(2026, 1, 5), origin)
    previous_outside = score(outside, 'obsolete', date(2026, 1, 2), origin - timedelta(days=3))
    other_model = score(outside, 'original-other-model', date(2026, 1, 5), origin, model='model-B')
    db_session.add_all([current_outside, previous_outside, other_model])
    for index in range(35):
        db_session.add(score(first, f'narrow-{index}', date(2026, 1, 5), origin + timedelta(minutes=index + 1)))
    # A newly written old date must not supplant an unselected current date.
    db_session.add(score(outside, 'historical-backfill', date(2025, 12, 1), origin + timedelta(hours=2)))
    db_session.commit()
    expected = {current_outside.id, other_model.id}
    obsolete_id = previous_outside.id
    result = pipeline._prune_unreferenced_scores(db_session)
    db_session.commit()
    remaining = set(db_session.scalars(select(Score.id)).all())
    assert expected <= remaining
    assert obsolete_id not in remaining
    assert 'original-full' in result['protected_factor_batch_ids']
    assert result['score_rows_deleted'] > 0
