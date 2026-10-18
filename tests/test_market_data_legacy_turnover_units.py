"""Legacy history acquisition and source adapters share turnover percent units.

Provider acquisition boundaries and configuration are isolated. The strict
Sina HTTP/parser path has its own frozen-wire adapter regression tests.
"""
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services import akshare_registry, market_data
from app.services.market_data_sources.sources._normalize import (
    normalize_cn_em_history, normalize_cn_stock_sina_history,
)

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize("route", ["eastmoney", "sina_fallback", "sina_bj_primary"])
@pytest.mark.parametrize("sina_value,em_value,expected", [
    (.02, 2.0, 2.0),
    (0.0, 0.0, 0.0),
    (.5, 50.0, 50.0),
    ("0.000001", "0.0001", .0001),
    (None, None, None),
    ("unavailable", "unavailable", None),
])
def test_legacy_history_and_normalizers_use_same_turnover_percentage_points(
    monkeypatch, route, sina_value, em_value, expected,
):
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", True)
    monkeypatch.setattr(akshare_registry, "_config_cache", {
        key: {"enabled": True, "delay_min_ms": 0, "delay_max_ms": 0, "max_retries": 1}
        for key in ("stock_zh_a_hist", "stock_zh_a_daily", "stock_zh_a_hist_tx")
    })
    day = date(2026, 1, 5)
    sina = pd.DataFrame([{"date": day, "open": 10, "high": 11, "low": 9, "close": 10.5,
        "volume": 1000, "amount": 10500, "turnover": sina_value}])
    em = pd.DataFrame([{"日期": day, "开盘": 10, "最高": 11, "最低": 9, "收盘": 10.5,
        "成交量": 1000, "成交额": 10500, "换手率": em_value}])

    def em_supplier(**_kwargs):
        if route != "eastmoney":
            raise ValueError("Isolated Eastmoney unavailable; take real legacy fallback")
        return em.copy(deep=True)

    def sina_supplier(**_kwargs):
        # New Sina transport already returns percent units; the legacy path
        # must not normalize its result a second time.
        return normalize_cn_stock_sina_history(sina.copy(deep=True), day, day)

    def forbidden_supplier(**_kwargs):
        raise AssertionError("An unexpected provider must not be contacted")

    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", em_supplier)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_daily", sina_supplier)
    monkeypatch.setattr(market_data, "fetch_sina_history_raw", sina_supplier)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist_tx", forbidden_supplier)
    symbol = SimpleNamespace(symbol="920001" if route == "sina_bj_primary" else "600001",
                             market="bj" if route == "sina_bj_primary" else "sh", asset_type="stock")
    fetched = market_data._fetch_history(symbol, day, day, "qfq")
    normalized = (normalize_cn_em_history(em) if route == "eastmoney"
                  else normalize_cn_stock_sina_history(sina, day, day))
    assert len(fetched) == len(normalized) == 1
    assert fetched.iloc[0].trade_date.date() == day
    assert fetched.iloc[0].close == 10.5
    for result in (fetched, normalized):
        actual = result.iloc[0].turnover_rate
        if expected is None:
            assert pd.isna(actual)
        else:
            assert actual == pytest.approx(expected, abs=1e-12)
    # Provider input frames are reusable; normalization must not multiply their
    # existing values again or mutate the caller's data.
    assert sina.iloc[0].turnover == sina_value
    assert em.iloc[0]["换手率"] == em_value


def test_legacy_sina_standard_history_preserves_unknown_turnover_and_dates(monkeypatch):
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", True)
    monkeypatch.setattr(akshare_registry, "_config_cache", {
        "stock_zh_a_daily": {"enabled": True, "delay_min_ms": 0, "delay_max_ms": 0, "max_retries": 1},
    })
    frame = pd.DataFrame([{"date": date(2026, 1, day), "open": 10, "high": 11, "low": 9,
        "close": 10, "volume": 100, "amount": 1000} for day in (2, 5, 6)])
    monkeypatch.setattr(market_data.ak, "stock_zh_a_daily", lambda **_kwargs: frame.copy(deep=True))
    monkeypatch.setattr(market_data, "fetch_sina_history_raw", lambda **_kwargs:
        normalize_cn_stock_sina_history(frame.copy(deep=True), date(2026, 1, 5), date(2026, 1, 5)))
    symbol = SimpleNamespace(symbol="920001", market="bj", asset_type="stock")
    result = market_data._fetch_history(symbol, date(2026, 1, 5), date(2026, 1, 5), "qfq")
    assert result.trade_date.dt.date.tolist() == [date(2026, 1, 5)]
    assert result.turnover_rate.isna().all()

