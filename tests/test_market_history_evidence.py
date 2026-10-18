"""No network/database: prove receipt integrity, date identity and adjustment selection."""
from datetime import date
import hashlib
import json

import pandas as pd
import pytest

from app.services.market_history_evidence import (
    HistoryRequest, capture_sdk_history, capture_tencent_http_history,
    load_history_evidence, write_history_research_snapshot, load_history_research_snapshot,
    capture_baostock_history, capture_eastmoney_http_history,
    write_trusted_history_dataset, load_trusted_history_dataset,
)

pytestmark = pytest.mark.whitebox


START, DAY, END = date(2026, 8, 6), date(2026, 8, 7), date(2026, 8, 10)


def request(adjust="qfq", symbol="sh601168"):
    return HistoryRequest(symbol, DAY, DAY, adjust)


def sdk_frame(provider="eastmoney"):
    if provider == "eastmoney":
        return pd.DataFrame([{"日期": DAY, "开盘": "40.13", "最高": "43.29", "最低": "39.44",
            "收盘": "43.07", "成交量": "1110388", "成交额": "4656185449", "换手率": "4.66"}])
    if provider == "sina":
        return pd.DataFrame([{"date": DAY, "open": 40.13, "high": 43.29, "low": 39.44,
            "close": 43.07, "volume": 111038800., "amount": 4656185449., "turnover": .0466}])
    return pd.DataFrame([{"date": DAY, "open": "40.13", "close": "43.07", "high": "43.29",
        "low": "39.44", "amount": "1110388"}])


def response(*, symbol="sh601168", keys=None, code=0):
    row = [str(DAY), "40.13", "43.07", "43.29", "39.44", "1110388"]
    return ("kline_dayqfq2026=" + json.dumps({"code": code, "data": {symbol:
        keys if keys is not None else {"qfqday": [row]}}})).encode()


@pytest.mark.parametrize("provider", ["eastmoney", "sina", "tencent"])
def test_sdk_receipt_preserves_dates_and_units_without_certifying_adjustment(tmp_path, provider):
    raw = sdk_frame(provider)
    original = raw.copy(deep=True)
    reference = capture_sdk_history(artifact_root=tmp_path, capture_id=provider,
        request=request(), provider=provider, sdk_version="1.18.30", raw_frame=raw)
    frame, manifest = load_history_evidence(reference)
    assert manifest["request"]["adjust"] == "qfq"
    assert manifest["actual_adjustment"] is None
    assert manifest["returned_mode"] is None
    assert manifest["adjustment_evidence"] == "sdk_request_only"
    assert manifest["raw_dates"] == manifest["normalized_dates"] == [str(DAY)]
    assert manifest["unit_contract"]["volume_unit"] == "share"
    assert manifest["provider_units"]
    assert frame.iloc[0].volume == 111038800
    assert manifest["frame_origin"] == "sdk_return"
    pd.testing.assert_frame_equal(raw, original)
    with pytest.raises(ValueError, match="history_research_adjustment_unverified"):
        write_history_research_snapshot(artifact_root=tmp_path, snapshot_id="research", receipts=[reference])


def test_tencent_selects_requested_key_even_when_raw_day_comes_first(tmp_path):
    raw_day = [str(DAY), "27.70", "26.37", "27.74", "26.12", "575533"]
    qfq_day = [str(DAY), "40.13", "43.07", "43.29", "39.44", "1110388"]
    payload = response(keys={"day": [raw_day], "qfqday": [qfq_day]})
    ref = capture_tencent_http_history(artifact_root=tmp_path, capture_id="exact-qfq",
        request=request(), response_bytes=payload)
    frame, manifest = load_history_evidence(ref)
    assert frame.iloc[0].close == 43.07
    assert manifest["returned_mode"] == "qfqday"
    assert manifest["actual_adjustment"] == "qfq"
    assert manifest["adjustment_evidence"] == "response_key"
    assert manifest["adjustment_asof_certified"] is False
    assert manifest["files"]["response.bin"] == hashlib.sha256(payload).hexdigest()
    assert (tmp_path / "exact-qfq" / "response.bin").read_bytes() == payload


