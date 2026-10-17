"""Sina raw bundle -> replayed receipt -> frozen field lineage, no I/O transport."""
from datetime import date
import hashlib
import json

import pytest

from app.services import market_history_evidence as evidence
from app.services.market_data_sources.sources import sina_history_evidence as sina


DAY = date(2026, 8, 7)
SYMBOL = "sz000425"


def bundle():
    return {"history_bytes": b'var KLC_K2_sz000425="K2/ABC";',
        "shares_bytes": b'var KKE_ShareAmount_sz000425=([{"date":"2026-08-06","amount":10}]);',
        "qfq_bytes": b'var sz000425qfq={"total":1,"data":[{"d":"2026-08-06","f":"2"}]};'}


@pytest.fixture
def decoder(monkeypatch):
    monkeypatch.setattr(sina, "_decode_compressed_history", lambda _encoded: [{
        "date": "2026-08-07T00:00:00.000Z", "open": 80.26, "high": 86.58,
        "low": 78.88, "close": 86.14, "volume": 2000, "amount": 86000}])


def capture(root, *, capture_id="sina"):
    return evidence.capture_sina_http_history_bundle(artifact_root=root, capture_id=capture_id,
        request=evidence.HistoryRequest(SYMBOL, DAY, DAY, ""), **bundle())


def tx_response(*, adjust="qfq", close=None, quantity="20"):
    row = ([str(DAY), "40.13", close or "43.07", "43.29", "39.44", quantity] if adjust == "qfq"
        else [str(DAY), "80.26", close or "86.14", "86.58", "78.88", quantity])
    return json.dumps({"code": 0, "data": {SYMBOL: {"qfqday" if adjust == "qfq" else "day": [row]}}}).encode()


def em_response(close="43.07", amount="4656185449"):
    return json.dumps({"rc": 0, "data": {"code": "000425", "klines": [
        f"{DAY},40.13,{close},43.29,39.44,1110388,{amount},10,7.3,2.94,4.66"]}}).encode()


def build(tmp_path, monkeypatch, *, em_close=None, em_amount="4656185449", raw_close=None,
    qfq_close=None, raw_quantity="20", qfq_quantity="20"):
    monkeypatch.setattr(evidence, "TRUSTED_HISTORY_DATASET_ROOT", tmp_path)
    directory = tmp_path / "research" / "receipts"
    request = evidence.HistoryRequest(SYMBOL, DAY, DAY, "qfq")
    refs = [capture(directory), evidence.capture_tencent_http_history(artifact_root=directory,
        capture_id="tx", request=request, response_bytes=tx_response(close=qfq_close, quantity=qfq_quantity)),
        evidence.capture_tencent_http_history(artifact_root=directory, capture_id="tx-raw",
            request=evidence.HistoryRequest(SYMBOL, DAY, DAY, ""),
            response_bytes=tx_response(adjust="", close=raw_close, quantity=raw_quantity))]
    if em_close is not None:
        refs.append(evidence.capture_eastmoney_http_history(artifact_root=directory, capture_id="em",
            request=request, response_bytes=em_response(em_close, em_amount)))
    result = evidence.write_trusted_history_dataset(dataset_id="research", receipts=refs,
        cohort_symbols=["000425"], calendar=[DAY], plan_sha256="a" * 64)
    return evidence.load_trusted_history_dataset("research"), result


def test_three_raw_payloads_and_turnover_derivation_are_replayed(tmp_path, decoder):
    reference = capture(tmp_path)
    frame, receipt = evidence.load_history_evidence(reference)
    assert frame.iloc[0].close == 86.14
    assert frame.iloc[0].volume == 2000
    assert frame.iloc[0].amount == 86000
    assert frame.iloc[0].turnover_rate == 2
    assert receipt["provider"] == "sina_http"
    assert receipt["frame_origin"] == "parsed_sina_http_bundle"
    assert receipt["actual_adjustment"] == ""
    assert receipt["adjustment_evidence"] == "unadjusted_history_wire"
    assert receipt["derivation_direction"] == "backward_asof_only"
    assert receipt["adjustment_asof_certified"] is False
    for name, raw in bundle().items():
        filename = name.replace("_bytes", ".response")
        assert receipt["files"][filename] == hashlib.sha256(raw).hexdigest()
    assert "known_share_count" in receipt["derived_turnover_formula"]


def test_sina_liquidity_needs_independent_tx_price_verification(tmp_path, monkeypatch, decoder):
    dataset, _ = build(tmp_path, monkeypatch)
    bar = dataset.bars.iloc[0]
    assert bar.close == 43.07 and bar.volume == 2000 and bar.turnover_rate == 2
    manifest = json.loads((dataset.directory / "manifest.json").read_bytes())
    source = [item for item in manifest["evidence"]["field_lineage"] if "turnover_rate" in item["fields"]][0]
    assert source["verified_against_receipt_sha256"]


