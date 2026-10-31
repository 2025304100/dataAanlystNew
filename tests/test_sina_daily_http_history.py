"""Exercise production Sina adapters with frozen HTTP bytes, never live I/O."""
from datetime import date
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from app.services import akshare_registry, market_data
from app.services.market_data_sources.sources import sina_history_http, sina_history_evidence
from app.services.market_data_sources.sources.akshare_source import SinaStockSource
from app.services.market_data_sources.sources.base import SourceUnavailable

pytestmark = pytest.mark.whitebox


DAY = date(2024, 1, 2)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", True)
    monkeypatch.setattr(akshare_registry, "_config_cache", {key: {
        "enabled": True, "delay_min_ms": 0, "delay_max_ms": 0, "max_retries": 1}
        for key in ("stock_zh_a_hist", "stock_zh_a_daily", "stock_zh_a_hist_tx")})
    def forbidden_sdk(**_kwargs):
        pytest.fail("Sina's lossy SDK must not be called")
    monkeypatch.setattr(market_data.ak, "stock_zh_a_daily", forbidden_sdk)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", lambda **_kwargs: (_ for _ in ()).throw(ValueError("isolated EM unavailable")))


class Response:
    def __init__(self, payload, *, status=200, advertised_size=None):
        self.payload, self.status_code, self.closed = payload, status, False
        self.headers = {} if advertised_size is None else {"Content-Length": str(advertised_size)}

    def iter_content(self, chunk_size):
        yield self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def close(self):
        self.closed = True


def install_bundle(monkeypatch, *, symbol="sz000425", actual_wire=False):
    if actual_wire:
        folder = Path(__file__).parent / "fixtures/sina_000425_history_20261016"
        bodies = {key: folder.joinpath(key + ".response").read_bytes() for key in ("history", "shares", "qfq")}
        proof = json.loads(folder.joinpath("provenance.json").read_text())
        assert {key: hashlib.sha256(value).hexdigest() for key, value in bodies.items()} == proof["sha256"]
    else:
        bodies = {
            "history": f'var KLC_K2_{symbol}="K2/ABC";'.encode(),
            "shares": f'var KKE_ShareAmount_{symbol}=([{{"date":"2024-01-01","amount":10}}]);'.encode(),
            "qfq": f'var {symbol}qfq={{"total":1,"data":[{{"d":"2024-01-01","f":"2"}}]}};'.encode(),
        }
        def decode(_encoded):
            return [{"date": f"2024-01-{day:02}T00:00:00.000Z", "open": 10, "high": 12, "low": 8,
                     "close": 11, "volume": 2000, "amount": 21000} for day in (2, 3)]
        monkeypatch.setattr(sina_history_evidence, "_decode_compressed_history", decode)
    calls, responses = [], []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        key = "shares" if "getAmountBySymbol" in url else "qfq" if url.endswith("qfq.js") else "history"
        response = Response(bodies[key])
        responses.append(response)
        return response
    monkeypatch.setattr(sina_history_http.requests, "get", get)
    return calls, responses


@pytest.mark.parametrize("route", ["adapter", "legacy_fallback", "legacy_bj"])
def test_daily_sina_paths_keep_real_identical_days_and_percent_once(monkeypatch, configured, route):
    code, market = ("920001", "bj") if route == "legacy_bj" else ("000425", "sz")
    calls, responses = install_bundle(monkeypatch, symbol=market + code)
    symbol = SimpleNamespace(symbol=code, market=market, asset_type="stock")
    frame = (SinaStockSource().fetch(symbol, DAY, date(2024, 1, 3), "qfq") if route == "adapter"
             else market_data._fetch_history(symbol, DAY, date(2024, 1, 3), "qfq"))
    assert frame.trade_date.dt.date.tolist() == [DAY, date(2024, 1, 3)]
    assert frame.close.tolist() == [5.5, 5.5]
    assert frame.volume.tolist() == [2000, 2000]
    assert frame.amount.tolist() == [21000, 21000]
    assert frame.turnover_rate.tolist() == [2, 2]  # never 200 from a second normalize
    assert frame.attrs["history_response"]["actual_adjustment"] == "qfq"
    assert len(calls) == 3 and all(response.closed for response in responses)
    assert all(kwargs == {"timeout": sina_history_http.SINA_HISTORY_TIMEOUT,
                          "allow_redirects": False, "stream": True} for _url, kwargs in calls)


