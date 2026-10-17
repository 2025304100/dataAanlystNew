"""A-share acquisition emits shares/CNY/percentage points across fallback paths.

Provider SDK functions are the only transport boundary replaced. ETF and US
contracts retain their prior behavior; no live database, source or worker runs.
"""
from datetime import date
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services import akshare_registry, market_data
from app.services.market_data_sources.sources._normalize import (
    normalize_cn_stock_em_history,
    normalize_cn_stock_sina_history,
    normalize_us_history,
)
from app.services.market_data_sources.sources.akshare_source import (
    EMETFSource, EMStockSource, SinaStockSource, TxStockSource,
)
from app.services.market_data_sources.sources import tencent_history
from app.services.market_data_sources.sources import akshare_source
from app.services.market_history_contract import tencent_quantity_input_contract


DAY = date(2026, 8, 7)
ENDPOINTS = {
    "eastmoney": "stock_zh_a_hist", "sina": "stock_zh_a_daily", "tencent": "stock_zh_a_hist_tx",
}


def _contract(provider):
    return {
        "provider": provider, "endpoint": ENDPOINTS[provider], "version": "cn-stock-shares-v1",
        "volume_unit": "share", "amount_unit": "CNY", "turnover_unit": "percentage_point",
    }


def _em_frame(volume="1110388.00"):
    return pd.DataFrame([{"日期": DAY, "开盘": "40.13", "最高": "43.29", "最低": "39.44",
        "收盘": "43.07", "成交量": volume, "成交额": "4656185449.00", "换手率": "4.66"}])


def _sina_frame(volume="111038800"):
    return pd.DataFrame([{"date": DAY, "open": "40.13", "high": "43.29", "low": "39.44",
        "close": "43.07", "volume": volume, "amount": "4656185449.00", "turnover": ".0466"}])


def _tx_frame():
    result = pd.DataFrame([{"date": DAY, "open": "40.13", "high": "43.29", "low": "39.44",
        "close": "43.07", "amount": "1110388.00"}])
    result.attrs["tencent_quantity_contract"] = tencent_quantity_input_contract("sh601168")
    return result


@pytest.fixture
def configured_providers(monkeypatch):
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", True)
    monkeypatch.setattr(akshare_registry, "_config_cache", {
        key: {"enabled": True, "delay_min_ms": 0, "delay_max_ms": 0, "max_retries": 1}
        for key in (*ENDPOINTS.values(), "fund_etf_hist_em")
    })


@pytest.mark.parametrize("path", ["legacy", "modern"])
@pytest.mark.parametrize("provider", list(ENDPOINTS))
def test_actual_stock_acquisition_paths_share_one_explicit_unit_contract(
    monkeypatch, configured_providers, path, provider,
):
    frames = {"eastmoney": _em_frame(), "sina": _sina_frame(), "tencent": _tx_frame()}
    originals = {key: frame.copy(deep=True) for key, frame in frames.items()}
    calls = []

    def supplier(source):
        def fetch(**kwargs):
            calls.append((source, kwargs))
            if source != provider:
                raise ValueError("Isolated provider unavailable; exercise real fallback")
            return frames[source]
        return fetch

    for source, endpoint in ENDPOINTS.items():
        monkeypatch.setattr(market_data.ak, endpoint, supplier(source))
    def sina_supplier(**kwargs):
        raw = supplier("sina")(**kwargs)
        return normalize_cn_stock_sina_history(raw, DAY, DAY)
    monkeypatch.setattr(market_data, "fetch_sina_history_raw", sina_supplier, raising=False)
    monkeypatch.setattr(akshare_source, "fetch_sina_history_raw", sina_supplier, raising=False)
    def tx_http(_url, **kwargs):
        code, _period, start, end, _limit, adjust = kwargs["params"]["param"].split(",")
        calls.append(("tencent", {"symbol": code, "adjust": adjust,
            "start_date": start.replace("-", ""), "end_date": end.replace("-", "")}))
        if provider != "tencent":
            raise ValueError("Isolated provider unavailable")
        payload = {"code": 0, "data": {code: {"qfqday": [
            [str(DAY), "40.13", "43.07", "43.29", "39.44", "1110388.00"]]}}}
        return SimpleNamespace(status_code=200, content=json.dumps(payload).encode(), close=lambda: None)
    monkeypatch.setattr(tencent_history.requests, "get", tx_http)
    symbol = SimpleNamespace(symbol="601168", market="sh", asset_type="stock")
    adapters = {"eastmoney": EMStockSource, "sina": SinaStockSource, "tencent": TxStockSource}
    result = (market_data._fetch_history(symbol, DAY, DAY, "qfq") if path == "legacy"
              else adapters[provider]().fetch(symbol, DAY, DAY, "qfq"))
    assert result.attrs["history_contract"] == _contract(provider)
    assert result.trade_date.dt.date.tolist() == [DAY]
    assert result.iloc[0].volume == pytest.approx(111038800.0)
    assert result[["open", "high", "low", "close"]].iloc[0].tolist() == pytest.approx(
        [40.13, 43.29, 39.44, 43.07]
    )
    if provider == "tencent":
        assert result.amount.isna().all() and result.turnover_rate.isna().all()
    else:
        assert result.iloc[0].amount == pytest.approx(4656185449.0)
        assert result.iloc[0].turnover_rate == pytest.approx(4.66)
    assert calls[-1][0] == provider
    assert calls[-1][1]["adjust"] == "qfq"
    assert calls[-1][1]["start_date"] == calls[-1][1]["end_date"] == "20260807"
    # The unit contract does not certify that a provider honored qfq.
    assert "adjust" not in result.attrs["history_contract"]
    for source in frames:
        pd.testing.assert_frame_equal(frames[source], originals[source])


