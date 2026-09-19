from __future__ import annotations

from datetime import date, datetime

import pytest

from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.candidate_promote import _fallback_legacy_candidates


pytestmark = pytest.mark.whitebox


def test_legacy_candidate_includes_real_timestamp_and_credibility(db_session):
    created_at = datetime(2026, 7, 12, 2, 42, 35)
    symbol = Symbol(
        symbol="561980",
        name="Semiconductor ETF",
        asset_type="etf",
        market="sh",
        board="main",
        is_active=1,
    )
    scan_run = ScanRun(
        run_name="legacy candidate",
        scope_snapshot="{}",
        status="done",
        created_at=created_at,
    )
    db_session.add_all([symbol, scan_run])
    db_session.flush()
    db_session.add_all(
        [
            ScanResult(
                scan_run_id=scan_run.id,
                symbol_id=symbol.id,
                result_type="quality",
                rank_no=1,
                quality_score=77,
                timing_score=100,
                priority_score=87,
                stage="overheat",
                action="reduce",
                warning_days=3,
                valid_days=5,
                is_frozen=1,
                created_at=created_at,
            ),
            Score(
                symbol_id=symbol.id,
                trade_date=date(2026, 7, 12),
                quality_score=77,
                quality_grade="B",
                timing_score=100,
                priority_score=87,
                stage="overheat",
                action="reduce",
                data_credibility=1.0,
                calc_batch_id="legacy-labels",
                created_at=created_at,
            ),
        ]
    )
    db_session.commit()

    rows = _fallback_legacy_candidates(
        db_session,
        scan_run_id=scan_run.id,
        min_score=55,
        limit=10,
    )

    assert rows[0]["created_at"] == created_at.isoformat()
    assert rows[0]["data_credibility"] == 1.0
