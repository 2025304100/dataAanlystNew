from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from app.services import valuation_provider as provider

pytestmark = pytest.mark.whitebox


def _row(code="000001", day="2026-10-16"):
    return {"SECURITY_CODE": code, "TRADE_DATE": day + " 00:00:00",
            "PE_TTM": 10, "PB_MRQ": 1.2, "TOTAL_MARKET_CAP": 100,
            "NOTLIMITED_MARKETCAP_A": 80}


def _response(rows, *, count=None, pages=1):
    return Mock(json=lambda: {"success": True, "result": {
        "data": rows, "count": len(rows) if count is None else count, "pages": pages}})


def test_daily_request_limits_dates_codes_columns_and_page_size(monkeypatch):
    get = Mock(return_value=_response([_row()]))
    monkeypatch.setattr(provider.requests, "get", get)
    rows = provider.fetch_valuation_rows(None, ["000001"], date(2026, 10, 16), date(2026, 10, 16))
    params = get.call_args.kwargs["params"]
    assert params["filter"] == '(TRADE_DATE=\'2026-10-16\')(SECURITY_CODE in ("000001"))'
    assert params["columns"] == provider.COLUMNS and "ALL" not in params["columns"]
    assert params["pageSize"] == "1" and params["pageNumber"] == "1"
    assert rows[0]["trade_date"] == date(2026, 10, 16)
    assert rows[0]["pe_ttm"] == 10
    assert get.call_count == 1


def test_five_year_backfill_filters_server_side_on_every_page(monkeypatch):
    first_page = [_row(day=str(date(2021, 10, 18) + timedelta(days=i))) for i in range(1000)]
    get = Mock(side_effect=[_response(first_page, count=1001, pages=2),
                            _response([_row()], count=1001, pages=2)])
    monkeypatch.setattr(provider.requests, "get", get)
    rows = provider.fetch_valuation_rows(None, ["000001"], date(2021, 10, 16), date(2026, 10, 16))
    assert len(rows) == 1001
    for i, call in enumerate(get.call_args_list, 1):
        params = call.kwargs["params"]
        assert params["filter"] == '(TRADE_DATE>=\'2021-10-16\')(TRADE_DATE<=\'2026-10-16\')(SECURITY_CODE in ("000001"))'
        assert params["pageNumber"] == str(i)
        assert params["pageSize"] == "1000"


@pytest.mark.parametrize("rows,count,pages", [
    ([_row(day="2020-01-01")], 1, 1),  # supplier ignored date
    ([_row(code="600519")], 1, 1),     # supplier ignored selected symbol
    ([_row()], 5000, 5000),             # unbounded response: do not follow pages
    ([_row(), _row()], 1, 1),           # duplicate/oversized page
])
def test_out_of_scope_response_is_rejected_without_more_downloads(monkeypatch, rows, count, pages):
    get = Mock(return_value=_response(rows, count=count, pages=pages))
    monkeypatch.setattr(provider.requests, "get", get)
    with pytest.raises(ValueError):
        provider.fetch_valuation_rows(None, ["000001"], date(2026, 10, 16), date(2026, 10, 16))
    assert get.call_count == 1


def test_empty_day_has_one_request_and_no_fallback(monkeypatch):
    get = Mock(return_value=Mock(json=lambda: {"success": False, "code": 9201, "result": None}))
    monkeypatch.setattr(provider.requests, "get", get)
    assert provider.fetch_valuation_rows(None, ["000001"], date(2026, 10, 16), date(2026, 10, 16)) == []
    assert get.call_count == 1


def test_network_failure_has_two_attempt_cap(monkeypatch):
    get = Mock(side_effect=ConnectionError("offline"))
    monkeypatch.setattr(provider.requests, "get", get)
    with pytest.raises(ConnectionError):
        provider.fetch_valuation_rows(None, ["000001"], date(2026, 10, 16), date(2026, 10, 16))
    assert get.call_count == 2


def test_cancel_stops_before_network(monkeypatch):
    get = Mock()
    monkeypatch.setattr(provider.requests, "get", get)
    with pytest.raises(InterruptedError):
        provider.fetch_valuation_rows(None, ["000001"], date(2026, 10, 16), date(2026, 10, 16),
                                      should_cancel=lambda: True)
    assert not get.called
