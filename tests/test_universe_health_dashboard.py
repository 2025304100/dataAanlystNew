from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, event, select

from app.models.market_data import MarketCalendar
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.universe_health import coverage_percent, read_universe_quality
from app.services.universe_sync import get_universe_stats

import pytest

pytestmark = pytest.mark.whitebox


NOW = datetime(2026, 10, 25, 18, tzinfo=ZoneInfo('Asia/Shanghai'))


@pytest.mark.parametrize('n,total,expected', [(5554,5555,99.98),(1402,1403,99.93),(999999,1000000,99.99),(1717,1717,100),(0,0,0)])
def test_incomplete_counts_never_round_to_100(n,total,expected):
    assert coverage_percent(n,total) == expected


def seed(db):
    rows = [
        UniverseSymbol(symbol='510300',region='cn',market='sh',asset_type='etf',is_synced=1,bar_count=1,last_bar_date=date(2026,10,23)),
        UniverseSymbol(symbol='560650',region='cn',market='sh',asset_type='etf',is_synced=1,bar_count=27,last_bar_date=date(2026,10,20)),
        UniverseSymbol(symbol='600000',region='cn',market='sh',asset_type='stock',is_synced=1,bar_count=7,last_bar_date=date(2026,10,23)),
        UniverseSymbol(symbol='600001',region='cn',market='sh',asset_type='stock',is_synced=0,bar_count=1,last_bar_date=date(2026,10,23),listed_at=date(2020,1,1)),
        UniverseSymbol(symbol='SPY',region='us',market='us',asset_type='etf',is_synced=1,bar_count=1,last_bar_date=date(2026,10,23)),
        UniverseSymbol(symbol='510999',region='cn',market='sh',asset_type='etf',is_synced=1,bar_count=1,last_bar_date=date(2026,10,30),listed_at=date(2027,1,1)),
    ]
    db.add_all(rows);db.flush()
    dates = {0:date(2026,10,23),1:date(2026,10,16),3:date(2026,10,23),4:date(2026,10,23),5:date(2026,10,30)}
    for key,day in dates.items():
        db.add(UniverseDailyBar(universe_symbol_id=rows[key].id,trade_date=day,open=1,high=2,low=.5,close=1,source='original',volume=None))
    for offset in range(7):
        day=date(2026,10,19)+timedelta(days=offset)
        db.add(MarketCalendar(trade_date=day,is_trading_day=int(day.weekday()<5)))
    db.commit()


def test_full_record_coverage_does_not_certify_listing_dates_or_freshness(db_session):
    seed(db_session)
    report=read_universe_quality(db_session,now=NOW)
    etfs=report['scopes']['cn-etf']
    assert etfs['coverage_pct']==100 and etfs['with_bars']==3
    assert etfs['listing_date_recorded']==0 and etfs['listing_date_missing']==2 and etfs['listing_date_invalid']==1
    assert etfs['listing_date_pct']==0
    assert etfs['freshness']=={'current':1,'earlier':1,'unverified':1,'no_bars':0}
    assert etfs['future_bar_dates']==1
    assert report['historical_calendar_coverage']==report['price_quantity_basis']=='not_verified'


def test_actual_bars_and_all_registered_symbols_drive_coverage(db_session):
    seed(db_session)
    report=read_universe_quality(db_session,now=NOW)
    stocks=report['scopes']['cn-stock']
    assert stocks['total']==2 and stocks['with_bars']==1 and stocks['without_bars']==1
    assert stocks['sync_flag_without_bars']==1
    assert stocks['cache_mismatches']==1
    assert report['with_bars']==5 and report['without_bars']==1
    assert report['missing_bar_samples'][0]['symbol']=='600000'
    assert {r['symbol'] for r in report['cache_mismatch_samples']}=={'560650','600000'}


def test_weekend_uses_confirmed_completed_session_and_us_stays_unverified(db_session):
    seed(db_session)
    report=read_universe_quality(db_session,now=NOW)
    assert report['scopes']['cn-etf']['expected_latest_date']==date(2026,10,23)
    us=report['scopes']['us-etf']
    assert us['expected_latest_date'] is None and us['calendar_status']=='unverified'
    assert us['freshness']=={'current':0,'earlier':0,'unverified':1,'no_bars':0}


def test_synced_etf_with_no_bars_cannot_count_as_record_coverage(db_session):
    seed(db_session)
    symbol_id=db_session.scalar(select(UniverseSymbol.id).where(UniverseSymbol.symbol=='SPY'))
    db_session.execute(delete(UniverseDailyBar).where(UniverseDailyBar.universe_symbol_id==symbol_id));db_session.commit()
    report=read_universe_quality(db_session,now=NOW)
    us=report['scopes']['us-etf']
    assert us['sync_flag_count']==1 and us['with_bars']==0 and us['without_bars']==1
    assert us['coverage_pct']==0 and us['sync_flag_without_bars']==1
    assert report['stored_bar_rows']==4


def test_missing_calendar_never_guesses_a_weekday(db_session):
    seed(db_session)
    db_session.execute(delete(MarketCalendar));db_session.commit()
    report=read_universe_quality(db_session,now=NOW)
    group=report['scopes']['cn-etf']
    assert group['expected_latest_date'] is None and group['freshness']['current']==0
    assert group['freshness']['unverified']==3


def test_intraday_bar_does_not_certify_a_completed_session(db_session):
    seed(db_session)
    report=read_universe_quality(db_session,now=datetime(2026,10,23,14,tzinfo=ZoneInfo('Asia/Shanghai')))
    assert report['scopes']['cn-etf']['expected_latest_date']==date(2026,10,22)
    assert report['scopes']['cn-etf']['freshness']['current']==0
    assert report['scopes']['cn-etf']['freshness']['unverified']==2


def test_dashboard_queries_preserve_all_fields_and_never_write(db_session):
    seed(db_session)
    models=(UniverseSymbol,UniverseDailyBar,MarketCalendar)
    before={m.__tablename__:db_session.execute(select(m.__table__)).all() for m in models}
    def reject_write(conn,cursor,statement,params,context,executemany):
        assert statement.lstrip().upper().startswith('SELECT'),statement
    engine=db_session.get_bind();event.listen(engine,'before_cursor_execute',reject_write)
    try:
        read_universe_quality(db_session,now=NOW)
        stats=get_universe_stats(db_session)
    finally:event.remove(engine,'before_cursor_execute',reject_write)
    assert stats['synced_symbols']==5 and stats['total_symbols']==6 and stats['total_bars']==5
    assert stats['quality']['with_bars']==5
    assert before=={m.__tablename__:db_session.execute(select(m.__table__)).all() for m in models}


def test_empty_registry_has_no_complete_or_latest_claim(db_session):
    report=read_universe_quality(db_session,now=NOW)
    assert report['scopes']=={} and report['with_bars']==report['without_bars']==0
    assert report['historical_calendar_coverage']=='not_verified'
