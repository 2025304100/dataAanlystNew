"""Read scores from current local inputs without changing stored observations."""
from datetime import date, timedelta

import pytest
from sqlalchemy import event

from app.models.capital_flow import CapitalFlow
from app.models.symbol import Symbol
from app.services.capital_flow_data import get_main_net_inflow_score

pytestmark = pytest.mark.whitebox
DAY = date(2026, 10, 20)


def test_history_correction_does_not_reuse_the_saved_inflow_score(db_session):
    symbol = Symbol(symbol="000001", name="资金流读取", market="SZ", asset_type="stock")
    db_session.add(symbol)
    db_session.flush()
    history = [CapitalFlow(symbol_id=symbol.id, trade_date=DAY-timedelta(days=5-i),
                           main_net_inflow=10*i, source="test") for i in range(1, 5)]
    current = CapitalFlow(symbol_id=symbol.id, trade_date=DAY, main_net_inflow=50,
                          main_net_inflow_score=81.21320343559643, source="test")
    db_session.add_all([*history, current])
    db_session.commit()
    assert get_main_net_inflow_score(db_session, symbol, DAY) == pytest.approx(81.21320343559643)

    # A later historical correction leaves today's flow and stored score
    # untouched. Five equal observations have zero deviation and score 60.
    for row in history:
        row.main_net_inflow = 50
    db_session.commit()
    writes = []
    def record(_conn, _cursor, statement, *_args):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(statement)
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert get_main_net_inflow_score(db_session, symbol, DAY) == 60.0
        assert current.main_net_inflow_score == pytest.approx(81.21320343559643)
        assert writes == []
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_score_read_keeps_null_slots_inside_the_last_nine_rows(db_session):
    symbol = Symbol(symbol="000001", name="历史缺口", market="SZ", asset_type="stock")
    db_session.add(symbol)
    db_session.flush()
    db_session.add_all([CapitalFlow(symbol_id=symbol.id, trade_date=DAY-timedelta(days=i),
        main_net_inflow=10*i if i <= 3 else None) for i in range(1, 10)])
    db_session.add_all([
        CapitalFlow(symbol_id=symbol.id, trade_date=DAY-timedelta(days=10), main_net_inflow=1_000_000),
        CapitalFlow(symbol_id=symbol.id, trade_date=DAY, main_net_inflow=40, main_net_inflow_score=75),
        CapitalFlow(symbol_id=symbol.id, trade_date=DAY+timedelta(days=1), main_net_inflow=2_000_000),
    ])
    db_session.commit()
    assert get_main_net_inflow_score(db_session, symbol, DAY) is None


@pytest.mark.parametrize("saved_score", [None, 20.0])
def test_score_read_uses_exact_asof_inputs_when_the_stored_score_is_missing_or_old(db_session, saved_score):
    symbol = Symbol(symbol="000001", name="按日期读分", market="SZ", asset_type="stock")
    db_session.add(symbol)
    db_session.flush()
    db_session.add_all([CapitalFlow(symbol_id=symbol.id, trade_date=DAY-timedelta(days=5-i),
        main_net_inflow=10*i) for i in range(1, 5)])
    current = CapitalFlow(symbol_id=symbol.id, trade_date=DAY, main_net_inflow=50,
                          main_net_inflow_score=saved_score)
    db_session.add_all([current, CapitalFlow(symbol_id=symbol.id, trade_date=DAY+timedelta(days=1),
                                            main_net_inflow=1_000_000, main_net_inflow_score=95)])
    db_session.commit()
    assert get_main_net_inflow_score(db_session, symbol, DAY) == pytest.approx(81.21320343559643)
    assert current.main_net_inflow_score == saved_score
