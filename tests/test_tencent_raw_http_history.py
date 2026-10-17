"""Exercise actual source-chain wiring; replace only the HTTP/primary SDK boundaries."""
from datetime import date, timedelta
import json
from types import SimpleNamespace

import pytest
import requests

from app.services import akshare_registry, market_data
from app.services.market_data_sources.sources import tencent_history
from app.services.market_data_sources.sources import akshare_source
from app.services.market_data_sources.sources.akshare_source import TxStockSource
from app.services.market_data_sources.sources.base import SourceUnavailable


DAY = date(2026, 8, 7)
ROW = [str(DAY), "40.13", "43.07", "43.29", "39.44", "1110388.00"]


@pytest.fixture
def isolated_sources(monkeypatch):
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", True)
    monkeypatch.setattr(akshare_registry, "_config_cache", {
        key: {"enabled": True, "delay_min_ms": 0, "delay_max_ms": 0, "max_retries": 1}
        for key in ("stock_zh_a_hist", "stock_zh_a_daily", "stock_zh_a_hist_tx")})

    def primary_failed(**_kwargs):
        raise ValueError("isolated primary unavailable")

    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", primary_failed)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_daily", primary_failed)
    monkeypatch.setattr(market_data, "fetch_sina_history_raw", primary_failed, raising=False)
    monkeypatch.setattr(akshare_source, "fetch_sina_history_raw", primary_failed, raising=False)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist_tx", lambda **_kwargs:
        pytest.fail("The ambiguous SDK must not be contacted"))


def install_http(monkeypatch, keys=None, *, symbol="sh601168", code=0, status=200):
    calls, closed = [], []
    body = {"code": code, "data": {symbol: keys if keys is not None else {"qfqday": [ROW]}}}
    payload = ("kline_dayqfq2026=" + json.dumps(body)).encode()

    def get(url, **kwargs):
        calls.append((url, kwargs))
        def raise_status():
            if status >= 400:
                raise requests.HTTPError(f"HTTP {status}")
        return SimpleNamespace(status_code=status, content=payload,
            raise_for_status=raise_status,
            close=lambda: closed.append(True))

    monkeypatch.setattr(tencent_history.requests, "get", get)
    return calls, closed


def fetch(route, *, start=DAY, end=DAY, adjust="qfq", symbol="601168", market="sh"):
    model = SimpleNamespace(symbol=symbol, market=market, asset_type="stock")
    return (market_data._fetch_history(model, start, end, adjust) if route == "legacy"
            else TxStockSource().fetch(model, start, end, adjust))


@pytest.mark.parametrize("route", ["legacy", "modern"])
def test_qfqday_is_selected_when_unadjusted_day_also_exists(monkeypatch, isolated_sources, route):
    calls, closed = install_http(monkeypatch, {"day": [[str(DAY), "27.70", "26.37", "27.74", "26.12", "575533"]],
        "qfqday": [ROW]})
    frame = fetch(route)
    assert frame.iloc[0].close == 43.07 and frame.iloc[0].volume == 111038800
    assert frame.amount.isna().all() and frame.turnover_rate.isna().all()
    assert frame.trade_date.dt.date.tolist() == [DAY]
    assert frame.attrs["history_response"]["returned_mode"] == "qfqday"
    assert frame.attrs["history_response"]["raw_response_archived"] is False
    assert calls == [(tencent_history.TENCENT_HISTORY_URL, {"params": {
        "_var": "kline_dayqfq2026", "param": "sh601168,day,2026-08-07,2026-08-07,640,qfq"},
        "timeout": (5, 15), "allow_redirects": False})]
    assert closed == [True]


@pytest.mark.parametrize("route", ["legacy", "modern"])
@pytest.mark.parametrize("keys", [{"day": [ROW]}, {"hfqday": [ROW]}, {"qfqday": []}])
def test_missing_requested_mode_is_not_a_fallback_to_raw(monkeypatch, isolated_sources, route, keys):
    calls, closed = install_http(monkeypatch, keys)
    error = ValueError if route == "legacy" else SourceUnavailable
    with pytest.raises(error, match="tencent_adjustment_unverified"):
        fetch(route)
    assert len(calls) == 1 and closed == [True]


