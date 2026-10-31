"""Public bounded bar mirroring preserves universe values after business mirror retirement."""
from datetime import date

import pytest

from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.factors.bar_mirror import mirror_daily_bars
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize("subsequent_request", ["repeat", "in_place_correction", "cancel_and_resume"])
def test_bounded_mirror_rejects_business_overlay_after_repeat_correction_or_resume(
    db_session, tmp_path, subsequent_request,
):
    business = Symbol(symbol="600001", name="Temporary overlap", asset_type="stock", market="sh")
    universe = UniverseSymbol(symbol="600001", name="Temporary overlap", asset_type="stock", market="sh", region="cn")
    db_session.add_all([business, universe])
    db_session.flush()
    days = [date(2026, 1, day) for day in (2, 5, 6)]
    business_bars = []
    for day in [date(2026, 1, 1), *days, date(2026, 1, 7)]:
        db_session.add(UniverseDailyBar(
            universe_symbol_id=universe.id, trade_date=day,
            open=10, high=10.2, low=9.8, close=10, volume=1000,
            amount=10000, turnover_rate=2, source="isolated.universe",
        ))
        bar = DailyBar(symbol_id=business.id, trade_date=day,
            open=11, high=11.2, low=10.8, close=11, volume=2000,
            amount=22000, turnover_rate=9, source="isolated.business")
        db_session.add(bar)
        if day in days:
            business_bars.append(bar)
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / "overlapping-range.duckdb")
    options = {"warehouse": warehouse, "start_date": days[0], "end_date": days[-1], "batch_size": 1}

    def content():
        with warehouse.connection(read_only=True) as connection:
            return connection.execute("""SELECT trade_date, open, high, low, close,
                volume, amount, turnover_rate, source_origin, source,
                business_symbol_id, universe_symbol_id FROM raw_daily_bars
                WHERE symbol='600001' ORDER BY trade_date""").fetchall()

    first = mirror_daily_bars(db_session, **options)
    assert first.universe_rows == 3 and first.business_rows == 0
    expected = content()
    assert [row[0] for row in expected] == days
    assert all(row[4] == 10 and row[7:10] == (2, "universe_daily_bars", "isolated.universe") for row in expected)

    if subsequent_request == "in_place_correction":
        corrected = business_bars[1]
        previous_id = corrected.id
        corrected.open, corrected.high, corrected.low, corrected.close = 12, 12.4, 11.6, 12
        corrected.volume, corrected.amount, corrected.turnover_rate = 3000, 36000, 12
        corrected.source = "isolated.corrected"
        db_session.commit()
        assert corrected.id == previous_id
        # Business corrections no longer enter the warehouse.
    elif subsequent_request == "cancel_and_resume":
        cancelled = False

        def stop_after_first_universe_batch(phase, processed, _total, _message):
            nonlocal cancelled
            if phase == "universe_bars" and processed >= 1:
                cancelled = True

        interrupted = mirror_daily_bars(
            db_session, **options, should_cancel=lambda: cancelled,
            progress_callback=stop_after_first_universe_batch,
        )
        assert cancelled is True
        # The bounded universe mirror commits one complete symbol per batch.
        assert interrupted.universe_rows == 3 and interrupted.business_rows == 0
        assert warehouse.health().available is True

    repeated = mirror_daily_bars(db_session, **options)
    # Bounded passes replay universe corrections, never the retired overlay.
    assert content() == expected
    # A cancelled pass has already committed this symbol; its resume only
    # clears the checkpoint. The following fresh pass replays the full range.
    assert repeated.universe_rows == (0 if subsequent_request == "cancel_and_resume" else 3)
    assert repeated.business_rows == 0
    if subsequent_request == "cancel_and_resume":
        fresh = mirror_daily_bars(db_session, **options)
        assert fresh.universe_rows == 3 and fresh.business_rows == 0
        assert content() == expected
    assert warehouse.health().raw_daily_bars == 3

