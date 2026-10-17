"""Read-only retention protection works on the supported MySQL 5.7 boundary."""
from datetime import date, datetime

import pytest
from sqlalchemy import event
from sqlalchemy.exc import ProgrammingError

from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.pipeline_task import retention_protected_score_ids


pytestmark = pytest.mark.whitebox


def _score(db, symbol_id, key, day, *, model=None, mode="manual", cutoff=None, alpha=70):
    score = Score(symbol_id=symbol_id, trade_date=date(2026, 10, day), weight_mode=mode,
        factor_model_run_id=model, factor_data_cutoff_at=cutoff, model_alpha_score=alpha,
        quality_score=70, quality_grade="B", timing_score=70, stage="start", action="open",
        priority_score=70, calc_batch_id=key, created_at=datetime(2026, 10, 16),
        published_at=datetime(2026, 10, 16))
    db.add(score)
    db.flush()
    return score.id


def test_readonly_protection_preserves_null_groups_latest_eligible_and_stable_order(db_session):
    db = db_session
    symbol = Symbol(symbol="600001", name="Temporary retention", asset_type="stock", market="CN")
    db.add(symbol)
    db.flush()
    high = datetime(2026, 10, 16, 16)
    low = datetime(2026, 10, 15, 16)
    # Dates outrank cutoff, and cutoff outranks the final ID tie breaker.
    _score(db, symbol.id, "old-date-high-cutoff", 10, cutoff=high)
    _score(db, symbol.id, "newer-date-null-cutoff", 11)
    _score(db, symbol.id, "newer-date-low-cutoff", 11, cutoff=low)
    winner = _score(db, symbol.id, "same-date-high-cutoff-first", 11, cutoff=high)
    winner = _score(db, symbol.id, "same-date-high-cutoff-last", 11, cutoff=high)
    eligible = _score(db, symbol.id, "model-valid", 10, model="A", mode="ridge", cutoff=low)
    _score(db, symbol.id, "model-invalid-alpha", 12, model="A", mode="ridge", cutoff=high, alpha=101)
    latest = _score(db, symbol.id, "model-latest-no-cutoff", 13, model="A", mode="ridge", alpha=70)
    other_model = _score(db, symbol.id, "other-model", 9, model="B", mode="ridge", cutoff=low)
    other_mode = _score(db, symbol.id, "other-mode", 8, model="A", mode="shadow", cutoff=low)
    _score(db, symbol.id, "null-cutoff-first", 14, model="C", mode="ridge", alpha=None)
    null_tie = _score(db, symbol.id, "null-cutoff-last", 14, model="C", mode="ridge", alpha=None)
    db.commit()
    statements = []

    def mysql57_readonly_boundary(_conn, _cursor, statement, parameters, _ctx, _many):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT"), statement
        if " OVER (" in statement.upper():
            raise ProgrammingError(statement, parameters, RuntimeError("MySQL 5.7 syntax error 1064: OVER"))

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", mysql57_readonly_boundary)
    try:
        assert retention_protected_score_ids(db) == {winner, eligible, latest, other_model, other_mode, null_tie}
    finally:
        event.remove(engine, "before_cursor_execute", mysql57_readonly_boundary)
    assert statements


def test_readonly_protection_keeps_all_three_evidence_reference_types(db_session):
    from app.models.decision_engine import DecisionEvidence, DecisionRun, StrategyExecutionSnapshot
    from app.models.journal_entry import JournalEntry
    from app.models.portfolio import Portfolio
    from app.models.trade_setup import TradeSetup

    db = db_session
    symbol = Symbol(symbol="600002", name="Referenced score", asset_type="stock", market="CN")
    portfolio = Portfolio(name="Temporary reference fixture", account_type="paper", total_capital=1000,
                          investable_ratio=.8, cash_reserve_ratio=.2)
    db.add_all([symbol, portfolio])
    db.flush()
    current = _score(db, symbol.id, "current", 16)
    decision = _score(db, symbol.id, "decision-old", 15)
    journal = _score(db, symbol.id, "journal-old", 14)
    setup = _score(db, symbol.id, "setup-old", 13)
    _score(db, symbol.id, "unreferenced-old", 12)
    stamp = datetime(2026, 10, 16)
    db.add(StrategyExecutionSnapshot(id="retention-snapshot", portfolio_id=portfolio.id,
        decision_clock_json="{}", member_snapshot_json="[]", snapshot_hash="retention", effective_from=stamp))
    db.flush()
    db.add(DecisionRun(id="retention-decision-run", strategy_snapshot_id="retention-snapshot",
        portfolio_id=portfolio.id, run_type="research_preflight", trade_date=date(2026, 10, 16),
        decision_at=stamp, data_cutoff_at=stamp, execution_at=stamp))
    db.flush()
    db.add(DecisionEvidence(id="retention-evidence", decision_run_id="retention-decision-run",
        strategy_snapshot_id="retention-snapshot", portfolio_id=portfolio.id, symbol_id=symbol.id,
        trade_date=date(2026, 10, 16), decision_at=stamp, data_cutoff_at=stamp, execution_at=stamp,
        action="HOLD", content_hash="retention-evidence", score_id=decision))
    db.add(JournalEntry(portfolio_id=portfolio.id, symbol_id=symbol.id, entry_type="review",
                        title="Retained reference", score_id=journal))
    db.add(TradeSetup(portfolio_id=portfolio.id, symbol_id=symbol.id, score_id=setup,
        stage="start", action="open", recommended_position_pct=0, recommended_position_amount=0))
    db.commit()
    statements = []

    def readonly(_conn, _cursor, statement, _parameters, _ctx, _many):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT"), statement

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", readonly)
    try:
        assert retention_protected_score_ids(db) == {current, decision, journal, setup}
    finally:
        event.remove(engine, "before_cursor_execute", readonly)
    assert len(statements) == 5