def test_existing_good_em_is_preferred_even_if_sina_has_other_feature_values(tmp_path, monkeypatch, decoder):
    dataset, _ = build(tmp_path, monkeypatch, em_close="43.07")
    bar = dataset.bars.iloc[0]
    assert bar.volume == 111038800 and bar.turnover_rate == 4.66
    quality = json.loads((dataset.directory / "quality.json").read_bytes())
    assert quality["per_symbol"]["000425"]["quantity_sources"]["eastmoney"]["accepted_rows"] == 1
    assert quality["per_symbol"]["000425"]["quantity_sources"]["sina_http"]["accepted_rows"] == 0


@pytest.mark.parametrize("close,amount", [("43.10", "4656185449"), ("43.07", "unavailable")])
def test_only_price_or_missing_quantity_quality_failure_allows_fallback(tmp_path, monkeypatch, decoder, close, amount):
    dataset, _ = build(tmp_path, monkeypatch, em_close=close, em_amount=amount)
    assert dataset.bars.iloc[0].volume == 2000 and dataset.bars.iloc[0].turnover_rate == 2


@pytest.mark.parametrize("filename", ["history.response", "shares.response", "qfq.response"])
def test_each_raw_component_is_checksum_guarded(tmp_path, decoder, filename):
    ref = capture(tmp_path)
    with (tmp_path / "sina" / filename).open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(ValueError, match="checksum_mismatch"):
        evidence.load_history_evidence(ref)


def test_changing_derivation_metadata_and_its_hash_does_not_change_raw_truth(tmp_path, decoder):
    ref = capture(tmp_path)
    path = tmp_path / "sina" / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["sina_evidence"]["asof_direction"] = "forward"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    changed = {**ref, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(ValueError, match="sina_derivation_mismatch"):
        evidence.load_history_evidence(changed)


def test_loader_rechecks_provider_priority_when_all_parquet_hashes_match(tmp_path, monkeypatch, decoder):
    dataset, _ = build(tmp_path, monkeypatch, em_close="43.07")
    path = dataset.directory
    manifest = json.loads((path / "manifest.json").read_bytes())
    ref = next(ref for ref in manifest["evidence"]["receipts"] if ref["capture_id"] == "sina")
    raw_ref = next(ref for ref in manifest["evidence"]["receipts"] if ref["capture_id"] == "tx-raw")
    source, _ = evidence.load_history_evidence({**ref, "path": str(path / ref["path"])})
    bars = dataset.bars.copy()
    for field in ("volume", "amount", "turnover_rate"):
        bars.loc[0, field] = source.iloc[0][field]
    evidence._write_parquet(path / "bars.parquet", bars)
    manifest["bars_sha256"] = hashlib.sha256((path / "bars.parquet").read_bytes()).hexdigest()
    for item in manifest["evidence"]["field_lineage"]:
        if "turnover_rate" in item["fields"]:
            item["receipt_sha256"] = ref["sha256"]
            item["verified_against_raw_receipt_sha256"] = raw_ref["sha256"]
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="quantity_provider_priority_violation"):
        evidence.load_trusted_history_dataset("research")


def test_legal_qfq_algorithm_difference_does_not_substitute_sina_prices(tmp_path, monkeypatch, decoder):
    dataset, _ = build(tmp_path, monkeypatch, qfq_close="43.12")
    # Synthetic source fixtures exercise the rule; this is not a market result.
    # Sina's factor-2 quote would be 43.07, but only its raw prices and quantity
    # are checked. The canonical price must remain Tencent's exact qfq quote.
    assert dataset.bars.iloc[0].close == 43.12
    assert dataset.bars.iloc[0].volume == 2000


@pytest.mark.parametrize("raw_close,accepted", [("86.16", True), ("86.17", False)])
def test_raw_price_agreement_cannot_be_replaced_with_qfq_agreement(tmp_path, monkeypatch, decoder, raw_close, accepted):
    if accepted:
        dataset, _ = build(tmp_path, monkeypatch, raw_close=raw_close)
        assert dataset.bars.iloc[0].close == 43.07
    else:
        with pytest.raises(ValueError, match="no_verified_samples"):
            build(tmp_path, monkeypatch, raw_close=raw_close)


@pytest.mark.parametrize("quantity,accepted", [("21", True), ("21.01", False), ("2000", False)])
def test_share_volume_gate_allows_one_lot_but_not_factor_100_errors(tmp_path, monkeypatch, decoder, quantity, accepted):
    if accepted:
        dataset, _ = build(tmp_path, monkeypatch, raw_quantity=quantity, qfq_quantity=quantity)
        assert dataset.bars.iloc[0].volume == 2000
    else:
        with pytest.raises(ValueError, match="no_verified_samples"):
            build(tmp_path, monkeypatch, raw_quantity=quantity, qfq_quantity=quantity)


def test_tencent_raw_and_qfq_volume_must_be_identical(tmp_path, monkeypatch, decoder):
    with pytest.raises(ValueError, match="no_verified_samples"):
        build(tmp_path, monkeypatch, raw_quantity="20", qfq_quantity="21")
    quality = json.loads((tmp_path / "research" / "quality.json").read_bytes())
    assert quality["per_symbol"]["000425"]["quantity_sources"]["sina_http"]["tx_raw_qfq_volume_disagreement_dates"] == [str(DAY)]