@pytest.mark.parametrize("route", ["adapter", "legacy_fallback"])
def test_actual_compressed_http_fixture_runs_local_decoder_and_production_adapter(monkeypatch, configured, route):
    calls, responses = install_bundle(monkeypatch, actual_wire=True)
    symbol = SimpleNamespace(symbol="000425", market="sz", asset_type="stock")
    start, end = date(2026, 10, 14), date(2026, 10, 15)
    mode = "" if route == "adapter" else "qfq"
    frame = (SinaStockSource().fetch(symbol, start, end, mode) if route == "adapter"
             else market_data._fetch_history(symbol, start, end, mode))
    assert frame.trade_date.dt.date.tolist() == [start, end]
    assert frame.close.tolist() == [7.27, 7.03]
    assert frame.volume.tolist() == [92224225, 111451405]
    assert frame.amount.tolist() == [678346536, 790238885]
    assert frame.iloc[1].turnover_rate == pytest.approx(111451405 / (899808.4894 * 10000) * 100)
    assert frame.attrs["sina_evidence"]["price_method"] == (
        "raw_ohlc" if mode == "" else "raw_ohlc / effective_qfq_factor rounded_2_decimals")
    assert len(calls) == 3 and all(response.closed for response in responses)


def test_registry_disable_blocks_every_sina_http_request(monkeypatch, configured):
    akshare_registry._config_cache["stock_zh_a_daily"]["enabled"] = False
    monkeypatch.setattr(sina_history_http.requests, "get", lambda *_a, **_k: pytest.fail("disabled source contacted"))
    symbol = SimpleNamespace(symbol="000425", market="sz", asset_type="stock")
    with pytest.raises(SourceUnavailable, match="disabled"):
        SinaStockSource().fetch(symbol, DAY, DAY, "qfq")
    with pytest.raises(Exception, match="disabled"):
        market_data._safe_stock_zh_a_daily("sz000425", "20240102", "20240102", "qfq")


@pytest.mark.parametrize("adjust", ["hfq", "unknown"])
def test_unsupported_adjustment_fails_before_any_http(monkeypatch, adjust):
    monkeypatch.setattr(sina_history_http.requests, "get", lambda *_a, **_k: pytest.fail("unsupported adjustment contacted source"))
    with pytest.raises(ValueError, match="unsupported_adjustment"):
        sina_history_http.fetch_sina_history_raw(symbol="sz000425", start_date="20240102", end_date="20240103", adjust=adjust)


@pytest.mark.parametrize("advertised", [True, False])
def test_oversize_response_is_rejected_and_closed(monkeypatch, advertised):
    limit = sina_history_http.SINA_HISTORY_MAX_RESPONSE_BYTES
    response = Response(b"x" * (limit + 1), advertised_size=limit + 1 if advertised else None)
    monkeypatch.setattr(sina_history_http.requests, "get", lambda *_a, **_k: response)
    with pytest.raises(ValueError, match="too_large"):
        sina_history_http.fetch_sina_history_raw(symbol="sz000425", start_date="20240102", end_date="20240103", adjust="qfq")
    assert response.closed


def test_transport_failure_uses_existing_retry_and_registry_delay(monkeypatch, configured):
    # This case exercises two allowed rounds; the default fixture's ceiling
    # of one must continue to prevent retries in all other cases.
    akshare_registry._config_cache['stock_zh_a_daily']['max_retries'] = 2
    calls, responses = install_bundle(monkeypatch)
    get = sina_history_http.requests.get
    attempts, delays, sleeps = [], [], []
    def fail_once(url, **kwargs):
        attempts.append(url)
        if len(attempts) == 1:
            raise requests.ConnectionError("isolated connection failure")
        return get(url, **kwargs)
    monkeypatch.setattr(sina_history_http.requests, "get", fail_once)
    monkeypatch.setattr(akshare_registry, "apply_delay", lambda key: delays.append(key))
    monkeypatch.setattr("app.services.akshare_utils.time.sleep", lambda delay: sleeps.append(delay))
    frame = market_data._safe_stock_zh_a_daily("sz000425", "20240102", "20240103", "qfq")
    assert len(frame) == 2 and len(attempts) == 4 and len(calls) == 3
    assert delays == ["stock_zh_a_daily", "stock_zh_a_daily"] and sleeps == [1.0]
    assert all(response.closed for response in responses)