@pytest.mark.parametrize("quantity,expected", [
    (0, 0.0), ("0.01", 1.0), (None, None), ("unavailable", None),
])
def test_em_stock_lots_convert_once_and_missing_is_preserved(quantity, expected):
    frame = _em_frame(quantity)
    original = frame.copy(deep=True)
    first = normalize_cn_stock_em_history(frame)
    second = normalize_cn_stock_em_history(frame)
    pd.testing.assert_frame_equal(first, second)
    pd.testing.assert_frame_equal(frame, original)
    if expected is None:
        assert pd.isna(first.iloc[0].volume)
    else:
        assert first.iloc[0].volume == pytest.approx(expected)
    assert first.iloc[0].amount == pytest.approx(4656185449.0)
    assert first.attrs["history_contract"] == _contract("eastmoney")


@pytest.mark.parametrize("normalize", [market_data._normalize_cn_stock_sina_history,
                                        normalize_cn_stock_sina_history], ids=["legacy", "modern"])
@pytest.mark.parametrize("quantity,expected", [("111038800", 111038800.0), (0, 0.0), (None, None)])
def test_sina_stock_already_uses_shares(normalize, quantity, expected):
    frame = _sina_frame(quantity)
    original = frame.copy(deep=True)
    result = normalize(frame, DAY, DAY)
    if expected is None:
        assert pd.isna(result.iloc[0].volume)
    else:
        assert result.iloc[0].volume == pytest.approx(expected)
    assert result.attrs["history_contract"] == _contract("sina")
    assert result.iloc[0].amount == pytest.approx(4656185449.0)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("path", ["legacy", "modern"])
def test_em_etf_does_not_inherit_stock_conversion_or_contract(monkeypatch, configured_providers, path):
    frame = _em_frame()
    original = frame.copy(deep=True)
    monkeypatch.setattr(market_data.ak, "fund_etf_hist_em", lambda **_kwargs: frame)
    symbol = SimpleNamespace(symbol="510300", market="sh", asset_type="etf")
    result = (market_data._fetch_history(symbol, DAY, DAY, "qfq") if path == "legacy"
              else EMETFSource().fetch(symbol, DAY, DAY, "qfq"))
    assert result.iloc[0].volume == pytest.approx(1110388.0)
    assert result.iloc[0].amount == pytest.approx(4656185449.0)
    assert "history_contract" not in result.attrs
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("normalize", [market_data._normalize_us_history, normalize_us_history],
                         ids=["legacy", "modern"])
def test_us_does_not_inherit_cn_stock_contract(normalize):
    frame = _sina_frame()
    result = normalize(frame, DAY, DAY)
    assert result.iloc[0].volume == pytest.approx(111038800.0)
    assert "history_contract" not in result.attrs


def test_empty_cn_stock_frames_attach_independent_contract_without_mutating_input():
    frame = pd.DataFrame()
    em = normalize_cn_stock_em_history(frame)
    sina = normalize_cn_stock_sina_history(frame, DAY, DAY)
    assert em.empty and sina.empty
    assert em.attrs["history_contract"] == _contract("eastmoney")
    assert sina.attrs["history_contract"] == _contract("sina")
    em.attrs["history_contract"]["provider"] = "changed"
    assert sina.attrs["history_contract"] == _contract("sina")
    assert frame.attrs == {}
