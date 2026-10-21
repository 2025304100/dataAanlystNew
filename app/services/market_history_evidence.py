"""Local, immutable receipts for already acquired CN stock history.

This module performs no network request, SQL write, model registration or
activation. A unit contract describes normalization; it never certifies that a
provider honored the requested price adjustment. Only an actual response key
can establish Tencent's returned adjustment here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterable, Mapping

import duckdb
import numpy as np
import pandas as pd

from app.services.market_history_contract import cn_history_instrument, history_source_tag, tencent_quantity_input_contract
from app.services.market_data_sources.sources._normalize import (
    normalize_cn_stock_em_history,
    normalize_cn_stock_sina_history,
    normalize_cn_stock_tx_history,
)


_SCHEMA = 1
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_PROVIDERS = {
    "eastmoney": ("stock_zh_a_hist", "日期"),
    "sina": ("stock_zh_a_daily", "date"),
    "tencent": ("stock_zh_a_hist_tx", "date"),
    "baostock": ("query_history_k_data_plus", "date"),
    "sina_http": ("sina_history_http_bundle_v1", "trade_date"),
}
_ADJUSTMENTS = {"": "day", "qfq": "qfqday", "hfq": "hfqday"}
_HISTORY_COLUMNS = ["trade_date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
_CROSS_PROVIDER_PRICE_ATOL = 0.02
_CROSS_PROVIDER_VOLUME_ATOL_SHARES = 100
_SINA_TURNOVER_FORMULA = "volume_shares / (known_share_count_ten_thousand_shares * 10000) * 100"
_PROVIDER_UNITS = {
    "eastmoney": {"成交量": "lot_100_shares", "成交额": "CNY", "换手率": "percentage_point"},
    "sina": {"volume": "share", "amount": "CNY", "turnover": "fraction"},
    "tencent": {"amount": "lot_100_shares", "cash_amount": "unavailable", "turnover_rate": "unavailable"},
    "baostock": {"volume": "share", "amount": "CNY", "turn": "percentage_point"},
    "sina_http": {"volume": "share", "amount": "CNY", "share_count": "ten_thousand_shares",
        "turnover_rate": "derived_percentage_point"},
}


@dataclass(frozen=True)
class HistoryRequest:
    symbol: str
    start_date: date
    end_date: date
    adjust: str

    def validate(self) -> None:
        if (not isinstance(self.symbol, str) or not re.fullmatch(r"(?:sh|sz|bj)?\d{6}", self.symbol)
                or type(self.start_date) is not date or type(self.end_date) is not date
                or self.start_date > self.end_date or self.adjust not in _ADJUSTMENTS):
            raise ValueError("history_evidence_invalid_request")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _scalar(value: Any) -> Any:
    """Keep SDK scalar types and exact binary floats; never pickle evidence."""
    if value is None or value is pd.NA or value is pd.NaT:
        return {"type": "null"}
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (pd.Timestamp, datetime)):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, float):
        return {"type": "float", "value": value.hex()}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, (str, int, bool)):
        return {"type": type(value).__name__, "value": value}
    raise ValueError(f"history_evidence_unsupported_sdk_scalar:{type(value).__name__}")


def _sdk_bytes(frame: pd.DataFrame) -> bytes:
    if not frame.columns.is_unique or not all(isinstance(c, str) for c in frame.columns):
        raise ValueError("history_evidence_invalid_columns")
    return _json_bytes({"columns": list(frame.columns), "dtypes": [str(t) for t in frame.dtypes],
        "index": [_scalar(v) for v in frame.index],
        "rows": [[_scalar(v) for v in row] for row in frame.itertuples(index=False, name=None)]})


def _dates(values: Iterable[Any]) -> list[str]:
    result = []
    for value in values:
        if not isinstance(value, (str, date, datetime, pd.Timestamp, np.datetime64)):
            raise ValueError("history_evidence_invalid_date")
        try:
            parsed = pd.Timestamp(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("history_evidence_invalid_date") from exc
        if pd.isna(parsed) or parsed.tzinfo is not None or parsed != parsed.normalize():
            raise ValueError("history_evidence_invalid_date")
        result.append(parsed.date().isoformat())
    if len(set(result)) != len(result):
        raise ValueError("history_evidence_duplicate_date")
    return result


def _validate_normalized(frame: pd.DataFrame) -> None:
    if frame.empty or list(frame.columns) != _HISTORY_COLUMNS:
        raise ValueError("history_evidence_invalid_normalized_frame")
    if frame.attrs.get("history_contract", {}).get("provider") == "baostock":
        if frame.attrs["history_contract"] != {"provider": "baostock",
                "endpoint": "query_history_k_data_plus", "version": "cn-stock-shares-v1",
                "volume_unit": "share", "amount_unit": "CNY", "turnover_unit": "percentage_point"}:
            raise ValueError("history_evidence_invalid_unit_contract")
    else:
        history_source_tag(frame)  # Reject a malformed or mismatched explicit unit contract.
    if frame.attrs.get("history_contract") is None:
        raise ValueError("history_evidence_missing_unit_contract")
    _dates(frame.trade_date)
    values = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("history_evidence_invalid_ohlc")
    if not ((frame.low <= frame[["open", "close"]].min(axis=1))
            & (frame.high >= frame[["open", "close"]].max(axis=1))).all():
        raise ValueError("history_evidence_invalid_ohlc")
    for field in ("volume", "amount", "turnover_rate"):
        known = frame[field].dropna().to_numpy(dtype=float)
        if not np.isfinite(known).all() or (known < 0).any():
            raise ValueError("history_evidence_invalid_quantity")


def _write_parquet(path: Path, frame: pd.DataFrame) -> None:
    with duckdb.connect(":memory:") as connection:
        connection.register("receipt_frame", frame)
        connection.execute("COPY receipt_frame TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(path)])
    with path.open("r+b") as handle:
        import os
        os.fsync(handle.fileno())


def _write_exclusive(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        import os
        os.fsync(handle.fileno())


def _request_dict(request: HistoryRequest) -> dict:
    return {**asdict(request), "start_date": request.start_date.isoformat(),
            "end_date": request.end_date.isoformat()}


def capture_sdk_history(*, artifact_root: Path, capture_id: str, request: HistoryRequest,
    provider: str, sdk_version: str, raw_frame: pd.DataFrame,
    acquisition_metadata: Mapping[str, Any] | None = None) -> dict:
    """Freeze a raw SDK frame and its normalizer result before any SQL write.

    SDK frames omit the server's response key. Their actual adjustment remains
    unknown even when the request says qfq; research export rejects these by
    default. Use ``capture_tencent_http_history`` when response bytes exist.
    """
    if provider not in {"eastmoney", "sina", "tencent"}:
        raise ValueError("history_evidence_use_provider_verified_capture")
    if provider == "tencent":
        raw_frame = raw_frame.copy(deep=True)
        raw_frame.attrs["tencent_quantity_contract"] = tencent_quantity_input_contract(request.symbol)
    return _capture(artifact_root=artifact_root, capture_id=capture_id, request=request,
        provider=provider, sdk_version=sdk_version, raw_frame=raw_frame,
        response_bytes=None, returned_mode=None, acquisition_metadata=acquisition_metadata)


def _parse_tencent_response(request: HistoryRequest, response_bytes: bytes) -> tuple[pd.DataFrame, str]:
    """Select the exact requested K-line key from Tencent's actual response.

    AkShare 1.18.30 prefers ``day`` even when qfq was requested. This parser
    deliberately never falls back to day/hfqday when qfqday is absent.
    """
    request.validate()
    if not request.symbol.startswith(("sh", "sz")) or not isinstance(response_bytes, bytes):
        raise ValueError("history_evidence_invalid_tencent_response")
    try:
        text = response_bytes.decode("utf-8-sig").strip()
        if not text.startswith("{"):
            # Tencent's endpoint returns `kline_dayqfq2026={...}` JSONP-style text.
            left, separator, right = text.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", left.strip()):
                raise ValueError("invalid response envelope")
            text = right
        body = json.loads(text.removesuffix(";"))
        if body.get("code", 0) != 0:
            raise ValueError("unsuccessful response")
        data = body["data"][request.symbol]
        mode = _ADJUSTMENTS[request.adjust]
        rows = data[mode]
        if not isinstance(rows, list) or not rows or any(not isinstance(r, list) or len(r) < 6 for r in rows):
            raise ValueError("invalid requested data key")
        raw = pd.DataFrame([r[:6] for r in rows], columns=["date", "open", "close", "high", "low", "amount"])
        raw.attrs["tencent_quantity_contract"] = tencent_quantity_input_contract(request.symbol)
    except (UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("history_evidence_tencent_adjustment_unverified") from exc
    return raw, mode


def capture_tencent_http_history(*, artifact_root: Path, capture_id: str,
    request: HistoryRequest, response_bytes: bytes, sdk_version: str = "direct-http",
    acquisition_metadata: Mapping[str, Any] | None = None) -> dict:
    """Freeze Tencent's exact requested response key without SDK fallback."""
    raw, mode = _parse_tencent_response(request, response_bytes)
    return _capture(artifact_root=artifact_root, capture_id=capture_id, request=request,
        provider="tencent", sdk_version=sdk_version, raw_frame=raw,
        response_bytes=response_bytes, returned_mode=mode, acquisition_metadata=acquisition_metadata)


