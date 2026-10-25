"""Combination score lookup: stable output, bounded memory, no lost revisions."""
from datetime import date, datetime, timedelta
import json
import tracemalloc

import pytest
from sqlalchemy import insert

from app.models.score import Score
from app.models.symbol import Symbol
from app.services.score_query_service import fetch_latest_pit_scores

pytestmark = pytest.mark.whitebox
DAY = date(2026, 10, 23)
CUTOFF = datetime(2026, 10, 23, 8)


def seed(db, count=2, days=2):
    symbols = [Symbol(symbol=f"600{i:03}", name=f"query-{i}", market="SH", asset_type="stock")
               for i in range(count)]
    db.add_all(symbols)
    db.flush()
    rows = []
    for symbol in symbols:
        for offset in range(days):
            rows.append(dict(symbol_id=symbol.id, trade_date=DAY-timedelta(days=offset),
                factor_model_run_id="query-model", factor_set_id="query-set", weight_mode="ridge",
                factor_data_cutoff_at=CUTOFF-timedelta(days=offset), published_at=CUTOFF-timedelta(days=offset),
                model_alpha_score=80, quality_score=80, timing_score=80, priority_score=80,
                quality_grade="A", stage="growth", action="hold", calc_batch_id=f"batch-{offset}",
                factor_member_versions_json=json.dumps({"factor": {"version": 1}}),
                factor_scores_json=json.dumps({"evidence": "x"*2048})))
    db.execute(insert(Score), rows)
    db.commit()
    return [symbol.id for symbol in symbols]


def lookup(db, ids, **extra):
    return fetch_latest_pit_scores(db, factor_model_run_id="query-model", factor_set_id="query-set",
        symbol_ids=ids, decision_date=DAY, data_cutoff_at=CUTOFF, require_published_at=True, **extra)


def test_equal_scores_have_stable_symbol_order_independent_of_insert_order(db_session):
    ids = seed(db_session)
    rows = lookup(db_session, list(reversed(ids)))
    assert [(row.symbol_id, row.score_rank) for row in rows] == [(ids[0], 1), (ids[1], 2)]


def test_lookup_does_not_materialize_the_entire_score_history(db_session):
    ids = seed(db_session, count=6, days=300)
    tracemalloc.start()
    try:
        rows = lookup(db_session, ids)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert {row.symbol_id for row in rows} == set(ids)
    assert all(row.trade_date == DAY for row in rows)
    assert peak < 6_000_000, f"lookup loaded history into memory: {peak} bytes"


def test_version_lookup_preserves_older_matching_revision_and_missing_symbol(db_session):
    ids = seed(db_session)
    from sqlalchemy import update
    db_session.execute(update(Score).where(Score.trade_date == DAY).values(
        factor_member_versions_json='{"factor":{"version":2}}'))
    db_session.commit()
    rows = lookup(db_session, ids+[999999], factor_member_versions={"factor": 1})
    assert {row.symbol_id for row in rows} == set(ids)
    assert all(row.trade_date == DAY-timedelta(days=1) for row in rows)
