"""Isolated HTTP/SQL regressions: opening pages must not clean or revalue rows."""
from datetime import date, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select

from app.api.routes import dashboard, sim_accounts
from app.db.session import get_db
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio, Position
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.sim_accounts import build_sim_account_summary, latest_price_for_symbol, latest_prices_for_symbols


@pytest.fixture
def dashboard_case(db_session, monkeypatch):
    db = db_session
    now = datetime(2026, 10, 17, 2)
    monkeypatch.setattr(dashboard, '_now', lambda: now)
    portfolio = Portfolio(name='Readonly dashboard', account_type='simulated', total_capital=100000,
        investable_ratio=.9, cash_reserve_ratio=.1, currency='CNY')
    db.add(portfolio); db.flush()
    symbols = [Symbol(symbol=f'60000{i}', name=f'Isolated {i}', market='sh', asset_type='stock') for i in range(1, 10)]
    db.add_all(symbols); db.flush()
    run = ScanRun(run_name='isolated', portfolio_id=portfolio.id, scope_snapshot='{}', status='done')
    db.add(run); db.flush()
    candidates = []
    for i, symbol in enumerate(symbols):
        row = ScanResult(scan_run_id=run.id, symbol_id=symbol.id, result_type='executable', rank_no=i + 1,
            priority_score=90 - i, quality_score=70, timing_score=60, valid_days=5,
            created_at=now - timedelta(days=20 if i < 6 or i == 7 else 1),
            is_frozen=int(i == 7), is_active=int(i != 8))
        db.add(row); candidates.append(row)
    positions = [Position(portfolio_id=portfolio.id, symbol_id=symbols[0].id, quantity=100,
        avg_cost=10, latest_price=7, market_value=700, position_pct=.007, asset_type='stock'),
        Position(portfolio_id=portfolio.id, symbol_id=symbols[1].id, quantity=10,
        avg_cost=8, latest_price=12, market_value=120, position_pct=.0012, asset_type='stock'),
        Position(portfolio_id=portfolio.id, symbol_id=symbols[2].id, quantity=2,
        avg_cost=5, latest_price=0, market_value=10, position_pct=.0001, asset_type='stock')]
    db.add_all(positions)
    # A newer trading day was loaded first; old history is backfilled afterwards.
    newest = DailyBar(symbol_id=symbols[0].id, trade_date=date(2026, 10, 16), open=20, high=20, low=20, close=20)
    db.add(newest); db.flush()
    historical = DailyBar(symbol_id=symbols[0].id, trade_date=date(2026, 10, 1), open=3, high=3, low=3, close=3)
    db.add(historical); db.commit()
    assert historical.id > newest.id
    app = FastAPI()
    app.include_router(dashboard.router, prefix='/api/v1')
    app.include_router(sim_accounts.router, prefix='/api/v1')
    def session_override():
        yield db
    app.dependency_overrides[get_db] = session_override
    with TestClient(app) as client:
        yield client, db, portfolio, symbols, candidates, positions


def _snapshot(db, model):
    return [tuple(getattr(row, column.name) for column in model.__table__.columns)
        for row in db.scalars(select(model).order_by(model.id)).all()]


@pytest.mark.parametrize('endpoint', ['dashboard/workbench', 'dashboard/overview', 'sim-account'])
def test_gets_filter_without_deleting_committing_or_dirtying_positions(dashboard_case, endpoint):
    client, db, portfolio, _, _, _ = dashboard_case
    before = {model: _snapshot(db, model) for model in (ScanResult, Position)}
    writes, commits = [], []
    def on_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
            writes.append(statement)
    def on_commit(session):
        commits.append(True)
    event.listen(db.get_bind(), 'before_cursor_execute', on_sql)
    event.listen(db, 'before_commit', on_commit)
    try:
        url = (f'/api/v1/portfolios/{portfolio.id}/sim-account' if endpoint == 'sim-account'
            else f'/api/v1/{endpoint}?portfolio_id={portfolio.id}&market_group=all')
        for _ in range(2):
            response = client.get(url)
            assert response.status_code == 200, response.text
            assert not db.dirty and not db.deleted
            db.flush()  # A caller's later write must not persist page-read mutations.
        assert writes == [] and commits == []
        assert {model: _snapshot(db, model) for model in before} == before
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', on_sql)
        event.remove(db, 'before_commit', on_commit)


def test_overview_and_workbench_agree_on_visible_candidates_without_consuming_history(dashboard_case):
    client, db, portfolio, symbols, candidates, _ = dashboard_case
    workbench = client.get(f'/api/v1/dashboard/workbench?portfolio_id={portfolio.id}&candidate_limit=1').json()
    overview = client.get(f'/api/v1/dashboard/overview?portfolio_id={portfolio.id}').json()
    assert len(workbench['candidates']) == 1
    assert {row['symbol_id'] for row in overview['top_candidates']} == {symbols[6].id, symbols[7].id}
    assert workbench['latest_scan']['executable_count'] == 2  # Before display limit.
    assert workbench['latest_scan']['total_results'] == len(candidates)
    assert len(db.scalars(select(ScanResult)).all()) == len(candidates)