@pytest.mark.parametrize("adjust,key", [("", "day"), ("qfq", "qfqday"), ("hfq", "hfqday")])
def test_adjustment_key_is_explicit(tmp_path, adjust, key):
    row = [str(DAY), "40.13", "43.07", "43.29", "39.44", "1110388"]
    ref = capture_tencent_http_history(artifact_root=tmp_path, capture_id="capture",
        request=request(adjust), response_bytes=response(keys={key: [row]}))
    _, manifest = load_history_evidence(ref)
    assert manifest["returned_mode"] == key
    assert manifest["actual_adjustment"] == adjust


@pytest.mark.parametrize("payload", [
    response(keys={"day": [[str(DAY), "1", "1", "1", "1", "1"]]}),
    response(keys={"hfqday": [[str(DAY), "1", "1", "1", "1", "1"]]}),
    response(symbol="sh600000"), response(code=1), response(keys={"qfqday": []}),
    response(keys={"qfqday": [[str(DAY), "1"]]}), b"not-json", b"evil-code()={}",
])
def test_missing_requested_mode_or_malformed_response_never_falls_back(tmp_path, payload):
    with pytest.raises(ValueError, match="history_evidence_tencent_adjustment_unverified"):
        capture_tencent_http_history(artifact_root=tmp_path, capture_id="bad",
            request=request(), response_bytes=payload)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("bad_date", [None, "not-a-date", 20260807, "2026-08-07T13:00:00", "2026-08-07T00:00:00Z"])
def test_no_date_inference_or_intraday_date_truncation(tmp_path, bad_date):
    raw = sdk_frame().assign(日期=bad_date)
    with pytest.raises(ValueError, match="history_evidence_invalid_date"):
        capture_sdk_history(artifact_root=tmp_path, capture_id="bad", request=request(),
            provider="eastmoney", sdk_version="1.18.30", raw_frame=raw)


def test_duplicate_date_is_not_deduplicated_or_relocated(tmp_path):
    raw = pd.concat([sdk_frame(), sdk_frame()], ignore_index=True)
    with pytest.raises(ValueError, match="history_evidence_duplicate_date"):
        capture_sdk_history(artifact_root=tmp_path, capture_id="bad", request=request(),
            provider="eastmoney", sdk_version="1.18.30", raw_frame=raw)


def test_only_explicit_date_range_filter_is_permitted(tmp_path):
    raw = pd.concat([sdk_frame().assign(日期=START), sdk_frame(), sdk_frame().assign(日期=END)], ignore_index=True)
    ref = capture_sdk_history(artifact_root=tmp_path, capture_id="filtered", request=request(),
        provider="eastmoney", sdk_version="1.18.30", raw_frame=raw)
    frame, manifest = load_history_evidence(ref)
    assert manifest["raw_dates"] == list(map(str, [START, DAY, END]))
    assert manifest["normalized_dates"] == [str(DAY)]
    assert len(frame) == 1


def test_normalizer_cannot_reassign_a_date(tmp_path, monkeypatch):
    from app.services import market_history_evidence as service
    normalize = service.normalize_cn_stock_em_history

    def moved(raw):
        result = normalize(raw)
        result["trade_date"] = pd.Timestamp("2026-06-26")
        return result

    monkeypatch.setattr(service, "normalize_cn_stock_em_history", moved)
    with pytest.raises(ValueError, match="history_evidence_date_mapping_changed"):
        capture_sdk_history(artifact_root=tmp_path, capture_id="bad", request=request(),
            provider="eastmoney", sdk_version="1.18.30", raw_frame=sdk_frame())


@pytest.mark.parametrize("field,value", [("收盘", None), ("收盘", "NaN"), ("最高", 0), ("成交量", -1)])
def test_invalid_values_cannot_be_certified(tmp_path, field, value):
    raw = sdk_frame().assign(**{field: value})
    with pytest.raises(ValueError, match="history_evidence_invalid"):
        capture_sdk_history(artifact_root=tmp_path, capture_id="bad", request=request(),
            provider="eastmoney", sdk_version="1.18.30", raw_frame=raw)


