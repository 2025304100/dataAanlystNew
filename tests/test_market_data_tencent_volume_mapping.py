"""Tencent's sixth daily-K field is quantity, despite the SDK name ``amount``.

Keep the provider SDK boundary isolated: no real provider request or database
write is needed to exercise either public adapter or the legacy fallback path.
"""
from datetime import date
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services import akshare_registry, market_data
from app.services.market_data_sources.sources._normalize import normalize_cn_stock_tx_history
from app.services.market_data_sources.sources.akshare_source import TxStockSource
from app.services.market_data_sources.sources.base import SourceUnavailable
from app.services.market_data_sources.sources import tencent_history
from app.services.market_data_sources.sources import akshare_source
from app.services.market_history_contract import tencent_quantity_input_contract


NORMALIZERS = [market_data._normalize_cn_stock_tx_history, normalize_cn_stock_tx_history]
DAY = date(2026, 8, 7)


def _tx_frame(quantity="1110388.00"):
    # Tencent day/qfqday field order: date, open, close, high, low, quantity.
    # stock_zh_a_hist_tx currently calls that final field "amount".
    result = pd.DataFrame([{
        "date": DAY, "open": "40.13", "close": "43.07", "high": "43.29",
        "low": "39.44", "amount": quantity,
    }])
    result.attrs["tencent_quantity_contract"] = tencent_quantity_input_contract("sh601168")
    return result


@pytest.mark.parametrize("normalize", NORMALIZERS, ids=["legacy", "adapter"])
@pytest.mark.parametrize("quantity,expected", [
    ("1110388.00", 111038800.0), (0, 0.0), (None, None), (float("nan"), None),
    ("unavailable", None),
])
def test_tencent_quantity_never_becomes_cash_amount(normalize, quantity, expected):
    frame = _tx_frame(quantity)
    original = frame.copy(deep=True)
    result = normalize(frame, DAY, DAY)
    assert len(result) == 1
    if expected is None:
        assert pd.isna(result.iloc[0].volume)
    else:
        # The verified A-share endpoint quantity is lots: convert once to
        # shares without estimating cash turnover as volume * close.
        assert result.iloc[0].volume == pytest.approx(expected)
    assert result.amount.isna().all()
    assert result.turnover_rate.isna().all()
    assert result.attrs["history_contract"] == {
        "provider": "tencent", "endpoint": "stock_zh_a_hist_tx",
        "version": "cn-stock-shares-v1", "volume_unit": "share",
        "amount_unit": "CNY", "turnover_unit": "percentage_point",
    }
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("normalize", NORMALIZERS, ids=["legacy", "adapter"])
def test_tencent_missing_quantity_stays_unknown(normalize):
    frame = _tx_frame().drop(columns=["amount"])
    result = normalize(frame, DAY, DAY)
    assert len(result) == 1
    assert result.volume.isna().all()
    assert result.amount.isna().all()


@pytest.mark.parametrize("normalize", NORMALIZERS, ids=["legacy", "adapter"])
@pytest.mark.parametrize("extra", [
    {"volume": 111038800},
    {"turnover": .0466},
    {"turnover_rate": 4.66},
    {"volume": 111038800, "turnover": .0466},
    {"unverified_field": "new SDK schema"},
])
def test_tencent_changed_schema_cannot_reinterpret_cash_as_quantity(normalize, extra):
    # A newer SDK can expose both real cash amount and explicit share volume.
    # Deny that schema until its independent raw-response contract is verified.
    frame = _tx_frame("4656185449.00").assign(**extra)
    original = frame.copy(deep=True)
    with pytest.raises(ValueError, match="^unsupported Tencent history schema$"):
        normalize(frame, DAY, DAY)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("normalize", NORMALIZERS, ids=["legacy", "adapter"])
def test_tencent_empty_new_schema_is_rejected_before_contract_is_attached(normalize):
    frame = pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume", "amount", "turnover"])
    with pytest.raises(ValueError, match="^unsupported Tencent history schema$"):
        normalize(frame, DAY, DAY)
    assert "history_contract" not in frame.attrs


@pytest.mark.parametrize("normalize", NORMALIZERS, ids=["legacy", "adapter"])
def test_tencent_duplicate_legacy_field_is_ambiguous_and_rejected(normalize):
    frame = pd.concat([_tx_frame(), _tx_frame()[["amount"]]], axis=1)
    with pytest.raises(ValueError, match="^unsupported Tencent history schema$"):
        normalize(frame, DAY, DAY)


