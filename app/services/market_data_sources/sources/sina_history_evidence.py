"""Parse frozen Sina HTTP responses without executing any remote JavaScript.

The compressed K-line string is decoded only by the installed AKShare decoder.
Share capital and qfq factors are backward-as-of joins onto real K-line dates;
neither their calendars nor suspended-day repeats may create or remove bars.
"""
from __future__ import annotations

from datetime import date
from copy import deepcopy
from functools import lru_cache
import hashlib
import importlib.metadata
import json
import math
import re

import pandas as pd

from app.services.market_data_sources.sources._normalize import attach_cn_stock_history_contract


_COLUMNS = ["trade_date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _reject_json_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _unique_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _bound_json(payload: bytes, binding: str, *, parenthesized: bool = False):
    if not isinstance(payload, bytes) or len(payload) > _MAX_RESPONSE_BYTES:
        raise ValueError("Invalid Sina response bytes")
    text = payload.decode("utf-8-sig").strip()
    # Only outer comments are permitted; embedded JS statements remain part
    # of the JSON input and are rejected by json.loads.
    while text.startswith("/*"):
        end = text.find("*/", 2)
        if end < 0:
            raise ValueError("Unterminated Sina response comment")
        text = text[end + 2:].strip()
    while text.endswith("*/"):
        start = text.rfind("/*")
        if start < 0:
            raise ValueError("Invalid Sina response comment")
        text = text[:start].strip()
    match = re.fullmatch(r"var\s+" + re.escape(binding) + r"\s*=\s*(.*?)\s*;?", text, re.S)
    if match is None:
        raise ValueError(f"Sina response binding does not match {binding}")
    body = match.group(1).strip()
    if parenthesized and body.startswith("(") and body.endswith(")"):
        body = body[1:-1].strip()
    return json.loads(body, parse_constant=_reject_json_constant, object_pairs_hook=_unique_json_keys)


def _decode_compressed_history(encoded: str) -> list[dict]:
    from akshare.stock.stock_zh_a_sina import hk_js_decode
    from py_mini_racer import MiniRacer

    # Never evaluate the response. `call` serializes the compressed string as
    # a value passed to the locally installed decoder's function d.
    with MiniRacer() as decoder:
        decoder.eval(hk_js_decode, timeout_sec=3, max_memory=128 * 1024 * 1024)
        return decoder.call("d", encoded, timeout_sec=5, max_memory=128 * 1024 * 1024)


@lru_cache(maxsize=1)
def _decoder_identity() -> dict:
    from akshare.stock.stock_zh_a_sina import hk_js_decode

    # These are public installed-package identities; no environment/config
    # values are inspected. Persist the exact local decoder code identity.
    runtime_versions = {}
    for distribution in ("mini-racer", "py-mini-racer"):
        try:
            runtime_versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
    return {
        "name": "akshare.stock.stock_zh_a_sina.hk_js_decode:d",
        "code_sha256": hashlib.sha256(hk_js_decode.encode("utf-8")).hexdigest(),
        "akshare_version": importlib.metadata.version("akshare"),
        "javascript_runtime": runtime_versions,
    }


def _day(value, *, history=False) -> pd.Timestamp:
    pattern = r"\d{4}-\d{2}-\d{2}T00:00:00\.000Z" if history else r"\d{4}-\d{2}-\d{2}"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"Invalid Sina date: {value!r}")
    try:
        return pd.Timestamp(date.fromisoformat(value[:10]))
    except ValueError as exc:
        raise ValueError(f"Invalid Sina date: {value!r}") from exc