def test_original_frame_keeps_exact_sdk_floats_and_typed_dates(tmp_path):
    raw = sdk_frame("sina")
    raw.loc[0, "turnover"] = 0.12345678901234567
    capture_sdk_history(artifact_root=tmp_path, capture_id="exact", request=request(),
        provider="sina", sdk_version="1.18.30", raw_frame=raw)
    stored = json.loads((tmp_path / "exact" / "raw_sdk.json").read_text(encoding="utf-8"))
    assert stored["rows"][0][0] == {"type": "date", "value": str(DAY)}
    assert stored["rows"][0][-1] == {"type": "float", "value": float(raw.iloc[0].turnover).hex()}


@pytest.mark.parametrize("filename", ["manifest.json", "response.bin", "raw_sdk.json", "normalized.parquet"])
def test_each_artifact_is_checked_before_replay(tmp_path, filename):
    ref = capture_tencent_http_history(artifact_root=tmp_path, capture_id="capture",
        request=request(), response_bytes=response())
    with (tmp_path / "capture" / filename).open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(ValueError, match="history_evidence_checksum_mismatch"):
        load_history_evidence(ref)


def test_capture_id_cannot_overwrite_previous_evidence(tmp_path):
    args = dict(artifact_root=tmp_path, capture_id="capture", request=request(), response_bytes=response())
    ref = capture_tencent_http_history(**args)
    before = (tmp_path / "capture" / "manifest.json").read_bytes()
    with pytest.raises(ValueError, match="history_evidence_immutable_conflict"):
        capture_tencent_http_history(**args)
    assert (tmp_path / "capture" / "manifest.json").read_bytes() == before
    load_history_evidence(ref)


def test_research_snapshot_only_exports_checked_receipts(tmp_path):
    ref = capture_tencent_http_history(artifact_root=tmp_path / "receipts", capture_id="capture",
        request=request(), response_bytes=response())
    result = write_history_research_snapshot(artifact_root=tmp_path / "research",
        snapshot_id="experiment-one", receipts=[ref])
    path = tmp_path / "research" / "experiment-one"
    manifest = json.loads((path / "manifest.json").read_text())
    assert hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest() == result["sha256"]
    assert hashlib.sha256((path / "history.parquet").read_bytes()).hexdigest() == manifest["sha256"]
    assert manifest["row_count"] == 1 and manifest["actual_adjustment"] == "qfq"
    assert manifest["receipts"] == [ref]
    assert manifest["point_in_time_certified"] is False
    assert manifest["adjustment_asof_certified"] is False
    loaded, verified = load_history_research_snapshot(result)
    assert verified == manifest
    assert loaded.iloc[0].close == 43.07
    with pytest.raises(ValueError, match="history_research_immutable_conflict"):
        write_history_research_snapshot(artifact_root=tmp_path / "research", snapshot_id="experiment-one", receipts=[ref])


def test_research_rejects_duplicate_or_mixed_adjustment_samples(tmp_path):
    ref = capture_tencent_http_history(artifact_root=tmp_path, capture_id="qfq",
        request=request(), response_bytes=response())
    with pytest.raises(ValueError, match="history_research_mixed_or_duplicate_inputs"):
        write_history_research_snapshot(artifact_root=tmp_path, snapshot_id="duplicate", receipts=[ref, ref])
    raw = capture_tencent_http_history(artifact_root=tmp_path, capture_id="raw",
        request=request(""), response_bytes=response(keys={"day": [[str(DAY), "1", "1", "1", "1", "1"]]}))
    with pytest.raises(ValueError, match="history_research_mixed_or_duplicate_inputs"):
        write_history_research_snapshot(artifact_root=tmp_path, snapshot_id="mixed", receipts=[ref, raw])


@pytest.mark.parametrize("target", ["manifest.json", "history.parquet", "receipt"])
def test_research_loader_checks_input_and_raw_receipt_files(tmp_path, target):
    ref = capture_tencent_http_history(artifact_root=tmp_path / "receipts", capture_id="capture",
        request=request(), response_bytes=response())
    result = write_history_research_snapshot(artifact_root=tmp_path / "research",
        snapshot_id="experiment", receipts=[ref])
    file = (tmp_path / "receipts" / "capture" / "response.bin" if target == "receipt"
            else tmp_path / "research" / "experiment" / target)
    with file.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(ValueError, match="checksum_mismatch"):
        load_history_research_snapshot(result)


def em_response(symbol="601168", close="43.07", day=DAY):
    return json.dumps({"rc": 0, "data": {"code": symbol, "klines": [
        f"{day},40.13,{close},43.29,39.44,1110388,4656185449,10,7.3,2.94,4.66"]}}).encode()