def test_backfilled_history_never_replaces_latest_price_and_missing_bars_keep_fallback(dashboard_case):
    client, db, portfolio, symbols, _, _ = dashboard_case
    assert latest_price_for_symbol(db, symbols[0].id) == 20
    workbench = client.get(f'/api/v1/dashboard/workbench?portfolio_id={portfolio.id}').json()
    account = client.get(f'/api/v1/portfolios/{portfolio.id}/sim-account').json()['summary']
    positions = {row['symbol_id']: row for row in workbench['positions']}
    assert positions[symbols[0].id]['latest_price'] == 20
    assert positions[symbols[0].id]['unrealized_pnl'] == 1000
    assert positions[symbols[1].id]['latest_price'] == 12  # Cached quote if no bar.
    assert positions[symbols[2].id]['latest_price'] == 5   # Cost if no quote or bar.
    assert sum(row['market_value'] for row in positions.values()) == account['market_value'] == 2130


def test_writing_account_summary_can_still_refresh_positions_explicitly(dashboard_case):
    _, db, portfolio, _, _, positions = dashboard_case
    summary = build_sim_account_summary(db, portfolio)
    db.commit()
    db.refresh(positions[0])
    assert summary['market_value'] == 2130
    assert positions[0].latest_price == 20 and positions[0].market_value == 2000


@pytest.mark.parametrize('age_days,frozen,active,visible', [(4, 0, 1, True), (5, 0, 1, False),
    (100, 1, 1, True), (0, 0, 0, False)])
def test_candidate_expiry_boundary_and_explicit_visibility(age_days, frozen, active, visible, monkeypatch):
    now = datetime(2026, 10, 17, 2)
    monkeypatch.setattr(dashboard, '_now', lambda: now)
    candidate = ScanResult(created_at=now - timedelta(days=age_days), valid_days=5,
        is_frozen=frozen, is_active=active)
    assert dashboard._is_scan_result_valid(candidate) is visible


@pytest.mark.parametrize('endpoint,expected_bar_queries', [('workbench', 2), ('sim-account', 1)])
def test_page_valuation_batches_quotes_instead_of_querying_each_position(dashboard_case, endpoint, expected_bar_queries):
    client, db, portfolio, _, _, _ = dashboard_case
    queries = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if 'daily_bars' in statement.lower():
            queries.append(statement)
    event.listen(db.get_bind(), 'before_cursor_execute', record)
    try:
        url = (f'/api/v1/dashboard/workbench?portfolio_id={portfolio.id}' if endpoint == 'workbench'
            else f'/api/v1/portfolios/{portfolio.id}/sim-account')
        result = client.get(url)
        assert result.status_code == 200, result.text
        assert len(queries) == expected_bar_queries
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', record)


def test_batch_dates_are_independent_for_each_symbol_and_empty_input_has_no_io(dashboard_case):
    _, db, _, symbols, _, _ = dashboard_case
    db.add(DailyBar(symbol_id=symbols[1].id, trade_date=date(2026, 9, 30), open=15, high=15, low=15, close=15))
    db.add(DailyBar(symbol_id=symbols[1].id, trade_date=date(2026, 10, 15), open=18, high=18, low=18, close=18))
    db.commit()
    assert latest_prices_for_symbols(db, [symbols[0].id, symbols[1].id, symbols[0].id]) == {
        symbols[0].id: 20, symbols[1].id: 18}
    class NoQuery:
        def execute(self, *args, **kwargs):
            pytest.fail('empty position list queried prices')
    assert latest_prices_for_symbols(NoQuery(), []) == {}


def test_fractional_quotes_keep_precision_until_amounts_are_rounded(dashboard_case):
    client, db, portfolio, symbols, _, positions = dashboard_case
    symbols[0].asset_type = positions[0].asset_type = 'etf'
    newest = db.scalars(select(DailyBar).where(DailyBar.symbol_id == symbols[0].id)
        .order_by(DailyBar.trade_date.desc())).first()
    newest.close = 3.141
    db.commit()
    workbench = client.get(f'/api/v1/dashboard/workbench?portfolio_id={portfolio.id}').json()
    account = client.get(f'/api/v1/portfolios/{portfolio.id}/sim-account').json()['summary']
    assert account['market_value'] == sum(row['market_value'] for row in workbench['positions']) == 444.1
    assert workbench['positions'][0]['latest_price'] == 3.141
    assert account['unrealized_pnl'] == -645.9
    build_sim_account_summary(db, portfolio)
    db.commit(); db.refresh(positions[0])
    assert positions[0].latest_price == 3.141


@pytest.mark.parametrize('market_group,expected_indices', [('all', [1, 0]), ('us', [1])])
def test_latest_score_query_keeps_active_mode_per_symbol_limit_and_market_scope(dashboard_case, market_group, expected_indices):
    client, db, portfolio, symbols, _, _ = dashboard_case
    symbols[1].market = 'us'
    for symbol, weight_mode, priority, batch in [(symbols[0], 'manual', 99, 'old'),
            (symbols[0], 'manual', 10, 'new'), (symbols[0], 'ridge', 100, 'inactive-model'),
            (symbols[1], 'manual', 30, 'second'), (symbols[2], 'manual', 8, 'third')]:
        db.add(Score(symbol_id=symbol.id, trade_date=date(2026, 10, 16), quality_score=priority,
            quality_grade='B', timing_score=60, priority_score=priority, stage='start', action='watch',
            weight_mode=weight_mode, calc_batch_id=batch))
    db.commit()
    result = client.get(f'/api/v1/dashboard/workbench?portfolio_id={portfolio.id}&score_limit=2&market_group={market_group}')
    assert result.status_code == 200, result.text
    rows = result.json()['latest_scores']
    assert [row['symbol_id'] for row in rows] == [symbols[index].id for index in expected_indices]
    assert all(row['weight_mode'] == 'manual' for row in rows)
    assert [row['priority_score'] for row in rows] == [30, 10][:len(rows)]
