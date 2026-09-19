from datetime import date, datetime
from types import SimpleNamespace

from app.services.backtest import _latest_score_on_or_before


def _score(score_id, trade_date, published_at):
    return SimpleNamespace(id=score_id, trade_date=trade_date, published_at=published_at)


def test_latest_score_excludes_future_publication_at_pit_cutoff():
    scores = {
        7: [
            _score(1, date(2026, 1, 2), datetime(2026, 1, 2, 8)),
            # Revision has the same score date but was published later.
            _score(2, date(2026, 1, 2), datetime(2026, 1, 5, 8)),
        ]
    }
    selected = _latest_score_on_or_before(
        scores, 7, date(2026, 1, 2),
        published_cutoff_at=datetime(2026, 1, 3, 0),
    )
    assert selected is scores[7][0]


def test_latest_score_without_pit_cutoff_preserves_legacy_selection():
    scores = {
        7: [
            _score(1, date(2026, 1, 2), datetime(2026, 1, 2, 8)),
            _score(2, date(2026, 1, 2), datetime(2026, 1, 5, 8)),
        ]
    }
    assert _latest_score_on_or_before(scores, 7, date(2026, 1, 2)).id == 2