@pytest.mark.parametrize("normalize", NORMALIZERS, ids=["legacy", "adapter"])
def test_tencent_empty_frame_keeps_existing_empty_contract(normalize):
    frame = pd.DataFrame()
    result = normalize(frame, DAY, DAY)
    assert result.empty
    assert frame.empty


@pytest.mark.parametrize("route", ["legacy_fallback", "adapter"])
def test_tencent_fetch_preserves_qfq_request_dates_and_price_fields(monkeypatch, route):
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", True)
    monkeypatch.setattr(akshare_registry, "_config_cache", {
        key: {"enabled": True, "delay_min_ms": 0, "delay_max_ms": 0, "max_retries": 1}
        for key in ("stock_zh_a_hist", "stock_zh_a_daily", "stock_zh_a_hist_tx")
    })
    requests = []
    frame = _tx_frame()
    original = frame.copy(deep=True)

    def unavailable(**_kwargs):
        raise ValueError("Isolated primary provider unavailable; exercise Tencent fallback")

    def tx_supplier(_url, **kwargs):
        requests.append(kwargs)
        payload = {"code": 0, "data": {"sh601168": {"qfqday": [
            [str(DAY), "40.13", "43.07", "43.29", "39.44", "1110388.00"]]}}}
        return SimpleNamespace(status_code=200, content=json.dumps(payload).encode(), close=lambda: None)

    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", unavailable)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_daily", unavailable)
    monkeypatch.setattr(market_data, "fetch_sina_history_raw", unavailable, raising=False)
    monkeypatch.setattr(akshare_source, "fetch_sina_history_raw", unavailable, raising=False)
    monkeypatch.setattr(tencent_history.requests, "get", tx_supplier)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist_tx", lambda **_kwargs: pytest.fail("Ambiguous SDK must not be called"))
    symbol = SimpleNamespace(symbol="601168", market="sh", asset_type="stock")
    result = (market_data._fetch_history(symbol, DAY, DAY, "qfq")
              if route == "legacy_fallback" else TxStockSource().fetch(symbol, DAY, DAY, "qfq"))
    assert len(requests) == 1
    assert requests[0]["params"]["param"] == "sh601168,day,2026-08-07,2026-08-07,640,qfq"
    assert requests[0]["timeout"] == (5, 15)
    assert requests[0]["allow_redirects"] is False
    assert result.trade_date.dt.date.tolist() == [DAY]
    assert result[["open", "high", "low", "close"]].iloc[0].tolist() == pytest.approx(
        [40.13, 43.29, 39.44, 43.07]
    )
    assert result.iloc[0].volume == pytest.approx(111038800.0)
    assert result.amount.isna().all()
    assert result.turnover_rate.isna().all()
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("route", ["legacy_fallback", "adapter"])
def test_tencent_fetch_never_reads_new_sdk_cash_volume_schema(monkeypatch, route):
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", True)
    monkeypatch.setattr(akshare_registry, "_config_cache", {
        key: {"enabled": True, "delay_min_ms": 0, "delay_max_ms": 0, "max_retries": 1}
        for key in ("stock_zh_a_hist", "stock_zh_a_daily", "stock_zh_a_hist_tx")
    })
    frame = _tx_frame("4656185449.00").assign(volume=111038800, turnover=.0466)

    def unavailable(**_kwargs):
        raise ValueError("Isolated primary provider unavailable")

    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", unavailable)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_daily", unavailable)
    monkeypatch.setattr(market_data, "fetch_sina_history_raw", unavailable, raising=False)
    monkeypatch.setattr(akshare_source, "fetch_sina_history_raw", unavailable, raising=False)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist_tx", lambda **_kwargs: pytest.fail("Ambiguous SDK must not be called"))
    payload = {"code": 0, "data": {"sh601168": {"qfqday": [
        [str(DAY), "40.13", "43.07", "43.29", "39.44", "1110388.00"]]}}}
    monkeypatch.setattr(tencent_history.requests, "get", lambda *_args, **_kwargs:
        SimpleNamespace(status_code=200, content=json.dumps(payload).encode(), close=lambda: None))
    symbol = SimpleNamespace(symbol="601168", market="sh", asset_type="stock")
    result = (market_data._fetch_history(symbol, DAY, DAY, "qfq")
        if route == "legacy_fallback" else TxStockSource().fetch(symbol, DAY, DAY, "qfq"))
    assert result.iloc[0].volume == 111038800
    assert result.amount.isna().all()
    assert "history_contract" not in frame.attrs
