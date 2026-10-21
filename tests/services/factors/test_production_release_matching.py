from datetime import date
import pytest
from app.services.simulation_matching_engine import MarketBar, OrderPlan, OrderSide, match_order_plan

pytestmark = pytest.mark.whitebox


def test_explicit_no_limit_session_keeps_price_range_and_tick_checks():
    day = date(2026,10,20)
    plan = OrderPlan('explicit-no-limit',1,1,day,'NEXT_OPEN',OrderSide.BUY,100)
    bar = MarketBar(day,open=15.,high=16.,low=14.,close=15.,pre_close=10.,volume=1000000,
        price_limit_mode='NO_LIMIT')
    result = match_order_plan(plan,bar)
    assert result.final_status == 'FILLED' and result.executed_price == 15.01
    bar.open = 17.
    assert match_order_plan(plan,bar).final_status != 'FILLED'


def test_explicit_limited_session_cannot_fall_back_to_a_guessed_ten_percent():
    day = date(2026,10,20)
    plan = OrderPlan('explicit-limited',1,1,day,'NEXT_OPEN',OrderSide.BUY,100)
    bar = MarketBar(day,open=10.,high=10.1,low=9.9,close=10.,pre_close=10.,volume=1000000,
        price_limit_mode='LIMITED')
    result = match_order_plan(plan,bar)
    assert result.filled_quantity == 0 and 'PRICE_LIMIT_DATA_MISSING' in result.reason_codes
    bar.upper_limit_price=10.5;bar.lower_limit_price=9.5
    assert match_order_plan(plan,bar).final_status == 'FILLED'


def test_independent_outcome_eligibility_uses_dated_limits_for_st_and_no_limit_days():
    import pandas as pd
    from app.services.factors.production_independent_evidence import dated_tradable_targets
    days = tuple(pd.bdate_range('2026-09-01',periods=6).date)
    symbols = ('000001','000002')
    raw = {}; constraints = {}; adjusted = []
    for symbol in symbols:
        for index,day in enumerate(days):
            price = 10.
            if index == 1:
                price = 10.5 if symbol == '000001' else 12.
            row = {'open':price,'high':price,'low':price,'close':price,'volume':1000000.,'amount':price*1000000}
            raw[(symbol,day)] = row
            adjusted.append({'symbol':symbol,'trade_date':pd.Timestamp(day),**row})
            constraints[(symbol,day)] = {'price_limit_mode':'LIMITED' if symbol == '000001' else 'NO_LIMIT',
                'upper_limit_price':10.5 if symbol == '000001' else None,
                'lower_limit_price':9.5 if symbol == '000001' else None,'tick_size':.01,'is_tradable':True}
    panel = dated_tradable_targets([{'date':days[0]}],days,symbols,raw,pd.DataFrame(adjusted),constraints)
    rows = panel.set_index('symbol')
    assert not bool(rows.loc['000001','is_tradable'])  # A 5% ST limit is locked.
    assert bool(rows.loc['000002','is_tradable'])  # Explicit no-limit 20% is allowed.