@pytest.mark.parametrize("route", ["legacy", "modern"])
@pytest.mark.parametrize("keys", [
    {"qfqday": [ROW, ROW]},
    {"qfqday": [["2026-08-06", *ROW[1:]]]},
    {"qfqday": [["2026-08-10", *ROW[1:]]]},
    {"qfqday": [["2026-08-07T14:59:00", *ROW[1:]]]},
])
def test_duplicate_wrong_or_intraday_dates_are_rejected(monkeypatch, isolated_sources, route, keys):
    install_http(monkeypatch, keys)
    with pytest.raises(ValueError if route == "legacy" else SourceUnavailable):
        fetch(route)


@pytest.mark.parametrize("route", ["legacy", "modern"])
def test_wrong_instrument_is_rejected(monkeypatch, isolated_sources, route):
    install_http(monkeypatch, symbol="sz000001")
    with pytest.raises(ValueError if route == "legacy" else SourceUnavailable,
                       match="tencent_adjustment_unverified"):
        fetch(route)


@pytest.mark.parametrize("route", ["legacy", "modern"])
def test_range_over_640_calendar_days_fails_before_http(monkeypatch, isolated_sources, route):
    calls, _ = install_http(monkeypatch)
    with pytest.raises(ValueError if route == "legacy" else SourceUnavailable,
                       match="range_exceeds_640_calendar_days"):
        fetch(route, start=DAY - timedelta(days=640))
    assert calls == []


@pytest.mark.parametrize("adjust,key", [("", "day"), ("qfq", "qfqday"), ("hfq", "hfqday")])
def test_all_adjustments_are_selected_explicitly(monkeypatch, isolated_sources, adjust, key):
    calls, _ = install_http(monkeypatch, {key: [ROW]})
    result = fetch("modern", adjust=adjust)
    assert result.attrs["history_response"]["actual_adjustment"] == adjust
    assert calls[0][1]["params"]["param"].endswith(",640," + adjust)


def test_640_calendar_day_boundary_is_not_silently_shortened(monkeypatch, isolated_sources):
    calls, _ = install_http(monkeypatch)
    start = DAY - timedelta(days=639)
    result = fetch("modern", start=start)
    assert result.iloc[0].close == 43.07
    assert f",{start.isoformat()},{DAY.isoformat()},640,qfq" in calls[0][1]["params"]["param"]


def test_disabled_endpoint_is_never_contacted(monkeypatch, isolated_sources):
    calls, _ = install_http(monkeypatch)
    akshare_registry._config_cache["stock_zh_a_hist_tx"]["enabled"] = False
    with pytest.raises(SourceUnavailable, match="disabled by configuration"):
        fetch("modern")
    assert calls == []


def test_legacy_bj_fallback_does_not_send_unsupported_tencent_symbol(monkeypatch, isolated_sources):
    calls, _ = install_http(monkeypatch)
    with pytest.raises(ValueError, match="unsupported_symbol"):
        fetch("legacy", symbol="920072", market="bj")
    assert calls == []


@pytest.mark.parametrize("route", ["legacy", "modern"])
def test_redirect_is_not_followed_or_accepted(monkeypatch, isolated_sources, route):
    calls, closed = install_http(monkeypatch, status=302)
    with pytest.raises(ValueError if route == "legacy" else SourceUnavailable, match="unexpected_http_status"):
        fetch(route)
    assert len(calls) == 1 and calls[0][1]["allow_redirects"] is False
    assert closed == [True]


@pytest.mark.parametrize("route", ["legacy", "modern"])
@pytest.mark.parametrize("row", [
    [str(DAY), "40.13", None, "43.29", "39.44", "1110388"],
    [str(DAY), "40.13", "43.07", "41", "39.44", "1110388"],
    [str(DAY), "40.13", "43.07", "43.29", "39.44", "-1"],
])
def test_invalid_price_or_negative_quantity_cannot_be_a_successful_source(monkeypatch, isolated_sources, route, row):
    calls, _ = install_http(monkeypatch, {"qfqday": [row]})
    with pytest.raises(ValueError if route == "legacy" else SourceUnavailable, match="history_evidence_invalid"):
        fetch(route)
    assert len(calls) == 1