def baostock_frame(flag="2", code="sh.601168"):
    return pd.DataFrame([{"date": str(DAY), "code": code, "open": "40.13", "high": "43.29",
        "low": "39.44", "close": "43.07", "volume": "111038800", "amount": "4656185449",
        "turn": "4.66", "adjustflag": flag}])


def test_baostock_actual_adjustflag_and_code_are_required_per_bar(tmp_path):
    ref = capture_baostock_history(artifact_root=tmp_path, capture_id="bao", request=request(),
        sdk_version="0.8.9", raw_frame=baostock_frame())
    frame, receipt = load_history_evidence(ref)
    assert receipt["actual_adjustment"] == "qfq"
    assert receipt["returned_mode"] == "adjustflag:2"
    assert frame.iloc[0].turnover_rate == 4.66
    for flag, code in [("3", "sh.601168"), ("2", "sh.600000")]:
        with pytest.raises(ValueError, match="baostock_adjustment_unverified"):
            capture_baostock_history(artifact_root=tmp_path, capture_id="bad", request=request(),
                sdk_version="0.8.9", raw_frame=baostock_frame(flag, code))


def test_eastmoney_http_capture_preserves_raw_bytes_without_claiming_qfq(tmp_path):
    raw = em_response()
    ref = capture_eastmoney_http_history(artifact_root=tmp_path, capture_id="em", request=request(), response_bytes=raw)
    frame, receipt = load_history_evidence(ref)
    assert receipt["actual_adjustment"] is None and receipt["request"]["adjust"] == "qfq"
    assert receipt["files"]["response.bin"] == hashlib.sha256(raw).hexdigest()
    assert frame.iloc[0].volume == 111038800
    with pytest.raises(ValueError, match="invalid_eastmoney_response"):
        capture_eastmoney_http_history(artifact_root=tmp_path, capture_id="bad", request=request(),
            response_bytes=em_response("600000"))


def trusted_fixture(tmp_path, monkeypatch, *, cohort=("601168",), em_close="43.07", dataset_id="experiment"):
    from app.services import market_history_evidence as service
    monkeypatch.setattr(service, "TRUSTED_HISTORY_DATASET_ROOT", tmp_path)
    root = tmp_path / dataset_id / "receipts"
    em = capture_eastmoney_http_history(artifact_root=root, capture_id="em", request=request(),
        response_bytes=em_response(close=em_close))
    tx = capture_tencent_http_history(artifact_root=root, capture_id="tx", request=request(), response_bytes=response())
    return write_trusted_history_dataset(dataset_id=dataset_id, receipts=[em, tx], cohort_symbols=cohort,
        calendar=[DAY], plan_sha256="f" * 64)


def test_trusted_dataset_join_is_field_linked_to_two_independent_receipts(tmp_path, monkeypatch):
    result = trusted_fixture(tmp_path, monkeypatch)
    dataset = load_trusted_history_dataset("experiment")
    assert dataset.manifest_sha256 == result["sha256"]
    assert dataset.symbols == ("601168",)
    assert dataset.calendar == (DAY,)
    assert dataset.bars.iloc[0].close == 43.07  # Tencent qfqday OHLC.
    assert dataset.bars.iloc[0].volume == 111038800  # EM lots -> shares.
    assert dataset.bars.iloc[0].amount == 4656185449  # EM CNY.
    assert dataset.bars.iloc[0].turnover_rate == 4.66  # EM percentage points.
    assert dataset.identity["research_only"] is True
    assert dataset.identity["research_evaluation_status"] == "previously_observed_development_comparison"
    assert dataset.identity["receipt_count"] == 2


def test_missing_symbol_remains_in_frozen_coverage_denominator(tmp_path, monkeypatch):
    trusted_fixture(tmp_path, monkeypatch, cohort=("000001", "601168"))
    dataset = load_trusted_history_dataset("experiment")
    assert dataset.symbols == ("000001", "601168")
    assert set(dataset.bars.symbol) == {"601168"}
    quality = json.loads((tmp_path / "experiment" / "quality.json").read_bytes())
    assert quality["per_symbol"]["000001"]["accepted_rows"] == 0
    assert quality["per_symbol"]["000001"]["reason"] == "missing_provider_receipt"