def _parse_eastmoney_response(request: HistoryRequest, response_bytes: bytes) -> pd.DataFrame:
    """Freeze EM's actual daily-K JSON; adjustment still needs cross-validation."""
    request.validate()
    try:
        body = json.loads(response_bytes)
        data = body["data"]
        if body.get("rc", 0) != 0 or data["code"] != request.symbol[-6:]:
            raise ValueError("instrument mismatch")
        rows = [line.split(",") for line in data["klines"]]
        if not rows or any(len(row) < 11 for row in rows):
            raise ValueError("unsupported daily K fields")
        raw = pd.DataFrame([row[:11] for row in rows], columns=["日期", "开盘", "收盘", "最高", "最低",
            "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("history_evidence_invalid_eastmoney_response") from exc
    return raw


def capture_eastmoney_http_history(*, artifact_root: Path, capture_id: str,
    request: HistoryRequest, response_bytes: bytes, sdk_version: str = "direct-http",
    acquisition_metadata: Mapping[str, Any] | None = None) -> dict:
    """Freeze EM JSON while leaving its adjustment claim unverified alone."""
    raw = _parse_eastmoney_response(request, response_bytes)
    return _capture(artifact_root=artifact_root, capture_id=capture_id, request=request,
        provider="eastmoney", sdk_version=sdk_version, raw_frame=raw,
        response_bytes=response_bytes, returned_mode=None, acquisition_metadata=acquisition_metadata)


def capture_baostock_history(*, artifact_root: Path, capture_id: str,
    request: HistoryRequest, sdk_version: str, raw_frame: pd.DataFrame,
    acquisition_metadata: Mapping[str, Any] | None = None) -> dict:
    """Freeze SDK rows including actual code/adjustflag returned per bar."""
    request.validate()
    required = {"date", "code", "open", "high", "low", "close", "volume", "amount", "turn", "adjustflag"}
    flag = {"": "3", "qfq": "2", "hfq": "1"}[request.adjust]
    if (not request.symbol.startswith(("sh", "sz")) or raw_frame.empty or not required.issubset(raw_frame)
            or not raw_frame["code"].eq(request.symbol[:2] + "." + request.symbol[-6:]).all()
            or not raw_frame["adjustflag"].eq(flag).all()):
        raise ValueError("history_evidence_baostock_adjustment_unverified")
    return _capture(artifact_root=artifact_root, capture_id=capture_id, request=request,
        provider="baostock", sdk_version=sdk_version, raw_frame=raw_frame,
        response_bytes=None, returned_mode=f"adjustflag:{flag}", acquisition_metadata=acquisition_metadata)


def _parse_sina_bundle(request: HistoryRequest, history_bytes: bytes,
    shares_bytes: bytes, qfq_bytes: bytes) -> pd.DataFrame:
    from app.services.market_data_sources.sources.sina_history_evidence import parse_sina_history_bundle
    if request.adjust not in {"", "qfq"}:
        raise ValueError("history_evidence_sina_bundle_invalid_adjust")
    return parse_sina_history_bundle(symbol=request.symbol, history_bytes=history_bytes,
        shares_bytes=shares_bytes, qfq_bytes=qfq_bytes,
        start_date=request.start_date, end_date=request.end_date, adjust=request.adjust)


def capture_sina_http_history_bundle(*, artifact_root: Path, capture_id: str, request: HistoryRequest,
    history_bytes: bytes, shares_bytes: bytes, qfq_bytes: bytes, sdk_version: str = "direct-http",
    acquisition_metadata: Mapping[str, Any] | None = None) -> dict:
    """Freeze three raw responses and explicitly derived turnover/price basis.

    The factor wire is retained even in raw mode; it is not applied when
    request.adjust is empty. Trusted research consumes only the raw liquidity
    fields after independent Tencent raw price/volume verification.
    """
    request.validate()
    frame = _parse_sina_bundle(request, history_bytes, shares_bytes, qfq_bytes)
    return _capture(artifact_root=artifact_root, capture_id=capture_id, request=request,
        provider="sina_http", sdk_version=sdk_version, raw_frame=frame,
        response_bytes=None, returned_mode=("raw_klc_history" if request.adjust == "" else "qfq_factors_backward_asof"),
        extra_responses={"history.response": history_bytes, "shares.response": shares_bytes, "qfq.response": qfq_bytes},
        bundle_metadata=frame.attrs.get("sina_evidence", {}), acquisition_metadata=acquisition_metadata)


def _normalize_received(raw_frame: pd.DataFrame, provider: str, request: HistoryRequest) -> pd.DataFrame:
    raw_dates = _dates(raw_frame[_PROVIDERS[provider][1]])
    expected_dates = [d for d in raw_dates if request.start_date.isoformat() <= d <= request.end_date.isoformat()]
    source = raw_frame.copy(deep=True)
    if provider == "eastmoney":
        # Unlike Sina/Tencent this normalizer has no range arguments.
        keep = [request.start_date.isoformat() <= d <= request.end_date.isoformat() for d in raw_dates]
        normalized = normalize_cn_stock_em_history(source.loc[keep].copy())
    elif provider == "sina":
        normalized = normalize_cn_stock_sina_history(source, request.start_date, request.end_date)
    elif provider == "tencent":
        normalized = normalize_cn_stock_tx_history(source, request.start_date, request.end_date)
    elif provider == "sina_http":
        normalized = source[_HISTORY_COLUMNS].copy()
        normalized["trade_date"] = pd.to_datetime(normalized.trade_date)
        normalized.attrs["history_contract"] = {"provider": "sina", "endpoint": "stock_zh_a_daily",
            "version": "cn-stock-shares-v1", "volume_unit": "share", "amount_unit": "CNY",
            "turnover_unit": "percentage_point"}
    else:
        normalized = source.rename(columns={"date": "trade_date", "turn": "turnover_rate"})[_HISTORY_COLUMNS].copy()
        normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
        normalized = normalized.loc[(normalized.trade_date >= pd.Timestamp(request.start_date))
            & (normalized.trade_date <= pd.Timestamp(request.end_date))].reset_index(drop=True)
        for column in _HISTORY_COLUMNS[1:]:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        normalized.attrs["history_contract"] = {"provider": "baostock",
            "endpoint": "query_history_k_data_plus", "version": "cn-stock-shares-v1",
            "volume_unit": "share", "amount_unit": "CNY", "turnover_unit": "percentage_point"}
    _validate_normalized(normalized)
    normalized_dates = _dates(normalized.trade_date)
    if normalized_dates != expected_dates:
        raise ValueError("history_evidence_date_mapping_changed")
    return normalized


def _capture(*, artifact_root: Path, capture_id: str, request: HistoryRequest, provider: str,
    sdk_version: str, raw_frame: pd.DataFrame, response_bytes: bytes | None,
    returned_mode: str | None, extra_responses: Mapping[str, bytes] | None = None,
    bundle_metadata: Mapping[str, Any] | None = None,
    acquisition_metadata: Mapping[str, Any] | None = None) -> dict:
    request.validate()
    if not isinstance(capture_id, str) or not _SAFE_ID.fullmatch(capture_id):
        raise ValueError("history_evidence_invalid_capture_id")
    if provider not in _PROVIDERS or not isinstance(sdk_version, str) or not sdk_version:
        raise ValueError("history_evidence_invalid_provider")
    endpoint, date_column = _PROVIDERS[provider]
    if date_column not in raw_frame:
        raise ValueError("history_evidence_missing_date")
    raw_bytes = _sdk_bytes(raw_frame)
    if acquisition_metadata is not None and not isinstance(acquisition_metadata, Mapping):
        raise ValueError("history_evidence_invalid_acquisition_metadata")
    frozen_acquisition = json.loads(_json_bytes(dict(acquisition_metadata or {})))
    raw_dates = _dates(raw_frame[date_column])
    normalized = _normalize_received(raw_frame, provider, request)
    normalized_dates = _dates(normalized.trade_date)
    root = Path(artifact_root).resolve()
    destination = root / capture_id
    if destination.exists():
        raise ValueError("history_evidence_immutable_conflict")
    root.mkdir(parents=True, exist_ok=True)
    # An incomplete staging directory is intentionally retained on I/O failure;
    # it is never accepted as a receipt and can be investigated independently.
    stage = Path(tempfile.mkdtemp(prefix=f".{capture_id}-", dir=root))
    _write_exclusive(stage / "raw_sdk.json", raw_bytes)
    _write_parquet(stage / "normalized.parquet", normalized)
    files = {"raw_sdk.json": _sha(raw_bytes),
             "normalized.parquet": _sha((stage / "normalized.parquet").read_bytes())}
    if response_bytes is not None:
        _write_exclusive(stage / "response.bin", response_bytes)
        files["response.bin"] = _sha(response_bytes)
    if extra_responses:
        if provider != "sina_http" or set(extra_responses) != {"history.response", "shares.response", "qfq.response"}:
            raise ValueError("history_evidence_invalid_response_bundle")
        for name, payload in extra_responses.items():
            _write_exclusive(stage / name, payload)
            files[name] = _sha(payload)
    manifest = {"schema_version": _SCHEMA, "kind": "cn_stock_history_receipt",
        "capture_id": capture_id, "request": _request_dict(request), "provider": provider,
        "endpoint": endpoint,
        "transport_endpoint": ({"tencent": "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
            "eastmoney": "https://push2his.eastmoney.com/api/qt/stock/kline/get"}.get(provider)
            if response_bytes is not None else None),
        "sdk_version": sdk_version, "sdk_columns": list(raw_frame.columns),
        "capture_implementation_sha256": _sha(Path(__file__).read_bytes()),
        "adapter_normalizer_sha256": _sha((Path(__file__).parent / "market_data_sources" / "sources" / "_normalize.py").read_bytes()),
        "sdk_dtypes": [str(t) for t in raw_frame.dtypes],
        "frame_origin": ("parsed_sina_http_bundle" if provider == "sina_http"
            else "selected_http_response_key" if response_bytes is not None else "sdk_return"),
        "provider_units": ({**_PROVIDER_UNITS[provider], "amount": raw_frame.attrs["tencent_quantity_contract"]["volume_input_unit"]}
            if provider == "tencent" else _PROVIDER_UNITS[provider]),
        "returned_mode": returned_mode,
        "actual_adjustment": request.adjust if returned_mode is not None else None,
        "adjustment_evidence": ("returned_adjustflag" if provider == "baostock"
            else ("unadjusted_history_wire" if request.adjust == "" else "qfq_factor_wire_requires_crosscheck") if provider == "sina_http"
            else "response_key" if returned_mode is not None else "sdk_request_only"),
        "adjustment_asof_certified": False,
        "unit_contract": dict(normalized.attrs["history_contract"]),
        "raw_date_column": date_column, "raw_dates": raw_dates,
        "normalized_dates": normalized_dates, "range_filter_only": True,
        "row_count": len(normalized), "files": files,
        "captured_at": datetime.now(timezone.utc).isoformat()}
    if frozen_acquisition:
        manifest["acquisition_metadata"] = frozen_acquisition
    if provider == "tencent":
        manifest["input_unit_contract"] = dict(raw_frame.attrs["tencent_quantity_contract"])
        manifest["quantity_contract_binding"] = "response_symbol" if response_bytes is not None else "requested_sdk_symbol_only"
    if provider == "sina_http":
        manifest["sina_evidence"] = dict(bundle_metadata or {})
        manifest["derived_turnover_formula"] = _SINA_TURNOVER_FORMULA
        manifest["derivation_direction"] = "backward_asof_only"
        manifest["provider_endpoint_templates"] = {
            "history.response": f"https://finance.sina.com.cn/realstock/company/{request.symbol}/hisdata_klc2/klc_kl.js",
            "shares.response": f"https://stock.finance.sina.com.cn/stock/api/jsonp.php/var%20KKE_ShareAmount_{request.symbol}=/StockService.getAmountBySymbol?_=20&symbol={request.symbol}",
            "qfq.response": f"https://finance.sina.com.cn/realstock/company/{request.symbol}/qfq.js"}
    payload = _json_bytes(manifest)
    _write_exclusive(stage / "manifest.json", payload)
    _commit_staged_directory(stage, destination)
    return {"path": str(destination / "manifest.json"), "sha256": _sha(payload),
            "capture_id": capture_id, "row_count": len(normalized)}


def load_history_evidence(reference: Mapping[str, Any]) -> tuple[pd.DataFrame, dict]:
    """Verify immutable bytes and receipt invariants before replay/export."""
    try:
        path = Path(reference["path"]).resolve()
        payload = path.read_bytes()
        if _sha(payload) != reference["sha256"]:
            raise ValueError("history_evidence_checksum_mismatch")
        manifest = json.loads(payload)
        if (manifest["schema_version"] != _SCHEMA or manifest["kind"] != "cn_stock_history_receipt"
                or manifest["capture_id"] != path.parent.name
                or set(manifest["files"]) not in ({"raw_sdk.json", "normalized.parquet"},
                    {"raw_sdk.json", "normalized.parquet", "response.bin"},
                    {"raw_sdk.json", "normalized.parquet", "history.response", "shares.response", "qfq.response"})):
            raise ValueError("history_evidence_invalid_manifest")
        for name, digest in manifest["files"].items():
            if _sha((path.parent / name).read_bytes()) != digest:
                raise ValueError("history_evidence_checksum_mismatch")
        raw = _decode_sdk_frame((path.parent / "raw_sdk.json").read_bytes())
        request = HistoryRequest(manifest["request"]["symbol"],
            date.fromisoformat(manifest["request"]["start_date"]),
            date.fromisoformat(manifest["request"]["end_date"]), manifest["request"]["adjust"])
        request.validate()
        provider = manifest["provider"]
        if provider not in _PROVIDERS or manifest["endpoint"] != _PROVIDERS[provider][0]:
            raise ValueError("history_evidence_invalid_provider")
        if provider == "tencent":
            expected_units = tencent_quantity_input_contract(request.symbol)
            if manifest.get("input_unit_contract") != expected_units:
                raise ValueError("history_evidence_legacy_tencent_quantity_contract_requires_new_revision")
            raw.attrs["tencent_quantity_contract"] = expected_units
        bundle_replay = None
        if provider == "sina_http":
            if (set(manifest["files"]) != {"raw_sdk.json", "normalized.parquet", "history.response", "shares.response", "qfq.response"}
                    or manifest["actual_adjustment"] != request.adjust
                    or manifest["returned_mode"] != ("raw_klc_history" if request.adjust == "" else "qfq_factors_backward_asof")
                    or manifest["adjustment_evidence"] != ("unadjusted_history_wire" if request.adjust == "" else "qfq_factor_wire_requires_crosscheck")
                    or manifest.get("derived_turnover_formula") != _SINA_TURNOVER_FORMULA
                    or manifest.get("derivation_direction") != "backward_asof_only"):
                raise ValueError("history_evidence_sina_adjustment_unverified")
            bundle_replay = _parse_sina_bundle(request,
                (path.parent / "history.response").read_bytes(), (path.parent / "shares.response").read_bytes(),
                (path.parent / "qfq.response").read_bytes())
            if manifest["sina_evidence"] != bundle_replay.attrs.get("sina_evidence", {}):
                raise ValueError("history_evidence_sina_derivation_mismatch")
            _assert_same_frame(raw, bundle_replay, "history_evidence_raw_response_mismatch")
        elif "response.bin" in manifest["files"]:
            response = (path.parent / "response.bin").read_bytes()
            if provider == "tencent":
                received, mode = _parse_tencent_response(request, response)
                if (manifest["returned_mode"] != mode or manifest["actual_adjustment"] != request.adjust
                        or manifest["adjustment_evidence"] != "response_key"):
                    raise ValueError("history_evidence_adjustment_mismatch")
            elif provider == "eastmoney":
                received = _parse_eastmoney_response(request, response)
                if manifest["actual_adjustment"] is not None:
                    raise ValueError("history_evidence_adjustment_unverified")
            else:
                raise ValueError("history_evidence_invalid_provider")
            _assert_same_frame(raw, received, "history_evidence_raw_response_mismatch")
        elif provider == "baostock" and manifest["adjustment_evidence"] == "returned_adjustflag":
            flag = {"": "3", "qfq": "2", "hfq": "1"}[request.adjust]
            if (not raw["adjustflag"].eq(flag).all() or not raw["code"].eq(
                    request.symbol[:2] + "." + request.symbol[-6:]).all()
                    or manifest["returned_mode"] != f"adjustflag:{flag}"
                    or manifest["actual_adjustment"] != request.adjust):
                raise ValueError("history_evidence_baostock_adjustment_unverified")
        elif manifest["actual_adjustment"] is not None:
            raise ValueError("history_evidence_adjustment_unverified")
        replay = _normalize_received(raw, provider, request)
        with duckdb.connect(":memory:") as connection:
            frame = connection.execute("SELECT * FROM read_parquet(?)",
                [str(path.parent / "normalized.parquet")]).fetchdf()
        frame.attrs["history_contract"] = manifest["unit_contract"]
        _validate_normalized(frame)
        if len(frame) != manifest["row_count"] or _dates(frame.trade_date) != manifest["normalized_dates"]:
            raise ValueError("history_evidence_content_mismatch")
        if (_dates(raw[manifest["raw_date_column"]]) != manifest["raw_dates"]
                or manifest["unit_contract"] != replay.attrs["history_contract"]):
            raise ValueError("history_evidence_content_mismatch")
        _assert_same_frame(frame, replay, "history_evidence_normalized_replay_mismatch")
        return frame, manifest
    except (OSError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("history_evidence_unreadable") from exc


def _assert_same_frame(left: pd.DataFrame, right: pd.DataFrame, message: str) -> None:
    try:
        pd.testing.assert_frame_equal(left.reset_index(drop=True), right.reset_index(drop=True),
            check_dtype=False, check_exact=True)
    except AssertionError as exc:
        raise ValueError(message) from exc


def _decode_sdk_frame(payload: bytes) -> pd.DataFrame:
    encoded = json.loads(payload)

    def scalar(cell):
        kind = cell["type"]
        if kind == "null":
            return None
        value = cell["value"]
        if kind == "float":
            return float.fromhex(value)
        if kind == "decimal":
            return Decimal(value)
        if kind == "datetime":
            return pd.Timestamp(value)
        if kind == "date":
            return date.fromisoformat(value)
        if kind not in {"str", "int", "bool"}:
            raise ValueError("history_evidence_invalid_sdk_scalar")
        return value

    if not isinstance(encoded["rows"], list) or any(len(row) != len(encoded["columns"]) for row in encoded["rows"]):
        raise ValueError("history_evidence_invalid_sdk_frame")
    return pd.DataFrame([[scalar(v) for v in row] for row in encoded["rows"]], columns=encoded["columns"])


def write_history_research_snapshot(*, artifact_root: Path, snapshot_id: str,
    receipts: Iterable[Mapping[str, Any]]) -> dict:
    """Export only verified returned modes; no live tables or old artifacts.

    This freezes historical observations acquired now. It does not make them
    point-in-time data and does not establish a new untouched validation set.
    """
    if not isinstance(snapshot_id, str) or not _SAFE_ID.fullmatch(snapshot_id):
        raise ValueError("history_research_invalid_id")
    references = list(receipts)
    if not references:
        raise ValueError("history_research_missing_receipts")
    frames, modes = [], set()
    for reference in references:
        frame, manifest = load_history_evidence(reference)
        if (manifest["adjustment_evidence"] != "response_key"
                or manifest["returned_mode"] != _ADJUSTMENTS.get(manifest["actual_adjustment"])
                or "response.bin" not in manifest["files"]):
            raise ValueError("history_research_adjustment_unverified")
        frame = frame.copy()
        frame["symbol"] = manifest["request"]["symbol"]
        frame["capture_id"] = manifest["capture_id"]
        frames.append(frame)
        modes.add(manifest["actual_adjustment"])
    combined = pd.concat(frames, ignore_index=True)
    if len(modes) != 1 or combined.duplicated(["symbol", "trade_date"]).any():
        raise ValueError("history_research_mixed_or_duplicate_inputs")
    combined = combined.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    root = Path(artifact_root).resolve()
    destination = root / snapshot_id
    if destination.exists():
        raise ValueError("history_research_immutable_conflict")
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}-", dir=root))
    _write_parquet(stage / "history.parquet", combined)
    manifest = {"schema_version": _SCHEMA, "kind": "cn_stock_history_research_snapshot",
        "snapshot_id": snapshot_id, "row_count": len(combined), "actual_adjustment": next(iter(modes)),
        "adjustment_asof_certified": False, "point_in_time_certified": False,
        "data_file": "history.parquet", "sha256": _sha((stage / "history.parquet").read_bytes()),
        "receipts": references, "created_at": datetime.now(timezone.utc).isoformat()}
    payload = _json_bytes(manifest)
    _write_exclusive(stage / "manifest.json", payload)
    _commit_staged_directory(stage, destination)
    return {"path": str(destination / "manifest.json"), "sha256": _sha(payload),
            "row_count": len(combined)}


def load_history_research_snapshot(reference: Mapping[str, Any]) -> tuple[pd.DataFrame, dict]:
    """Verify a research input and every contributing receipt before use.

    The caller can embed the returned reference in existing audit attributes
    or a model's input manifest; no new SQL audit action is fabricated here.
    """
    try:
        path = Path(reference["path"]).resolve()
        payload = path.read_bytes()
        if _sha(payload) != reference["sha256"]:
            raise ValueError("history_research_checksum_mismatch")
        manifest = json.loads(payload)
        if (manifest["schema_version"] != _SCHEMA
                or manifest["kind"] != "cn_stock_history_research_snapshot"
                or manifest["snapshot_id"] != path.parent.name
                or manifest["data_file"] != "history.parquet"
                or manifest["actual_adjustment"] not in _ADJUSTMENTS
                or manifest["point_in_time_certified"] is not False
                or manifest["adjustment_asof_certified"] is not False):
            raise ValueError("history_research_invalid_manifest")
        data_path = path.parent / "history.parquet"
        if _sha(data_path.read_bytes()) != manifest["sha256"]:
            raise ValueError("history_research_checksum_mismatch")
        expected = []
        for receipt in manifest["receipts"]:
            frame, source = load_history_evidence(receipt)
            if (source["actual_adjustment"] != manifest["actual_adjustment"]
                    or source["adjustment_evidence"] != "response_key"
                    or source["returned_mode"] != _ADJUSTMENTS[manifest["actual_adjustment"]]
                    or "response.bin" not in source["files"]):
                raise ValueError("history_research_adjustment_unverified")
            frame = frame.copy()
            frame["symbol"] = source["request"]["symbol"]
            frame["capture_id"] = source["capture_id"]
            expected.append(frame)
        if not expected:
            raise ValueError("history_research_missing_receipts")
        combined = pd.concat(expected, ignore_index=True).sort_values(["trade_date", "symbol"]).reset_index(drop=True)
        if combined.duplicated(["symbol", "trade_date"]).any():
            raise ValueError("history_research_mixed_or_duplicate_inputs")
        with duckdb.connect(":memory:") as connection:
            actual = connection.execute("SELECT * FROM read_parquet(?)", [str(data_path)]).fetchdf()
        if len(actual) != manifest["row_count"]:
            raise ValueError("history_research_content_mismatch")
        try:
            pd.testing.assert_frame_equal(actual, combined, check_dtype=False, check_exact=True)
        except AssertionError as exc:
            raise ValueError("history_research_content_mismatch") from exc
        return actual, manifest
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("history_research_unreadable") from exc


TRUSTED_HISTORY_DATASET_ROOT = Path(__file__).resolve().parents[2] / "tmp" / "trusted_history_datasets"


@dataclass(frozen=True)
class TrustedHistoryDataset:
    dataset_id: str
    directory: Path
    manifest_sha256: str
    bars: pd.DataFrame
    symbols: tuple[str, ...]
    calendar: tuple[date, ...]
    identity: dict


def write_trusted_history_dataset(*, dataset_id: str, receipts: Iterable[Mapping[str, Any]],
    cohort_symbols: Iterable[str], calendar: Iterable[date], plan_sha256: str,
    etf_cash_dividends: Mapping[str, list[dict]] | None = None) -> dict:
    """Join TX qfq prices and EM/Sina liquidity without modifying live data.

    Missing symbols and rejected date pairs stay in the quality report. They
    are never substituted using factor performance. The complete frozen cohort
    remains the training coverage denominator.
    """
    if not isinstance(dataset_id, str) or not _SAFE_ID.fullmatch(dataset_id):
        raise ValueError("trusted_history_invalid_dataset_id")
    if not isinstance(plan_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
        raise ValueError("trusted_history_missing_plan_fingerprint")
    symbols = tuple(cohort_symbols)
    days = tuple(calendar)
    if (not symbols or symbols != tuple(sorted(set(symbols)))
            or any(not isinstance(s, str) or not re.fullmatch(r"\d{6}", s) for s in symbols)):
        raise ValueError("trusted_history_invalid_cohort")
    if not days or any(type(day) is not date for day in days) or days != tuple(sorted(set(days))):
        raise ValueError("trusted_history_invalid_calendar")
    root = TRUSTED_HISTORY_DATASET_ROOT.resolve()
    directory = _inside(root / dataset_id, root)
    if (directory / "manifest.json").exists() or (directory / "bars.parquet").exists():
        raise ValueError("trusted_history_immutable_conflict")
    by_symbol: dict[str, dict[str, tuple[pd.DataFrame, dict, dict]]] = {}
    references = []
    for reference in receipts:
        receipt_path = _inside(Path(reference["path"]), directory)
        frame, manifest = load_history_evidence({**reference, "path": str(receipt_path)})
        symbol, provider = manifest["request"]["symbol"][-6:], manifest["provider"]
        receipt_adjust = manifest["request"]["adjust"]
        if symbol not in symbols or not (receipt_adjust == "qfq" or receipt_adjust == "" and provider in {"tencent", "sina_http", "baostock"}):
            raise ValueError("trusted_history_invalid_receipt_scope")
        if provider not in {"eastmoney", "sina_http", "tencent", "baostock"}:
            raise ValueError("trusted_history_unsupported_provider")
        provider_key = provider + "_raw" if provider in {"tencent", "baostock"} and receipt_adjust == "" else provider
        if provider_key in by_symbol.setdefault(symbol, {}):
            raise ValueError("trusted_history_duplicate_provider_receipt")
        local = {**reference, "path": receipt_path.relative_to(directory).as_posix()}
        by_symbol[symbol][provider_key] = frame.set_index("trade_date"), manifest, local
        references.append(local)
    rows, lineage, quality, etf_adjustments = [], [], {}, {}
    provider_families = {"baostock" if provider == "baostock_raw" else provider
        for providers in by_symbol.values() for provider in providers}
    if "baostock" in provider_families and provider_families != {"baostock"}:
        raise ValueError("trusted_history_mixed_price_provider_family")
    canonical_price_source = "baostock_adjustflag2" if provider_families == {"baostock"} else "tencent_qfqday"
    date_index = pd.DatetimeIndex(days)
    for symbol in symbols:
        providers = by_symbol.get(symbol, {})
        if cn_history_instrument(symbol)['asset_type'] == 'etf' and 'baostock_raw' not in providers:
            raise ValueError('trusted_history_etf_raw_prices_required')
        if "baostock" in providers:
            frame, receipt, ref = providers["baostock"]
            if (receipt["actual_adjustment"] != "qfq" or receipt["returned_mode"] != "adjustflag:2"
                    or receipt["adjustment_evidence"] != "returned_adjustflag"):
                raise ValueError("trusted_history_adjustment_unverified")
            if cn_history_instrument(symbol)['asset_type'] == 'etf':
                from app.services.etf_history import verify_etf_price_adjustment
                raw_frame, _raw_receipt, raw_ref = providers['baostock_raw']
                events = list((etf_cash_dividends or {}).get(symbol, []))
                review = verify_etf_price_adjustment(raw_frame.reset_index(), frame.reset_index(),
                    events, directory=directory)
                etf_adjustments[symbol] = {'raw_receipt_sha256': raw_ref['sha256'],
                    'qfq_receipt_sha256': ref['sha256'], 'cash_dividends': events, 'review': review}
            used = frame.index.intersection(date_index).sort_values()
            values = frame.loc[used].rename_axis("trade_date").reset_index().assign(symbol=symbol)
            rows.append(values)
            lineage.append({"symbol": symbol, "receipt_sha256": ref["sha256"],
                "fields": _HISTORY_COLUMNS[1:], "dates": [day.date().isoformat() for day in used]})
            quality[symbol] = {"accepted_rows": len(used), "missing_calendar_dates":
                [day.date().isoformat() for day in date_index.difference(used)]}
            continue
        if "tencent" not in providers or not ({"eastmoney", "sina_http"} & set(providers)):
            quality[symbol] = {"accepted_rows": 0, "reason": "missing_provider_receipt",
                "missing_providers": (["tencent"] if "tencent" not in providers else [])
                    + (["eastmoney_or_sina_http"] if not ({"eastmoney", "sina_http"} & set(providers)) else [])}
            continue
        tx, tx_receipt, tx_ref = providers["tencent"]
        if (tx_receipt["actual_adjustment"] != "qfq"
                or tx_receipt["returned_mode"] != "qfqday" or tx_receipt["adjustment_evidence"] != "response_key"):
            raise ValueError("trusted_history_adjustment_unverified")
        prices = ["open", "high", "low", "close"]
        quantities = ["volume", "amount", "turnover_rate"]
        selected_dates = pd.DatetimeIndex([])
        source_quality = {}
        for candidate in ("eastmoney", "sina_http"):
            if candidate not in providers:
                source_quality[candidate] = {"reason": "missing_provider_receipt", "accepted_rows": 0}
                continue
            source, receipt, ref = providers[candidate]
            if ((candidate == "eastmoney" and "response.bin" not in receipt["files"])
                    or (candidate == "sina_http" and (receipt["adjustment_evidence"] != "unadjusted_history_wire"
                        or receipt["actual_adjustment"] != ""))):
                raise ValueError("trusted_history_adjustment_unverified")
            common = source.index.intersection(tx.index).intersection(date_index).sort_values()
            tx_volume_rejected = pd.DatetimeIndex([])
            if "tencent_raw" in providers:
                observed_raw, observed_receipt, _observed_ref = providers["tencent_raw"]
                if observed_receipt["actual_adjustment"] != "" or observed_receipt["returned_mode"] != "day":
                    raise ValueError("trusted_history_raw_adjustment_unverified")
                overlap = common.intersection(observed_raw.index)
                different = overlap[~np.isclose(observed_raw.loc[overlap, "volume"].to_numpy(float),
                    tx.loc[overlap, "volume"].to_numpy(float), rtol=0, atol=0)]
                tx_volume_rejected = different
                common = common.difference(different)
            raw_ref = None
            if candidate == "sina_http":
                if "tencent_raw" not in providers:
                    source_quality[candidate] = {"reason": "missing_unadjusted_tencent_receipt", "accepted_rows": 0}
                    continue
                raw, raw_receipt, raw_ref = providers["tencent_raw"]
                if raw_receipt["actual_adjustment"] != "" or raw_receipt["returned_mode"] != "day":
                    raise ValueError("trusted_history_raw_adjustment_unverified")
                common = common.intersection(raw.index)
                raw_checks = _raw_liquidity_checks(source, raw, tx, common)
                equal = raw_checks["price_match"] & raw_checks["source_volume_match"] & raw_checks["tx_volume_match"]
            else:
                raw_checks = None
                equal = np.isclose(source.loc[common, prices].to_numpy(float), tx.loc[common, prices].to_numpy(float),
                    rtol=0, atol=_CROSS_PROVIDER_PRICE_ATOL + 1e-12).all(axis=1)
            finite_quantity = np.isfinite(source.loc[common, quantities].to_numpy(float)).all(axis=1)
            eligible = common[equal & finite_quantity]
            used = eligible.difference(selected_dates).sort_values()
            if len(used):
                values = tx.loc[used, prices].join(source.loc[used, quantities]).rename_axis("trade_date").reset_index().assign(symbol=symbol)
                rows.append(values)
                lineage.extend([
                    {"symbol": symbol, "receipt_sha256": tx_ref["sha256"], "fields": prices,
                        "dates": [day.date().isoformat() for day in used]},
                    {"symbol": symbol, "receipt_sha256": ref["sha256"], "fields": quantities,
                        "verified_against_receipt_sha256": tx_ref["sha256"],
                        **({"verified_against_raw_receipt_sha256": raw_ref["sha256"]} if raw_ref is not None else {}),
                        "dates": [day.date().isoformat() for day in used]},
                ])
                selected_dates = selected_dates.union(used)
            source_quality[candidate] = {"accepted_rows": len(used),
                "provider_price_disagreement_dates": [day.date().isoformat() for day in
                    common[~(raw_checks["price_match"] if raw_checks is not None else equal)]],
                "missing_quantity_dates": [day.date().isoformat() for day in common[~finite_quantity]],
                "tx_raw_qfq_volume_disagreement_dates": [day.date().isoformat() for day in tx_volume_rejected],
                "missing_calendar_dates": [day.date().isoformat() for day in date_index.difference(common)]}
            if raw_checks is not None:
                source_quality[candidate]["raw_volume_disagreement_dates"] = [day.date().isoformat() for day in common[~raw_checks["source_volume_match"]]]
                source_quality[candidate]["tx_raw_qfq_volume_disagreement_dates"] = [day.date().isoformat() for day in
                    tx_volume_rejected.union(common[~raw_checks["tx_volume_match"]])]
        quality[symbol] = {"accepted_rows": len(selected_dates), "quantity_provider_priority": ["eastmoney", "sina_http"],
            "quantity_sources": source_quality, "provider_price_disagreement_dates":
                source_quality["eastmoney"].get("provider_price_disagreement_dates", []),
            "missing_calendar_dates": [day.date().isoformat() for day in date_index.difference(selected_dates)]}
    directory.mkdir(parents=True, exist_ok=True)
    quality_payload = {"schema_version": 1, "dataset_id": dataset_id,
        "plan_sha256": plan_sha256, "cohort_symbols": list(symbols), "cohort_size": len(symbols),
        "cross_provider_price_atol_CNY": _CROSS_PROVIDER_PRICE_ATOL, "per_symbol": quality,
        "cross_provider_volume_atol_shares": _CROSS_PROVIDER_VOLUME_ATOL_SHARES,
        "quantity_provider_priority": ["eastmoney", "sina_http"],
        "raw_trade_date_relocation_allowed": False}
    # A failed build still leaves an explicit immutable quality report; it is
    # never a loadable training input without the committed manifest.
    quality_path = directory / "quality.json"
    _write_exclusive(quality_path, _json_bytes(quality_payload))
    if not rows:
        raise ValueError("trusted_history_no_verified_samples")
    bars = pd.concat(rows, ignore_index=True)[["symbol", *_HISTORY_COLUMNS]]
    bars = bars.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    if set(bars.trade_date.dt.date) != set(days):
        raise ValueError("trusted_history_whole_calendar_day_missing")
    _write_parquet(directory / "bars.parquet", bars)
    manifest = {"id": dataset_id, "schema_version": 1, "research_only": True,
        "cohort_symbols": list(symbols), "bars_path": "bars.parquet",
        "bars_sha256": _sha((directory / "bars.parquet").read_bytes()), "adjust": "qfq",
        "units": {"volume": "share", "amount": "CNY", "turnover_rate": "percentage_point"},
        "date_range": {"start": days[0].isoformat(), "end": days[-1].isoformat()},
        "calendar": [day.isoformat() for day in days], "retrospective": True,
        "research_evaluation_status": "previously_observed_development_comparison",
        "plan_sha256": plan_sha256, "quality_path": "quality.json", "quality_sha256": _sha(quality_path.read_bytes()),
        "evidence": {"receipts": references, "field_lineage": lineage,
            "quantity_provider_priority": ["eastmoney", "sina_http"],
            "cross_provider_price_atol_CNY": _CROSS_PROVIDER_PRICE_ATOL,
            "cross_provider_volume_atol_shares": _CROSS_PROVIDER_VOLUME_ATOL_SHARES,
            "sina_price_verification_mode": "unadjusted_ohlc_only", "canonical_price_source": canonical_price_source},
        "created_at": datetime.now(timezone.utc).isoformat()}
    if etf_adjustments:
        manifest['evidence']['etf_adjustments'] = etf_adjustments
    payload = _json_bytes(manifest)
    _write_exclusive(directory / "manifest.json", payload)
    verified = load_trusted_history_dataset(dataset_id)
    return {"path": str(directory / "manifest.json"), "sha256": verified.manifest_sha256,
        "row_count": len(bars), "cohort_size": len(symbols), "identity": verified.identity}


def _inside(path: Path, directory: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(directory.resolve()):
        raise ValueError("trusted_history_path_outside_dataset")
    return resolved


def _commit_staged_directory(stage: Path, destination: Path) -> None:
    """Retry transient Windows access denial; preserve evidence on exhaustion."""
    for attempt in range(3):
        if destination.exists():
            raise ValueError("history_evidence_immutable_conflict")
        try:
            stage.rename(destination)
            return
        except PermissionError:
            if destination.exists():
                raise ValueError("history_evidence_immutable_conflict")
            if attempt == 2:
                raise
            time.sleep(.05 * (attempt + 1))


def _raw_liquidity_checks(source: pd.DataFrame, raw: pd.DataFrame, qfq: pd.DataFrame,
    dates: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    """Keep raw price/quantity verification separate from canonical qfq prices."""
    prices = ["open", "high", "low", "close"]
    return {"price_match": np.isclose(source.loc[dates, prices].to_numpy(float), raw.loc[dates, prices].to_numpy(float),
                rtol=0, atol=_CROSS_PROVIDER_PRICE_ATOL + 1e-12).all(axis=1),
            "source_volume_match": np.isclose(source.loc[dates, "volume"].to_numpy(float), raw.loc[dates, "volume"].to_numpy(float),
                rtol=0, atol=_CROSS_PROVIDER_VOLUME_ATOL_SHARES + 1e-9),
            "tx_volume_match": np.isclose(raw.loc[dates, "volume"].to_numpy(float), qfq.loc[dates, "volume"].to_numpy(float),
                rtol=0, atol=0)}


def load_trusted_history_dataset(dataset_id: str) -> TrustedHistoryDataset:
    """Load only a server-owned, research-only dataset with field-level replay.

    A matching parquet hash alone is insufficient. Each bar field must equal
    its normalized provider receipt at the same original symbol/date. BaoStock
    must return adjustflag=2 per bar. EM requested qfq requires independent
    Tencent qfqday OHLC agreement for every used day (two-cent price tolerance,
    fixed before inspecting this research cohort's factor/model performance).
    """
    if not isinstance(dataset_id, str) or not _SAFE_ID.fullmatch(dataset_id):
        raise ValueError("trusted_history_invalid_dataset_id")
    root = TRUSTED_HISTORY_DATASET_ROOT.resolve()
    directory = _inside(root / dataset_id, root)
    try:
        payload = (directory / "manifest.json").read_bytes()
        manifest = json.loads(payload)
        if manifest.get('schema_version') == 2:
            from app.services.market_history_composition import load_composed_history
            return load_composed_history(dataset_id, directory, payload)
        if (manifest.get("id") != dataset_id or manifest.get("schema_version") != 1
                or manifest.get("research_only") is not True or manifest.get("retrospective") is not True
                or manifest.get("adjust") != "qfq" or manifest.get("bars_path") != "bars.parquet"
                or manifest.get("units") != {"volume": "share", "amount": "CNY", "turnover_rate": "percentage_point"}):
            raise ValueError("trusted_history_invalid_manifest")
        if (manifest.get("quality_path") != "quality.json"
                or not re.fullmatch(r"[0-9a-f]{64}", manifest.get("plan_sha256", ""))
                or manifest.get("evidence", {}).get("cross_provider_price_atol_CNY") != _CROSS_PROVIDER_PRICE_ATOL
                or manifest.get("evidence", {}).get("cross_provider_volume_atol_shares") != _CROSS_PROVIDER_VOLUME_ATOL_SHARES
                or manifest.get("evidence", {}).get("sina_price_verification_mode") != "unadjusted_ohlc_only"
                or manifest.get("evidence", {}).get("canonical_price_source") not in {"tencent_qfqday", "baostock_adjustflag2"}
                or manifest.get("evidence", {}).get("quantity_provider_priority") != ["eastmoney", "sina_http"]):
            raise ValueError("trusted_history_invalid_quality_contract")
        quality_path = _inside(directory / "quality.json", directory)
        if _sha(quality_path.read_bytes()) != manifest["quality_sha256"]:
            raise ValueError("trusted_history_quality_checksum_mismatch")
        quality = json.loads(quality_path.read_bytes())
        symbols = tuple(manifest["cohort_symbols"])
        if (not symbols or symbols != tuple(sorted(set(symbols)))
                or any(not isinstance(s, str) or not re.fullmatch(r"\d{6}", s) for s in symbols)):
            raise ValueError("trusted_history_invalid_cohort")
        if (quality.get("cohort_symbols") != list(symbols) or set(quality.get("per_symbol", {})) != set(symbols)
                or quality.get("plan_sha256") != manifest["plan_sha256"]
                or quality.get("cross_provider_price_atol_CNY") != _CROSS_PROVIDER_PRICE_ATOL
                or quality.get("cross_provider_volume_atol_shares") != _CROSS_PROVIDER_VOLUME_ATOL_SHARES
                or quality.get("quantity_provider_priority") != ["eastmoney", "sina_http"]):
            raise ValueError("trusted_history_invalid_quality_contract")
        calendar = tuple(date.fromisoformat(day) for day in manifest["calendar"])
        if (not calendar or calendar != tuple(sorted(set(calendar)))
                or calendar[0].isoformat() != manifest["date_range"]["start"]
                or calendar[-1].isoformat() != manifest["date_range"]["end"]):
            raise ValueError("trusted_history_invalid_calendar")
        bars_path = _inside(directory / "bars.parquet", directory)
        if _sha(bars_path.read_bytes()) != manifest["bars_sha256"]:
            raise ValueError("trusted_history_bars_checksum_mismatch")
        with duckdb.connect(":memory:") as connection:
            bars = connection.execute("SELECT * FROM read_parquet(?)", [str(bars_path)]).fetchdf()
        if set(bars.columns) != {"symbol", *_HISTORY_COLUMNS} or bars.empty:
            raise ValueError("trusted_history_invalid_bars")
        if not bars.symbol.map(lambda s: isinstance(s, str) and s in symbols).all():
            raise ValueError("trusted_history_invalid_bar_symbol")
        bars["trade_date"] = pd.to_datetime(bars.trade_date)
        if (bars.duplicated(["symbol", "trade_date"]).any()
                or not set(bars.symbol).issubset(set(symbols))
                or set(bars.trade_date.dt.date) != set(calendar)):
            raise ValueError("trusted_history_invalid_bar_keys")
        if any(pd.Timestamp(day) != pd.Timestamp(day).normalize() or pd.Timestamp(day).tzinfo is not None
               for day in bars.trade_date):
            raise ValueError("trusted_history_invalid_bar_date")
        for symbol in sorted(set(bars.symbol)):
            frame = bars.loc[bars.symbol.eq(symbol), _HISTORY_COLUMNS].copy()
            frame.attrs["history_contract"] = {"provider": "eastmoney", "endpoint": "stock_zh_a_hist",
                "version": "cn-stock-shares-v1", "volume_unit": "share", "amount_unit": "CNY",
                "turnover_unit": "percentage_point"}
            _validate_normalized(frame)
        for symbol in symbols:
            if quality["per_symbol"][symbol].get("accepted_rows") != int(bars.symbol.eq(symbol).sum()):
                raise ValueError("trusted_history_quality_row_count_mismatch")
        evidence = manifest["evidence"]
        sources = {}
        for reference in evidence["receipts"]:
            raw_path = Path(reference["path"])
            raw_path = raw_path if raw_path.is_absolute() else directory / raw_path
            local = {**reference, "path": str(_inside(raw_path, directory))}
            frame, receipt = load_history_evidence(local)
            if reference["sha256"] in sources:
                raise ValueError("trusted_history_duplicate_receipt")
            sources[reference["sha256"]] = (frame.set_index("trade_date"), receipt)
        covered = set()
        field_set = set(_HISTORY_COLUMNS) - {"trade_date"}
        for lineage in evidence["field_lineage"]:
            symbol, fields = lineage["symbol"], lineage["fields"]
            source, receipt = sources[lineage["receipt_sha256"]]
            if (symbol not in symbols or receipt["request"]["symbol"][-6:] != symbol
                    or not isinstance(fields, list) or not fields or not set(fields).issubset(field_set)
                    or len(fields) != len(set(fields))):
                raise ValueError("trusted_history_invalid_lineage")
            selected = bars.loc[bars.symbol.eq(symbol)].set_index("trade_date")
            dates = selected.index.intersection(source.index)
            if "dates" in lineage:
                declared = pd.to_datetime(lineage["dates"])
                if len(set(declared)) != len(declared) or not set(declared).issubset(set(dates)):
                    raise ValueError("trusted_history_invalid_lineage_dates")
                dates = declared
            if len(dates) == 0:
                raise ValueError("trusted_history_empty_lineage")
            if (receipt["provider"] == "baostock" and receipt["actual_adjustment"] == "qfq"
                    and receipt["adjustment_evidence"] == "returned_adjustflag"
                    and receipt["returned_mode"] == "adjustflag:2"
                    and evidence["canonical_price_source"] == "baostock_adjustflag2"):
                pass
            elif ((receipt["provider"] == "eastmoney" and receipt["request"]["adjust"] == "qfq" and "response.bin" in receipt["files"])
                    or (receipt["provider"] == "sina_http" and receipt["actual_adjustment"] == ""
                        and receipt["adjustment_evidence"] == "unadjusted_history_wire")):
                if not set(fields).issubset({"volume", "amount", "turnover_rate"}) or evidence["canonical_price_source"] != "tencent_qfqday":
                    raise ValueError("trusted_history_frozen_field_source_violation")
                peer, peer_receipt = sources[lineage["verified_against_receipt_sha256"]]
                if (peer_receipt["provider"] != "tencent" or peer_receipt["actual_adjustment"] != "qfq"
                        or peer_receipt["returned_mode"] != "qfqday" or peer_receipt["adjustment_evidence"] != "response_key"
                        or peer_receipt["request"]["symbol"][-6:] != symbol
                        or not set(dates).issubset(set(peer.index))):
                    raise ValueError("trusted_history_adjustment_unverified")
                if receipt["provider"] == "sina_http":
                    raw, raw_receipt = sources[lineage["verified_against_raw_receipt_sha256"]]
                    if (raw_receipt["provider"] != "tencent" or raw_receipt["actual_adjustment"] != ""
                            or raw_receipt["returned_mode"] != "day" or raw_receipt["adjustment_evidence"] != "response_key"
                            or raw_receipt["request"]["symbol"][-6:] != symbol or not set(dates).issubset(raw.index)):
                        raise ValueError("trusted_history_raw_adjustment_unverified")
                    checks = _raw_liquidity_checks(source, raw, peer, pd.DatetimeIndex(dates))
                    if not checks["price_match"].all():
                        raise ValueError("trusted_history_raw_provider_price_disagreement")
                    if not checks["source_volume_match"].all() or not checks["tx_volume_match"].all():
                        raise ValueError("trusted_history_raw_provider_volume_disagreement")
                    for primary, primary_receipt in sources.values():
                        if primary_receipt["provider"] != "eastmoney" or primary_receipt["request"]["symbol"][-6:] != symbol:
                            continue
                        overlap = pd.DatetimeIndex(dates).intersection(primary.index)
                        if len(overlap):
                            good_price = np.isclose(primary.loc[overlap, ["open", "high", "low", "close"]].to_numpy(float),
                                peer.loc[overlap, ["open", "high", "low", "close"]].to_numpy(float),
                                rtol=0, atol=_CROSS_PROVIDER_PRICE_ATOL + 1e-12).all(axis=1)
                            known_quantity = np.isfinite(primary.loc[overlap, ["volume", "amount", "turnover_rate"]].to_numpy(float)).all(axis=1)
                            if (good_price & known_quantity).any():
                                raise ValueError("trusted_history_quantity_provider_priority_violation")
                elif not np.allclose(source.loc[dates, ["open", "high", "low", "close"]].to_numpy(float),
                    peer.loc[dates, ["open", "high", "low", "close"]].to_numpy(float), rtol=0,
                    atol=_CROSS_PROVIDER_PRICE_ATOL + 1e-12):
                    raise ValueError("trusted_history_provider_price_disagreement")
            elif (receipt["provider"] == "tencent" and receipt["actual_adjustment"] == "qfq"
                    and receipt["returned_mode"] == "qfqday" and receipt["adjustment_evidence"] == "response_key"
                    and evidence["canonical_price_source"] == "tencent_qfqday"
                    and set(fields).issubset({"open", "high", "low", "close"})):
                for other, other_receipt in sources.values():
                    if (other_receipt["provider"] != "tencent" or other_receipt["actual_adjustment"] != ""
                            or other_receipt["returned_mode"] != "day"
                            or other_receipt["request"]["symbol"][-6:] != symbol):
                        continue
                    overlap = pd.DatetimeIndex(dates).intersection(other.index)
                    if not np.allclose(source.loc[overlap, "volume"].to_numpy(float),
                            other.loc[overlap, "volume"].to_numpy(float), rtol=0, atol=0):
                        raise ValueError("trusted_history_raw_qfq_volume_disagreement")
            else:
                raise ValueError("trusted_history_adjustment_unverified")
            left = selected.loc[dates, fields].to_numpy(dtype=float)
            right = source.loc[dates, fields].to_numpy(dtype=float)
            if not np.allclose(left, right, rtol=0, atol=0, equal_nan=True):
                raise ValueError("trusted_history_field_value_mismatch")
            for day in dates:
                for field in fields:
                    key = symbol, pd.Timestamp(day).date(), field
                    if key in covered:
                        raise ValueError("trusted_history_duplicate_field_lineage")
                    covered.add(key)
        needed = {(row.symbol, row.trade_date.date(), field) for row in bars.itertuples() for field in field_set}
        if covered != needed:
            raise ValueError("trusted_history_incomplete_field_lineage")
        digest = _sha(payload)
        identity = {"dataset_id": dataset_id, "manifest_sha256": digest, "bars_sha256": manifest["bars_sha256"],
            "cohort_symbols": list(symbols), "research_only": True,
            "research_evaluation_status": "previously_observed_development_comparison",
            "retrospective": True, "point_in_time_certified": False,
            "adjustment_asof_certified": False, "adjust": "qfq", "units": manifest["units"],
            "date_range": manifest["date_range"], "receipt_count": len(sources)}
        instruments = {symbol: cn_history_instrument(symbol) for symbol in symbols}
        asset_types = {item['asset_type'] for item in instruments.values()}
        if 'etf' in asset_types:
            raw_symbols = {receipt['request']['symbol'][-6:] for _frame, receipt in sources.values()
                if receipt['provider'] == 'baostock' and receipt['actual_adjustment'] == ''
                and receipt['returned_mode'] == 'adjustflag:3'}
            if any(item['asset_type'] == 'etf' and symbol not in raw_symbols
                   for symbol, item in instruments.items()):
                raise ValueError('trusted_history_etf_raw_prices_required')
            for _frame, receipt in sources.values():
                requested = receipt['request']['symbol']
                instrument = instruments[requested[-6:]]
                if requested != instrument['market'] + requested[-6:]:
                    raise ValueError('trusted_history_instrument_exchange_conflict')
            from app.services.etf_history import verify_etf_price_adjustment
            adjustments = evidence.get('etf_adjustments', {})
            etf_symbols = {symbol for symbol, item in instruments.items() if item['asset_type'] == 'etf'}
            if set(adjustments) != etf_symbols:
                raise ValueError('trusted_history_etf_dividend_review_required')
            reviews = {}
            for symbol in sorted(etf_symbols):
                item = adjustments[symbol]
                raw_frame, raw_receipt = sources[item['raw_receipt_sha256']]
                qfq_frame, qfq_receipt = sources[item['qfq_receipt_sha256']]
                if (raw_receipt['provider'] != 'baostock' or raw_receipt['actual_adjustment'] != ''
                        or qfq_receipt['provider'] != 'baostock' or qfq_receipt['actual_adjustment'] != 'qfq'
                        or any(receipt['request']['symbol'] != instruments[symbol]['market'] + symbol
                               for receipt in (raw_receipt, qfq_receipt))):
                    raise ValueError('trusted_history_etf_price_receipt_binding_invalid')
                review = verify_etf_price_adjustment(raw_frame.reset_index(), qfq_frame.reset_index(),
                    item['cash_dividends'], directory=directory)
                if review != item['review']:
                    raise ValueError('trusted_history_etf_dividend_review_changed')
                reviews[symbol] = review
            # Preserve the identity of existing stock-only research inputs.
            identity.update(asset_type=next(iter(asset_types)) if len(asset_types) == 1 else 'mixed',
                            instruments=instruments, etf_price_review=reviews)
        return TrustedHistoryDataset(dataset_id, directory, digest,
            bars.sort_values(["trade_date", "symbol"]).reset_index(drop=True), symbols, calendar, identity)
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("trusted_history_unreadable") from exc
