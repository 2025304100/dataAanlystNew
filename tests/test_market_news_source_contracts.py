"""Supplier contracts discovered by read-only live six-source probing."""
from datetime import datetime

import pandas as pd
import pytest

from app.services import market_events as service

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize("wrapper,expected", [
    ("_fetch_cctv_news", "20261026"),
    ("_fetch_baidu_economic_news", "20261027"),
    ("_fetch_baidu_report_news", "20261027"),
])
def test_daily_sources_get_explicit_china_date_across_utc_midnight(monkeypatch, wrapper, expected):
    monkeypatch.setattr(service, "_now", lambda: datetime(2026, 10, 26, 16, 30))
    calls = []
    def fetch(fetcher, **kwargs):
        calls.append(kwargs)
        return pd.DataFrame()
    monkeypatch.setattr("app.services.akshare_utils.call_akshare_with_retry", fetch)
    assert getattr(service, wrapper)() == []
    assert calls[0]["date"] == expected


@pytest.mark.parametrize("timestamp,expected", [
    ("2026-10-26 17:01:29", datetime(2026,10,26,9,1,29)),
    ("2026-10-26T17:01:29+08:00", datetime(2026,10,26,9,1,29)),
    ("2026-10-26T09:01:29Z", datetime(2026,10,26,9,1,29)),
    ("2026-10-26", datetime(2026,10,25,16)),
])
def test_supplier_publication_normalizes_once_to_naive_utc(monkeypatch, timestamp, expected):
    monkeypatch.setattr("app.services.akshare_utils.call_akshare_with_retry",
        lambda *args, **kwargs: pd.DataFrame([{"标题":"央行发布新的政策消息", "发布时间":timestamp}]))
    rows = service._fetch_global_news()
    assert rows[0]["published_at"] == expected
    assert rows[0]["published_at"].tzinfo is None


def test_caixin_missing_publication_stays_unknown_in_fetch_result(monkeypatch):
    monkeypatch.setattr("app.services.akshare_utils.call_akshare_with_retry",
        lambda *args, **kwargs: pd.DataFrame([{"tag":"市场", "summary":"央行发布新的政策消息", "url":"https://example.com/news"}]))
    rows = service._fetch_caixin_news()
    assert rows[0]["published_at"] is None  # do not invent supplier publication times


def test_cookie_failure_remains_an_observable_source_error(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("Failed to obtain Baidu cookies")
    monkeypatch.setattr("app.services.akshare_utils.call_akshare_with_retry", fail)
    with pytest.raises(RuntimeError, match="cookies"):
        service._fetch_baidu_economic_news()