@pytest.mark.parametrize("close,accepted", [("43.09", True), ("43.10", False)])
def test_price_tolerance_is_two_cents_fixed_before_model_results(tmp_path, monkeypatch, close, accepted):
    if accepted:
        trusted_fixture(tmp_path, monkeypatch, em_close=close)
        assert load_trusted_history_dataset("experiment").bars.iloc[0].close == 43.07
    else:
        with pytest.raises(ValueError, match="trusted_history_no_verified_samples"):
            trusted_fixture(tmp_path, monkeypatch, em_close=close)
        assert not (tmp_path / "experiment" / "manifest.json").exists()
        quality = json.loads((tmp_path / "experiment" / "quality.json").read_bytes())
        assert quality["per_symbol"]["601168"]["provider_price_disagreement_dates"] == [str(DAY)]


@pytest.mark.parametrize("field", ["close", "volume", "amount", "turnover_rate"])
def test_matching_updated_parquet_hash_cannot_hide_changed_values(tmp_path, monkeypatch, field):
    from app.services import market_history_evidence as service
    trusted_fixture(tmp_path, monkeypatch)
    dataset = load_trusted_history_dataset("experiment")
    bars = dataset.bars.copy()
    bars.loc[0, field] += .01 if field == "close" else 1
    path = tmp_path / "experiment"
    service._write_parquet(path / "bars.parquet", bars)
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["bars_sha256"] = hashlib.sha256((path / "bars.parquet").read_bytes()).hexdigest()
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="trusted_history_field_value_mismatch"):
        load_trusted_history_dataset("experiment")


def test_dataset_path_and_receipt_path_cannot_escape_fixed_root(tmp_path, monkeypatch):
    trusted_fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="invalid_dataset_id"):
        load_trusted_history_dataset("../experiment")
    path = tmp_path / "experiment" / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["evidence"]["receipts"][0]["path"] = "../../outside.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="trusted_history_path_outside_dataset"):
        load_trusted_history_dataset("experiment")


def test_complete_calendar_day_cannot_be_silently_deleted(tmp_path, monkeypatch):
    from app.services import market_history_evidence as service
    monkeypatch.setattr(service, "TRUSTED_HISTORY_DATASET_ROOT", tmp_path)
    root = tmp_path / "experiment" / "receipts"
    refs = [capture_eastmoney_http_history(artifact_root=root, capture_id="em", request=request(), response_bytes=em_response()),
        capture_tencent_http_history(artifact_root=root, capture_id="tx", request=request(), response_bytes=response())]
    with pytest.raises(ValueError, match="whole_calendar_day_missing"):
        write_trusted_history_dataset(dataset_id="experiment", receipts=refs, cohort_symbols=["601168"],
            calendar=[START, DAY], plan_sha256="f" * 64)
    assert not (tmp_path / "experiment" / "manifest.json").exists()


def test_trusted_dataset_cannot_replace_existing_manifest(tmp_path, monkeypatch):
    trusted_fixture(tmp_path, monkeypatch)
    manifest = (tmp_path / "experiment" / "manifest.json").read_bytes()
    with pytest.raises(ValueError, match="trusted_history_immutable_conflict"):
        write_trusted_history_dataset(dataset_id="experiment", receipts=[], cohort_symbols=["601168"],
            calendar=[DAY], plan_sha256="f" * 64)
    assert (tmp_path / "experiment" / "manifest.json").read_bytes() == manifest


def test_actual_wire_parameters_and_observation_time_are_frozen_as_metadata(tmp_path):
    metadata = {"url": "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
        "params": {"param": "sh601168,day,2026-08-07,2026-08-07,640,qfq"},
        "at": "2026-10-16T15:00:00Z", "status": 200, "attempt": 1}
    ref = capture_tencent_http_history(artifact_root=tmp_path, capture_id="capture",
        request=request(), response_bytes=response(), acquisition_metadata=metadata)
    metadata["params"]["param"] = "changed after acquisition"
    _, manifest = load_history_evidence(ref)
    assert manifest["acquisition_metadata"]["params"]["param"].endswith(",640,qfq")
    assert manifest["acquisition_metadata"]["at"] == "2026-10-16T15:00:00Z"
    assert manifest["adjustment_asof_certified"] is False
