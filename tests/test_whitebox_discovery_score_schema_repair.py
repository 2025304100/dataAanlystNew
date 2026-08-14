from __future__ import annotations

from sqlalchemy import inspect, select, text

from app.db.init_db import _ensure_sqlite_score_columns
from app.models.score import Score


def test_legacy_scores_table_repairs_traceability_columns(db_session):
    """A legacy scores table must remain queryable by the current Score ORM."""
    engine = db_session.get_bind()
    db_session.commit()

    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_scores_factor_set_id"))
        conn.execute(text("ALTER TABLE scores DROP COLUMN factor_set_id"))
        conn.execute(text("ALTER TABLE scores DROP COLUMN factor_member_versions_json"))

    before = {column["name"] for column in inspect(engine).get_columns("scores")}
    assert "factor_set_id" not in before
    assert "factor_member_versions_json" not in before

    _ensure_sqlite_score_columns(engine)
    _ensure_sqlite_score_columns(engine)

    after = {column["name"] for column in inspect(engine).get_columns("scores")}
    assert "factor_set_id" in after
    assert "factor_member_versions_json" in after

    # This is the same full-model SELECT shape that previously failed discovery.
    assert db_session.execute(select(Score).limit(1)).scalar_one_or_none() is None
