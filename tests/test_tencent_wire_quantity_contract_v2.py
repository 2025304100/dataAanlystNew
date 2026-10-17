"""Board-bound provider units and transactional evidence-directory publication."""
from datetime import date
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services import akshare_registry, market_data, market_history_evidence as evidence
from app.services.market_history_contract import tencent_quantity_input_contract
from app.services.market_data_sources.sources import akshare_source, tencent_history
from app.services.market_data_sources.sources._normalize import normalize_cn_stock_tx_history


DAY = date(2025, 1, 13)
NORMALIZERS = [market_data._normalize_cn_stock_tx_history, normalize_cn_stock_tx_history]


def raw_frame(symbol, quantity):
    frame = pd.DataFrame([{"date": DAY, "open": 80, "close": 81, "high": 82, "low": 79, "amount": quantity}])
    frame.attrs["tencent_quantity_contract"] = tencent_quantity_input_contract(symbol)
    return frame


@pytest.mark.parametrize("normalize", NORMALIZERS, ids=["legacy", "modern"])
@pytest.mark.parametrize("symbol,multiplier", [("sh688368", 1), ("sh601168", 100), ("sz000425", 100), ("sz300750", 100)])
@pytest.mark.parametrize("quantity", [1.23, 2824947.0])
def test_same_wire_number_uses_declared_board_units_never_numeric_size(normalize, symbol, multiplier, quantity):
    frame = raw_frame(symbol, quantity)
    result = normalize(frame, DAY, DAY)
    assert result.iloc[0].volume == quantity * multiplier
    assert result.amount.isna().all() and result.turnover_rate.isna().all()
    assert frame.attrs["tencent_quantity_contract"]["volume_to_shares"] == multiplier


@pytest.mark.parametrize("normalize", NORMALIZERS, ids=["legacy", "modern"])
def test_unmarked_nonempty_sdk_frame_is_not_guessed(normalize):
    frame = raw_frame("sh688368", 2824947)
    frame.attrs.clear()
    with pytest.raises(ValueError, match="unverified_tencent_history_quantity_contract"):
        normalize(frame, DAY, DAY)


@pytest.mark.parametrize("symbol", ["sh689009", "sz688368", "sh000425", "sh510300", "sz159915", "bj920072"])
def test_unverified_or_conflicting_instrument_families_fail_closed(symbol):
    with pytest.raises(ValueError):
        tencent_quantity_input_contract(symbol)


def test_a_forged_multiplier_is_not_an_explicit_contract():
    frame = raw_frame("sh688368", 1)
    frame.attrs["tencent_quantity_contract"]["volume_to_shares"] = True
    with pytest.raises(ValueError, match="unverified_tencent_history_quantity_contract"):
        normalize_cn_stock_tx_history(frame, DAY, DAY)


def test_star_receipt_preserves_raw_share_unit_and_replays_after_attrs_serialization(tmp_path):
    payload = json.dumps({"code": 0, "data": {"sh688368": {"day": [
        [str(DAY), "80", "81", "82", "79", "2824947"]]}}}).encode()
    ref = evidence.capture_tencent_http_history(artifact_root=tmp_path, capture_id="star",
        request=evidence.HistoryRequest("sh688368", DAY, DAY, ""), response_bytes=payload)
    frame, manifest = evidence.load_history_evidence(ref)
    assert frame.iloc[0].volume == 2824947
    assert manifest["provider_units"]["amount"] == "share"
    assert manifest["input_unit_contract"]["version"] == "tencent-cn-stock-wire-quantity-v2"
    assert manifest["input_unit_contract"]["instrument_family"] == "sh_star_688"


