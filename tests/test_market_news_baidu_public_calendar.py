"""Baidu vendor HTTP contract through the public collection API."""
from datetime import datetime
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes.market_events import router
from app.db.session import get_db
from app.services.baidu_market_calendar import fetch_baidu_calendar

pytestmark = pytest.mark.whitebox


def test_baidu_collection_uses_public_calendar_without_cookie_preflight(db_session,monkeypatch):
    import requests
    from curl_cffi import requests as curl_requests
    day=datetime.now().strftime("%Y-%m-%d")
    # The website response lacks cookies, while the public data API is usable.
    page=Mock(cookies={},text="<html>public page</html>")
    page.raise_for_status.return_value=None
    monkeypatch.setattr(curl_requests.Session,"get",lambda *args,**kwargs:page)
    response=Mock()
    response.raise_for_status.return_value=None
    response.json.return_value={"ResultCode":0,"Result":{"calendarInfo":[{"date":day,"total":1,"list":[
        {"title":"央行公布经济指标","region":"中国","date":day,"time":"08:00","pubVal":"3.2","formerVal":"3.1","indicateVal":"3.0"}]}]}}
    monkeypatch.setattr(requests,"get",lambda *args,**kwargs:response)
    app=FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db]=lambda:db_session
    with TestClient(app) as client:
        result=client.post("/market-events/collect",json={"sources":["baidu"]})
        assert result.status_code==200
        assert result.json()["status"]=="done"
        assert result.json()["collected"]==1
        item=client.get("/market-events").json()["events"][0]
        assert "央行公布经济指标" in item["title"]


def test_report_calendar_has_a_company_title_and_date_instead_of_stock_code_as_title(monkeypatch):
    response=Mock()
    response.raise_for_status.return_value=None
    response.json.return_value={"ResultCode":0,"Result":{"calendarInfo":[{"date":"2026-10-26","total":1,
        "list":[{"name":"测试公司","code":"000001","date":"2026-10-26","time":"--","reportType":"三季报","exchange":"SZ"}]}]}}
    monkeypatch.setattr("requests.get",lambda *args,**kwargs:response)
    frame=fetch_baidu_calendar(date="20261026",category="report_time")
    assert frame.iloc[0]["title"]=="财报日历：测试公司（000001）三季报"
    assert frame.iloc[0]["date"]=="2026-10-26"
    assert "日历计划时间" in frame.iloc[0]["summary"]


@pytest.mark.parametrize("body",[
    {"ResultCode":1,"Result":{}},
    {"ResultCode":0,"Result":{}},
    {"ResultCode":0,"Result":{"calendarInfo":[{"date":"2020-01-01","list":[]}]}},
    {"ResultCode":0,"Result":{"calendarInfo":[{"date":"2026-10-26","list":[{}]}]}},
])
def test_invalid_vendor_response_is_a_failure_not_healthy_empty(monkeypatch,body):
    response=Mock()
    response.raise_for_status.return_value=None
    response.json.return_value=body
    monkeypatch.setattr("requests.get",lambda *args,**kwargs:response)
    with pytest.raises(RuntimeError):fetch_baidu_calendar(date="20261026",category="economic_data")


def test_valid_empty_calendar_is_distinct_from_schema_failure(monkeypatch):
    response=Mock()
    response.raise_for_status.return_value=None
    response.json.return_value={"ResultCode":0,"Result":{"calendarInfo":[]}}
    monkeypatch.setattr("requests.get",lambda *args,**kwargs:response)
    assert fetch_baidu_calendar(date="20261026",category="economic_data").empty
