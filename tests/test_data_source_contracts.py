from datetime import date
from unittest.mock import Mock

import pandas as pd
import pytest

from app.services.data_source_contracts import DataContractError, endpoint_contracts, normalize_units
from app.services.market_data_sources.sources._normalize import (
    normalize_cn_stock_em_history, normalize_cn_stock_sina_history,
)

pytestmark = pytest.mark.whitebox


def test_em_adapter_and_management_share_units_without_double_conversion():
    raw = pd.DataFrame([{"日期": "2026-10-26", "开盘": 10, "最高": 11,
                         "最低": 9, "收盘": 10, "成交量": 123, "成交额": 123000, "换手率": 2}])
    normalized = normalize_cn_stock_em_history(raw)
    assert normalized.iloc[0].volume == 12300
    assert normalized.iloc[0].turnover_rate == 2
    repeated = normalize_cn_stock_em_history(normalized)
    assert repeated.iloc[0].volume == 12300
    contract = endpoint_contracts("stock_zh_a_hist")[0]
    assert contract["fields"][0]["multiplier"] == 100
    assert raw.iloc[0]["成交量"] == 123


def test_sina_ratio_is_not_em_percentage_points():
    raw = pd.DataFrame([{"date": "2026-10-26", "open": 10, "high": 11,
                         "low": 9, "close": 10, "volume": 12300, "turnover": .02}])
    result = normalize_cn_stock_sina_history(raw, date(2026, 10, 26), date(2026, 10, 26))
    assert result.iloc[0].volume == 12300
    assert result.iloc[0].turnover_rate == 2
    with pytest.raises(DataContractError, match="CONFLICT"):
        normalize_units(result, "stock_zh_a_hist")


def test_tencent_family_metadata_uses_verified_resolver():
    contracts = endpoint_contracts("stock_zh_a_hist_tx")
    families = {c["instrument_family"]: c for c in contracts}
    assert families["sh_star_688"]["fields"][0]["multiplier"] == 1
    assert families["sh_main"]["fields"][0]["multiplier"] == 100
    assert all(c["unavailable_fields"] == ("amount", "turnover_rate") for c in contracts)
    assert endpoint_contracts("fuyao") == []
    with pytest.raises(DataContractError, match="UNVERIFIED"):
        normalize_units(pd.DataFrame(), "fuyao")


def test_api_exposes_the_adapter_contract_without_database_writes():
    from app.api.routes.akshare_apis import _to_status
    from app.services.akshare_registry import get_registry_entry
    result = _to_status(get_registry_entry("stock_zh_a_hist"), None)
    assert result.contract_status == "declared"
    assert result.data_contracts == endpoint_contracts("stock_zh_a_hist")


def test_adapter_does_not_hide_contract_errors_as_supplier_failure(monkeypatch):
    from types import SimpleNamespace
    from app.services.market_data_sources.sources import akshare_source as adapters
    bad = pd.DataFrame({"volume": [1]})
    bad.attrs["unit_normalization"] = {"endpoint": "wrong", "version": "wrong"}
    monkeypatch.setattr(adapters, "call_akshare_with_retry", lambda *args, **kwargs: bad)
    with pytest.raises(DataContractError):
        adapters.EMStockSource().fetch(SimpleNamespace(symbol="600000"), date(2026, 10, 26), date(2026, 10, 26), "qfq")


@pytest.fixture
def managed_calls(monkeypatch):
    from app.services import akshare_registry as registry, akshare_guard as guard
    guard.reset_all()
    monkeypatch.setattr(registry, "is_api_enabled", lambda key: True)
    monkeypatch.setattr(registry, "apply_delay", lambda key: None)
    monkeypatch.setattr(registry, "get_max_retries", lambda key: 1)
    yield registry
    guard.reset_all()


def test_explicit_attempt_budget_cannot_override_management(managed_calls):
    from app.services.akshare_utils import call_akshare_with_retry
    supplier = Mock(side_effect=ConnectionError("unavailable"))
    with pytest.raises(ConnectionError):
        call_akshare_with_retry(supplier, api_key="stock_zh_a_hist", max_attempts=5)
    assert supplier.call_count == 1


def test_disable_while_pacing_stops_before_supplier(managed_calls, monkeypatch):
    from app.services.akshare_utils import call_akshare_with_retry, AkshareApiDisabledError
    def disable(key):
        monkeypatch.setattr(managed_calls, "is_api_enabled", lambda key: False)
    monkeypatch.setattr(managed_calls, "apply_delay", disable)
    supplier = Mock()
    with pytest.raises(AkshareApiDisabledError):
        call_akshare_with_retry(supplier, api_key="stock_zh_a_hist")
    supplier.assert_not_called()