def test_real_frozen_star_payload_extra_dates_are_filtered_without_date_migration(monkeypatch):
    fixture = Path(__file__).parent / "fixtures" / "tencent_sh688368_raw_20261016.response"
    payload = fixture.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == "a8213ec243a52e0e8d66401220f4f8ed82c2d186263bb259b37803cb7ca5aa30"
    calls = []
    monkeypatch.setattr(tencent_history.requests, "get", lambda url, **kw:
        (calls.append((url, kw)) or SimpleNamespace(status_code=200, content=payload, close=lambda: None)))
    raw = tencent_history.fetch_tencent_history_raw(symbol="sh688368", start_date="20250113", end_date="20261014", adjust="")
    assert len(raw) == 640
    for normalize in NORMALIZERS:
        frame = normalize(raw, DAY, date(2026, 10, 14))
        assert len(frame) == 422
        assert frame.trade_date.dt.date.iloc[0] == DAY
        assert frame.iloc[0].volume == 2824947
    detail = raw.attrs["history_response"]
    assert detail["raw_date_count"] == 640 and detail["selected_date_count"] == 422
    assert detail["range_filter_only"] is True and detail["range_completeness_certified"] is False
    assert detail["raw_date_range"]["start"] < "2025-01-13"
    assert calls[0][1]["params"]["param"] == "sh688368,day,2025-01-13,2026-10-14,640,"


@pytest.mark.parametrize("route", ["legacy", "modern"])
def test_real_star_payload_production_source_chain_keeps_native_share_quantity(monkeypatch, route):
    from app.services.market_data_sources.sources.akshare_source import TxStockSource
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", True)
    monkeypatch.setattr(akshare_registry, "_config_cache", {key: {"enabled": True,
        "delay_min_ms": 0, "delay_max_ms": 0, "max_retries": 1}
        for key in ("stock_zh_a_hist", "stock_zh_a_daily", "stock_zh_a_hist_tx")})
    def failed(**kwargs):
        raise ValueError("isolated primary failure")
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", failed)
    monkeypatch.setattr(market_data, "fetch_sina_history_raw", failed)
    monkeypatch.setattr(akshare_source, "fetch_sina_history_raw", failed)
    payload = (Path(__file__).parent / "fixtures" / "tencent_sh688368_qfq_20261016.response").read_bytes()
    monkeypatch.setattr(tencent_history.requests, "get", lambda *_a, **_kw:
        SimpleNamespace(status_code=200, content=payload, close=lambda: None))
    symbol = SimpleNamespace(symbol="688368", market="sh", asset_type="stock")
    frame = (market_data._fetch_history(symbol, DAY, date(2026, 10, 14), "qfq") if route == "legacy"
        else TxStockSource().fetch(symbol, DAY, date(2026, 10, 14), "qfq"))
    assert len(frame) == 422 and frame.iloc[0].volume == 2824947
    assert frame.amount.isna().all() and frame.turnover_rate.isna().all()


def test_permission_error_on_first_directory_commit_is_retried_without_overwrite(tmp_path, monkeypatch):
    stage, target = tmp_path / ".stage", tmp_path / "published"
    stage.mkdir(); (stage / "manifest.json").write_text("immutable")
    original, attempts = Path.rename, []
    def transient(self, destination):
        attempts.append(str(self))
        if len(attempts) == 1:
            raise PermissionError(5, "Access is denied")
        return original(self, destination)
    monkeypatch.setattr(Path, "rename", transient)
    monkeypatch.setattr(evidence.time, "sleep", lambda _delay: None)
    evidence._commit_staged_directory(stage, target)
    assert len(attempts) == 2 and (target / "manifest.json").read_text() == "immutable"


def test_existing_receipt_blocks_directory_commit_without_any_rename(tmp_path, monkeypatch):
    stage, target = tmp_path / ".stage", tmp_path / "published"
    stage.mkdir(); target.mkdir(); (target / "manifest.json").write_text("old")
    monkeypatch.setattr(Path, "rename", lambda *_a: pytest.fail("Existing receipt must not be replaced"))
    with pytest.raises(ValueError, match="immutable_conflict"):
        evidence._commit_staged_directory(stage, target)
    assert stage.exists() and (target / "manifest.json").read_text() == "old"


def test_repeated_access_denial_is_bounded_and_preserves_uncommitted_evidence(tmp_path, monkeypatch):
    stage, target = tmp_path / ".stage", tmp_path / "published"
    stage.mkdir(); (stage / "response.bin").write_bytes(b"raw")
    calls = []
    def denied(*_a):
        calls.append(True)
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(Path, "rename", denied)
    monkeypatch.setattr(evidence.time, "sleep", lambda _delay: None)
    with pytest.raises(PermissionError):
        evidence._commit_staged_directory(stage, target)
    assert len(calls) == 3 and (stage / "response.bin").read_bytes() == b"raw" and not target.exists()
