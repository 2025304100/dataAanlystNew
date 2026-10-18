from datetime import date
from unittest.mock import Mock
import json

import pytest

from app.models.app_setting import AppSetting
from app.models.market_data import MarketCalendar
from app.services import trade_calendar_sync as service
from app.services.market_session import is_cn_trading_day

pytestmark = pytest.mark.whitebox


def _annual_page(year=2026):
    periods = {"元旦": ("1月1日", "1月3日"), "春节": ("2月15日", "2月23日"),
        "清明节": ("4月4日", "4月6日"), "劳动节": ("5月1日", "5月5日"),
        "端午节": ("6月19日", "6月21日"), "中秋节": ("9月25日", "9月27日"),
        "国庆节": ("10月1日", "10月7日")}
    rows = "".join(f"<tr><td>{name}：</td><td>{start}（星期一）至{end}（星期二）休市，之后照常开市。</td></tr>"
        for name, (start, end) in periods.items())
    return f"<h2>{year}年休市安排</h2><table>{rows}</table>"


def test_parse_exchange_calendar_includes_holidays_and_makeup_weekends():
    year, days = service.parse_exchange_calendar(_annual_page())
    assert year == 2026 and sum(days.values()) == 242
    assert not days[date(2026, 10, 1)]
    assert not days[date(2026, 10, 7)]
    assert days[date(2026, 10, 8)]
    assert not days[date(2026, 10, 10)]  # administrative makeup workday is still closed
    assert not days[date(2026, 10, 17)]


@pytest.mark.parametrize("html", ["<html>blocked</html>", _annual_page().replace("国庆节", "unknown")])
def test_changed_or_incomplete_page_is_rejected(html):
    with pytest.raises(ValueError):
        service.parse_exchange_calendar(html)


def test_refresh_caches_online_calendar_and_keeps_other_years(db_session, monkeypatch):
    old = MarketCalendar(trade_date=date(2025, 10, 1), is_trading_day=0)
    wrong = MarketCalendar(trade_date=date(2026, 10, 1), is_trading_day=1)
    db_session.add_all([old, wrong])
    db_session.commit()
    response = Mock(content=_annual_page().encode("utf-8"))
    fetch = Mock(return_value=response)
    monkeypatch.setattr(service.requests, "get", fetch)
    first = service.refresh_cn_trade_calendar(force=True)
    second = service.refresh_cn_trade_calendar()
    assert first["status"] == "done" and second["status"] == "cached"
    assert fetch.call_count == 1
    db_session.expire_all()
    assert db_session.query(MarketCalendar).count() == 366
    assert not is_cn_trading_day(db_session, date(2026, 10, 1))
    assert is_cn_trading_day(db_session, date(2026, 10, 8))
    assert db_session.get(MarketCalendar, date(2025, 10, 1)).is_trading_day == 0
    assert json.loads(db_session.get(AppSetting, service.STATE_KEY).value_json)["source"] == service.SOURCE_URL


def test_failed_refresh_preserves_cached_days(db_session, monkeypatch):
    db_session.add(MarketCalendar(trade_date=date(2026, 10, 1), is_trading_day=0))
    db_session.commit()
    monkeypatch.setattr(service.requests, "get", Mock(side_effect=ConnectionError("offline")))
    with pytest.raises(ConnectionError):
        service.refresh_cn_trade_calendar(force=True)
    assert db_session.get(MarketCalendar, date(2026, 10, 1)).is_trading_day == 0
    assert db_session.get(AppSetting, service.STATE_KEY) is None
