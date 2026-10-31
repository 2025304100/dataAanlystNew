"""Fetch Tencent daily K-lines without the SDK's raw-day preference bug."""
from __future__ import annotations

from datetime import datetime
import hashlib

import pandas as pd
import requests

from app.services.market_history_evidence import HistoryRequest, _dates, _normalize_received, _parse_tencent_response
from app.services.market_history_contract import tencent_quantity_input_contract
from app.services.data_source_execution import source_boundary


TENCENT_HISTORY_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
TENCENT_HISTORY_TIMEOUT = (5, 15)
TENCENT_HISTORY_MAX_CALENDAR_DAYS = 640


@source_boundary('stock_zh_a_hist_tx')
def fetch_tencent_history_raw(*, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    """Return the existing six-column raw-lot schema, with a verified mode key.

    Tencent limits one response to 640 K-lines. Without a trusted exchange
    calendar, at most 640 inclusive calendar days guarantees that bound. Longer
    ranges fail before I/O instead of accepting a silently truncated history.
    Callers retain the configured stock_zh_a_hist_tx policy/retry wrapper.
    """
    try:
        start = datetime.strptime(start_date, "%Y%m%d").date()
        end = datetime.strptime(end_date, "%Y%m%d").date()
        if start_date != start.strftime("%Y%m%d") or end_date != end.strftime("%Y%m%d"):
            raise ValueError("non-canonical dates")
    except (TypeError, ValueError) as exc:
        raise ValueError("tencent_history_invalid_request_dates") from exc
    request = HistoryRequest(symbol, start, end, adjust)
    request.validate()
    if not symbol.startswith(("sh", "sz")):
        raise ValueError("tencent_history_unsupported_symbol")
    tencent_quantity_input_contract(symbol)  # Unknown instrument families fail before I/O.
    if (end - start).days + 1 > TENCENT_HISTORY_MAX_CALENDAR_DAYS:
        raise ValueError("tencent_history_range_exceeds_640_calendar_days")
    params = {"_var": f"kline_day{adjust}{start.year}",
        "param": f"{symbol},day,{start.isoformat()},{end.isoformat()},640,{adjust}"}
    response = requests.get(TENCENT_HISTORY_URL, params=params, timeout=TENCENT_HISTORY_TIMEOUT,
        allow_redirects=False)
    try:
        if response.status_code != 200:
            response.raise_for_status()
            raise ValueError("tencent_history_unexpected_http_status")
        payload = response.content
        frame, mode = _parse_tencent_response(request, payload)
    finally:
        response.close()
    dates = _dates(frame["date"])
    selected = [day for day in dates if start.isoformat() <= day <= end.isoformat()]
    if len(dates) > 640 or not selected:
        raise ValueError("tencent_history_invalid_or_empty_selected_range")
    # Check the unchanged normalizer can retain every original date and valid
    # OHLC before a downstream writer sees this successful supplier response.
    # Return the original six-column lots frame, not this validation copy.
    _normalize_received(frame, "tencent", request)
    frame.attrs["history_response"] = {"provider": "tencent", "endpoint": TENCENT_HISTORY_URL,
        "symbol": symbol, "requested_adjustment": adjust, "actual_adjustment": adjust,
        "returned_mode": mode, "adjustment_evidence": "response_key",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "raw_date_count": len(dates), "selected_date_count": len(selected), "range_filter_only": True,
        "raw_date_range": {"start": min(dates), "end": max(dates)},
        "selected_date_range": {"start": min(selected), "end": max(selected)},
        "range_completeness_certified": False,
        "input_unit_contract": dict(frame.attrs["tencent_quantity_contract"]),
        "response_sha256": hashlib.sha256(payload).hexdigest(), "raw_response_archived": False}
    return frame