def _number(value, field: str, *, positive=False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Invalid Sina {field}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid Sina {field}") from exc
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise ValueError(f"Invalid Sina {field}")
    return number


def _effective_rows(rows, date_key: str, value_key: str, field: str, multiplier=1.0, *, zero_unknown=False):
    if not isinstance(rows, list):
        raise ValueError(f"Invalid Sina {field} rows")
    records, dates = [], set()
    for row in rows:
        if not isinstance(row, dict) or date_key not in row or value_key not in row:
            raise ValueError(f"Invalid Sina {field} record")
        day = _day(row[date_key])
        if day in dates:
            raise ValueError(f"duplicate Sina {field} date")
        dates.add(day)
        raw_value = _number(row[value_key], field, positive=not zero_unknown)
        value = raw_value * multiplier
        if not math.isfinite(value):
            raise ValueError(f"Invalid Sina {field}")
        # A zero share-capital observation is an unknown effective interval,
        # not an event to delete. Keep the boundary so an older positive
        # observation cannot leak through it in the backward-as-of join.
        if zero_unknown and raw_value == 0:
            value = float("nan")
        records.append({"trade_date": day, field: value})
    return pd.DataFrame(records, columns=["trade_date", field]).astype(
        {"trade_date": "datetime64[ns]", field: "float64"}).sort_values("trade_date")


def parse_sina_history_bundle(
    symbol: str,
    history_bytes: bytes,
    shares_bytes: bytes,
    qfq_bytes: bytes,
    start_date: date,
    end_date: date,
    *,
    adjust: str = "qfq",
) -> pd.DataFrame:
    """Return requested OHLC, native share volume, CNY amount and percent.

    Missing earlier or explicitly zero share observations leave turnover unknown. Missing
    effective qfq factors reject the requested frame. These are current HTTP
    observations, not evidence that the data was available historically.
    """
    if not isinstance(symbol, str) or re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol) is None:
        raise ValueError("Sina symbol must include its exchange prefix")
    if adjust not in {"", "qfq"}:
        raise ValueError("Unsupported Sina adjustment")
    start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
    if pd.isna(start) or pd.isna(end) or start > end or start.tz is not None or end.tz is not None:
        raise ValueError("Invalid Sina requested date range")
    encoded = _bound_json(history_bytes, f"KLC_K2_{symbol}")
    if not isinstance(encoded, str) or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", encoded) is None:
        raise ValueError("Invalid Sina compressed history string")
    shares = _bound_json(shares_bytes, f"KKE_ShareAmount_{symbol}", parenthesized=True)
    qfq = _bound_json(qfq_bytes, f"{symbol}qfq")
    if (not isinstance(qfq, dict) or not isinstance(qfq.get("data"), list)
            or type(qfq.get("total")) is not int or qfq["total"] != len(qfq["data"])):
        raise ValueError("Invalid Sina qfq factor payload")
    share_frame = _effective_rows(shares, "date", "amount", "outstanding_share", 10000.0, zero_unknown=True)
    factor_frame = _effective_rows(qfq["data"], "d", "f", "qfq_factor")
    decoded = _decode_compressed_history(encoded)
    if not isinstance(decoded, list):
        raise ValueError("Invalid decoded Sina history")
    records, dates = [], set()
    for row in decoded:
        if not isinstance(row, dict):
            raise ValueError("Invalid decoded Sina history record")
        day = _day(row.get("date"), history=True)
        if day in dates:
            raise ValueError("duplicate Sina history date")
        dates.add(day)
        record = {"trade_date": day}
        for field in ("open", "high", "low", "close", "volume", "amount"):
            record[field] = _number(row.get(field), field, positive=field in {"open", "high", "low", "close"})
        if record["high"] < max(record["open"], record["close"], record["low"]) or record["low"] > min(record["open"], record["close"]):
            raise ValueError("Invalid Sina OHLC bounds")
        records.append(record)
    frame = pd.DataFrame(records, columns=_COLUMNS[:-1]).astype({"trade_date": "datetime64[ns]"})
    frame = frame.loc[frame["trade_date"].between(start, end)].sort_values("trade_date").reset_index(drop=True)
    if not frame.empty:
        if adjust == "qfq":
            frame = pd.merge_asof(frame, factor_frame, on="trade_date", direction="backward")
            if frame["qfq_factor"].isna().any():
                raise ValueError("Missing effective Sina qfq factor")
            for field in ("open", "high", "low", "close"):
                frame[field] = (frame[field] / frame["qfq_factor"]).round(2)
        frame = pd.merge_asof(frame, share_frame, on="trade_date", direction="backward")
        frame["turnover_rate"] = frame["volume"] / frame["outstanding_share"] * 100.0
    else:
        frame["turnover_rate"] = pd.Series(dtype="float64")
    result = attach_cn_stock_history_contract(frame[_COLUMNS], "sina")
    result.attrs["sina_evidence"] = {
        "symbol": symbol, "adjust": adjust, "history_rows": len(decoded), "selected_rows": len(result),
        "price_method": "raw_ohlc" if adjust == "" else "raw_ohlc / effective_qfq_factor rounded_2_decimals",
        "decoder": deepcopy(_decoder_identity()),
        "shares_records": len(share_frame), "qfq_records": len(factor_frame),
        "shares_unknown_effective_records": int(share_frame["outstanding_share"].isna().sum()),
        "turnover_unknown_selected_rows": int(result["turnover_rate"].isna().sum()),
        "turnover_method": "raw_volume / effective_outstanding_share * 100",
        "shares_source_unit": "ten_thousand_share", "volume_source_unit": "share",
        "amount_source_unit": "CNY", "asof_direction": "backward",
        "historical_availability_proven": False,
        "response_sha256": {name: hashlib.sha256(payload).hexdigest() for name, payload in
                            (("history", history_bytes), ("shares", shares_bytes), ("qfq", qfq_bytes))},
    }
    return result
